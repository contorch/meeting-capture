"""Daemon: record system audio, transcribe each chunk, append to the meeting's transcript row.

Capture and transcription are decoupled. The capture loop (main thread) cuts
chunks, decides which meeting each belongs to (_next_session, at the chunk's
start time) and hands it to a bounded queue; one TranscriptionWorker thread
drains the queue in order and, when it has nothing new, retries parked audio.
Capture never waits on transcription: a full queue parks the chunk instead.

Every chunk gets a small JSON note beside it (chunk-X.json) the moment it is
queued, naming the meeting it belongs to, so audio a stopped or killed daemon
never got to — the chunk being transcribed when launchd's SIGTERM arrives
included — goes back into its own meeting at the next start (adopt_orphans).

Failures never lose audio. A chunk that can't be transcribed is moved to
FAILED_AUDIO_DIR with its note (meeting id, attempt count, last error):

  * the engine is unavailable (no key, on-device model missing, ...): parked,
    and the retry of parked audio waits until an engine is available again;
  * the engine says this file can't be read (on-device exit 70): parked with
    an attempt counted; after MAX_ATTEMPTS it moves to quarantine/ and the
    queue moves on;
  * anything else (Gemini/network errors; on-device exit 1, a hang, a crash):
    parked, NOT counted yet — one failure can't tell a bad file from a broken
    engine. _Judge counts it against the file only when the next attempt
    shows the engine working. Two in a row mean the engine: nothing is
    counted, the retry pass stops (no hammering) and the next one runs after
    the next session, at startup, or when the engine comes back. So a
    systemic failure never walks good audio into quarantine.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
import time
import wave
from pathlib import Path

from .mic import default_devices_snapshot, is_mic_active, mic_name
from .paths import (
    AUDIO_DIR,
    FAILED_AUDIO_DIR,
    LOG_FILE,
    PAUSE_FILE,
    PID_FILE,
    ensure_dirs,
)
from . import meetings, store
from .recorder import (
    Chunk,
    find_sysaudio,
    mic_capture_enabled,
    mic_capture_supported,
    stream_chunks,
)
from .linein import linein_mode_enabled, stream_chunks_linein
from .live import LIVE_FIXES, live_blocker, live_mode_enabled, run_live_session
from .transcriber import (
    ChunkFailed,
    TranscriptionUnavailable,
    apple_status,
    clear_apple_status_cache,
    install_apple_model,
    last_backend,
    model_needed,
    resolve_backend,
    stt_choice,
    transcribe,
)
from .watchdog import check_and_maybe_exit

SESSION_GAP_SECONDS = 15 * 60
# Line-in mode: how long to wait before retrying when the USB interface can't be
# opened (unplugged, wrong name), so a missing device doesn't crash-loop launchd.
LINEIN_RETRY_SECONDS = 30.0
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

# Transcription worker.
QUEUE_MAX = 64                 # chunks waiting for transcription before new ones are parked
MAX_ATTEMPTS = 3               # per-file failures before a chunk is quarantined
REPROBE_AFTER_FAILURES = 3     # unclassified failures in a row before the on-device probe is redone
WORKER_IDLE_POLL_S = 2.0
ENGINE_RECHECK_S = 60.0        # how often a blocked worker asks whether an engine is back
MODEL_INSTALL_RETRY_S = 3600.0 # at most one automatic on-device model download per hour
ORPHAN_MIN_AGE_S = 120.0       # chunk files left in AUDIO_DIR by a killed daemon
# On SIGTERM, how long the worker gets to finish the chunk in hand. launchd
# sends SIGKILL after the agent's ExitTimeOut (15 s, supervisor.EXIT_TIMEOUT_S;
# the system default is ~5 s); sysaudio's own shutdown takes up
# to 5 s of that. A chunk not finished in time keeps its note and is retried
# into its meeting at the next start.
STOP_GRACE_S = 10.0

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


def _session_id(started_at: float) -> str:
    """One transcript row per meeting, e.g. meeting-2026-09-28T14-00-00."""
    stamp = dt.datetime.fromtimestamp(started_at).strftime("%Y-%m-%dT%H-%M-%S")
    return f"meeting-{stamp}"


def _next_session(current: str | None, started_at: float, last_chunk_end: float) -> str:
    """The meeting a chunk starting at `started_at` belongs to: the current
    one, unless there is none yet, the gap since the last chunk exceeds
    SESSION_GAP_SECONDS, or a "start new meeting" request was made at or
    before `started_at` (meetings.py) — then a new one."""
    cut = meetings.starts_new_meeting(started_at)
    if current is None or cut is not None or (started_at - last_chunk_end) > SESSION_GAP_SECONDS:
        new = _session_id(started_at)
        if new == current:   # same second as the previous meeting's start
            new = _session_id(started_at + 1)
        log.info("new session: %s", new)
        if cut is not None:
            meetings.clear_cut(cut)
        return new
    return current


def _started_iso(meeting_id: str) -> str:
    stamp = meeting_id[len("meeting-"):]
    try:
        return dt.datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%S").isoformat()
    except ValueError:
        return ""


# Chunk roles → transcript speaker labels. "them" chunks may still contain
# per-clip [SPEAKER_n] prefixes from Gemini when several remote voices are
# distinguishable within the clip.
ROLE_LABELS = {"me": "**Me:**", "them": "**Them:**"}


def _line(started_at: float, role: str, text: str) -> str:
    ts = dt.datetime.fromtimestamp(started_at).strftime("%H:%M:%S")
    label = ROLE_LABELS.get(role)
    prefix = f"[{ts}] {label} " if label else f"[{ts}] "
    return f"{prefix}{text}\n\n"


def _append(meeting_id: str, chunk: Chunk, text: str) -> None:
    if not text:
        return
    store.append(meeting_id, _line(chunk.started_at, chunk.role, text), _started_iso(meeting_id))


# --- parked audio -----------------------------------------------------------------------

_FAILED_NAME = re.compile(r"^chunk-(\d+)-(me|them)\.wav$")


def _quarantine_dir() -> Path:
    return FAILED_AUDIO_DIR / "quarantine"


def _meta_path(wav: Path) -> Path:
    return wav.with_suffix(".json")


def _read_meta(wav: Path) -> dict:
    try:
        data = json.loads(_meta_path(wav).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(wav: Path, meta: dict) -> None:
    path = _meta_path(wav)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.error("could not record retry state for %s: %s", wav.name, exc)


def _drop_meta(wav: Path) -> None:
    try:
        _meta_path(wav).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove %s: %s", _meta_path(wav).name, exc)


def _discard(wav: Path) -> None:
    for p in (wav, _meta_path(wav)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _why(reason: BaseException | str) -> str:
    text = str(reason)
    if isinstance(reason, BaseException) and not text:
        return type(reason).__name__
    return text


def _park_failed(chunk: Chunk, reason: BaseException | str, meeting_id: str | None = None,
                 count: bool = False) -> Path | None:
    """Keep a chunk whose transcription failed in FAILED_AUDIO_DIR for a later
    retry, remembering its meeting and (with count=True) one more failed
    attempt. A chunk that has failed MAX_ATTEMPTS times goes to quarantine/."""
    reason = _why(reason)
    try:
        FAILED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        dest = FAILED_AUDIO_DIR / chunk.path.name
        already_parked = chunk.path.resolve() == dest.resolve() if chunk.path.exists() else False
        if not already_parked:
            chunk.path.replace(dest)
    except OSError as exc:
        log.error("could not park failed chunk %s: %s", chunk.path, exc)
        return None
    # A chunk parked straight from the queue brings the note written when it
    # was queued (submit); it is removed only once the new one is written.
    meta = _read_meta(dest if already_parked else chunk.path)
    attempts = int(meta.get("attempts") or 0) + (1 if count else 0)
    meta.update(attempts=attempts, last_error=str(reason)[:300], parked_at=time.time())
    if meeting_id or meta.get("meeting_id"):
        meta["meeting_id"] = meeting_id or meta.get("meeting_id")
    if count and attempts >= MAX_ATTEMPTS:
        out = _quarantine(dest, meta, reason)
        if not already_parked:
            _drop_meta(chunk.path)
        return out
    _write_meta(dest, meta)
    if not already_parked:
        _drop_meta(chunk.path)
        log.warning(
            "transcription failed (%s) — kept %.1fs of audio at %s; %s",
            reason, chunk.duration_seconds, dest,
            f"retried later (attempt {attempts} of {MAX_ATTEMPTS})" if count
            else "transcribed once that is fixed",
        )
    elif count:
        log.warning("parked %s failed (%s) — attempt %d of %d",
                    dest.name, reason, attempts, MAX_ATTEMPTS)
    return dest


def _quarantine(wav: Path, meta: dict, reason) -> Path | None:
    qdir = _quarantine_dir()
    try:
        qdir.mkdir(parents=True, exist_ok=True)
        dest = qdir / wav.name
        wav.replace(dest)
    except OSError as exc:
        log.error("could not quarantine %s: %s", wav, exc)
        _write_meta(wav, meta)
        return wav
    _write_meta(dest, meta)
    try:
        _meta_path(wav).unlink()
    except FileNotFoundError:
        pass
    log.warning(
        "giving up on %s after %d failed attempts (%s) — moved to %s "
        "(move it back to %s to try again)",
        wav.name, meta.get("attempts", 0), reason, dest, FAILED_AUDIO_DIR,
    )
    return dest


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 16000)


def parked_chunks() -> list[Chunk]:
    """Failed chunks waiting for a retry, oldest first. A file whose header
    can't be read is still listed (estimated duration): skipping it would
    leave it parked forever instead of failing its way into quarantine."""
    out: list[Chunk] = []
    if not FAILED_AUDIO_DIR.is_dir():
        return out
    for path in sorted(FAILED_AUDIO_DIR.glob("chunk-*.wav")):
        m = _FAILED_NAME.match(path.name)
        if not m:
            continue
        try:
            dur = _wav_duration(path)
        except Exception:   # wave raises a bare RuntimeError on some malformed RIFF files
            try:
                dur = path.stat().st_size / 32000.0
            except OSError:
                continue
        out.append(Chunk(path=path, started_at=float(m.group(1)), duration_seconds=dur, role=m.group(2)))
    return out


def parked_counts() -> dict:
    """{"queued": n, "parked": n, "quarantined": n} — for status/doctor.
    "queued": chunks in AUDIO_DIR — waiting for (or in) transcription while
    the daemon runs, or left there by a daemon that stopped before
    transcribing them (the next start retries them)."""
    def _n(d: Path) -> int:
        return len([p for p in d.glob("chunk-*.wav") if _FAILED_NAME.match(p.name)]) if d.is_dir() else 0
    return {"queued": _n(AUDIO_DIR), "parked": _n(FAILED_AUDIO_DIR), "quarantined": _n(_quarantine_dir())}


def adopt_orphans(exclude=frozenset(), min_age_s: float = ORPHAN_MIN_AGE_S) -> int:
    """Chunk files left in AUDIO_DIR by a daemon that was stopped or crashed
    before transcribing them: park them, with the note naming their meeting,
    so the retry picks them up and puts them back into that meeting."""
    if not AUDIO_DIR.is_dir():
        return 0
    now, moved = time.time(), 0
    for path in sorted(AUDIO_DIR.glob("chunk-*.wav")):
        if path in exclude or not _FAILED_NAME.match(path.name):
            continue
        try:
            if now - path.stat().st_mtime < min_age_s:
                continue
            FAILED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            path.replace(FAILED_AUDIO_DIR / path.name)
            moved += 1
        except OSError:
            continue
        note = _meta_path(path)
        try:
            note.replace(FAILED_AUDIO_DIR / note.name)
        except FileNotFoundError:
            pass                 # queued by a version that wrote no note
        except OSError as exc:
            log.warning("could not move %s with its audio: %s", note.name, exc)
    for note in AUDIO_DIR.glob("chunk-*.json"):          # a note whose audio is gone
        try:
            if not note.with_suffix(".wav").exists() and now - note.stat().st_mtime >= min_age_s:
                note.unlink()
        except OSError:
            continue
    if moved:
        log.info("found %d chunk(s) an earlier run never transcribed — queued them for retry", moved)
    return moved


# --- one transcription attempt --------------------------------------------------------

OK, UNAVAILABLE, CHUNK_FAILED, FAILED = "ok", "unavailable", "chunk-failed", "failed"


def _attempt(chunk: Chunk, meeting_id: str) -> tuple[str, str]:
    """Transcribe one chunk and append it to `meeting_id`. Returns (outcome,
    text-or-error). The audio is deleted on success and parked otherwise;
    only CHUNK_FAILED counts an attempt here (see _Judge for FAILED)."""
    try:
        text = transcribe(chunk.path, role=chunk.role)
    except TranscriptionUnavailable as exc:
        _park_failed(chunk, exc, meeting_id)
        return UNAVAILABLE, _why(exc)
    except ChunkFailed as exc:
        _park_failed(chunk, exc, meeting_id, count=True)
        return CHUNK_FAILED, _why(exc)
    except Exception as exc:
        # Keep the audio. Deleting it here meant a call recorded before the
        # Gemini key was set up was lost for good. Not counted: this may be
        # the engine (on-device exit 1 / hang / crash, Gemini, network).
        _park_failed(chunk, exc, meeting_id)
        return FAILED, f"{type(exc).__name__}: {_why(exc)}"
    _append(meeting_id, chunk, text)
    _discard(chunk.path)
    return OK, text


def _charge(chunk: Chunk, why: str) -> None:
    """Count one failed attempt against a chunk parked earlier — unless it was
    recovered or quarantined meanwhile."""
    parked = FAILED_AUDIO_DIR / chunk.path.name
    if parked.exists():
        _park_failed(dataclasses.replace(chunk, path=parked), why, count=True)


class _Judge:
    """Tells a bad file from a broken engine for unclassified failures (FAILED:
    on-device exit 1, a hang or a crash; Gemini or network errors).

    The first FAILED after anything else is held, not counted. If the next
    attempt shows the engine working — a chunk transcribes, or the engine
    rejects a file by name (CHUNK_FAILED) — the held failure counts one
    attempt toward that file's quarantine. If the next attempt — on another
    file — fails the same way, it is the engine: nothing is counted, and
    `streak` keeps rising. (The held file failing again, e.g. retried at the
    session's end, says nothing about the engine: it stays held.)

    So a systemic problem (an outage, Apple's speech service broken after an
    OS update or wedged while its probe still says usable) never walks good
    audio into quarantine, while a file that fails on its own still gets
    there. The worker shares one judge between new and parked chunks."""

    def __init__(self) -> None:
        self.streak = 0                       # unclassified failures in a row
        self._held: tuple[Chunk, str] | None = None

    def record(self, chunk: Chunk, outcome: str, detail: str) -> None:
        if outcome == FAILED:
            if self._held is not None and self._held[0].path.name == chunk.path.name:
                self._held = (chunk, detail)
                return
            self.streak += 1
            self._held = (chunk, detail) if self.streak == 1 else None
            return
        held, self._held, self.streak = self._held, None, 0
        if held is not None and outcome in (OK, CHUNK_FAILED):
            _charge(*held)                    # the engine works: it was that file


SUSPECT = "suspect"


class _RetryPass:
    """Parked chunks to retry, oldest first, regrouped into meetings: a chunk
    goes back to the meeting it was recorded in (its sidecar), else to the
    meeting of the chunk before it unless the gap exceeds SESSION_GAP_SECONDS.

    An unclassified error (FAILED) may be the engine/network/key — stop,
    don't hammer — or this one file. So after the first one the next chunk is
    tried once (SUSPECT) and the judge decides: a second failure in a row
    stops the pass with nothing counted. One bad file can't hold up the rest
    for ever, and an outage costs one extra request per pass."""

    def __init__(self, chunks: list[Chunk], judge: _Judge | None = None) -> None:
        self.chunks = chunks
        self.i = 0
        self.session: str | None = None
        self.last_end = 0.0
        self.recovered = 0
        self.judge = judge if judge is not None else _Judge()

    def done(self) -> bool:
        return self.i >= len(self.chunks)

    def step(self) -> tuple[str, str]:
        chunk = self.chunks[self.i]
        self.i += 1
        if not chunk.path.exists():
            return "gone", ""
        recorded_in = _read_meta(chunk.path).get("meeting_id")
        if recorded_in:
            self.session = str(recorded_in)
        elif self.session is None or (chunk.started_at - self.last_end) > SESSION_GAP_SECONDS:
            self.session = _session_id(chunk.started_at)
        self.last_end = chunk.started_at + chunk.duration_seconds
        outcome, detail = _attempt(chunk, self.session)
        if outcome == OK:
            self.recovered += 1
            log.info("recovered parked %s %.1fs [%s] -> %s (%d chars)%s",
                     chunk.path.name, chunk.duration_seconds, chunk.role, self.session,
                     len(detail), _via())
        self.judge.record(chunk, outcome, detail)
        if outcome == FAILED and self.judge.streak == 1 and not self.done():
            return SUSPECT, detail            # try the next one to tell file from engine
        return outcome, detail


def retry_failed_chunks() -> int:
    """Transcribe parked chunks now (synchronously), regrouping them into
    meetings. Stops when the engine is unavailable or on an error that isn't
    about one file (key/network/quota — no point hammering); a file that
    fails on its own is counted and skipped. Returns how many were recovered."""
    chunks = parked_chunks()
    if not chunks:
        return 0
    log.info("retrying %d parked chunk(s) from earlier failed transcriptions", len(chunks))
    rp = _RetryPass(chunks)
    while not rp.done():
        outcome, detail = rp.step()
        if outcome in (UNAVAILABLE, FAILED):
            log.warning("retry still failing (%s) — %d chunk(s) remain parked in %s",
                        detail, len(parked_chunks()), FAILED_AUDIO_DIR)
            break
    if rp.recovered:
        log.info("recovered %d parked chunk(s) into transcripts", rp.recovered)
    return rp.recovered


def _via() -> str:
    b = last_backend()
    return f" via {b}" if b else ""


# --- the worker -----------------------------------------------------------------------

class _SessionEnd:
    __slots__ = ("gen",)

    def __init__(self, gen: int) -> None:
        self.gen = gen


_STOP = object()


class TranscriptionWorker:
    """The one thread that transcribes: new chunks first (in the order they
    were captured), parked chunks when there is nothing new. The capture loop
    only calls submit()/begin_session()/end_session(), which never block."""

    def __init__(self, maxsize: int = QUEUE_MAX) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._gen = 0
        self._inflight: set[Path] = set()
        self._retry_wanted = False
        self._pass: _RetryPass | None = None
        self._judge = _Judge()                # one for new and parked chunks alike
        self.blocked: str | None = None       # why no engine can run, while that lasts
        self._next_check = 0.0
        self._last_install = -MODEL_INSTALL_RETRY_S
        self._installing = False

    # -- capture side ------------------------------------------------------------------

    def start(self) -> "TranscriptionWorker":
        self._thread = threading.Thread(target=self._run, name="transcribe", daemon=True)
        self._thread.start()
        return self

    def begin_session(self) -> None:
        with self._lock:
            self._gen += 1

    def submit(self, chunk: Chunk, meeting_id: str) -> bool:
        """Queue a chunk for transcription. Never blocks: if the worker is that
        far behind, the chunk is parked for the retry instead. Its meeting is
        written beside it first, so a daemon stopped or killed before the chunk
        is transcribed loses neither the audio nor which meeting it belongs to."""
        _write_meta(chunk.path, {"meeting_id": meeting_id, "attempts": 0})
        with self._lock:
            self._inflight.add(chunk.path)
        try:
            self._q.put_nowait((chunk, meeting_id))
            return True
        except queue.Full:
            with self._lock:
                self._inflight.discard(chunk.path)
            _park_failed(chunk, f"transcription is {self._q.maxsize} chunks behind", meeting_id)
            self.request_retry()
            return False

    def end_session(self) -> None:
        """The capture session ended. The worker logs it after the session's
        last chunk, so the menu bar's REC state (pipeline-monitor reads the
        log) doesn't see chunk lines after "session ended"."""
        with self._lock:
            gen = self._gen
        try:
            self._q.put_nowait(_SessionEnd(gen))
        except queue.Full:
            log.info("mic inactive — session ended")
            self.request_retry()

    def request_retry(self) -> None:
        self._retry_wanted = True

    def stop(self, timeout: float | None = None) -> None:
        """The daemon is exiting: park whatever is still queued, give the chunk
        being transcribed up to `timeout` (STOP_GRACE_S) to finish, and stop. A
        chunk still unfinished then stays in AUDIO_DIR with its note (submit)
        and goes back into its meeting at the next start."""
        timeout = STOP_GRACE_S if timeout is None else timeout
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                with self._lock:
                    self._inflight.discard(item[0].path)
                _park_failed(item[0], "the daemon stopped before transcribing it", item[1])
        try:
            self._q.put_nowait(_STOP)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                with self._lock:
                    left = sorted(p.name for p in self._inflight)
                if left:
                    log.info("stopping mid-transcription — %s kept with its meeting; "
                             "transcribed at the next start", ", ".join(left))

    def idle(self) -> bool:
        return self._q.empty() and self._pass is None

    # -- worker side -------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            busy = self._pass is not None and self.blocked is None
            try:
                item = self._q.get_nowait() if busy else self._q.get(timeout=WORKER_IDLE_POLL_S)
            except queue.Empty:
                item = None
            if item is _STOP:
                return
            try:
                if isinstance(item, _SessionEnd):
                    self._session_ended(item)
                elif item is not None:
                    self._new_chunk(*item)
                else:
                    self._idle()
            except Exception:
                log.exception("transcription worker error (carrying on)")

    def _new_chunk(self, chunk: Chunk, meeting_id: str) -> None:
        outcome, detail = _attempt(chunk, meeting_id)
        with self._lock:
            self._inflight.discard(chunk.path)
        self._judge.record(chunk, outcome, detail)
        self._maybe_reprobe(detail)
        if outcome == OK:
            # Log vocabulary is a contract: the Contorch menu bar
            # (pipeline-monitor status.recording_status) parses
            # "chunk …s [role] -> <meeting id> (N chars)" and
            # "new session: <meeting id>" to show ● REC. Change both together.
            # Anything appended goes after "(N chars)".
            log.info("chunk %.1fs [%s] -> %s (%d chars)%s",
                     chunk.duration_seconds, chunk.role, meeting_id or "?", len(detail), _via())
            if self.blocked is not None:      # e.g. a key was added: it works again
                log.info("transcription available again (%s)", _via().strip() or "ok")
                self.blocked = None
                self._retry_wanted = True
        elif outcome == UNAVAILABLE:
            self._block(detail)

    def _session_ended(self, marker: _SessionEnd) -> None:
        with self._lock:
            current = marker.gen == self._gen
        if current:      # no newer session started meanwhile
            log.info("mic inactive — session ended")
        self.request_retry()

    def _maybe_reprobe(self, detail: str) -> None:
        """Every REPROBE_AFTER_FAILURES unclassified failures in a row, forget
        the cached on-device probe so the next chunk asks the helper again: a
        wedged or broken speech service that the probe now reports as
        unusable then parks audio as unavailable (and auto falls back to
        Gemini) instead of burning a helper run, or a timeout, per chunk."""
        n = self._judge.streak
        if n and n % REPROBE_AFTER_FAILURES == 0 and stt_choice() != "gemini":
            log.warning("%d transcriptions in a row failed (last: %s) — rechecking on-device "
                        "transcription; the audio is kept and not counted against the files", n, detail)
            clear_apple_status_cache()

    def _block(self, reason: str) -> None:
        if self.blocked is None:
            log.warning("transcription unavailable (%s) — audio is kept in %s and transcribed "
                        "once an engine is available", reason, FAILED_AUDIO_DIR)
        self.blocked = reason
        self._next_check = time.monotonic() + ENGINE_RECHECK_S
        self._pass = None

    def _idle(self) -> None:
        now = time.monotonic()
        if now >= self._next_check:
            self._next_check = now + ENGINE_RECHECK_S
            self._maybe_install_model()
            if self.blocked is not None:
                b = resolve_backend()
                if not b.ready:
                    self.blocked = b.reason
                    return
                log.info("transcription available again: %s — %s", b.engine, b.reason)
                self.blocked = None
                self._retry_wanted = True
        if self.blocked is not None:
            return
        if self._pass is None:
            if not self._retry_wanted:
                return
            self._retry_wanted = False
            with self._lock:
                inflight = frozenset(self._inflight)
            adopt_orphans(exclude=inflight)
            chunks = parked_chunks()
            if not chunks:
                return
            log.info("retrying %d parked chunk(s) from earlier failed transcriptions", len(chunks))
            self._pass = _RetryPass(chunks, self._judge)
        rp = self._pass
        outcome, detail = rp.step()
        if outcome in (FAILED, SUSPECT):
            self._maybe_reprobe(detail)
        if outcome == UNAVAILABLE:
            self._block(detail)
        elif outcome == FAILED:
            log.warning("retry still failing (%s) — %d chunk(s) remain parked in %s; "
                        "trying again after the next session", detail, len(parked_chunks()), FAILED_AUDIO_DIR)
            self._pass = None
        if rp.done() or self._pass is None:
            if rp.recovered:
                log.info("recovered %d parked chunk(s) into transcripts", rp.recovered)
            self._pass = None

    def _maybe_install_model(self) -> None:
        """On-device chosen (auto/apple), supported here, model not on the Mac
        yet: download it once (in the background), like `meeting-capture
        language` would. At most once an hour."""
        now = time.monotonic()
        if self._installing or now - self._last_install < MODEL_INSTALL_RETRY_S:
            return
        b = resolve_backend()
        if not model_needed(b):         # the rule `meeting-capture stt --json` reports as needs_model
            return
        st = apple_status(b.locale)     # the probe model_needed just ran (cached)
        self._last_install, self._installing = now, True

        def _work() -> None:
            t0 = time.monotonic()
            log.info("downloading the on-device speech model for %s from Apple (one time)", st.locale)
            try:
                install_apple_model(st.locale)
                log.info("on-device speech model for %s installed in %.0fs", st.locale, time.monotonic() - t0)
                self._next_check = 0.0           # pick it up right away
                _log_upgrade_notice()            # auto may switch to on this Mac now
            except Exception as exc:
                log.warning("could not install the on-device speech model for %s: %s", st.locale, exc)
            finally:
                self._installing = False

        threading.Thread(target=_work, name="install-model", daemon=True).start()


