"""Transcription for meeting-capture audio chunks (hosted Gemini backend).

Audio chunks are transcribed via Google's Gemini audio models (google-genai):
cheap (~$0.0002/min on gemini-2.5-flash), with far fewer silence
hallucinations than local models and best-effort speaker labels via prompt
instructions.

Requires a Google API key, resolved in order from:
  $GOOGLE_API_KEY, $GEMINI_API_KEY, or ~/.config/google/key (mode 600).

The model defaults to gemini-2.5-flash; override with MEETING_CAPTURE_GEMINI_MODEL.

(A local mlx-whisper backend was removed: it ran on the GPU and its unbounded
MLX Metal buffer cache leaked tens of GB in a long-lived daemon. Transcription
is hosted-only now.)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

ENV_GEMINI_MODEL = "MEETING_CAPTURE_GEMINI_MODEL"
GEMINI_KEY_FILE = Path.home() / ".config" / "google" / "key"

# Ask for a clean transcript with speaker labels when multiple voices are
# present. Returns empty for silent audio rather than hallucinated filler.
GEMINI_TRANSCRIBE_INSTRUCTION = (
    "Transcribe the audio. Return only the spoken text, nothing else. "
    "If multiple speakers are clearly distinguishable, prefix each "
    "speaker turn with [SPEAKER_1], [SPEAKER_2], etc. (consistent "
    "within this clip only — speaker IDs do NOT carry across clips). "
    "If the audio is silent, contains only background noise, or has no "
    "intelligible speech, return an empty string. Do not invent or "
    "filler-fill text. Do not add commentary, summary, or formatting "
    "beyond the speaker prefixes."
)


def transcribe(audio_path: Path, model: Optional[str] = None) -> str:
    """Transcribe a single audio chunk via Gemini.

    Args:
        audio_path: WAV file (16kHz mono int16 expected).
        model: Gemini model name override (defaults from ENV_GEMINI_MODEL /
            DEFAULT_GEMINI_MODEL).

    Returns:
        Transcribed text. Empty string for silent / unintelligible audio.
    """
    return _transcribe_gemini(audio_path, model)


def _transcribe_gemini(audio_path: Path, model: Optional[str]) -> str:
    """Hosted Gemini audio transcription backend."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "meeting-capture requires the google-genai package. "
            "Install with: pip install -e ."
        ) from e

    api_key = _resolve_gemini_api_key()
    if not api_key:
        raise RuntimeError(
            "Gemini transcription needs a Google API key. "
            "Set GOOGLE_API_KEY or GEMINI_API_KEY, or write the key to "
            f"{GEMINI_KEY_FILE} (mode 600)."
        )

    model = model or os.environ.get(ENV_GEMINI_MODEL, DEFAULT_GEMINI_MODEL)
    # The google-genai SDK has no read timeout by default — a half-open TLS
    # connection (network blip, server idle close) can wedge the daemon
    # indefinitely on SSL_read. Pass an explicit per-request timeout so a
    # stuck call surfaces as an error and the next chunk can proceed.
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=60_000),  # ms
    )

    audio_bytes = audio_path.read_bytes()
    response = client.models.generate_content(
        model=model,
        contents=[
            GEMINI_TRANSCRIBE_INSTRUCTION,
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
        ],
        config={"temperature": 0.0},
    )
    return (response.text or "").strip()


def _resolve_gemini_api_key() -> Optional[str]:
    """Look up the Gemini API key in env first, then ~/.config/google/key."""
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        v = os.environ.get(var)
        if v:
            return v.strip()
    if GEMINI_KEY_FILE.exists():
        try:
            return GEMINI_KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None
