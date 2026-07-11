"""Always-on system audio capture via the sysaudio CLI (ScreenCaptureKit, macOS 13+).

sysaudio is our own small Swift binary that uses ScreenCaptureKit's SCStream
with capturesAudio=true to tap system audio output. On macOS 15+ it also
captures the default microphone (SCK captureMicrophone) so both sides of a
meeting are recorded: system audio = the other participants ("them"), the
mic = the device owner ("me").

Wire protocol from sysaudio:
  - without --mic: raw int16 LE mono PCM on stdout (audiotee-compatible).
  - with --mic:    framed stdout. Each frame is 1 tag byte ('S' system |
                   'M' mic), a 4-byte little-endian payload length, then the
                   payload (int16 LE mono PCM). Framing is decided by the
                   --mic flag we pass, never by runtime permission state, so
                   the parser can't desync when the mic grant is missing.

Each channel runs through its own silence-gap chunker; emitted Chunks carry a
role ("them"/"me") that the daemon uses to label transcript lines.

We previously used audiotee (Core Audio Process Tap, macOS 14.2+). Switched
2026-04-26 because Process Tap silently captured zeros on the dev machine —
no errors logged, "audio device started successfully" — but every PCM byte
was 0. Survived `sudo killall coreaudiod` and a reboot. SCK has a different
underlying mechanism and worked immediately. Lesson preserved as repo_knowledge.
"""
from __future__ import annotations

import ctypes
import os
import platform
import select
import shutil
import signal as _signal
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

# When sysaudio is alive AND producing PCM but every byte is zero-amplitude,
# something has stolen system audio capture out from under us — most often
# another SCK consumer (Cluely, Loom, OBS, recall.ai-based tools) starting
# mid-session, or the default output device changing. We can't distinguish
# "user is just listening quietly" from "audio routing is dead" in real
# time, so we wait until SILENT_AUDIO_BAIL_S of consecutive silence has
# passed — long enough that genuine quiet stretches don't trigger it, short
# enough that we catch a broken meeting before it's all lost. After bailing,
# the daemon's outer loop respawns sysaudio, which is enough to recover
# from a routing change in many cases.
#
# This bail is keyed to the SYSTEM channel only: five silent mic minutes
# just means the user is listening, but five all-zero system minutes during
# a live meeting means capture is broken.
SILENT_AUDIO_BAIL_S = 300.0

# Backstop for the SILENT_AUDIO_BAIL_S logic above: if we've gone this long
# without successfully emitting ANY chunk (regardless of whether PCM is all-
# zero or has occasional noise spikes that keep silent_audio_s from ticking),
# bail. This catches the failure mode where the buffer fills with noise that
# never accumulates enough to chunk — silent_audio_s never ticks because
# each noise block resets it, but no real speech is being captured either.
NO_EMIT_BAIL_S = 300.0

SYSAUDIO_ENV_VAR = "MEETING_CAPTURE_SYSAUDIO"
# Back-compat: also accept the old audiotee env var if set.
AUDIOTEE_ENV_VAR = "MEETING_CAPTURE_AUDIOTEE"

# Mic (own-voice) capture: on by default where supported; set to 0/false/off
# to force system-audio-only capture.
MIC_ENV_VAR = "MEETING_CAPTURE_MIC"

FRAME_TAG_SYSTEM = 0x53  # 'S'
FRAME_TAG_MIC = 0x4D     # 'M'
FRAME_HEADER_BYTES = 5
# A frame payload beyond this means we've desynced from the wire protocol
# (sysaudio emits ~8KB payloads at 0.25s cadence).
MAX_FRAME_PAYLOAD = 1 << 20

ROLE_SYSTEM = "them"
ROLE_MIC = "me"

FLUSH_MIN_SECONDS = 3.0


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


def mic_capture_supported() -> bool:
    """SCK microphone capture needs macOS 15+."""
    ver = platform.mac_ver()[0]
    try:
        return int(ver.split(".")[0]) >= 15
    except (ValueError, IndexError):
        return False


