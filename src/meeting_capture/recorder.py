"""Always-on system audio capture via the sysaudio CLI (ScreenCaptureKit, macOS 13+).

sysaudio is our own small Swift binary that uses ScreenCaptureKit's SCStream
with capturesAudio=true to tap system audio output. Streams raw int16 LE mono
PCM on stdout. We read that, chunk by silence, and emit WAV files for the
transcriber. No driver, no sudo, no kernel extension — only the user-grantable
Screen Recording TCC permission (granted to the parent terminal/launcher).

We previously used audiotee (Core Audio Process Tap, macOS 14.2+). Switched
2026-04-26 because Process Tap silently captured zeros on the dev machine —
no errors logged, "audio device started successfully" — but every PCM byte
was 0. Survived `sudo killall coreaudiod` and a reboot. SCK has a different
underlying mechanism and worked immediately. Lesson preserved as repo_knowledge.
"""
from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 16_000
CHANNELS = 1
BYTES_PER_SAMPLE = 2  # int16 little-endian (audiotee resampled mode)
CHUNK_DURATION = 0.25  # how often audiotee flushes to stdout
SAMPLES_PER_BLOCK = int(SAMPLE_RATE * CHUNK_DURATION)
BLOCK_BYTES = SAMPLES_PER_BLOCK * CHANNELS * BYTES_PER_SAMPLE

SILENCE_RMS = 0.005
SILENCE_GAP_SECONDS = 3.0
MIN_CHUNK_SECONDS = 8.0
MAX_CHUNK_SECONDS = 600.0

# When sysaudio stops producing PCM without exiting (transient SCK hiccup,
# display sleep, etc.), the daemon used to block forever on a naive
# proc.stdout.read(). We poll the pipe via select() and bail after this
# many seconds of silence so the daemon can respawn the subprocess.
SELECT_POLL_S = 5.0
STALL_BAIL_S = 30.0

SYSAUDIO_ENV_VAR = "MEETING_CAPTURE_SYSAUDIO"
# Back-compat: also accept the old audiotee env var if set.
AUDIOTEE_ENV_VAR = "MEETING_CAPTURE_AUDIOTEE"


def find_audiotee() -> Path | None:
    """Locate the audiotee binary. Kept for back-compat / fallback only."""
    env = os.environ.get(AUDIOTEE_ENV_VAR)
    if env and Path(env).is_file():
        return Path(env)

    pkg_dir = Path(__file__).resolve().parent
    for ancestor in [pkg_dir, *pkg_dir.parents]:
        candidate = ancestor / "bin" / "audiotee"
        if candidate.is_file():
            return candidate
        if (ancestor / ".git").exists():
            break

    on_path = shutil.which("audiotee")
    return Path(on_path) if on_path else None


def find_sysaudio() -> Path | None:
    """Locate the sysaudio (ScreenCaptureKit) binary."""
    env = os.environ.get(SYSAUDIO_ENV_VAR)
    if env and Path(env).is_file():
        return Path(env)

    pkg_dir = Path(__file__).resolve().parent
    for ancestor in [pkg_dir, *pkg_dir.parents]:
        candidate = ancestor / "bin" / "sysaudio"
        if candidate.is_file():
            return candidate
        if (ancestor / ".git").exists():
            break

    on_path = shutil.which("sysaudio")
    return Path(on_path) if on_path else None


def find_capture_binary() -> Path | None:
    """Find the audio-capture binary to spawn. Prefer sysaudio (SCK), fall back to audiotee."""
    return find_sysaudio() or find_audiotee()


def _rms_int16(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    floats = block.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(np.square(floats, dtype=np.float64))))


@dataclass
class Chunk:
    path: Path
    started_at: float
    duration_seconds: float


FLUSH_MIN_SECONDS = 3.0


