"""Local transcription via mlx-whisper (Apple Silicon).

Whisper's `initial_prompt` biases token prediction toward the listed terms.
It is the most effective fix for proper-noun and acronym errors (no model
size will recover terms that aren't in training data — e.g. internal project
names, custom acronyms). Override via the `MEETING_CAPTURE_WHISPER_PROMPT`
env var to add domain-specific vocabulary.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

# Generic technical vocabulary — Whisper "small" tends to mishear these.
# Override via MEETING_CAPTURE_WHISPER_PROMPT for project-specific terms.
DEFAULT_INITIAL_PROMPT = (
    "This is a technical engineering meeting discussing software "
    "architecture, APIs, rate limiting, throttling, retries, JWT and "
    "OAuth authentication, integrators, gRPC, REST, OpenSearch, "
    "ChromaDB, Envoy, Kubernetes, design documents, and TDDs."
)


def transcribe(
    audio_path: Path,
    model: str | None = None,
    initial_prompt: str | None = None,
) -> str:
    import mlx_whisper

    model = model or os.environ.get("MEETING_CAPTURE_WHISPER_MODEL", DEFAULT_MODEL)
    if initial_prompt is None:
        initial_prompt = os.environ.get(
            "MEETING_CAPTURE_WHISPER_PROMPT", DEFAULT_INITIAL_PROMPT
        )

    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model,
        initial_prompt=initial_prompt or None,
    )
    return (result.get("text") or "").strip()
