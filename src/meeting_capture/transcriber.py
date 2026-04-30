"""Local transcription via mlx-whisper (Apple Silicon).

Whisper's `initial_prompt` biases token prediction toward the listed terms.
It is the most effective fix for proper-noun and acronym errors (no model
size will recover terms that aren't in training data — e.g. internal project
names, custom acronyms).

Configuration precedence (most → least specific):
  1. Explicit `initial_prompt=...` argument to `transcribe()`
  2. Per-machine file: ~/.meeting-capture/vocab.txt
  3. Env var:           MEETING_CAPTURE_WHISPER_PROMPT
  4. Built-in default

The vocab file is the recommended way to bias for project-specific terms —
edit with `meeting-capture vocab edit`. An empty file means "no prompt at
all" (explicit opt-out, useful when biasing hurts on a particular machine's
audio).
"""
from __future__ import annotations

import os
from pathlib import Path

from .paths import VOCAB_FILE

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

# Generic technical vocabulary — Whisper "small" tends to mishear these.
# Override per machine via ~/.meeting-capture/vocab.txt for project-specific
# terms (use `meeting-capture vocab edit`).
DEFAULT_INITIAL_PROMPT = (
    "This is a technical engineering meeting discussing software "
    "architecture, APIs, rate limiting, throttling, retries, JWT and "
    "OAuth authentication, integrators, gRPC, REST, OpenSearch, "
    "ChromaDB, Envoy, Kubernetes, design documents, and TDDs."
)

ENV_MODEL = "MEETING_CAPTURE_WHISPER_MODEL"
ENV_PROMPT = "MEETING_CAPTURE_WHISPER_PROMPT"


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
    model: str | None = None,
    initial_prompt: str | None = None,
) -> str:
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
