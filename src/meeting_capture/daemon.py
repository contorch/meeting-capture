"""Daemon: record system audio, transcribe each chunk, append to a session transcript."""
from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .mic import default_devices_snapshot, is_mic_active, mic_name
from .paths import (
    AUDIO_DIR,
    LOG_FILE,
    PAUSE_FILE,
    PID_FILE,
    TRANSCRIPTS_DIR,
    ensure_dirs,
)
from .recorder import (
    Chunk,
    find_sysaudio,
    mic_capture_enabled,
    mic_capture_supported,
    stream_chunks,
)
from .live import live_mode_enabled, run_live_session
from .transcriber import transcribe
from .watchdog import check_and_maybe_exit

SESSION_GAP_SECONDS = 15 * 60
MIC_POLL_INTERVAL = 2.0

# A capture session that dies this quickly without producing a single chunk
# means sysaudio failed to start at all — almost always because its Screen
# Recording TCC grant is missing (a macOS update or a sysaudio rebuild
# invalidates it). Without a backoff the outer loop respawns sysaudio
# immediately while the mic is still active, and every spawn pops the
# "sysaudio would like to record this computer's screen and audio" dialog
# again — observed at ~15 prompts/second (225 in 14s on 2026-09-03).
FAST_FAIL_SECONDS = 10.0
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0

log = logging.getLogger("meeting-capture")


def _setup_logging() -> None:
    # Log to stderr only. launchd routes our stderr → LOG_FILE via StandardErrorPath,
    # so adding a FileHandler here would double-write every line.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _is_paused() -> bool:
    return PAUSE_FILE.exists()


def _session_path(started_at: float) -> Path:
    stamp = dt.datetime.fromtimestamp(started_at).strftime("%Y-%m-%dT%H-%M-%S")
    return TRANSCRIPTS_DIR / f"meeting-{stamp}.md"


# Chunk roles → transcript speaker labels. "them" chunks may still contain
# per-clip [SPEAKER_n] prefixes from Gemini when several remote voices are
# distinguishable within the clip.
ROLE_LABELS = {"me": "**Me:**", "them": "**Them:**"}


def _append(transcript_path: Path, chunk: Chunk, text: str) -> None:
    if not text:
        return
    if not transcript_path.exists():
        header = f"# Meeting transcript {transcript_path.stem}\n\n"
        transcript_path.write_text(header, encoding="utf-8")
    ts = dt.datetime.fromtimestamp(chunk.started_at).strftime("%H:%M:%S")
    label = ROLE_LABELS.get(chunk.role)
    prefix = f"[{ts}] {label} " if label else f"[{ts}] "
    with transcript_path.open("a", encoding="utf-8") as f:
        f.write(f"{prefix}{text}\n\n")


def _append_text(transcript_path: Path, role: str, text: str, started_at: float | None = None) -> None:
    """Append a role-labeled line to a transcript (live path; no Chunk object)."""
    text = text.strip()
    if not text:
        return
    if not transcript_path.exists():
        header = f"# Meeting transcript {transcript_path.stem}\n\n"
        transcript_path.write_text(header, encoding="utf-8")
    ts = dt.datetime.fromtimestamp(started_at or time.time()).strftime("%H:%M:%S")
    label = ROLE_LABELS.get(role)
    prefix = f"[{ts}] {label} " if label else f"[{ts}] "
    with transcript_path.open("a", encoding="utf-8") as f:
        f.write(f"{prefix}{text}\n\n")


class FailureBackoff:
    """Escalating delay after consecutive fast-failing capture sessions.

    ``record()`` is called once per ended session with how long it ran and how
    many chunks it produced. A session that emitted a chunk, or simply stayed
    up longer than ``fast_fail_s``, resets the streak. Otherwise the streak
    grows and ``delay`` doubles from ``base_s`` up to ``max_s``.
    """

    def __init__(
        self,
        fast_fail_s: float = FAST_FAIL_SECONDS,
        base_s: float = BACKOFF_BASE_SECONDS,
        max_s: float = BACKOFF_MAX_SECONDS,
    ) -> None:
        self.fast_fail_s = fast_fail_s
        self.base_s = base_s
        self.max_s = max_s
        self.failures = 0

    def record(self, session_seconds: float, chunks: int) -> float:
        """Register an ended session; return seconds to wait before the next one."""
        if chunks > 0 or session_seconds >= self.fast_fail_s:
            self.failures = 0
            return 0.0
        self.failures += 1
        return self.delay

    @property
    def delay(self) -> float:
        if self.failures == 0:
            return 0.0
        return min(self.base_s * (2 ** (self.failures - 1)), self.max_s)


def _permission_hint() -> str:
    binary = find_sysaudio()
    where = str(binary) if binary else "bin/sysaudio"
    return (
        "sysaudio is most likely being denied Screen Recording. Re-add "
        f"{where} under System Settings -> Privacy & Security -> Screen & System "
        "Audio Recording (a macOS update or a sysaudio rebuild invalidates the "
        "previous grant), then the next session will pick it up automatically."
    )


def _write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()))