def mic_capture_enabled() -> bool:
    """Mic capture is on by default where supported; MEETING_CAPTURE_MIC=0 opts out."""
    v = os.environ.get(MIC_ENV_VAR, "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return mic_capture_supported()


class _DisclaimedProc:
    """Minimal Popen-alike that spawns via posix_spawn with TCC responsibility
    disclaimed (`responsibility_spawnattrs_setdisclaim`).

    Why: TCC resolves permission prompts against the *responsible process*.
    Under launchd that's the daemon's python — an unbundled binary with no
    usage description — so a mic permission request is auto-denied without
    ever showing a prompt (and nothing appears in System Settings to toggle).
    Disclaiming makes the child (sysaudio, which embeds an Info.plist with
    NSMicrophoneUsageDescription) its own responsible process, so the prompt
    fires and the grant sticks to "sysaudio" in every launch context. Same
    mechanism Chromium and Karabiner use for their helper binaries; the
    symbol is private but stable since macOS 10.14.
    """

    def __init__(self, cmd: list[str]) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        # posix_spawnattr_t / posix_spawn_file_actions_t are pointer-sized
        # opaque types on darwin.
        attr = ctypes.c_void_p()
        if libc.posix_spawnattr_init(ctypes.byref(attr)) != 0:
            raise OSError("posix_spawnattr_init failed")
        libc.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), 1)

        fa = ctypes.c_void_p()
        libc.posix_spawn_file_actions_init(ctypes.byref(fa))
        r, w = os.pipe()
        os.set_inheritable(w, True)
        libc.posix_spawn_file_actions_adddup2(ctypes.byref(fa), w, 1)
        libc.posix_spawn_file_actions_addclose(ctypes.byref(fa), r)

        argv = (ctypes.c_char_p * (len(cmd) + 1))(
            *[c.encode() for c in cmd], None
        )
        env_items = [f"{k}={v}".encode() for k, v in os.environ.items()]
        envp = (ctypes.c_char_p * (len(env_items) + 1))(*env_items, None)

        pid = ctypes.c_int()
        try:
            rc = libc.posix_spawn(
                ctypes.byref(pid), cmd[0].encode(),
                ctypes.byref(fa), ctypes.byref(attr), argv, envp,
            )
        finally:
            libc.posix_spawn_file_actions_destroy(ctypes.byref(fa))
            libc.posix_spawnattr_destroy(ctypes.byref(attr))
            os.close(w)
        if rc != 0:
            os.close(r)
            raise OSError(rc, f"posix_spawn failed for {cmd[0]}")

        self.pid = pid.value
        self.stdout = os.fdopen(r, "rb", buffering=0)
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is None:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done == self.pid:
                self._returncode = os.waitstatus_to_exitcode(status)
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.time() + timeout
        while self.poll() is None:
            if deadline is not None and time.time() >= deadline:
                raise subprocess.TimeoutExpired(cmd=str(self.pid), timeout=timeout)
            time.sleep(0.05)
        return self._returncode  # type: ignore[return-value]

    def _send(self, sig: int) -> None:
        if self._returncode is None:
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass

    def terminate(self) -> None:
        self._send(_signal.SIGTERM)

    def kill(self) -> None:
        self._send(_signal.SIGKILL)


def _spawn_capture(cmd: list[str], disclaim: bool):
    """Spawn the capture binary; disclaim TCC responsibility for mic-mode sysaudio."""
    if disclaim and sys.platform == "darwin":
        try:
            return _DisclaimedProc(cmd)
        except (OSError, AttributeError) as exc:
            print(
                f"disclaimed spawn failed ({exc}) — falling back to plain spawn "
                "(mic permission prompts may not work under launchd)",
                file=sys.stderr, flush=True,
            )
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)


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
    role: str = ROLE_SYSTEM


class _FrameParser:
    """Incremental parser for sysaudio's framed stdout (tag, len LE32, payload)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Consume bytes; return complete (tag, payload) frames.

        Raises ValueError on an impossible header (unknown tag / oversized
        payload) — that means we've desynced from the subprocess and the only
        safe recovery is a respawn.
        """
        self._buf.extend(data)
        frames: list[tuple[int, bytes]] = []
        while len(self._buf) >= FRAME_HEADER_BYTES:
            tag = self._buf[0]
            length = int.from_bytes(self._buf[1:5], "little")
            if tag not in (FRAME_TAG_SYSTEM, FRAME_TAG_MIC) or length > MAX_FRAME_PAYLOAD:
                raise ValueError(
                    f"desynced from sysaudio frame stream (tag=0x{tag:02x}, len={length})"
                )
            if len(self._buf) < FRAME_HEADER_BYTES + length:
                break
            payload = bytes(self._buf[FRAME_HEADER_BYTES : FRAME_HEADER_BYTES + length])
            del self._buf[: FRAME_HEADER_BYTES + length]
            frames.append((tag, payload))
        return frames


