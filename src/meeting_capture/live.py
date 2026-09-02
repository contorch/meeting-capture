"""Live (real-time) transcription via gemini-3.5-transcribe-live.

Opt in with MEETING_CAPTURE_MODE=live. Instead of chunking audio and
transcribing after each pause (the batch path in recorder/daemon), this streams
sysaudio's PCM straight to Gemini over WebSockets and gets ~1s interim
hypotheses plus finalized utterances — the latency the in-meeting copilot needs.

Two independent live sessions run per meeting: the microphone ("me") and system
audio ("them"), fed from the same framed sysaudio stream the batch path uses.
Each finalized utterance is:
  * appended to the same ~/transcripts/meeting-*.md file (memory works
    identically to batch mode), and
  * written to a per-session JSONL feed under ~/.meeting-capture/live/ that the
    `meeting-capture live` tail and the copilot consume.

Robustness: a live WebSocket lives ~10 minutes, so each channel reconnects on
close/deadline; PCM that arrives during a reconnect is buffered (bounded) and
the newest audio wins if the buffer overflows. sysaudio keeps streaming across
reconnects.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import select
import time
from pathlib import Path
from typing import Callable, Optional

from .paths import LIVE_DIR
from .recorder import (
    FRAME_TAG_MIC,
    FRAME_TAG_SYSTEM,
    ROLE_MIC,
    ROLE_SYSTEM,
    SAMPLE_RATE,
    _FrameParser,
    _spawn_capture,
    find_capture_binary,
    mic_capture_enabled,
)

log = logging.getLogger("meeting-capture.live")

LIVE_MODEL = "gemini-3.5-transcribe-live"
MIME = f"audio/pcm;rate={SAMPLE_RATE}"

# Reconnect a channel a little before the ~10-minute server cap.
SESSION_SECONDS = 9 * 60
# Cap the per-channel PCM backlog while a socket is reconnecting (~10s of audio).
MAX_QUEUE_CHUNKS = 100
# How often to poll sysaudio's pipe for PCM (seconds).
READ_POLL_S = 1.0
# Bail if sysaudio produces no bytes for this long (matches batch STALL_BAIL_S).
STALL_BAIL_S = 30.0

TAG_ROLE = {FRAME_TAG_SYSTEM: ROLE_SYSTEM, FRAME_TAG_MIC: ROLE_MIC}


def live_mode_enabled() -> bool:
    return os.environ.get("MEETING_CAPTURE_MODE", "batch").strip().lower() == "live"


def feed_path(session_stem: str) -> Path:
    return LIVE_DIR / f"{session_stem}.jsonl"


def _now_iso() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


class _FeedWriter:
    """Appends transcript events to the session's JSONL feed (interim + final)."""

    def __init__(self, session_stem: str) -> None:
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = feed_path(session_stem)

    def write(self, role: str, kind: str, text: str) -> None:
        rec = {"ts": time.time(), "clock": _now_iso(), "role": role, "kind": kind, "text": text}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _transcription_config(types_mod):
    """Live input-transcription config, with custom vocabulary when available."""
    from .transcriber import load_vocabulary

    kwargs = {"language_codes": []}  # auto-detect
    vocab = load_vocabulary()
    if vocab and "custom_vocabulary" in types_mod.AudioTranscriptionConfig.model_fields:
        kwargs["custom_vocabulary"] = vocab
    return types_mod.AudioTranscriptionConfig(**kwargs)


