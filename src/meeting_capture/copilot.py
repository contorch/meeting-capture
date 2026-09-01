"""In-meeting copilot: Claude/Gemini watches the live transcript and whispers help.

Phase 3 of live mode. Reads the JSONL feed produced by ``live.py`` (finalized
utterances from the meeting), and when the other side ("them") says something
you'd want help answering — a question, a reference to a past decision, a
number — it retrieves relevant context from your past meetings and asks an LLM
for a short whisper: the fact, the commitment, the answer, with where it came
from. If nothing useful applies, it stays quiet.

This is the "torch lit during the call": contorch's memory surfaced in the
conversational beat, not read afterwards.

Run it in a terminal pane during a meeting:

    MEETING_CAPTURE_MODE=live meeting-capture run     # (one pane: the daemon)
    meeting-capture copilot                           # (another: the whispers)

Design choices:
  * Triggers on FINAL "them" utterances only (the other person addressing you
    is the moment help matters), debounced so it never spams.
  * Retrieval is over ~/transcripts/*.md — your meeting memory, plain files,
    zero extra deps. Pluggable via a retriever hook so a contorch semantic
    search can drop in later (Phase 3.1).
  * The LLM is instructed to output NONE when it has nothing useful, so silence
    is the default and whispers are rare and worth reading.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .paths import LIVE_DIR, TRANSCRIPTS_DIR

log = logging.getLogger("meeting-capture.copilot")

COPILOT_MODEL = os.environ.get("MEETING_CAPTURE_COPILOT_MODEL", "gemini-2.5-flash")
# Don't fire on trivially short utterances or more often than this.
MIN_TRIGGER_CHARS = 12
DEBOUNCE_SECONDS = 8.0
# How many recent finals to give the LLM as conversation context.
CONTEXT_TURNS = 8
# Retrieval breadth.
MAX_SNIPPETS = 4
SNIPPET_CHARS = 280

_STOPWORDS = {
    "the", "and", "for", "you", "your", "was", "were", "can", "did", "does",
    "what", "when", "where", "who", "why", "how", "with", "that", "this",
    "have", "has", "are", "our", "their", "about", "would", "could", "should",
    "we", "i", "a", "an", "to", "of", "in", "on", "is", "it", "do", "so",
}

_QUESTION_RE = re.compile(r"\?\s*$")
_INTERROGATIVE_RE = re.compile(
    r"\b(what|when|where|who|why|how|which|did (?:we|you)|do (?:we|you)|"
    r"are (?:we|you)|is (?:it|there)|can (?:we|you)|should (?:we|you)|"
    r"remember|remind me|last time|we (?:said|decided|agreed)|you (?:said|promised))\b",
    re.IGNORECASE,
)


def is_trigger(text: str) -> bool:
    """Should this utterance prompt the copilot to consider helping?"""
    t = text.strip()
    if len(t) < MIN_TRIGGER_CHARS:
        return False
    return bool(_QUESTION_RE.search(t) or _INTERROGATIVE_RE.search(t))


def keywords(text: str) -> list[str]:
    """Salient lowercase terms from an utterance, for retrieval."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower())
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        if w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


@dataclass
class Snippet:
    source: str
    text: str


