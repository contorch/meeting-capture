"""Who runs the recorder daemon, behind one interface: the only code in
meeting-capture that starts, stops, registers or restarts the recorder agent.
pipeline-monitor, `contorch` and the settings page go through it
(`meeting-capture start|stop|restart|install|uninstall --json`).

Backends:
  launchctl  the per-user LaunchAgent plist in ~/Library/LaunchAgents that
             `meeting-capture install` writes (Homebrew, source checkouts).
  none       nothing is installed (`meeting-capture run` in a terminal).

Settings are not here: they live in ~/.meeting-capture/env (config.py). A
legacy plist's EnvironmentVariables hold only PATH, the pinned sysaudio (the
recorder's TCC identity) and CONTORCH_CHANNEL.

The operations, the same for every backend:
  current()   what is installed: backend, label, the environment it injects,
              program, pinned sysaudio
  restart()   make the daemon read its settings again. A stopped recorder
              stays stopped (it reads them when it starts). Legacy plist:
              bootout + bootstrap, retried while the old process exits —
              never `kickstart -k`, which restarts from launchd's in-memory
              copy of the plist and would bring back settings the migration
              moved out of it (contorch-macos config-env-file study, E2/E6).
  start() / stop()   resume / stop, persisting across login (enable +
              bootstrap; disable + bootout)
  install() / uninstall()
  status()    the job as launchd sees it (`launchctl print`)

Every mutating operation first asks the channel guard (channel_guard.allowed:
pipeline-monitor's marker decides which install may touch the recorder) and
two job facts that need no marker: an SMAppService job holding the label
belongs to Contorch.app, and a legacy plist that runs another install's
existing interpreter belongs to that install. A refusal is
{"ok": False, "error": {"code": channel_conflict|agent_elsewhere, …}}; the CLI
exits 3.
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

SCHEMA = "meeting-capture.agent/1"
# launchctl exit statuses meaning "no such job loaded" (bootout, kickstart, list).
NOT_LOADED = (3, 36, 113)
# bootstrap while the previous process still exits: "5: Input/output error"
# (E2b); retried.
BOOTSTRAP_BUSY = (5, 37)
# The daemon's SIGTERM path takes up to STOP_GRACE_S (10 s) to finish the
# chunk in hand; launchd's default kill timeout on this Mac is ~5 s (E4).
EXIT_TIMEOUT_S = 15
SM_MANAGED = "com.apple.xpc.ServiceManagement"
EXIT_REFUSED = 3


@dataclass
class Agent:
    backend: str                      # "launchctl" | "none"
    label: str = field(default_factory=lambda: paths.LAUNCHD_LABEL)
    env: dict = field(default_factory=dict)   # environment the agent injects into the daemon
    program: str | None = None        # the interpreter it runs
    sysaudio: str | None = None       # the pinned capture helper: the TCC identity
    plist: str | None = None          # launchctl: the plist path

    @property
    def installed(self) -> bool:
        return self.backend != "none"

    @property
    def target(self) -> str:
        return f"gui/{os.getuid()}/{self.label}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target(label: str | None = None) -> str:
    return f"{_domain()}/{label or paths.LAUNCHD_LABEL}"


def _read_plist(plist: Path) -> dict:
    try:
        return plistlib.loads(plist.read_bytes())
    except Exception:
        return {}


def current() -> Agent:
    """The recorder's supervisor as installed now."""
    plist = Path(paths.LAUNCHD_PLIST)
    if plist.is_file():
        payload = _read_plist(plist)
        env = {str(k): str(v) for k, v in (payload.get("EnvironmentVariables") or {}).items()}
        args = payload.get("ProgramArguments") or [None]
        return Agent("launchctl", label=payload.get("Label", paths.LAUNCHD_LABEL), env=env,
                     program=args[0], sysaudio=env.get("MEETING_CAPTURE_SYSAUDIO"), plist=str(plist))
    return Agent("none")


def pinned_sysaudio() -> Path | None:
    """The sysaudio the installed recorder runs (its TCC identity), if it exists."""
    s = current().sysaudio
    return Path(s) if s and Path(s).is_file() else None


# ------------------------------------------------------------------ launchd facts

