"""Which install channel this process is, and whether it may change the
recorder's agent on this Mac.

The channel comes from one place only: $CONTORCH_CHANNEL. Contorch.app's
stubs set it to `app`, the Homebrew wrappers export `brew`, and the legacy
plist that `meeting-capture install` writes carries `brew` or `dev`. Unset or
unknown means `dev` (a source checkout). Nothing here guesses from paths.

The guard is a READER. The rule lives in pipeline-monitor
(`pipeline_monitor.channel`), the only writer of ~/.contorch/channel.json
(`contorch.channel/1`); it precomputes who may write (`writers`), an optional
operation token (`op`) and the message to show (`blocked_message`). Readers
only test membership:

    no marker                                         -> allowed
    channel() in writers and (op absent/null
      or $CONTORCH_OP == op.id)                       -> allowed
    anything else, an unreadable marker included      -> refused (exit 3)

Contract fixtures: pipeline-monitor contract/channel_guard/*.json, vendored in
tests/fixtures/channel_guard with their sha256 (PIN) and run by
tests/test_channel_guard.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CHANNELS = ("app", "brew", "dev")
EXIT_BLOCKED = 3
DEFAULT_MESSAGE = ("Contorch on this Mac is managed by another install (see {marker}); "
                   "meeting-capture will not change its recorder from here.")


def channel() -> str:
    """$CONTORCH_CHANNEL: app | brew | dev (unset or unknown = dev)."""
    v = os.environ.get("CONTORCH_CHANNEL") or ""
    return v if v in CHANNELS else "dev"


def marker_path() -> Path:
    """~/.contorch/channel.json (pipeline-monitor's, read-only here)."""
    from . import paths
    return paths.HOME / ".contorch" / "channel.json"


def allowed(marker: Path | None = None) -> tuple[bool, str | None]:
    """(True, None), or (False, the message to print before exiting 3)."""
    p = Path(marker or marker_path())
    if not p.exists():
        return True, None
    fallback = DEFAULT_MESSAGE.format(marker=p)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return False, fallback
    msg = doc.get("blocked_message")
    msg = msg if isinstance(msg, str) and msg else fallback
    writers = doc.get("writers")
    if not isinstance(writers, list) or channel() not in writers:
        return False, msg
    op = doc.get("op")
    if op is not None:
        op_id = op.get("id") if isinstance(op, dict) else None
        if not op_id or os.environ.get("CONTORCH_OP") != op_id:
            return False, msg
    return True, None