async def _channel(
    client,
    types_mod,
    role: str,
    pcm_q: "asyncio.Queue[Optional[bytes]]",
    on_event: Callable[[str, str, str], None],
    stop: asyncio.Event,
) -> None:
    """Run one channel: connect → stream PCM → emit interim/final → reconnect."""
    config = types_mod.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=_transcription_config(types_mod),
    )
    while not stop.is_set():
        try:
            async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
                log.info("live[%s] connected", role)
                deadline = asyncio.get_event_loop().time() + SESSION_SECONDS

                async def _send() -> None:
                    while not stop.is_set() and asyncio.get_event_loop().time() < deadline:
                        try:
                            data = await asyncio.wait_for(pcm_q.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        if data is None:
                            return
                        await session.send_realtime_input(
                            audio=types_mod.Blob(data=data, mime_type=MIME)
                        )

                sender = asyncio.create_task(_send())
                try:
                    async for msg in session.receive():
                        sc = getattr(msg, "server_content", None)
                        if sc is not None:
                            interim = getattr(sc, "interim_input_transcription", None)
                            if interim and getattr(interim, "text", None):
                                on_event(role, "interim", interim.text)
                            final = getattr(sc, "input_transcription", None)
                            if final and getattr(final, "text", None):
                                on_event(role, "final", final.text)
                        if asyncio.get_event_loop().time() >= deadline:
                            break
                finally:
                    sender.cancel()
                    try:
                        await sender
                    except (asyncio.CancelledError, Exception):
                        pass
                log.info("live[%s] session rolled over, reconnecting", role)
        except Exception as exc:
            if stop.is_set():
                return
            log.warning("live[%s] connection error (%s: %s) — retrying in 2s",
                        role, type(exc).__name__, str(exc)[:160])
            await asyncio.sleep(2.0)


async def _pump_pcm(proc, queues: dict, stop: asyncio.Event) -> None:
    """Read framed PCM from sysaudio and fan out to per-channel queues.

    Runs the blocking pipe read in an executor so the event loop keeps serving
    the WebSocket channels.
    """
    parser = _FrameParser()
    fd = proc.stdout.fileno()
    loop = asyncio.get_event_loop()
    silent_s = 0.0

    def _read() -> bytes:
        ready, _, _ = select.select([fd], [], [], READ_POLL_S)
        if not ready:
            return b""
        try:
            return os.read(fd, 65536)
        except OSError:
            return b""

    while not stop.is_set():
        data = await loop.run_in_executor(None, _read)
        if not data:
            silent_s += READ_POLL_S
            if silent_s >= STALL_BAIL_S:
                log.warning("live: sysaudio produced no PCM for %.0fs — stopping so daemon respawns", silent_s)
                stop.set()
            continue
        silent_s = 0.0
        try:
            frames = parser.feed(data)
        except ValueError as exc:
            log.warning("live: %s — stopping so daemon respawns", exc)
            stop.set()
            return
        for tag, payload in frames:
            q = queues.get(TAG_ROLE.get(tag))
            if q is None:
                continue
            if q.qsize() >= MAX_QUEUE_CHUNKS:
                try:
                    q.get_nowait()  # drop oldest; newest audio matters most for live
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(payload)


async def _run(should_record: Callable[[], bool], session_stem: str, append: Callable[[str, str], None]) -> None:
    from google import genai
    from google.genai import types
    from .transcriber import REQUEST_TIMEOUT_MS, _resolve_gemini_api_key

    api_key = _resolve_gemini_api_key()
    if not api_key:
        raise RuntimeError("live mode needs a Google API key (GOOGLE_API_KEY / GEMINI_API_KEY / ~/.config/google/key)")

    binary = find_capture_binary()
    if binary is None:
        raise RuntimeError("no audio-capture binary (sysaudio) found")
    want_mic = binary.name == "sysaudio" and mic_capture_enabled()
    cmd = [str(binary), "--sample-rate", str(SAMPLE_RATE)] + (["--mic"] if want_mic else [])
    proc = _spawn_capture(cmd, disclaim=want_mic)

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))
    feed = _FeedWriter(session_stem)
    stop = asyncio.Event()

    roles = [ROLE_SYSTEM] + ([ROLE_MIC] if want_mic else [])
    queues = {r: asyncio.Queue() for r in roles}

    def on_event(role: str, kind: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        feed.write(role, kind, text)
        if kind == "final":
            append(role, text)  # into the .md, same as batch mode

    async def _gate() -> None:
        while should_record():
            await asyncio.sleep(1.0)
        stop.set()

    tasks = [asyncio.create_task(_pump_pcm(proc, queues, stop)), asyncio.create_task(_gate())]
    for r in roles:
        tasks.append(asyncio.create_task(_channel(client, types, r, queues[r], on_event, stop)))

    log.info("live: streaming %s → %s (feed: %s)", "+".join(roles), LIVE_MODEL, feed.path.name)
    try:
        await stop.wait()
    finally:
        for q in queues.values():
            q.put_nowait(None)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    log.info("live: session ended")


def run_live_session(should_record: Callable[[], bool], session_stem: str, append: Callable[[str, str], None]) -> None:
    """Blocking entrypoint the daemon calls in place of the batch chunk loop."""
    asyncio.run(_run(should_record, session_stem, append))
