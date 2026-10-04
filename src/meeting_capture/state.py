"""Is a meeting being recorded right now? One answer, from the daemon itself.

The daemon writes ~/.meeting-capture/state.json (`meeting-capture.state/1`)
atomically at every transition, and every HEARTBEAT_S while it records:

    {"schema", "pid", "state": "idle"|"recording"|"paused", "since",
     "meeting_id", "source": "sck"|"linein", "updated_at"}

`meeting-capture status --json` (`meeting-capture.status/1`) turns it into the
answer other programs act on — pipeline-monitor's ● REC, the update gate
(never install while recording, nor while it can't tell), adopt and heal:

    {"schema", "ok", "recording": true|false|null, "state", "since",
     "meeting_id", "pid", "stale", "reason"?}

`recording: null` means "can't tell": no state file, the daemon that wrote it
is gone, or it says recording but its heartbeat is older than STALE_S. Nobody
else parses the daemon log to decide anything.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import paths

STATE_SCHEMA = "meeting-capture.state/1"
STATUS_SCHEMA = "meeting-capture.status/1"
STATES = ("idle", "recording", "paused")
HEARTBEAT_S = 30.0
STALE_S = 90.0


def _path() -> Path:
    return Path(paths.STATE_FILE)


def write(doc: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state.", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, sort_keys=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read() -> dict | None:
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == STATE_SCHEMA else None


class Recorder:
    """The daemon's side: set() at each transition (written only when
    something changed), heartbeat() as often as convenient (it writes at most
    every HEARTBEAT_S, and only while recording). Never raises: a full disk
    must not stop a recording."""

    def __init__(self, source: str = "sck", clock=time.time) -> None:
        self.source = source
        self.clock = clock
        self.state: str | None = None
        self.meeting_id: str | None = None
        self.since: float | None = None
        self.written_at = 0.0

    def set(self, state: str, meeting_id: str | None = None) -> None:
        if state not in STATES:
            raise ValueError(state)
        meeting_id = meeting_id if state == "recording" else None
        if state == self.state and meeting_id == self.meeting_id:
            return
        if state != self.state:
            self.since = self.clock()
        self.state, self.meeting_id = state, meeting_id
        self._write()

    def meeting(self, meeting_id: str | None) -> None:
        """The meeting being recorded changed (or became known)."""
        if self.state == "recording" and meeting_id != self.meeting_id:
            self.meeting_id = meeting_id
            self._write()

    def heartbeat(self) -> None:
        if self.state == "recording" and self.clock() - self.written_at >= HEARTBEAT_S:
            self._write()

    def _write(self) -> None:
        now = self.clock()
        try:
            write({"schema": STATE_SCHEMA, "pid": os.getpid(), "state": self.state, "since": self.since,
                   "meeting_id": self.meeting_id, "source": self.source, "updated_at": now})
            self.written_at = now
        except Exception:
            pass

    def clear(self) -> None:
        """The daemon is exiting: remove the file if it is still ours."""
        doc = read()
        if doc and doc.get("pid") == os.getpid():
            _path().unlink(missing_ok=True)


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True               # exists, owned by someone else
    return True


def status(now: float | None = None) -> dict:
    """`meeting-capture status --json`. Never raises; `recording` is null
    whenever the answer would be a guess."""
    now = time.time() if now is None else now
    out = {"schema": STATUS_SCHEMA, "ok": True, "recording": None, "state": None, "since": None,
           "meeting_id": None, "pid": None, "stale": False}
    doc = read()
    if doc is None:
        out["reason"] = "no_state"            # no daemon has run (or a pre-0.8 one)
        return out
    out.update(state=doc.get("state"), since=doc.get("since"), meeting_id=doc.get("meeting_id"),
               pid=doc.get("pid"), source=doc.get("source"), updated_at=doc.get("updated_at"))
    if not _alive(doc.get("pid")):
        out["reason"] = "daemon_not_running"
        return out
    if doc.get("state") == "recording":
        try:
            age = now - float(doc.get("updated_at"))
        except (TypeError, ValueError):
            age = float("inf")
        if age > STALE_S:
            out["stale"] = True
            out["reason"] = "stale_heartbeat"
            return out
        out["recording"] = True
        return out
    if doc.get("state") in ("idle", "paused"):
        out["recording"] = False
        return out
    out["reason"] = "unknown_state"
    return out
