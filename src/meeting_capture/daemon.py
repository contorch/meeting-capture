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
from .recorder import Chunk, stream_chunks
from .transcriber import transcribe
from .watchdog import check_and_maybe_exit

SESSION_GAP_SECONDS = 15 * 60
MIC_POLL_INTERVAL = 2.0

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


def _append(transcript_path: Path, chunk: Chunk, text: str) -> None:
    if not text:
        return
    if not transcript_path.exists():
        header = f"# Meeting transcript {transcript_path.stem}\n\n"
        transcript_path.write_text(header, encoding="utf-8")
    ts = dt.datetime.fromtimestamp(chunk.started_at).strftime("%H:%M:%S")
    with transcript_path.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n\n")


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
    from .transcriber import DEFAULT_GEMINI_MODEL, ENV_GEMINI_MODEL, _resolve_gemini_api_key
    log.info(
        "transcription: gemini (model=%s, api_key=%s)",
        os.environ.get(ENV_GEMINI_MODEL, DEFAULT_GEMINI_MODEL),
        "present" if _resolve_gemini_api_key() else "MISSING — transcription will fail",
    )

    current_session: Path | None = None
    last_chunk_end: float = 0.0
    last_devices = default_devices_snapshot()
    log.info(
        "audio devices: input=%s output=%s",
        last_devices.get("input"), last_devices.get("output"),
    )
    last_device_check = 0.0
    last_footprint_check = 0.0

    def _watchdog_tick() -> None:
        # Throttle the footprint check to ~once a minute regardless of caller.
        nonlocal last_footprint_check
        now = time.time()
        if now - last_footprint_check >= 60.0:
            last_footprint_check = now
            check_and_maybe_exit()

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

            # Inner loop: stream chunks until the mic goes off (or pause is set).
            for chunk in stream_chunks(AUDIO_DIR, _should_record):
                chunk_end = chunk.started_at + chunk.duration_seconds
                if current_session is None or (chunk.started_at - last_chunk_end) > SESSION_GAP_SECONDS:
                    current_session = _session_path(chunk.started_at)
                    log.info("new session: %s", current_session.name)

                try:
                    text = transcribe(chunk.path)
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
                    "chunk %.1fs -> %s (%d chars)",
                    chunk.duration_seconds,
                    current_session.name if current_session else "?",
                    len(text),
                )

                # Per-chunk is where a transcription-path leak would accumulate.
                _watchdog_tick()

            log.info("mic inactive — session ended")
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        _clear_pid()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
