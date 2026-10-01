"""Line-in capture: record both meeting channels from one USB audio interface.

An alternative to the ScreenCaptureKit `sysaudio` path (recorder.py), for
running meeting-capture on a *separate* Mac that is not in the call. A 2-channel
USB audio interface feeds it both sides:

    interface channel 1  → you  ("me")
    interface channel 2  → the other participants  ("them")

Because that Mac is not a call participant there is no "mic in use" signal to
gate on, and there is nothing to gate: the interface is dedicated to this, so
we read it continuously. The per-channel silence chunker (recorder._ChannelChunker)
only emits a Chunk when a channel actually carries speech, so an idle interface
costs nothing, and daemon.py's existing SESSION_GAP_SECONDS logic groups the
chunks that do appear into per-meeting transcripts. Downstream — Gemini
transcription, the parked-audio retry, Me/Them labelling — is entirely reused.

Opt in with MEETING_CAPTURE_SOURCE=linein. Select the device and channel map
with MEETING_CAPTURE_INPUT_DEVICE / _ME_CHANNEL / _THEM_CHANNEL (see below).

sounddevice (PortAudio) is an optional dependency:  pip install 'meeting-capture[linein]'
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from .recorder import (
    BYTES_PER_SAMPLE,
    CHUNK_DURATION,
    ROLE_MIC,
    ROLE_SYSTEM,
    SAMPLE_RATE,
    Chunk,
    _ChannelChunker,
)

# Env config. Device may be a PortAudio index or a case-insensitive name
# substring ("UMC202HD"); unset picks the current default input.
SOURCE_ENV = "MEETING_CAPTURE_SOURCE"
DEVICE_ENV = "MEETING_CAPTURE_INPUT_DEVICE"
ME_CHANNEL_ENV = "MEETING_CAPTURE_ME_CHANNEL"      # 0-based; default 0 (interface input 1)
THEM_CHANNEL_ENV = "MEETING_CAPTURE_THEM_CHANNEL"  # 0-based; default 1 (interface input 2)


def linein_mode_enabled() -> bool:
    return os.environ.get(SOURCE_ENV, "").strip().lower() == "linein"


def _channel(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default


def me_channel() -> int:
    return _channel(ME_CHANNEL_ENV, 0)


def them_channel() -> int:
    return _channel(THEM_CHANNEL_ENV, 1)


def _import_sounddevice():
    try:
        import sounddevice as sd  # noqa: PLC0415
    except OSError as exc:  # PortAudio shared lib missing
        raise RuntimeError(
            "line-in capture needs PortAudio. Install with: pip install 'meeting-capture[linein]'"
        ) from exc
    except ImportError as exc:
        raise RuntimeError(
            "line-in capture needs the 'linein' extra. Install with: "
            "pip install 'meeting-capture[linein]'"
        ) from exc
    return sd


def refresh_devices() -> bool:
    """Re-scan audio devices. PortAudio reads the device list once, when it
    starts, so an interface plugged in (or re-plugged) after that is
    invisible to a long-running process — the daemon's 30 s retry and the
    settings page would never find it. Only call while this process has no
    stream open."""
    try:
        sd = _import_sounddevice()
    except RuntimeError:
        return False
    try:
        sd._terminate()
        sd._initialize()
        return True
    except Exception:
        return False


def list_input_devices() -> list[dict]:
    """Input-capable devices, for `doctor` / setup to show. Empty if PortAudio is absent."""
    try:
        sd = _import_sounddevice()
    except RuntimeError:
        return []
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            out.append({
                "index": idx,
                "name": dev["name"],
                "channels": dev["max_input_channels"],
                "default_samplerate": dev.get("default_samplerate"),
            })
    return out


def resolve_device(spec: str | None = None):
    """Turn the device spec (index, name substring, or None) into a PortAudio
    device index. Returns None to mean "PortAudio default input"."""
    sd = _import_sounddevice()
    spec = spec if spec is not None else os.environ.get(DEVICE_ENV)
    if not spec:
        return None
    spec = spec.strip()
    if spec.isdigit():
        return int(spec)
    matches = [d for d in list_input_devices() if spec.lower() in d["name"].lower()]
    if not matches and refresh_devices():   # plugged in after this process started?
        matches = [d for d in list_input_devices() if spec.lower() in d["name"].lower()]
    if not matches:
        names = ", ".join(d["name"] for d in list_input_devices()) or "(none found)"
        raise RuntimeError(f"no input device matching {spec!r}. Available: {names}")
    if len(matches) > 1:
        names = ", ".join(d["name"] for d in matches)
        raise RuntimeError(f"{spec!r} matches several devices: {names}. Be more specific.")
    return matches[0]["index"]


def validate(spec: str | None, me_ch: int, them_ch: int) -> dict:
    """Resolve a device spec and check the me/them map fits it, WITHOUT opening
    a stream. Raises RuntimeError with a user-facing message. Used by
    `meeting-capture source linein` so a bad device name fails immediately
    instead of becoming a daemon that retries in the background."""
    if me_ch < 0 or them_ch < 0:
        raise RuntimeError("channel numbers start at 0")
    if me_ch == them_ch:
        raise RuntimeError(
            f"me and them are both channel {me_ch} — each side needs its own "
            "interface input, or every line is transcribed twice"
        )
    sd = _import_sounddevice()
    dev = resolve_device(spec)
    info = sd.query_devices(dev, "input") if dev is not None else sd.query_devices(kind="input")
    need = max(me_ch, them_ch) + 1
    if info["max_input_channels"] < need:
        raise RuntimeError(
            f"{info['name']!r} has {info['max_input_channels']} input channel(s); "
            f"me=ch{me_ch}/them=ch{them_ch} needs {need}. Use a 2-input interface."
        )
    return {"index": dev, "name": info["name"], "channels": info["max_input_channels"]}


def _deinterleave(block: np.ndarray, nchannels: int, ch: int) -> np.ndarray:
    """One channel out of an interleaved int16 frame, as a mono array.

    A column of the reshaped view is a strided (non-contiguous) slice; copy it
    out so the caller gets a compact buffer to serialise and feed the chunker.
    """
    frames = block.reshape(-1, nchannels)
    return np.ascontiguousarray(frames[:, ch])


def pump_blocks(
    block_iter: Iterator[bytes],
    out_dir: Path,
    should_record,
    nchannels: int,
    me_ch: int,
    them_ch: int,
    sample_rate: int = SAMPLE_RATE,
) -> Iterator[Chunk]:
    """Core loop, hardware-free and unit-testable: split each interleaved int16
    block into the me/them channels, feed each chunker, yield finished Chunks.

    On a normal end (should_record() goes False, or the source is exhausted) the
    in-flight buffers are flushed as final chunks. The flush is deliberately NOT
    in the `finally`: yielding from a finally raises "generator ignored
    GeneratorExit" if the consumer stops early. The finally only closes the
    source so the audio stream is released however we exit.
    """
    block_bytes = int(sample_rate * CHUNK_DURATION) * BYTES_PER_SAMPLE
    me = _ChannelChunker(ROLE_MIC, out_dir, sample_rate)
    them = _ChannelChunker(ROLE_SYSTEM, out_dir, sample_rate)
    try:
        for raw in block_iter:
            if not should_record():
                break
            if not raw:
                continue
            frame = np.frombuffer(raw, dtype="<i2")
            usable = (len(frame) // nchannels) * nchannels
            if usable != len(frame):
                frame = frame[:usable]  # drop a torn tail frame
            yield from me.feed_bytes(_deinterleave(frame, nchannels, me_ch).tobytes(), block_bytes)
            yield from them.feed_bytes(_deinterleave(frame, nchannels, them_ch).tobytes(), block_bytes)
        for chunker in (me, them):
            chunk = chunker.flush()
            if chunk is not None:
                yield chunk
    finally:
        close = getattr(block_iter, "close", None)
        if close is not None:
            close()


def stream_chunks_linein(
    out_dir: Path,
    should_record,
    sample_rate: int = SAMPLE_RATE,
    device=None,
    nchannels: int | None = None,
) -> Iterator[Chunk]:
    """Yield finished Chunks from the USB audio interface while should_record().

    Reads a 2+ channel int16 stream from `device` (default: MEETING_CAPTURE_INPUT_DEVICE
    or the system default input), taking channel me_channel() as "me" and
    them_channel() as "them".
    """
    sd = _import_sounddevice()
    refresh_devices()   # no stream is open here; pick up a re-plugged interface
    dev = device if device is not None else resolve_device()
    me_ch, them_ch = me_channel(), them_channel()
    if me_ch == them_ch:
        raise RuntimeError(f"{ME_CHANNEL_ENV} and {THEM_CHANNEL_ENV} are both {me_ch}")
    need = max(me_ch, them_ch) + 1
    if nchannels is None:
        info = sd.query_devices(dev, "input") if dev is not None else sd.query_devices(kind="input")
        if info["max_input_channels"] < need:
            raise RuntimeError(
                f"{info['name']!r} has {info['max_input_channels']} input channel(s) but the "
                f"me/them map needs {need}. Use a 2-input interface, or set "
                f"{ME_CHANNEL_ENV}/{THEM_CHANNEL_ENV} to channels it has."
            )
        nchannels = need  # open only the channels we map
    if nchannels < need:
        raise RuntimeError(
            f"device has {nchannels} input channel(s) but the me/them map needs {need}. "
            f"Set {ME_CHANNEL_ENV}/{THEM_CHANNEL_ENV} to channels the device has."
        )

    blocksize = int(sample_rate * CHUNK_DURATION)
    stream = sd.RawInputStream(
        samplerate=sample_rate, device=dev, channels=nchannels,
        dtype="int16", blocksize=blocksize,
    )
    print(
        f"line-in: device={dev if dev is not None else 'default'} "
        f"channels={nchannels} me=ch{me_ch} them=ch{them_ch} @ {sample_rate}Hz",
        file=sys.stderr, flush=True,
    )
    stream.start()

    def _blocks() -> Iterator[bytes]:
        try:
            while should_record():
                data, overflowed = stream.read(blocksize)
                if overflowed:
                    print("line-in: input overflow (dropped samples)", file=sys.stderr, flush=True)
                yield bytes(data)
        finally:
            stream.stop()
            stream.close()

    yield from pump_blocks(_blocks(), out_dir, should_record, nchannels, me_ch, them_ch, sample_rate)