def _clear_pid() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def run() -> None:
    ensure_dirs()
    _setup_logging()
    _write_pid()

    def _shutdown(signum, frame):
        log.info("received signal %s, shutting down", signum)
        _clear_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("meeting-capture daemon starting (pid=%s, mic=%s)", os.getpid(), mic_name() or "unknown")
    from .transcriber import (
        _resolve_gemini_api_key, diarization_enabled, is_transcribe_model,
        load_vocabulary, resolve_model,
    )
    _model = resolve_model()
    log.info(
        "transcription: %s via %s (api_key=%s, vocab=%d terms, diarize=%s)",
        _model,
        "interactions API" if is_transcribe_model(_model) else "generate_content",
        "present" if _resolve_gemini_api_key() else "MISSING — transcription will fail",
        len(load_vocabulary()),
        diarization_enabled(),
    )
    if live_mode_enabled():
        log.info("MODE: live — real-time streaming transcription (in-meeting copilot feed)")
    else:
        log.info("MODE: batch — chunked transcription (default)")
    if mic_capture_enabled():
        log.info("mic capture (own voice): enabled — two-channel me/them transcripts")
    elif not mic_capture_supported():
        log.info("mic capture (own voice): disabled — needs macOS 15+ (system audio only)")
    else:
        log.info("mic capture (own voice): disabled via MEETING_CAPTURE_MIC (system audio only)")

    current_session: Path | None = None
    last_chunk_end: float = 0.0
    last_devices = default_devices_snapshot()
    log.info(
        "audio devices: input=%s output=%s",
        last_devices.get("input"), last_devices.get("output"),
    )
    last_device_check = 0.0
    last_footprint_check = 0.0
    backoff = FailureBackoff()

    def _watchdog_tick() -> None:
        # Throttle the footprint check to ~once a minute regardless of caller.
        nonlocal last_footprint_check
        now = time.time()
        if now - last_footprint_check >= 60.0:
            last_footprint_check = now
            check_and_maybe_exit()

    def _after_session(session_started: float, session_chunks: int) -> None:
        # Fast-fail backoff: if sysaudio died at once with no audio, hold off
        # before the outer loop respawns it so a missing TCC grant can't turn
        # into a storm of permission dialogs (see FAST_FAIL_SECONDS).
        session_seconds = time.time() - session_started
        delay = backoff.record(session_seconds, session_chunks)
        if delay <= 0:
            return
        log.warning(
            "capture session ended after %.1fs with no audio (%d in a row) — %s "
            "Retrying in %.0fs.",
            session_seconds, backoff.failures, _permission_hint(), delay,
        )
        resume_at = time.time() + delay
        while time.time() < resume_at and _is_mic_still_active_for_backoff():
            _watchdog_tick()
            time.sleep(min(MIC_POLL_INTERVAL, max(0.0, resume_at - time.time())))

    def _is_mic_still_active_for_backoff() -> bool:
        # Only worth waiting while the mic is still held (the storm condition);
        # if the call ended, drop back to the idle poll immediately.
        return is_mic_active() and not _is_paused()

    def _should_record() -> bool:
        # Log default-device changes (input + output). The mic poll runs
        # ~once per second; this is the same cadence so we catch a Bluetooth
        # disconnect / output reroute within a second of it happening.
        nonlocal last_devices, last_device_check
        now = time.time()
        if now - last_device_check >= 1.0:
            last_device_check = now
            devs = default_devices_snapshot()
            if devs != last_devices:
                log.info(
                    "audio devices changed: input %r → %r, output %r → %r",
                    last_devices.get("input"), devs.get("input"),
                    last_devices.get("output"), devs.get("output"),
                )
                last_devices = devs
        return is_mic_active() and not _is_paused()

    try:
        while True:
            # Outer loop: idle until the mic is in use by another app (= we're in a call).
            while not _should_record():
                _watchdog_tick()
                time.sleep(MIC_POLL_INTERVAL)

            log.info("mic active — starting recording session")
            session_started = time.time()

            if live_mode_enabled():
                # Live path: stream to Gemini in real time; finals land in the
                # same .md and in the copilot feed. One session file per meeting.
                started = time.time()
                if current_session is None or (started - last_chunk_end) > SESSION_GAP_SECONDS:
                    current_session = _session_path(started)
                    log.info("new session: %s", current_session.name)
                sess = current_session
                session_chunks = 0

                def _live_append(role: str, text: str, _sess=sess) -> None:
                    nonlocal session_chunks
                    session_chunks += 1
                    _append_text(_sess, role, text)

                try:
                    run_live_session(_should_record, sess.stem, _live_append)
                except Exception as exc:
                    log.exception("live session failed: %s", exc)
                last_chunk_end = time.time()
                log.info("mic inactive — session ended")
                _after_session(started, session_chunks)
                continue

            # Batch path: stream chunks until the mic goes off (or pause is set).
            session_chunks = 0
            for chunk in stream_chunks(AUDIO_DIR, _should_record):
                session_chunks += 1
                chunk_end = chunk.started_at + chunk.duration_seconds
                if current_session is None or (chunk.started_at - last_chunk_end) > SESSION_GAP_SECONDS:
                    current_session = _session_path(chunk.started_at)
                    log.info("new session: %s", current_session.name)

                try:
                    text = transcribe(chunk.path, role=chunk.role)
                except Exception as exc:
                    log.exception("transcription failed for %s: %s", chunk.path, exc)
                    text = ""

                _append(current_session, chunk, text)
                last_chunk_end = chunk_end

                try:
                    chunk.path.unlink()
                except FileNotFoundError:
                    pass

                log.info(
                    "chunk %.1fs [%s] -> %s (%d chars)",
                    chunk.duration_seconds,
                    chunk.role,
                    current_session.name if current_session else "?",
                    len(text),
                )

                # Per-chunk is where a transcription-path leak would accumulate.
                _watchdog_tick()

            log.info("mic inactive — session ended")
            _after_session(session_started, session_chunks)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        _clear_pid()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