def retrieve_transcripts(query: str, exclude_stem: str = "", limit: int = MAX_SNIPPETS,
                         transcripts_dir: Path = TRANSCRIPTS_DIR) -> list[Snippet]:
    """Keyword search over past meeting transcripts. Zero-dependency retriever.

    Scores each transcript line by how many query keywords it contains; returns
    the best lines across files, newest files first on ties.
    """
    terms = keywords(query)
    if not terms or not transcripts_dir.exists():
        return []
    scored: list[tuple[int, float, str, str]] = []
    files = sorted(transcripts_dir.glob("meeting-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        if exclude_stem and f.stem == exclude_stem:
            continue
        mtime = f.stat().st_mtime
        try:
            # Content lines only (drop the header + blank lines). A meeting is
            # Q-then-A across adjacent lines, so a matched line is returned with
            # the next couple of lines — otherwise the *answer* (which rarely
            # shares the question's keywords) never comes along.
            lines = [ln.strip() for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip() and not ln.startswith("#")]
        except OSError:
            continue
        for i, line in enumerate(lines):
            score = sum(1 for term in terms if term in line.lower())
            if score >= 1:  # any keyword match; the LLM filters precision via NONE
                window = " ".join(lines[i:i + 3])[:SNIPPET_CHARS]
                scored.append((score, mtime, f.stem, window))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    out: list[Snippet] = []
    seen: set[str] = set()
    for _score, _mt, stem, line in scored:
        if line in seen:
            continue
        seen.add(line)
        out.append(Snippet(source=stem, text=line))
        if len(out) >= limit:
            break
    return out


WHISPER_INSTRUCTION = (
    "You are a silent meeting copilot for the user (\"Me\"). The other side "
    "(\"Them\") just said something the user may need to respond to. Using the "
    "recent conversation and the retrieved notes from the user's PAST meetings, "
    "produce a whisper that helps the user answer RIGHT NOW: a decision they "
    "made, a number they committed to, a fact, or a reminder — grounded ONLY in "
    "the retrieved notes and conversation, never invented.\n\n"
    "Rules:\n"
    "- If the retrieved notes contain nothing genuinely useful for responding, "
    "output exactly: NONE\n"
    "- Otherwise output ONE or TWO short sentences, plain text, no preamble. "
    "Lead with the answer. If it comes from a specific past meeting, end with "
    "the source in parentheses.\n"
    "- Never fabricate facts, numbers, names, or commitments. Prefer NONE over a guess."
)


def build_prompt(utterance: str, context_turns: list[str], snippets: list[Snippet]) -> str:
    convo = "\n".join(context_turns[-CONTEXT_TURNS:]) or "(none)"
    notes = "\n".join(f"- {s.text}  [{s.source}]" for s in snippets) or "(no relevant past notes found)"
    return (
        f"{WHISPER_INSTRUCTION}\n\n"
        f"THEM just said:\n{utterance}\n\n"
        f"Recent conversation:\n{convo}\n\n"
        f"Retrieved notes from past meetings:\n{notes}\n"
    )


def _llm_whisper(prompt: str, model: str) -> str:
    from .transcriber import _client
    client, _types = _client()
    resp = client.models.generate_content(
        model=model, contents=[prompt], config={"temperature": 0.2},
    )
    return (resp.text or "").strip()


def consider(
    utterance: str,
    context_turns: list[str],
    exclude_stem: str = "",
    model: str = COPILOT_MODEL,
    retriever: Optional[Callable[[str, str], list[Snippet]]] = None,
    llm: Optional[Callable[[str, str], str]] = None,
) -> Optional[dict]:
    """Produce a whisper for one utterance, or None. Retriever/LLM are injectable for tests."""
    if not is_trigger(utterance):
        return None
    retrieve = retriever or (lambda q, ex: retrieve_transcripts(q, ex))
    snippets = retrieve(utterance, exclude_stem)
    prompt = build_prompt(utterance, context_turns, snippets)
    generate = llm or _llm_whisper
    try:
        text = (generate(prompt, model) or "").strip()
    except Exception as exc:  # LLM/network hiccup: stay silent, don't crash the pane
        log.warning("copilot LLM error (%s: %s)", type(exc).__name__, str(exc)[:120])
        return None
    if not text or text.upper().startswith("NONE"):
        return None
    return {"text": text, "sources": [s.source for s in snippets], "utterance": utterance}


def _newest_feed() -> Optional[Path]:
    if not LIVE_DIR.exists():
        return None
    feeds = [p for p in LIVE_DIR.glob("*.jsonl") if not p.name.endswith(".whispers.jsonl")]
    return max(feeds, key=lambda p: p.stat().st_mtime) if feeds else None


def watch(feed: Optional[Path] = None, model: str = COPILOT_MODEL,
          emit: Optional[Callable[[dict], None]] = None) -> int:
    """Follow a live feed and emit whispers on triggery 'them' finals."""
    feed = feed or _newest_feed()
    if feed is None:
        log.error("no live feed found — start a meeting with MEETING_CAPTURE_MODE=live")
        return 1
    whispers_path = feed.with_suffix(".whispers.jsonl")

    def _default_emit(w: dict) -> None:
        print(f"\n  💡 {w['text']}\n", flush=True)

    emit = emit or _default_emit
    context_turns: list[str] = []
    last_fire = 0.0
    stem = feed.stem

    log.info("copilot watching %s (model=%s)", feed.name, model)
    print(f"— copilot: watching {feed.name} —  (Ctrl-C to stop)\n", flush=True)

    import subprocess
    proc = subprocess.Popen(["tail", "-n", "0", "-F", str(feed)], stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != "final":
                continue
            role, text = rec.get("role"), (rec.get("text") or "").strip()
            if not text:
                continue
            context_turns.append(f"{'Me' if role == 'me' else 'Them'}: {text}")
            if role != "them":
                continue
            now = time.time()
            if now - last_fire < DEBOUNCE_SECONDS:
                continue
            w = consider(text, context_turns, exclude_stem=stem, model=model)
            if w:
                last_fire = now
                w["clock"] = rec.get("clock", "")
                with whispers_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(w, ensure_ascii=False) + "\n")
                emit(w)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0
