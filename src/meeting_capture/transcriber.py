"""Transcription for meeting-capture audio chunks (hosted Gemini backends).

Two backends, chosen by model name:

  * ``gemini-3.5-transcribe`` (default) — Google's purpose-built speech-to-text
    model via the Interactions API. Verbatim by default, deterministic proper
    nouns via ``custom_vocabulary`` (``~/.meeting-capture/vocab.txt``), optional
    speaker diarization for the "them" channel (MEETING_CAPTURE_DIARIZE=1 —
    mutually exclusive with vocabulary, per the API).
  * Any other Gemini model (e.g. ``gemini-2.5-flash``) — general audio
    understanding via ``generate_content`` with a transcription prompt. Also the
    automatic fallback if the transcribe backend errors, so a preview-model
    hiccup never loses a chunk.

Gotcha preserved here for posterity: sending audio to a ``gemini-3.5-transcribe``
model through ``generate_content`` returns an EMPTY transcript while still
billing the audio tokens — hence the hard dispatch below.

Requires a Google API key, resolved in order from:
  $GOOGLE_API_KEY, $GEMINI_API_KEY, or ~/.config/google/key (mode 600).
Override the model with MEETING_CAPTURE_GEMINI_MODEL.

(A local mlx-whisper backend was removed: it ran on the GPU and its unbounded
MLX Metal buffer cache leaked tens of GB in a long-lived daemon.)
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

from .paths import VOCAB_FILE

log = logging.getLogger("meeting-capture.transcriber")

DEFAULT_GEMINI_MODEL = "gemini-3.5-transcribe"
FALLBACK_GEMINI_MODEL = "gemini-2.5-flash"

ENV_GEMINI_MODEL = "MEETING_CAPTURE_GEMINI_MODEL"
ENV_DIARIZE = "MEETING_CAPTURE_DIARIZE"
GEMINI_KEY_FILE = Path.home() / ".config" / "google" / "key"

REQUEST_TIMEOUT_MS = 60_000
MAX_VOCAB_TERMS = 1000

# --- generate_content backend prompts ------------------------------------------------

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

# Mic ("me") chunks are a single known speaker — the device owner talking into
# their own microphone — so speaker labels are noise there.
GEMINI_TRANSCRIBE_INSTRUCTION_ME = (
    "Transcribe the audio. It is a single speaker talking into their own "
    "microphone during a meeting. Return only the spoken text, nothing else. "
    "Do not add speaker labels. If the audio is silent, contains only "
    "background noise, or has no intelligible speech, return an empty "
    "string. Do not invent or filler-fill text. Do not add commentary, "
    "summary, or formatting."
)


# --- model dispatch -----------------------------------------------------------------

def resolve_model(model: Optional[str] = None) -> str:
    return model or os.environ.get(ENV_GEMINI_MODEL, DEFAULT_GEMINI_MODEL)


def is_transcribe_model(model: str) -> bool:
    """True for the purpose-built batch speech-to-text models (Interactions API)."""
    return model.startswith("gemini-3.5-transcribe") and not model.endswith("-live")


def diarization_enabled() -> bool:
    """Diarize the 'them' channel? Off by default: the API makes it mutually
    exclusive with custom vocabulary, and proper-noun fidelity matters more
    for memory than speaker labels within a single channel."""
    return os.environ.get(ENV_DIARIZE, "0").strip().lower() in ("1", "true", "yes", "on")


def load_vocabulary(path: Path = VOCAB_FILE) -> list[str]:
    """Custom vocabulary terms (one per line, '#' comments), capped at the API limit."""
    if not path.exists():
        return []
    terms: list[str] = []
    seen: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            term = line.split("#", 1)[0].strip()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    except OSError:
        return []
    return terms[:MAX_VOCAB_TERMS]


def transcribe(
    audio_path: Path,
    model: Optional[str] = None,
    instruction: Optional[str] = None,
    role: str = "them",
) -> str:
    """Transcribe a single audio chunk.

    Args:
        audio_path: WAV file (16kHz mono int16 expected).
        model: model override (defaults from ENV_GEMINI_MODEL / DEFAULT_GEMINI_MODEL).
        instruction: prompt override for the generate_content backend only.
        role: "me" (own mic, single speaker) or "them" (system audio).

    Returns:
        Transcribed text. Empty string for silent / unintelligible audio.
        Diarized "them" chunks carry [SPEAKER_n] prefixes per speaker turn.
    """
    model = resolve_model(model)
    if model.endswith("-live"):
        raise RuntimeError(
            f"{model} is a streaming model; batch transcription needs "
            "gemini-3.5-transcribe (or set MEETING_CAPTURE_MODE=live)."
        )
    if is_transcribe_model(model):
        try:
            return _transcribe_interactions(audio_path, model, role)
        except Exception as exc:
            # Preview model / API hiccup: never lose the chunk. Fall back to the
            # general audio model, which has carried this pipeline for months.
            log.warning(
                "%s failed (%s: %s) — falling back to %s for %s",
                model, type(exc).__name__, str(exc)[:160], FALLBACK_GEMINI_MODEL, audio_path.name,
            )
            return _transcribe_gemini(audio_path, FALLBACK_GEMINI_MODEL, instruction, role)
    return _transcribe_gemini(audio_path, model, instruction, role)


# --- shared client -------------------------------------------------------------------

def _client():
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "meeting-capture requires the google-genai package (>=2.0). "
            "Install with: pip install -e ."
        ) from e

    api_key = _resolve_gemini_api_key()
    if not api_key:
        raise RuntimeError(
            "Gemini transcription needs a Google API key. "
            "Set GOOGLE_API_KEY or GEMINI_API_KEY, or write the key to "
            f"{GEMINI_KEY_FILE} (mode 600)."
        )
    # The SDK has no read timeout by default — a half-open TLS connection can
    # wedge the daemon indefinitely on SSL_read. Explicit per-request timeout.
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS)), types


# --- backend: Interactions API (gemini-3.5-transcribe) --------------------------------

def _transcription_config(role: str, vocab: list[str], diarize: bool) -> dict:
    """Per-channel config. Vocabulary and diarization/timestamps are mutually
    exclusive in the API; 'me' is one known speaker so it always takes vocab."""
    cfg: dict = {"mode": {"type": "verbatim"}}
    if role == "them" and diarize:
        cfg["mode"]["diarization_mode"] = "speaker"
        # Speaker annotations only populate alongside word-level timestamps.
        cfg["mode"]["timestamp_granularities"] = ["word"]
    elif vocab:
        cfg["custom_vocabulary"] = vocab
    return cfg


def _transcribe_interactions(audio_path: Path, model: str, role: str) -> str:
    client, _types = _client()
    if not hasattr(client, "interactions"):
        raise RuntimeError(
            f"{model} needs the Interactions API — upgrade with: pip install -U 'google-genai>=2.0'"
        )
    cfg = _transcription_config(role, load_vocabulary(), diarization_enabled())
    audio = {
        "type": "audio",
        "data": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
        "mime_type": "audio/wav",
    }
    interaction = client.interactions.create(
        model=model,
        input=[audio],
        generation_config={"transcription_config": cfg},
    )
    if "diarization_mode" in cfg["mode"]:
        diarized = format_diarized(_collect_annotations(interaction))
        if diarized:
            return diarized
    return (getattr(interaction, "output_text", None) or "").strip()


def _collect_annotations(interaction) -> list[dict]:
    """Flatten word annotations ({text, speaker, start_offset, ...}) from an interaction."""
    out: list[dict] = []
    for step in getattr(interaction, "steps", None) or []:
        for content in getattr(step, "content", None) or []:
            for ann in getattr(content, "annotations", None) or []:
                if hasattr(ann, "model_dump"):
                    out.append(ann.model_dump(exclude_none=True))
                elif isinstance(ann, dict):
                    out.append(ann)
    return out


def format_diarized(annotations: list[dict]) -> str:
    """Group consecutive same-speaker words into '[SPEAKER_n] text' lines.

    The API labels speakers 'spk:0', 'spk:1', …; we keep the transcript
    convention already used by the prompt backend ([SPEAKER_1], [SPEAKER_2]).
    """
    lines: list[str] = []
    cur_spk: Optional[str] = None
    cur_words: list[str] = []
    labels: dict[str, int] = {}

    def flush() -> None:
        if cur_words and cur_spk is not None:
            n = labels.setdefault(cur_spk, len(labels) + 1)
            lines.append(f"[SPEAKER_{n}] " + " ".join(cur_words))

    for ann in annotations:
        word = (ann.get("text") or "").strip()
        if not word:
            continue
        spk = str(ann.get("speaker") or "spk:0")
        if spk != cur_spk:
            flush()
            cur_spk, cur_words = spk, []
        cur_words.append(word)
    flush()
    return "\n".join(lines)


# --- backend: generate_content (general audio models) ---------------------------------

def _transcribe_gemini(
    audio_path: Path,
    model: Optional[str],
    instruction: Optional[str] = None,
    role: str = "them",
) -> str:
    """General Gemini audio-understanding backend (prompted transcription)."""
    client, types = _client()
    model = model or FALLBACK_GEMINI_MODEL
    if instruction is None:
        instruction = GEMINI_TRANSCRIBE_INSTRUCTION_ME if role == "me" else GEMINI_TRANSCRIBE_INSTRUCTION
    response = client.models.generate_content(
        model=model,
        contents=[
            instruction,
            types.Part.from_bytes(data=audio_path.read_bytes(), mime_type="audio/wav"),
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
