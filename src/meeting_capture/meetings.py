"""Start a new meeting (a new transcript) on request.

The recorder normally starts a new transcript only after 15 minutes without
speech, so back-to-back meetings land in one transcript. Asking for a new
meeting writes the current time to NEW_MEETING_FILE; the daemon gives the
first chunk that *starts* at or after that time a new meeting id, so speech
from before the click stays with the meeting it belongs to. Resuming after a
pause asks for a new meeting too.

Requested by: `meeting-capture new`, the settings page, the Contorch menu bar
("Start new meeting" in pipeline-monitor), and `meeting-capture resume`.
"""
from __future__ import annotations

import time

from .paths import NEW_MEETING_FILE


def request_new_meeting(at: float | None = None) -> float:
    at = time.time() if at is None else at
    NEW_MEETING_FILE.parent.mkdir(parents=True, exist_ok=True)
    NEW_MEETING_FILE.write_text(f"{at:.3f}")
    return at


def pending_cut() -> float | None:
    try:
        return float(NEW_MEETING_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def clear_cut(cut: float) -> None:
    """Remove the request once honoured — unless a newer one replaced it."""
    if pending_cut() == cut:
        try:
            NEW_MEETING_FILE.unlink()
        except FileNotFoundError:
            pass


def starts_new_meeting(chunk_started_at: float) -> float | None:
    """The pending cut this chunk honours, or None."""
    cut = pending_cut()
    return cut if cut is not None and chunk_started_at >= cut else None