def job(label: str | None = None) -> dict | None:
    """The loaded job as `launchctl print` shows it (None: not loaded):
    {state, pid, last_exit, managed_by, program}. The format is not API, so
    only these few lines are read, and a missing one is None."""
    res = _launchctl("print", _target(label))
    if res.returncode != 0:
        return None
    out = res.stdout or ""

    def field_(name: str) -> str | None:
        m = re.search(rf"^\s*{re.escape(name)} = (.+)$", out, re.M)
        return m.group(1).strip() if m else None

    pid = field_("pid")
    return {"state": field_("state"), "pid": int(pid) if pid and pid.isdigit() else None,
            "last_exit": field_("last exit code"), "managed_by": field_("managed_by"),
            "program": field_("program")}


def _loaded(label: str | None = None) -> bool:
    return _launchctl("list", label or paths.LAUNCHD_LABEL).returncode == 0


def _bootstrap(plist: str, deadline_s: float = EXIT_TIMEOUT_S + 5) -> subprocess.CompletedProcess:
    """bootstrap, retried while launchd still holds the job that is exiting."""
    end = time.monotonic() + deadline_s
    while True:
        res = _launchctl("bootstrap", _domain(), plist)
        if res.returncode == 0 or res.returncode not in BOOTSTRAP_BUSY or time.monotonic() >= end:
            return res
        time.sleep(0.1)


def _why(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or res.stdout or "").strip() or f"launchctl exit {res.returncode}"


# ------------------------------------------------------------------ guard

def _refusal(code: str, message: str) -> dict:
    return {"ok": False, "performed": False, "error": {"code": code, "message": message}}


def guard(installing_python: str | None = None) -> dict | None:
    """None when this install may change the recorder agent, else a refusal.
    The channel rule is pipeline-monitor's (the marker); the rest are facts
    about the job that need no marker."""
    from . import channel_guard
    ok, msg = channel_guard.allowed()
    if not ok:
        return _refusal("channel_conflict", msg or "another install manages Contorch on this Mac")
    if channel_guard.channel() != "app":
        j = job()
        if j and j.get("managed_by") == SM_MANAGED:
            return _refusal("channel_conflict",
                            "Contorch.app runs the recorder on this Mac (an SMAppService job holds "
                            f"{paths.LAUNCHD_LABEL}); use the app, or `contorch adopt` to switch back.")
    if installing_python and not channel_guard.marker_path().exists():
        other = current().program
        if (other and Path(other).exists()
                and os.path.normpath(other) != os.path.normpath(installing_python)):
            return _refusal("agent_elsewhere",
                            f"The recorder agent runs {other}, another install's. Run `meeting-capture "
                            "uninstall` with that install first (or `contorch adopt` to switch).")
    return None


# ------------------------------------------------------------------ operations

def _result(action: str, agent: Agent, **extra) -> dict:
    out = {"schema": SCHEMA, "ok": True, "action": action, "backend": agent.backend,
           "label": agent.label, "program": agent.program, "loaded": False, "performed": False}
    out.update(extra)
    if agent.installed and "loaded" not in extra:
        out["loaded"] = _loaded(agent.label)
    return out


def restart(agent: Agent | None = None) -> dict:
    """Restart the daemon so it reads its settings again. Never starts a
    recorder that is stopped."""
    agent = agent or current()
    if not agent.installed:
        return _result("restart", agent, why="no recorder agent is installed")
    refused = guard()
    if refused:
        return {**_result("restart", agent), **refused}
    if not _loaded(agent.label):
        return _result("restart", agent, loaded=False,
                       why="the recorder is stopped; it uses the new settings when it starts")
    _launchctl("bootout", agent.target)
    res = _bootstrap(agent.plist)
    if res.returncode != 0:
        return {**_result("restart", agent), **_refusal("launchctl_failed", _why(res))}
    return _result("restart", agent, performed=True)


