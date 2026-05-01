"""Transcription backends for meeting-capture audio chunks.

Two backends, switchable via the `MEETING_CAPTURE_TRANSCRIBER` env var:

  - whisper (default) — local mlx-whisper on Apple Silicon. Free, offline,
    no privacy concerns. `whisper-large-v3-turbo` by default, biased with
    `initial_prompt` for proper-noun accuracy.

  - gemini (opt-in) — hosted Gemini audio transcription via google-genai.
    Cheap (~$0.0002/min on gemini-2.5-flash), much fewer silence
    hallucinations than whisper-large, best-effort speaker labels via
    prompt instructions. Requires the `[gemini]` extra and a Google API
    key from $GOOGLE_API_KEY, $GEMINI_API_KEY, or ~/.config/google/key
    (mode 600).

Whisper's `initial_prompt` biases token prediction toward the listed terms.
It is the most effective fix for proper-noun and acronym errors (no model
size will recover terms that aren't in training data — e.g. internal project
names, custom acronyms).

Configuration precedence for the whisper prompt (most → least specific):
  1. Explicit `initial_prompt=...` argument to `transcribe()`
  2. Per-machine file: ~/.meeting-capture/vocab.txt
  3. Env var:           MEETING_CAPTURE_WHISPER_PROMPT
  4. Built-in default

The vocab file is the recommended way to bias for project-specific terms —
edit with `meeting-capture vocab edit`. An empty file means "no prompt at
all" (explicit opt-out, useful when biasing hurts on a particular machine's
audio).

Gemini ignores the whisper prompt knobs and uses GEMINI_TRANSCRIBE_INSTRUCTION
internally — its instruction-following accuracy is high enough that vocab
biasing is unnecessary, and it doesn't suffer the silence-hallucination
behavior that makes the whisper prompt important.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .paths import VOCAB_FILE

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Generic technical vocabulary — Whisper "small" tends to mishear these.
# Override per machine via ~/.meeting-capture/vocab.txt for project-specific
# terms (use `meeting-capture vocab edit`).
DEFAULT_INITIAL_PROMPT = (
    "This is a technical engineering meeting discussing software "
    "architecture, APIs, rate limiting, throttling, retries, JWT and "
    "OAuth authentication, integrators, gRPC, REST, OpenSearch, "
    "ChromaDB, Envoy, Kubernetes, design documents, and TDDs."
)

# Prompt for Gemini: ask for a clean transcript with speaker labels when
# multiple voices are present. Returns empty for silent audio rather than
# whisper's "Thank you" hallucinations.
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

ENV_TRANSCRIBER = "MEETING_CAPTURE_TRANSCRIBER"
ENV_MODEL = "MEETING_CAPTURE_WHISPER_MODEL"
ENV_PROMPT = "MEETING_CAPTURE_WHISPER_PROMPT"
ENV_GEMINI_MODEL = "MEETING_CAPTURE_GEMINI_MODEL"

GEMINI_KEY_FILE = Path.home() / ".config" / "google" / "key"


def resolved_prompt() -> tuple[str | None, str]:
    """Return (prompt_text, source_label). prompt_text is None when the user
    explicitly opted out (empty vocab file or empty env var)."""
    if VOCAB_FILE.exists():
        text = VOCAB_FILE.read_text(encoding="utf-8").strip()
        return (text or None, f"vocab file ({VOCAB_FILE})")
    if ENV_PROMPT in os.environ:
        v = os.environ[ENV_PROMPT]
        return (v or None, f"{ENV_PROMPT} env var")
    return (DEFAULT_INITIAL_PROMPT, "built-in default")


def transcribe(
    audio_path: Path,
    model: Optional[str] = None,
    initial_prompt: Optional[str] = None,
) -> str:
    """Transcribe a single audio chunk.

    Backend chosen by `MEETING_CAPTURE_TRANSCRIBER` env var (default
    "whisper"). Whisper is free + offline; Gemini is hosted, cheap, and
    has noticeably fewer silence hallucinations.

    Args:
        audio_path: WAV file (16kHz mono int16 expected).
        model: backend-specific override. Whisper: HF repo path. Gemini:
            model name like "gemini-2.5-flash". Defaults from the
            corresponding ENV_* var.
        initial_prompt: whisper-only — biasing prompt for proper nouns.
            Ignored by Gemini (which uses GEMINI_TRANSCRIBE_INSTRUCTION).

    Returns:
        Transcribed text. Empty string for silent / unintelligible audio
        (Gemini's preferred behavior; whisper unfortunately hallucinates
        "Thank you." instead).
    """
    backend = os.environ.get(ENV_TRANSCRIBER, "whisper").lower()
    if backend == "gemini":
        return _transcribe_gemini(audio_path, model)
    return _transcribe_whisper(audio_path, model, initial_prompt)


def _transcribe_whisper(
    audio_path: Path,
    model: Optional[str],
    initial_prompt: Optional[str],
) -> str:
    """Local mlx-whisper backend (default)."""
    import mlx_whisper

    model = model or os.environ.get(ENV_MODEL, DEFAULT_MODEL)
    if initial_prompt is None:
        initial_prompt, _ = resolved_prompt()

    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model,
        initial_prompt=initial_prompt or None,
    )
    return (result.get("text") or "").strip()


def _transcribe_gemini(audio_path: Path, model: Optional[str]) -> str:
    """Hosted Gemini audio transcription backend.

    Reads the audio file inline (Gemini accepts up to 20 MB inline; our
    8-second 16 kHz mono int16 chunks are ~256 KB). Uses temperature 0.0
    for stability.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            f"{ENV_TRANSCRIBER}=gemini requires the 'gemini' extra. "
            "Install with: pip install -e '.[gemini]'"
        ) from e

    api_key = _resolve_gemini_api_key()
    if not api_key:
        raise RuntimeError(
            f"{ENV_TRANSCRIBER}=gemini needs a Google API key. "
            "Set GOOGLE_API_KEY or GEMINI_API_KEY, or write the key to "
            f"{GEMINI_KEY_FILE} (mode 600)."
        )

    model = model or os.environ.get(ENV_GEMINI_MODEL, DEFAULT_GEMINI_MODEL)
    client = genai.Client(api_key=api_key)

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
