"""Who runs the recorder daemon, behind one interface: the only code in
meeting-capture that starts, stops, registers or restarts the recorder agent.
pipeline-monitor, `contorch` and the settings page go through it
(`meeting-capture start|stop|restart|install|uninstall --json`).

Backends:
  launchctl  the per-user LaunchAgent plist in ~/Library/LaunchAgents that
             `meeting-capture install` writes (Homebrew, source checkouts).
  app        the agent bundled in Contorch.app
             (Contents/Library/LaunchAgents/<label>.plist, BundleProgram
             "Contents/MacOS/Contorch Recorder"), registered with SMAppService
             in-process by registrar.py. Selected when $CONTORCH_CHANNEL is
             `app` and this process runs from a bundle that has that plist.
             No Swift and no other executable is involved.
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


BUNDLE_PROGRAM = "Contents/MacOS/Contorch Recorder"


@dataclass
class Agent:
    backend: str                      # "launchctl" | "app" | "none"
    label: str = field(default_factory=lambda: paths.LAUNCHD_LABEL)
    env: dict = field(default_factory=dict)   # environment the agent injects into the daemon
    program: str | None = None        # the interpreter it runs
    sysaudio: str | None = None       # the pinned capture helper: the TCC identity
    plist: str | None = None          # launchctl: the plist path; app: the bundled plist
    app: str | None = None            # app: Contorch.app
    remote: bool = False              # app: registered by an app this process doesn't run from
    conflict: str | None = None       # a second supervisor also claims the recorder

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


# ------------------------------------------------------------------ the bundle

def bundle_root(executable: str | None = None) -> Path | None:
    """The outermost `.app` this process runs from (from sys.executable's
    path; NSBundle.mainBundle inside contorch-python is the stub's embedded
    __info_plist, not the app). A locator only: the channel is $CONTORCH_CHANNEL."""
    import sys
    p = Path(os.path.abspath(executable or sys.executable))
    return next((a for a in reversed(p.parents) if a.suffix == ".app"), None)


def bundled_plist(root: Path) -> Path | None:
    """<app>/Contents/Library/LaunchAgents/<label>.plist (com.contorch.meeting-capture,
    or a lab build's <id>.meeting-capture)."""
    d = Path(root) / "Contents" / "Library" / "LaunchAgents"
    std = d / f"{paths.LAUNCHD_LABEL}.plist"
    if std.is_file():
        return std
    found = sorted(d.glob("*.meeting-capture.plist")) if d.is_dir() else []
    return found[0] if found else None


def bundle_sysaudio(root) -> Path:
    return Path(root) / "Contents" / "Helpers" / "sysaudio"


def bundle_version(root) -> str | None:
    info = _read_plist(Path(root) / "Contents" / "Info.plist")
    v = info.get("CFBundleVersion")
    return str(v) if v is not None else None


def _app_here() -> tuple[Path, Path] | None:
    """(bundle, bundled plist) when this process may drive the app backend."""
    from . import channel_guard
    if channel_guard.channel() != "app":
        return None
    root = bundle_root()
    if root is None:
        return None
    plist = bundled_plist(root)
    return (root, plist) if plist else None


def _record() -> dict:
    from . import registrar
    rec = registrar.read_record()
    return rec if rec.get("backend") == "app" and rec.get("app") else {}


def current() -> Agent:
    """The recorder's supervisor as installed now."""
    legacy = Path(paths.LAUNCHD_PLIST)
    here = _app_here()
    if here:
        root, plist = here
        label = _read_plist(plist).get("Label") or paths.LAUNCHD_LABEL
        return Agent("app", label=label, app=str(root), plist=str(plist),
                     program=str(root / BUNDLE_PROGRAM), sysaudio=str(bundle_sysaudio(root)),
                     conflict=str(legacy) if legacy.is_file() else None)
    rec = _record()
    if legacy.is_file():
        payload = _read_plist(legacy)
        env = {str(k): str(v) for k, v in (payload.get("EnvironmentVariables") or {}).items()}
        args = payload.get("ProgramArguments") or [None]
        return Agent("launchctl", label=payload.get("Label", paths.LAUNCHD_LABEL), env=env,
                     program=args[0], sysaudio=env.get("MEETING_CAPTURE_SYSAUDIO"), plist=str(legacy),
                     conflict=f"Contorch.app at {rec['app']}" if rec else None)
    if rec:
        return Agent("app", label=rec.get("label") or paths.LAUNCHD_LABEL, app=rec["app"],
                     program=str(Path(rec["app"]) / BUNDLE_PROGRAM), sysaudio=rec.get("sysaudio"),
                     remote=True)
    return Agent("none")


def pinned_sysaudio() -> Path | None:
    """The sysaudio the installed recorder runs (its TCC identity), if it
    exists: the app's record first (agent.json), then a legacy plist's pin."""
    rec = _record()
    for s in (rec.get("sysaudio"), _legacy_pin()):
        if s and Path(s).is_file():
            return Path(s)
    return None


def _legacy_pin() -> str | None:
    legacy = Path(paths.LAUNCHD_PLIST)
    if not legacy.is_file():
        return None
    env = _read_plist(legacy).get("EnvironmentVariables") or {}
    return env.get("MEETING_CAPTURE_SYSAUDIO")


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
            "program": field_("program"), "parent_bundle_version": field_("parent bundle version"),
            "codesigning": "OS_REASON_CODESIGNING" in out or "Launch Constraint Violation" in out}


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


