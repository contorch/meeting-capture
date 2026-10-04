"""The /meeting Claude Code skill: meeting-capture installs its own.

The skill ships as package data (meeting_capture/skills/meeting: SKILL.md and
the bin/feed reader), so the Homebrew venv and Contorch.app's bundle both have
it. `meeting-capture skill install` links ~/.claude/skills/meeting to it;
`uninstall` removes that link. A ~/.claude/skills/meeting that isn't ours —
a folder, or a link somewhere else — is the user's own copy and is kept.

Ours = a symlink whose target ends in meeting_capture/skills/meeting (any
install's; a link left by a moved or trashed app dangles and is replaced).
"""
from __future__ import annotations

import os
from pathlib import Path

SCHEMA = "meeting-capture.skill/1"
NAME = "meeting"
OURS_SUFFIX = os.path.join("meeting_capture", "skills", NAME)


def source() -> Path:
    return Path(__file__).resolve().parent / "skills" / NAME


def target() -> Path:
    from . import paths
    return paths.HOME / ".claude" / "skills" / NAME


def _ours(p: Path) -> bool:
    return p.is_symlink() and os.path.normpath(os.readlink(p)).endswith(OURS_SUFFIX)


def status() -> dict:
    t = target()
    if t.is_symlink():
        state = "linked" if _ours(t) and Path(os.readlink(t)) == source() else \
                ("ours_elsewhere" if _ours(t) else "user_copy")
    elif t.exists():
        state = "user_copy"
    else:
        state = "absent"
    return {"path": str(t), "source": str(source()), "state": state,
            "matches": state == "linked" and (t / "SKILL.md").is_file()}


def install() -> dict:
    t, src = target(), source()
    if not (src / "SKILL.md").is_file():
        return {"schema": SCHEMA, "ok": False, "path": str(t), "action": "none",
                "error": {"code": "no_skill", "message": f"the packaged skill is missing at {src}"}}
    if t.is_symlink() or t.exists():
        if not _ours(t):
            return {"schema": SCHEMA, "ok": True, "path": str(t), "action": "kept_user_copy"}
        if Path(os.readlink(t)) == src:
            return {"schema": SCHEMA, "ok": True, "path": str(t), "action": "none"}
        t.unlink()                                  # ours, from another install or a moved app
    t.parent.mkdir(parents=True, exist_ok=True)
    t.symlink_to(src, target_is_directory=True)
    return {"schema": SCHEMA, "ok": True, "path": str(t), "action": "linked"}


def uninstall() -> dict:
    t = target()
    if _ours(t):
        t.unlink()
        return {"schema": SCHEMA, "ok": True, "path": str(t), "action": "removed"}
    if t.is_symlink() or t.exists():
        return {"schema": SCHEMA, "ok": True, "path": str(t), "action": "kept_user_copy"}
    return {"schema": SCHEMA, "ok": True, "path": str(t), "action": "none"}