def stream_chunks(
    out_dir: Path,
    should_record,
    sample_rate: int = SAMPLE_RATE,
    capture_binary: Path | None = None,
) -> Iterator[Chunk]:
    """Yield finished audio chunks while should_record() is True.

    Spawns the audio-capture binary (sysaudio first, audiotee as fallback),
    reads int16 LE mono PCM from its stdout, chunks by silence. When
    should_record() flips to False (e.g. the user hangs up), the in-flight
    buffer is flushed as a final chunk if it's at least FLUSH_MIN_SECONDS
    long, then the iterator exits and the subprocess is terminated.
    """
    binary = capture_binary or find_capture_binary()
    if binary is None:
        raise RuntimeError(
            "No audio-capture binary found (sysaudio or audiotee). Run setup.sh "
            f"to build, or set {SYSAUDIO_ENV_VAR}=/path/to/sysaudio."
        )

    # sysaudio takes only --sample-rate. audiotee also accepts --chunk-duration.
    cmd = [str(binary), "--sample-rate", str(sample_rate)]
    if binary.name == "audiotee":
        cmd += ["--chunk-duration", str(CHUNK_DURATION)]
    # Inherit our stderr so the binary's diagnostic log lands in the daemon log via launchd.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
    if proc.stdout is None:
        raise RuntimeError("audiotee subprocess has no stdout")

    block_bytes = int(sample_rate * CHUNK_DURATION) * CHANNELS * BYTES_PER_SAMPLE

    buffer: list[np.ndarray] = []
    chunk_started: float | None = None
    silent_run = 0.0

    def _emit(audio: np.ndarray, started: float, min_seconds: float) -> Chunk | None:
        trimmed = _trim_trailing_silence(audio, sample_rate)
        if len(trimmed) / sample_rate < min_seconds:
            return None
        path = out_dir / f"chunk-{int(started)}.wav"
        sf.write(path, trimmed, sample_rate, subtype="PCM_16")
        return Chunk(path=path, started_at=started, duration_seconds=len(trimmed) / sample_rate)

    stdout_fd = proc.stdout.fileno()
    pending = bytearray()
    silent_pipe_s = 0.0  # seconds since sysaudio last produced any data

    try:
        while should_record():
            # Wait up to SELECT_POLL_S for data on the pipe. This lets us
            # notice a stall (sysaudio alive but not producing PCM) instead
            # of blocking forever on read().
            ready, _, _ = select.select([stdout_fd], [], [], SELECT_POLL_S)
            if not ready:
                silent_pipe_s += SELECT_POLL_S
                if silent_pipe_s >= STALL_BAIL_S:
                    print(
                        f"sysaudio stalled for {silent_pipe_s:.0f}s without "
                        "producing PCM — bailing so daemon can respawn it",
                        file=sys.stderr, flush=True,
                    )
                    break
                continue

            # os.read returns whatever is available (1..N bytes), unlike
            # proc.stdout.read which blocks until block_bytes arrives.
            # Accumulate into `pending` until we have a full block.
            try:
                got = os.read(stdout_fd, block_bytes - len(pending))
            except OSError:
                break
            if not got:
                break  # clean EOF — sysaudio exited
            pending.extend(got)
            silent_pipe_s = 0.0
            if len(pending) < block_bytes:
                continue

            raw = bytes(pending)
            pending.clear()
            block = np.frombuffer(raw, dtype="<i2")

            level = _rms_int16(block)
            if level >= SILENCE_RMS:
                if chunk_started is None:
                    chunk_started = time.time()
                buffer.append(block)
                silent_run = 0.0
            elif buffer:
                buffer.append(block)
                silent_run += CHUNK_DURATION

            duration = sum(len(b) for b in buffer) / sample_rate
            if buffer and (
                (silent_run >= SILENCE_GAP_SECONDS and duration >= MIN_CHUNK_SECONDS)
                or duration >= MAX_CHUNK_SECONDS
            ):
                audio = np.concatenate(buffer, axis=0)
                started = chunk_started or time.time()
                buffer.clear()
                chunk_started = None
                silent_run = 0.0

                chunk = _emit(audio, started, MIN_CHUNK_SECONDS)
                if chunk is not None:
                    yield chunk

        # should_record() went False — flush any in-flight buffer
        if buffer:
            audio = np.concatenate(buffer, axis=0)
            started = chunk_started or time.time()
            chunk = _emit(audio, started, FLUSH_MIN_SECONDS)
            if chunk is not None:
                yield chunk
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _trim_trailing_silence(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    window = int(sample_rate * CHUNK_DURATION)
    if window <= 0 or audio.size <= window:
        return audio
    end = audio.shape[0]
    while end > window:
        if _rms_int16(audio[end - window : end]) >= SILENCE_RMS:
            break
        end -= window
    return audio[:end]