class _ChannelChunker:
    """Silence-gap chunker state machine for one audio channel."""

    def __init__(self, role: str, out_dir: Path, sample_rate: int) -> None:
        self.role = role
        self.out_dir = out_dir
        self.sample_rate = sample_rate
        self.pending = bytearray()  # partial-block bytes
        self.buffer: list[np.ndarray] = []
        self.chunk_started: float | None = None
        self.silent_run = 0.0
        # Consecutive seconds of all-silence PCM while no chunk is buffering
        # (the stolen-capture detector reads this off the system channel).
        self.silent_audio_s = 0.0

    def feed_bytes(self, data: bytes, block_bytes: int) -> list[Chunk]:
        """Accumulate raw PCM bytes; process each full block; return emitted chunks."""
        self.pending.extend(data)
        chunks: list[Chunk] = []
        while len(self.pending) >= block_bytes:
            raw = bytes(self.pending[:block_bytes])
            del self.pending[:block_bytes]
            chunk = self._feed_block(np.frombuffer(raw, dtype="<i2"))
            if chunk is not None:
                chunks.append(chunk)
        return chunks

    def _feed_block(self, block: np.ndarray) -> Chunk | None:
        level = _rms_int16(block)
        if level >= SILENCE_RMS:
            self.silent_audio_s = 0.0
            if self.chunk_started is None:
                self.chunk_started = time.time()
            self.buffer.append(block)
            self.silent_run = 0.0
        elif self.buffer:
            self.buffer.append(block)
            self.silent_run += CHUNK_DURATION
        else:
            self.silent_audio_s += CHUNK_DURATION
            return None

        duration = sum(len(b) for b in self.buffer) / self.sample_rate
        if self.buffer and (
            (self.silent_run >= SILENCE_GAP_SECONDS and duration >= MIN_CHUNK_SECONDS)
            or duration >= MAX_CHUNK_SECONDS
        ):
            return self._emit(MIN_CHUNK_SECONDS)
        return None

    def _emit(self, min_seconds: float) -> Chunk | None:
        audio = np.concatenate(self.buffer, axis=0)
        started = self.chunk_started or time.time()
        self.buffer.clear()
        self.chunk_started = None
        self.silent_run = 0.0

        trimmed = _trim_trailing_silence(audio, self.sample_rate)
        if len(trimmed) / self.sample_rate < min_seconds:
            return None
        path = self.out_dir / f"chunk-{int(started)}-{self.role}.wav"
        sf.write(path, trimmed, self.sample_rate, subtype="PCM_16")
        return Chunk(
            path=path,
            started_at=started,
            duration_seconds=len(trimmed) / self.sample_rate,
            role=self.role,
        )

    def flush(self) -> Chunk | None:
        """Emit whatever is in-flight (e.g. the meeting just ended)."""
        if not self.buffer:
            return None
        return self._emit(FLUSH_MIN_SECONDS)