def start(agent: Agent | None = None) -> dict:
    """Start the recorder now and at every login."""
    agent = agent or current()
    if not agent.installed:
        return {**_result("start", agent), **_refusal("not_installed", "no recorder agent is installed "
                                                       "(meeting-capture install)")}
    refused = guard()
    if refused:
        return {**_result("start", agent), **refused}
    _launchctl("enable", agent.target)
    if _loaded(agent.label):
        res = _launchctl("kickstart", agent.target)      # loaded: make sure it runs (no -k)
    else:
        res = _bootstrap(agent.plist)
    if res.returncode != 0:
        return {**_result("start", agent), **_refusal("launchctl_failed", _why(res))}
    return _result("start", agent, performed=True)


def stop(agent: Agent | None = None, reason: str | None = None) -> dict:
    """Stop the recorder and keep it stopped across login."""
    agent = agent or current()
    if not agent.installed:
        return _result("stop", agent, why="no recorder agent is installed", reason=reason)
    refused = guard()
    if refused:
        return {**_result("stop", agent, reason=reason), **refused}
    _launchctl("disable", agent.target)
    res = _launchctl("bootout", agent.target)
    if res.returncode != 0 and res.returncode not in NOT_LOADED:
        return {**_result("stop", agent, reason=reason), **_refusal("launchctl_failed", _why(res))}
    # bootout returns while the daemon still finishes its chunk (ExitTimeOut);
    # the job is out of launchd's domain either way.
    return _result("stop", agent, performed=res.returncode == 0, reason=reason, loaded=False)


def plist_payload(python_exe: str, sysaudio: str | None, channel: str = "dev") -> bytes:
    """The legacy agent definition `install` writes. No settings in it: they
    are in the env file. ExitTimeOut lets the daemon finish its chunk."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"),
           "CONTORCH_CHANNEL": "brew" if channel == "brew" else "dev"}
    if sysaudio:
        env["MEETING_CAPTURE_SYSAUDIO"] = sysaudio
    payload = {
        "Label": paths.LAUNCHD_LABEL,
        "ProgramArguments": [python_exe, "-m", "meeting_capture.daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "EnvironmentVariables": env,
        "ProcessType": "Background",
        "ExitTimeOut": EXIT_TIMEOUT_S,
        # The daemon also opens this log itself; kept here so a crash before
        # it does (an import error) still lands somewhere.
        "StandardOutPath": str(paths.LOG_FILE),
        "StandardErrorPath": str(paths.LOG_FILE),
        "WorkingDirectory": str(Path.home()),
    }
    return plistlib.dumps(payload)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def install(python_exe: str, sysaudio: str | None) -> dict:
    """Write and load the legacy agent (Homebrew / source checkouts)."""
    from . import channel_guard, config
    refused = guard(installing_python=python_exe)
    if refused:
        return {**_result("install", current()), **refused}
    moved = config.migrate_from_plist()
    plist = Path(paths.LAUNCHD_PLIST)
    _write_atomic(plist, plist_payload(python_exe, sysaudio, channel_guard.channel()))
    agent = current()
    _launchctl("enable", agent.target)
    _launchctl("bootout", agent.target)     # launchd keeps a loaded job's old plist otherwise
    res = _bootstrap(agent.plist)
    if res.returncode != 0:
        return {**_result("install", agent, moved=moved), **_refusal("launchctl_failed", _why(res))}
    return _result("install", agent, performed=True, moved=moved, plist=agent.plist,
                   sysaudio=agent.sysaudio)


def uninstall() -> dict:
    """Unload and remove the legacy agent. Ends with `launchctl enable`, so no
    disabled override is left behind (a later bootstrap would fail with EIO)."""
    agent = current()
    if not agent.installed:
        return _result("uninstall", agent, why="no recorder agent is installed")
    refused = guard()
    if refused:
        return {**_result("uninstall", agent), **refused}
    _launchctl("bootout", agent.target)
    Path(agent.plist).unlink(missing_ok=True)
    _launchctl("enable", agent.target)
    return _result("uninstall", agent, performed=True, loaded=False)


def status() -> dict:
    agent = current()
    j = job(agent.label) if agent.installed else None
    return {"backend": agent.backend, "label": agent.label, "installed": agent.installed,
            "program": agent.program, "sysaudio": agent.sysaudio, "plist": agent.plist,
            "loaded": j is not None, "pid": (j or {}).get("pid"), "state": (j or {}).get("state"),
            "last_exit": (j or {}).get("last_exit"), "managed_by": (j or {}).get("managed_by")}
