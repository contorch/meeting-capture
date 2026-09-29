"""Transcripts go straight into the contorch database — no transcript files.

Each meeting is one row of the `transcripts` table in context-orchestrator's
SQLite database (~/.context-orchestrator/context.db, or $CO_DB_PATH). Lines
are appended to the row's `body` as they are transcribed, in the same
`[HH:MM:SS] **Me:** text` format the old ~/transcripts/*.md files had.
context-orchestrator indexes a row into Chroma once it has been quiet for a
minute, and serves the full text via its get_transcript tool.

The DDL below is a shared contract with
context-orchestrator/src/context_orchestrator/db.py — keep them identical.
Both sides run CREATE ... IF NOT EXISTS, so whichever starts first creates it.
context-orchestrator also adds full-text-search triggers on this table; they
update its FTS index on every append here, with nothing to do on this side.

If a write fails (the database is locked for longer than the timeout, the
disk is full) the line is queued in PENDING_FILE and written ahead of the
next line, so a transcribed sentence is never dropped.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .paths import STATE_DIR

log = logging.getLogger("meeting-capture.store")

DDL = """
CREATE TABLE IF NOT EXISTS transcripts (
    meeting_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    content_sha TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    indexed_at REAL,
    indexed_with TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_transcripts_updated ON transcripts(updated_at);
"""

PENDING_FILE = STATE_DIR / "unsaved-lines.jsonl"


def db_path() -> Path:
    p = os.environ.get("CO_DB_PATH")
    return Path(p).expanduser() if p else Path.home() / ".context-orchestrator" / "context.db"


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    return conn


def header(meeting_id: str) -> str:
    return f"# Meeting transcript {meeting_id}\n\n"


def _write(conn: sqlite3.Connection, meeting_id: str, line: str, started_at: str, now: float) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO transcripts (meeting_id, title, source, started_at, body, "
            "created_at, updated_at) VALUES (?, ?, 'meeting-capture', ?, ?, ?, ?)",
            (meeting_id, meeting_id, started_at, header(meeting_id), now, now),
        )
        conn.execute(
            "UPDATE transcripts SET body = body || ?, updated_at = ? WHERE meeting_id = ?",
            (line, now, meeting_id),
        )


def _flush_pending(conn: sqlite3.Connection) -> None:
    if not PENDING_FILE.exists():
        return
    entries = [json.loads(l) for l in PENDING_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    for i, e in enumerate(entries):
        try:
            _write(conn, e["meeting_id"], e["line"], e.get("started_at", ""), e["at"])
        except sqlite3.Error:
            # Keep only what didn't make it, so nothing is written twice.
            PENDING_FILE.write_text("".join(json.dumps(x) + "\n" for x in entries[i:]), encoding="utf-8")
            raise
    PENDING_FILE.unlink()
    log.info("wrote %d queued transcript line(s) to the database", len(entries))


def append(meeting_id: str, line: str, started_at: str = "", path: Optional[Path] = None) -> bool:
    """Append one transcript line to a meeting. Returns False if it had to be
    queued on disk instead (retried on the next append)."""
    now = time.time()
    try:
        conn = connect(path)
        try:
            _flush_pending(conn)
            _write(conn, meeting_id, line, started_at, now)
        finally:
            conn.close()
        return True
    except (sqlite3.Error, OSError, ValueError) as exc:
        log.error("could not write to %s (%s) — line queued in %s", path or db_path(), exc, PENDING_FILE)
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        with PENDING_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"meeting_id": meeting_id, "line": line,
                                "started_at": started_at, "at": now}) + "\n")
        return False


def get(meeting_id: str, path: Optional[Path] = None) -> Optional[dict]:
    if not (path or db_path()).exists():
        return None
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM transcripts WHERE meeting_id = ?", (meeting_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def recent(limit: int = 50, path: Optional[Path] = None, prefix: str = "meeting-") -> list[dict]:
    """Most recently updated meetings, newest first (body included)."""
    if not (path or db_path()).exists():
        return []
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM transcripts WHERE meeting_id LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (prefix + "%", limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count(path: Optional[Path] = None, prefix: str = "meeting-") -> int:
    if not (path or db_path()).exists():
        return 0
    conn = connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM transcripts WHERE meeting_id LIKE ?",
                            (prefix + "%",)).fetchone()[0]
    finally:
        conn.close()
