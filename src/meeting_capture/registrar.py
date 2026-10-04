"""The recorder's SMAppService agent inside Contorch.app: register,
unregister and report it, in-process, through PyObjC (ported from the
contorch-macos smappservice-from-python lab, smsvc.py).

Only a process running from inside the bundle may call it — the menu bar,
or a `Contents/MacOS/contorch-python` child such as the bundled
`meeting-capture` CLI. SMAppService finds the plist in the calling app's
`Contents/Library/LaunchAgents/` (from outside, unregister fails with
EINVAL; PROVEN in the lab). The bundled plist itself is generated at build
time by `meeting-capture plist --bundled` (supervisor.bundled_plist_payload).

On register this module writes ~/.meeting-capture/agent.json
({"backend": "app", "app", "label", "sysaudio", "version", "registered"}) so
that other installs on this Mac (a Homebrew CLI) know the app runs the
recorder and which sysaudio it pins; unregister removes it.

PyObjC comes with the `app` extra (pyobjc-framework-ServiceManagement); the
bundle always has it. Nothing here decides anything: supervisor.py does.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import paths

STATUS = {0: "not_registered", 1: "enabled", 2: "requires_approval", 3: "not_found"}


class Unavailable(RuntimeError):
    """PyObjC's ServiceManagement isn't importable here (not the app's Python)."""


def _sm():
    try:
        import ServiceManagement as SM   # pyobjc-framework-ServiceManagement
    except ImportError as exc:
        raise Unavailable("SMAppService needs pyobjc-framework-ServiceManagement "
                          "(meeting-capture[app]; Contorch.app ships it)") from exc
    return SM


def available() -> bool:
    try:
        _sm()
        return True
    except Unavailable:
        return False


def _service(label: str):
    return _sm().SMAppService.agentServiceWithPlistName_(f"{label}.plist")


def _err(e) -> dict | None:
    if e is None:
        return None
    return {"domain": str(e.domain()), "code": int(e.code()), "message": str(e.localizedDescription())}


def status(label: str | None = None) -> str:
    """not_registered | enabled | requires_approval | not_found. The API says
    `enabled` for a job that is dead, so the supervisor also asks launchd."""
    s = int(_service(label or paths.LAUNCHD_LABEL).status())
    return STATUS.get(s, str(s))


def _call(label: str, verb: str) -> dict:
    svc = _service(label)
    before = int(svc.status())
    fn = svc.registerAndReturnError_ if verb == "register" else svc.unregisterAndReturnError_
    ok, err = fn(None)
    after = int(svc.status())
    return {"ok": bool(ok), "error": _err(err), "status_before": STATUS.get(before, str(before)),
            "status_after": STATUS.get(after, str(after))}


def write_record(record: dict) -> None:
    p = Path(paths.AGENT_RECORD)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agent.", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, sort_keys=True, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_record() -> dict:
    try:
        doc = json.loads(Path(paths.AGENT_RECORD).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def remove_record() -> None:
    Path(paths.AGENT_RECORD).unlink(missing_ok=True)


def register(label: str, record: dict) -> dict:
    """Register the bundled agent; on success write agent.json. `record` is
    {app, sysaudio, version} from the supervisor."""
    out = _call(label, "register")
    if out["ok"] or out["status_after"] in ("enabled", "requires_approval"):
        write_record({"backend": "app", "label": label, "registered": True, **record})
    return out


def unregister(label: str) -> dict:
    out = _call(label, "unregister")
    if out["ok"] or out["status_after"] in ("not_registered", "not_found"):
        remove_record()
    return out