def _append_text(meeting_id: str, role: str, text: str, started_at: float | None = None) -> None:
    """Append a role-labeled line to a transcript (live path; no Chunk object)."""
    text = text.strip()
    if not text:
        return
    store.append(meeting_id, _line(started_at or time.time(), role, text), _started_iso(meeting_id))


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


_live_refusal_logged: str | None = None


def live_permitted() -> bool:
    """Can MODE=live stream this session? Live mode is an explicit opt-in to
    streaming the call to Gemini, so it runs with stt=auto as well (even when
    batch would transcribe on this Mac). Refused — the session runs batch —
    only with stt=apple, which never uploads, or without a Google API key
    (live could not connect; batch keeps the audio). live.live_blocker()."""
    global _live_refusal_logged
    why = live_blocker()
    if why is None:
        _live_refusal_logged = None
        return True
    if why != _live_refusal_logged:
        log.warning("MODE: live requested, but %s — running batch instead (%s)",
                    why, LIVE_FIXES.get(why, "see `meeting-capture doctor`"))
        _live_refusal_logged = why
    return False


_notice_logged = False


def _log_upgrade_notice(backend=None) -> None:
    """Once per run: tell someone with a Gemini key who never picked an
    engine that transcription now runs on this Mac (transcriber.upgrade_notice;
    status, doctor, `stt` and the settings page show it too)."""
    global _notice_logged
    if _notice_logged:
        return
    from .transcriber import NOTICE_CLI_HINT, upgrade_notice
    try:
        note = upgrade_notice(backend=backend)
    except Exception:
        log.debug("could not work out the engine notice", exc_info=True)
        return
    if note:
        _notice_logged = True
        log.warning("NOTE: %s — %s", note, NOTICE_CLI_HINT)