def guard(installing_python: str | None = None, adopt: bool = False, label: str | None = None,
          registering: bool = False) -> dict | None:
    """None when this install may change the recorder agent, else a refusal.
    The channel rule is pipeline-monitor's (the marker); the rest are facts
    about the job that need no marker. `adopt` (pipeline-monitor's adopt or
    rollback, run with its operation token) skips the agent_elsewhere fact —
    taking another install's agent over is the point — but never the guard."""
    from . import channel_guard
    ok, msg = channel_guard.allowed()
    if not ok:
        return _refusal("channel_conflict", msg or "another install manages Contorch on this Mac")
    j = job(label)
    if channel_guard.channel() != "app":
        if j and j.get("managed_by") == SM_MANAGED:
            return _refusal("channel_conflict",
                            "Contorch.app runs the recorder on this Mac (an SMAppService job holds "
                            f"{label or paths.LAUNCHD_LABEL}); use the app, or `contorch adopt` to switch back.")
    elif registering and not adopt and j and j.get("managed_by") != SM_MANAGED and Path(paths.LAUNCHD_PLIST).is_file():
        # SMAppService reports success while a legacy job holds the label
        # (PROVEN in the lab): the app must take the agent over through adopt.
        return _refusal("agent_elsewhere",
                        f"A Homebrew or source install's recorder agent ({paths.LAUNCHD_PLIST}) runs on "
                        "this Mac. Switch with `contorch adopt` (it backs up and moves it aside).")
    if adopt:
        return None
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


def _remote(action: str, agent: Agent, **extra) -> dict:
    return {**_result(action, agent, **extra),
            **_refusal("channel_conflict", f"Contorch.app ({agent.app}) runs the recorder on this Mac; "
                                           "change it from the app (or `contorch adopt` to switch back)")}


def _app_record(agent: Agent, registered: bool) -> dict:
    from . import __version__
    return {"app": agent.app, "sysaudio": agent.sysaudio, "version": bundle_version(agent.app),
            "meeting_capture": __version__, "registered": registered}


def _register(agent: Agent) -> dict:
    """registrar.register + the post-condition launchd sees (the API alone
    says `enabled` for a job that is dead)."""
    from . import registrar
    try:
        res = registrar.register(agent.label, _app_record(agent, True))
    except registrar.Unavailable as exc:
        return _refusal("not_in_bundle", str(exc))
    except Exception as exc:
        return _refusal("register_failed", f"{type(exc).__name__}: {exc}")
    if not res["ok"]:
        if res["status_after"] == "requires_approval":
            return {"ok": True, "performed": True, "approval_required": True}
        return _refusal("register_failed", (res.get("error") or {}).get("message") or res["status_after"])
    return {"ok": True, "performed": True, "approval_required": res["status_after"] == "requires_approval"}


def _unregister(agent: Agent) -> dict:
    from . import registrar
    try:
        res = registrar.unregister(agent.label)
    except registrar.Unavailable as exc:
        return _refusal("not_in_bundle", str(exc))
    except Exception as exc:
        return _refusal("unregister_failed", f"{type(exc).__name__}: {exc}")
    if not res["ok"] and res["status_after"] not in ("not_registered", "not_found"):
        return _refusal("unregister_failed", (res.get("error") or {}).get("message") or res["status_after"])
    return {"ok": True, "performed": res["status_before"] not in ("not_registered", "not_found")}


