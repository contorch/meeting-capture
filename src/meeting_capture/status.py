"""What the recorder is doing right now, in a small file anyone can read.

The daemon writes ~/.meeting-capture/state.json on every transition; the menu
bar item (swift/Sources/menubar) and the settings page read it instead of
asking the daemon. Written atomically (temp file + rename) so a reader never
sees half a file.

    {"state": "idle|recording|listening|paused|stopped", "pid": 123,
     "source": "sck|linein", "session": "meeting-…", "since": 1790000000.0}
"""
from __future__ import annotations

import json
import os
import time

from .paths import STATE_FILE

STATES = ("idle", "recording", "listening", "paused", "stopped")


def write(state: str, source: str = "sck", session: str | None = None) -> None:
    payload = {"state": state, "pid": os.getpid(), "source": source,
               "session": session or "", "since": time.time()}
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass  # status is best-effort; never let it stop recording


def read() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


class Reporter:
    """Writes only when something changed, so the idle poll doesn't touch
    the disk every two seconds."""

    def __init__(self, source: str) -> None:
        self.source = source
        self._last: tuple | None = None

    def __call__(self, state: str, session: str | None = None) -> None:
        key = (state, session or "")
        if key != self._last:
            self._last = key
            write(state, self.source, session)