def _log_engine() -> None:
    """Backend-neutral startup line naming the engine and why."""
    from .transcriber import (
        _resolve_gemini_api_key, diarization_enabled, is_transcribe_model,
        load_vocabulary, resolve_model,
    )
    b = resolve_backend()
    log.info("transcription engine: %s — %s (stt=%s, locale=%s)", b.engine, b.reason, b.choice, b.locale)
    _log_upgrade_notice(b)
    if b.engine == "gemini":
        model = resolve_model()
        log.info(
            "gemini: %s via %s (api_key=%s, vocab=%d terms, diarize=%s)",
            model,
            "interactions API" if is_transcribe_model(model) else "generate_content",
            "present" if _resolve_gemini_api_key() else "MISSING",
            len(load_vocabulary()),
            diarization_enabled(),
        )
    if not b.ready:
        log.warning("no transcription engine can run yet — audio is kept in %s until one can",
                    FAILED_AUDIO_DIR)


def _write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()))


def _another_daemon_running() -> bool:
    """Is the pid in PID_FILE — before this run writes its own — a live
    process other than this one? (Two daemons at once, e.g. `meeting-capture
    run` beside the launchd agent: the other one's queue is not ours to take.)"""
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True              # exists, owned by someone else
    return True


def adopt_at_start(other_daemon: bool) -> int:
    """Before capture starts: everything in AUDIO_DIR was left by the
    previous run (nothing is in flight in a fresh process), so park it all now,
    whatever its age — status counts it and the startup retry transcribes it
    into its meeting. If another daemon is running, only take what is old
    enough to be abandoned."""
    return adopt_orphans(min_age_s=ORPHAN_MIN_AGE_S if other_daemon else 0.0)