def restart(agent: Agent | None = None) -> dict:
    """Restart the daemon so it reads its settings again. Never starts a
    recorder that is stopped."""
    agent = agent or current()
    if not agent.installed:
        return _result("restart", agent, why="no recorder agent is installed")
    refused = guard(label=agent.label)
    if refused:
        return {**_result("restart", agent), **refused}
    if not _loaded(agent.label):
        return _result("restart", agent, loaded=False,
                       why="the recorder is stopped; it uses the new settings when it starts")
    if agent.backend == "app":
        # The bundled plist is sealed and carries no settings: nothing to
        # re-read, and an SMAppService job can't be bootstrapped by path.
        res = _launchctl("kickstart", "-k", agent.target)
        if res.returncode != 0:
            return {**_result("restart", agent), **_refusal("launchctl_failed", _why(res))}
        return _result("restart", agent, performed=True)
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
    if agent.remote:
        return _remote("start", agent)
    refused = guard(label=agent.label, registering=agent.backend == "app")
    if refused:
        return {**_result("start", agent), **refused}
    if agent.backend == "app":
        return {**_result("start", agent), **_register(agent), "loaded": _loaded(agent.label)}
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
    if agent.remote:
        return _remote("stop", agent, reason=reason)
    refused = guard(label=agent.label)
    if refused:
        return {**_result("stop", agent, reason=reason), **refused}
    if agent.backend == "app":
        # Unregistering stops the job and keeps it from starting at login;
        # start() registers it again.
        return {**_result("stop", agent, reason=reason), **_unregister(agent), "loaded": False}
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