def stream_chunks(
    out_dir: Path,
    should_record,
    sample_rate: int = SAMPLE_RATE,
    capture_binary: Path | None = None,
) -> Iterator[Chunk]:
    """Yield finished audio chunks while should_record() is True.

    Spawns the audio-capture binary (sysaudio first, audiotee as fallback).
    With sysaudio on macOS 15+ the mic is captured too, and each Chunk carries
    role "them" (system audio) or "me" (mic). When should_record() flips to
    False (e.g. the user hangs up), in-flight buffers are flushed as final
    chunks if they're at least FLUSH_MIN_SECONDS long, then the iterator
    exits and the subprocess is terminated.
    """
    binary = capture_binary or find_capture_binary()
    if binary is None:
        raise RuntimeError(
            "No audio-capture binary found (sysaudio or audiotee). Run setup.sh "
            f"to build, or set {SYSAUDIO_ENV_VAR}=/path/to/sysaudio."
        )

    mic_mode = binary.name == "sysaudio" and mic_capture_enabled()

    # sysaudio takes --sample-rate and --mic. audiotee also accepts --chunk-duration.
    cmd = [str(binary), "--sample-rate", str(sample_rate)]
    if binary.name == "audiotee":
        cmd += ["--chunk-duration", str(CHUNK_DURATION)]
    if mic_mode:
        cmd.append("--mic")
    # Inherit our stderr so the binary's diagnostic log lands in the daemon log via
    # launchd. In mic mode, spawn with TCC responsibility disclaimed so sysaudio's
    # embedded Info.plist can drive the Microphone permission prompt.
    proc = _spawn_capture(cmd, disclaim=mic_mode)
    if proc.stdout is None:
        raise RuntimeError("audio-capture subprocess has no stdout")

    block_bytes = int(sample_rate * CHUNK_DURATION) * CHANNELS * BYTES_PER_SAMPLE

    system_chunker = _ChannelChunker(ROLE_SYSTEM, out_dir, sample_rate)
    chunkers: dict[int, _ChannelChunker] = {FRAME_TAG_SYSTEM: system_chunker}
    if mic_mode:
        chunkers[FRAME_TAG_MIC] = _ChannelChunker(ROLE_MIC, out_dir, sample_rate)
    parser = _FrameParser() if mic_mode else None

    stdout_fd = proc.stdout.fileno()
    silent_pipe_s = 0.0          # seconds since sysaudio last produced any data
    last_emit_ts = time.time()   # wall-clock of the last successful chunk emit
    last_system_data_ts = time.time()  # last time SYSTEM-channel bytes arrived

    def _bail(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    try:
        while should_record():
            # Wait up to SELECT_POLL_S for data on the pipe. This lets us
            # notice a stall (sysaudio alive but not producing PCM) instead
            # of blocking forever on read().
            ready, _, _ = select.select([stdout_fd], [], [], SELECT_POLL_S)
            if not ready:
                silent_pipe_s += SELECT_POLL_S
                if silent_pipe_s >= STALL_BAIL_S:
                    _bail(
                        f"sysaudio stalled for {silent_pipe_s:.0f}s without "
                        "producing PCM — bailing so daemon can respawn it"
                    )
                    break
                continue

            # os.read returns whatever is available (1..N bytes), unlike
            # proc.stdout.read which blocks until a full block arrives.
            try:
                got = os.read(stdout_fd, 65536)
            except OSError:
                break
            if not got:
                break  # clean EOF — sysaudio exited
            silent_pipe_s = 0.0
            now = time.time()

            emitted: list[Chunk] = []
            if parser is not None:
                try:
                    frames = parser.feed(got)
                except ValueError as exc:
                    _bail(f"{exc} — bailing so daemon can respawn sysaudio")
                    break
                for tag, payload in frames:
                    if tag == FRAME_TAG_SYSTEM:
                        last_system_data_ts = now
                    emitted.extend(chunkers[tag].feed_bytes(payload, block_bytes))
                # Mic frames keep the pipe busy, so the pipe-level stall bail
                # above can't fire when only the system channel dies. Catch
                # that case here.
                if now - last_system_data_ts >= STALL_BAIL_S:
                    _bail(
                        f"no system-audio frames for {now - last_system_data_ts:.0f}s "
                        "(mic frames still flowing) — system capture died. Bailing "
                        "so daemon can respawn sysaudio."
                    )
                    break
            else:
                last_system_data_ts = now
                emitted.extend(system_chunker.feed_bytes(got, block_bytes))

            for chunk in emitted:
                last_emit_ts = time.time()
                yield chunk

            # Backstop bail: if no chunk has emitted in NO_EMIT_BAIL_S, the
            # buffer is probably full of inter-speech noise that never
            # accumulates enough usable audio to chunk. Catches the failure
            # mode where silent_audio_s never ticks because each noise spike
            # resets it but no speech is being captured either.
            since_emit = time.time() - last_emit_ts
            if since_emit >= NO_EMIT_BAIL_S:
                _bail(
                    f"no chunk emitted for {since_emit:.0f}s — recording is "
                    "alive but no usable speech is being captured. Likely the "
                    "audio source changed (output-device switch, BT headphone "
                    "disconnect, another SCK consumer like Cluely/Loom/OBS). "
                    "Bailing so daemon can respawn sysaudio."
                )
                break

            # Stolen-capture detector, keyed to the system channel (five
            # silent mic minutes just means the user is listening).
            if system_chunker.silent_audio_s >= SILENT_AUDIO_BAIL_S:
                _bail(
                    f"audio source has been silent for {system_chunker.silent_audio_s:.0f}s — "
                    "sysaudio is alive but PCM is all-zero. Likely another app "
                    "is now capturing system audio (Cluely, Loom, OBS, recall.ai-"
                    "based tools), or the default output device changed. Bailing "
                    "so daemon can respawn sysaudio."
                )
                break

        # should_record() went False (or we bailed) — flush in-flight buffers
        for chunker in chunkers.values():
            chunk = chunker.flush()
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