def _clear_pid() -> None:
    """Remove the pid file — only if it is still ours: a daemon finishing its
    grace period must not delete the pid file of the one that replaced it."""
    try:
        if PID_FILE.read_text().strip() not in (str(os.getpid()), ""):
            return
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove %s: %s", PID_FILE, exc)


def load_settings() -> list[str]:
    """First thing at start: move any settings an old agent plist still
    carries into ~/.meeting-capture/env (once), then load the file under this
    process's environment (config.apply: process env > file > default).
    Returns the keys moved. A legacy launchd job that still had them in its
    environment keeps them until it is loaded again (supervisor.restart)."""
    from . import config
    try:
        moved = config.migrate_from_plist()
    except Exception as exc:   # never keep the recorder from starting over it
        log.warning("could not move settings out of the agent plist: %s", exc)
        moved = []
    config.apply()
    return moved


def run() -> None:
    from . import config, paths
    moved = load_settings()
    ensure_dirs()
    _setup_logging()
    if moved:
        log.info("settings moved from the agent plist into %s: %s", paths.ENV_FILE, ", ".join(moved))
    over = config.overridden()
    if over:
        log.warning("settings in %s overridden by this process's environment: %s", paths.ENV_FILE, ", ".join(over))
    other_daemon = _another_daemon_running()
    _write_pid()

    def _shutdown(signum, frame):
        # The pid file stays until the worker has had its grace period (the
        # finally below): until then this daemon still owns AUDIO_DIR.
        log.info("received signal %s, shutting down", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("meeting-capture daemon starting (pid=%s, mic=%s)", os.getpid(), mic_name() or "unknown")
    _log_engine()
    linein = linein_mode_enabled()
    if linein:
        log.info("SOURCE: line-in — reading the USB audio interface continuously "
                 "(not a call participant; chunks only on speech)")
    elif live_mode_enabled() and live_permitted():
        log.info("MODE: live — real-time streaming transcription (in-meeting copilot feed)")
    else:
        log.info("MODE: batch — chunked transcription (default)")
    if mic_capture_enabled():
        log.info("mic capture (own voice): enabled — two-channel me/them transcripts")
    elif not mic_capture_supported():
        log.info("mic capture (own voice): disabled — needs macOS 15+ (system audio only)")
    else:
        log.info("mic capture (own voice): disabled via MEETING_CAPTURE_MIC (system audio only)")

    current_session: str | None = None
    last_chunk_end: float = 0.0
    last_devices = default_devices_snapshot()
    log.info(
        "audio devices: input=%s output=%s",
        last_devices.get("input"), last_devices.get("output"),
    )
    last_device_check = 0.0
    last_footprint_check = 0.0
    backoff = FailureBackoff()

    # One thread transcribes; capture only hands it chunks. Anything parked by
    # an earlier run (e.g. recorded before the key existed, the on-device
    # model wasn't installed yet, or the daemon was restarted mid-chunk) is
    # retried there whenever it's idle.
    adopt_at_start(other_daemon)
    worker = TranscriptionWorker().start()
    worker.request_retry()

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
        if linein:
            # Not a call participant, so there is no "mic in use" to wait for:
            # read continuously; the chunker only emits on actual speech.
            return not _is_paused()
        return is_mic_active() and not _is_paused()

    def _linein_chunks():
        try:
            yield from stream_chunks_linein(AUDIO_DIR, _should_record)
        except RuntimeError as exc:
            log.error("line-in capture unavailable: %s — retrying in %.0fs",
                      exc, LINEIN_RETRY_SECONDS)
            time.sleep(LINEIN_RETRY_SECONDS)

    try:
        while True:
            # Outer loop: idle until the mic is in use by another app (= we're in a call).
            while not _should_record():
                _watchdog_tick()
                time.sleep(MIC_POLL_INTERVAL)

            log.info("line-in: listening on the interface" if linein
                     else "mic active — starting recording session")
            session_started = time.time()
            worker.begin_session()

            if live_mode_enabled() and not linein and live_permitted():
                # Live path: stream to Gemini in real time; finals land in the
                # same transcript row and in the copilot feed. One row per meeting.
                started = time.time()
                current_session = _next_session(current_session, started, last_chunk_end)
                sess = current_session
                session_chunks = 0
                live_session = {"id": sess}

                def _live_append(role: str, text: str, _h=live_session) -> None:
                    nonlocal session_chunks
                    session_chunks += 1
                    # "Start new meeting" mid-call: a gap never happens here.
                    _h["id"] = _next_session(_h["id"], time.time(), time.time())
                    _append_text(_h["id"], role, text)

                try:
                    run_live_session(_should_record, sess, _live_append)
                except Exception as exc:
                    log.exception("live session failed: %s", exc)
                current_session = live_session["id"]
                last_chunk_end = time.time()
                log.info("mic inactive — session ended")
                _after_session(started, session_chunks)
                continue

            # Batch path: stream chunks until the mic goes off (or pause is set).
            # The meeting is decided here, at the chunk's start time; the
            # worker transcribes and appends in the same order.
            session_chunks = 0
            chunk_source = _linein_chunks() if linein else stream_chunks(AUDIO_DIR, _should_record)
            for chunk in chunk_source:
                session_chunks += 1
                current_session = _next_session(current_session, chunk.started_at, last_chunk_end)
                last_chunk_end = chunk.started_at + chunk.duration_seconds
                worker.submit(chunk, current_session)
                _watchdog_tick()

            worker.end_session()   # logs "mic inactive — session ended" after the last chunk
            _after_session(session_started, session_chunks)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        worker.stop()
        _clear_pid()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