def bundled_plist_payload(bundle_id: str, program: str = BUNDLE_PROGRAM,
                          label: str | None = None) -> bytes:
    """The agent plist Contorch.app carries in Contents/Library/LaunchAgents,
    generated at build time (`meeting-capture plist --bundled`; contorch-macos
    build-app.sh passes the bundle id). No settings, no paths outside the
    bundle: no StandardOutPath (the daemon opens its own log; `~` isn't
    expanded there), no WorkingDirectory, no PATH."""
    name = Path(program).name
    args = [name] if name != "contorch-python" else [name, "-m", "meeting_capture.daemon"]
    payload = {
        "Label": label or paths.LAUNCHD_LABEL,
        "BundleProgram": program,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ProcessType": "Background",
        "ExitTimeOut": EXIT_TIMEOUT_S,
        "AssociatedBundleIdentifiers": [bundle_id],
        "EnvironmentVariables": {"CONTORCH_CHANNEL": "app"},
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


def _move_legacy_aside(backup_dir: str | None) -> str | None:
    """Adopt: unload the legacy agent and move its plist out of
    ~/Library/LaunchAgents (into the backup dir when given), so launchd never
    loads it again and nothing reads its sysaudio pin."""
    import shutil
    import time as _time
    legacy = Path(paths.LAUNCHD_PLIST)
    if not legacy.is_file():
        return None
    label = _read_plist(legacy).get("Label") or paths.LAUNCHD_LABEL
    j = job(label)
    if j and j.get("managed_by") != SM_MANAGED:
        _launchctl("bootout", _target(label))
    _launchctl("enable", _target(label))     # no disabled override left for the label
    if backup_dir:
        dest = Path(backup_dir) / legacy.name
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        dest = Path(paths.ENV_FILE).parent / f"{legacy.name}.adopted-{int(_time.time())}"
    shutil.move(str(legacy), str(dest))
    return str(dest)


def install(python_exe: str, sysaudio: str | None, *, adopt: bool = False, load: bool = True,
            backup_dir: str | None = None) -> dict:
    """Install the recorder agent for this channel. Legacy: write and load
    the plist (Homebrew / source checkouts). App: register the bundled agent.
    `adopt` (pipeline-monitor's adopt, with its operation token): settings
    move into the env file, the other channel's agent is taken over — the
    legacy plist backed up into `backup_dir` and moved aside — and with
    `load=False` nothing is started yet (`meeting-capture start` does)."""
    from . import channel_guard, config
    agent = current()
    if agent.remote and not adopt:
        return _remote("install", agent)
    refused = guard(installing_python=python_exe, adopt=adopt,
                    label=agent.label if agent.backend == "app" else None,
                    registering=agent.backend == "app" and not agent.remote)
    if refused:
        return {**_result("install", agent), **refused}
    moved = config.migrate_from_plist()
    if agent.backend == "app" and not agent.remote:
        moved_aside = _move_legacy_aside(backup_dir) if adopt else None
        if not load:
            from . import registrar
            registrar.write_record({"backend": "app", "label": agent.label, **_app_record(agent, False)})
            return _result("install", agent, performed=True, moved=moved, moved_aside=moved_aside,
                           sysaudio=agent.sysaudio, loaded=False)
        reg = _register(agent)
        return {**_result("install", agent, moved=moved, moved_aside=moved_aside, sysaudio=agent.sysaudio),
                **reg, "loaded": _loaded(agent.label)}
    if adopt:
        from . import registrar
        if agent.remote:
            registrar.remove_record()      # rollback: the app's record goes with its agent
        if backup_dir and Path(paths.LAUNCHD_PLIST).is_file():
            import shutil
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            shutil.copy2(paths.LAUNCHD_PLIST, Path(backup_dir) / Path(paths.LAUNCHD_PLIST).name)
    plist = Path(paths.LAUNCHD_PLIST)
    _write_atomic(plist, plist_payload(python_exe, sysaudio, channel_guard.channel()))
    agent = current()
    if not load:
        return _result("install", agent, performed=True, moved=moved, plist=agent.plist,
                       sysaudio=agent.sysaudio, loaded=False)
    _launchctl("enable", agent.target)
    _launchctl("bootout", agent.target)     # launchd keeps a loaded job's old plist otherwise
    res = _bootstrap(agent.plist)
    if res.returncode != 0:
        return {**_result("install", agent, moved=moved), **_refusal("launchctl_failed", _why(res))}
    return _result("install", agent, performed=True, moved=moved, plist=agent.plist,
                   sysaudio=agent.sysaudio)


def uninstall(adopt: bool = False) -> dict:
    """Remove this channel's recorder agent. Legacy: unload and delete the
    plist, ending with `launchctl enable` so no disabled override is left
    behind (a later bootstrap would fail with EIO). App: unregister (only
    from inside the bundle) and drop agent.json. Settings and data stay."""
    agent = current()
    if not agent.installed:
        return _result("uninstall", agent, why="no recorder agent is installed")
    if agent.remote:
        return _remote("uninstall", agent)
    refused = guard(adopt=adopt, label=agent.label)
    if refused:
        return {**_result("uninstall", agent), **refused}
    if agent.backend == "app":
        return {**_result("uninstall", agent), **_unregister(agent), "loaded": False}
    _launchctl("bootout", agent.target)
    Path(agent.plist).unlink(missing_ok=True)
    _launchctl("enable", agent.target)
    return _result("uninstall", agent, performed=True, loaded=False)


def heal_reasons(agent: Agent | None = None) -> list[str]:
    """Why the app's registration is stale (empty: it isn't). After an app
    move or reinstall the job may point at the old bundle, fail to spawn, or
    hit a launch constraint (an ad-hoc update does, PROVEN)."""
    agent = agent or current()
    if agent.backend != "app" or agent.remote:
        return []
    why = []
    rec = _record()
    if rec and os.path.normpath(rec["app"]) != os.path.normpath(agent.app):
        why.append("moved")
    j = job(agent.label)
    if j:
        if (j.get("state") or "").startswith("spawn failed"):
            why.append("spawn_failed")
        if j.get("codesigning"):
            why.append("codesigning")
        if str(j.get("last_exit") or "").split(" ")[0] in ("78", "EX_CONFIG"):
            why.append("exit_78")
        version = bundle_version(agent.app)
        if j.get("parent_bundle_version") and version and j["parent_bundle_version"] != version:
            why.append("version")
        if j.get("program") and agent.app and not j["program"].startswith(agent.app):
            why.append("moved")
    elif rec.get("version") and rec["version"] != bundle_version(agent.app):
        why.append("version")
    return sorted(set(why))


def heal() -> dict:
    """Re-register the app's agent when its registration is stale
    (unregister + register); nothing otherwise. Run by the app at launch, only
    while `status --json` says nothing is being recorded."""
    agent = current()
    if agent.backend != "app" or agent.remote:
        return _result("heal", agent, why="nothing to heal (not the app's own agent)")
    why = heal_reasons(agent)
    if not why:
        return _result("heal", agent, why="the registration is current", reasons=[])
    refused = guard(label=agent.label, registering=True)
    if refused:
        return {**_result("heal", agent), **refused}
    un = _unregister(agent)
    if not un["ok"]:
        return {**_result("heal", agent, reasons=why), **un}
    return {**_result("heal", agent, reasons=why), **_register(agent), "loaded": _loaded(agent.label)}


def status() -> dict:
    agent = current()
    j = job(agent.label) if agent.installed else None
    out = {"backend": agent.backend, "label": agent.label, "installed": agent.installed,
           "program": agent.program, "sysaudio": agent.sysaudio, "plist": agent.plist,
           "app": agent.app, "conflict": agent.conflict,
           "loaded": j is not None, "pid": (j or {}).get("pid"), "state": (j or {}).get("state"),
           "last_exit": (j or {}).get("last_exit"), "managed_by": (j or {}).get("managed_by")}
    if agent.backend == "app" and not agent.remote:
        from . import registrar
        try:
            out["registration"] = registrar.status(agent.label)
        except Exception as exc:
            out["registration"] = f"unknown ({type(exc).__name__})"
    return out
