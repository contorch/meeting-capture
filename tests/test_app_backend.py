"""Contorch.app's bundled SMAppService agent: registrar (PyObjC stubbed),
the supervisor's app backend (stubbed at registrar level), adopt, heal,
`plist --bundled`, `where`, `skill`, and the blocker-4 contract (the app's
sysaudio, never Homebrew's, once the app has adopted). Nothing here registers
anything with the real SMAppService or launchd."""
import json
import plistlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

from meeting_capture import cli, config, paths, recorder, registrar, skill, supervisor

SM_PRINT = ("gui/501/com.contorch.meeting-capture = {{\n\tmanaged_by = com.apple.xpc.ServiceManagement\n"
            "\tstate = running\n\tpid = 77\n\tprogram = {app}/Contents/MacOS/Contorch Recorder\n"
            "\tparent bundle version = {version}\n}}\n")


class FakeLaunchctl:
    def __init__(self, loaded=True, print_out=None):
        self.calls, self.loaded, self.print_out = [], loaded, print_out

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "list":
            return subprocess.CompletedProcess(args, 0 if self.loaded else 113, "", "")
        if args[0] == "print":
            if self.print_out is None or not self.loaded:
                return subprocess.CompletedProcess(args, 113, "", "Could not find service")
            return subprocess.CompletedProcess(args, 0, self.print_out, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def verbs(self):
        return [" ".join(c[:2]) if c[0] == "kickstart" else c[0] for c in self.calls
                if c[0] not in ("list", "print")]


class FakeRegistrar:
    """Stands in for registrar._call (the only PyObjC touch point)."""
    def __init__(self):
        self.calls, self.status_ = [], "not_registered"
        self.fail = None

    def __call__(self, label, verb):
        self.calls.append((verb, label))
        before = self.status_
        if self.fail:
            return {"ok": False, "error": {"domain": "x", "code": 1, "message": self.fail},
                    "status_before": before, "status_after": before}
        self.status_ = "enabled" if verb == "register" else "not_registered"
        return {"ok": True, "error": None, "status_before": before, "status_after": self.status_}


def _bundle(tmp_path, bundle_id="com.contorch.app", version="42", label=None):
    app = tmp_path / "Applications" / "Contorch.app"
    c = app / "Contents"
    (c / "MacOS").mkdir(parents=True)
    (c / "Helpers").mkdir()
    (c / "Helpers" / "sysaudio").write_text("")
    (c / "MacOS" / "contorch-python").write_text("")
    (c / "Library" / "LaunchAgents").mkdir(parents=True)
    (c / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id, "CFBundleVersion": version}))
    name = f"{label or paths.LAUNCHD_LABEL}.plist"
    (c / "Library" / "LaunchAgents" / name).write_bytes(
        supervisor.bundled_plist_payload(bundle_id, label=label))
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    """This process runs as Contorch.app's contorch-python."""
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(sys, "executable", str(bundle / "Contents" / "MacOS" / "contorch-python"))
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    fake = FakeLaunchctl(loaded=False)
    monkeypatch.setattr(supervisor, "_launchctl", fake)
    reg = FakeRegistrar()
    monkeypatch.setattr(registrar, "_call", reg)
    monkeypatch.setattr(paths, "AGENT_RECORD", tmp_path / "state" / "agent.json")
    return types.SimpleNamespace(bundle=bundle, launchctl=fake, reg=reg)


def _brew_plist(tmp_path, monkeypatch, settings=None):
    brew_sysaudio = tmp_path / "opt" / "meeting-capture" / "bin" / "sysaudio"
    brew_sysaudio.parent.mkdir(parents=True)
    brew_sysaudio.write_text("")
    plist = tmp_path / "LaunchAgents" / "com.contorch.meeting-capture.plist"
    plist.parent.mkdir()
    payload = plistlib.loads(supervisor.plist_payload("/brew/venv/python3.12", str(brew_sysaudio), "brew"))
    payload["EnvironmentVariables"].update(settings or {})
    plist.write_bytes(plistlib.dumps(payload))
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    return plist, brew_sysaudio


# ---- plist --bundled ---------------------------------------------------------------

def test_plist_bundled_exact_keys(capsys):
    assert cli.main(["plist", "--bundled", "--program", "Contents/MacOS/Contorch Recorder",
                     "--bundle-id", "com.contorch.labtest.app"]) == 0
    out = capsys.readouterr().out
    p = plistlib.loads(out.encode())
    assert set(p) == {"Label", "BundleProgram", "ProgramArguments", "RunAtLoad", "KeepAlive", "ProcessType",
                      "ExitTimeOut", "AssociatedBundleIdentifiers", "EnvironmentVariables"}
    assert p["BundleProgram"] == "Contents/MacOS/Contorch Recorder"
    assert p["ProgramArguments"] == ["Contorch Recorder"]
    assert p["AssociatedBundleIdentifiers"] == ["com.contorch.labtest.app"]
    assert p["EnvironmentVariables"] == {"CONTORCH_CHANNEL": "app"}
    assert p["KeepAlive"] == {"SuccessfulExit": False, "Crashed": True}
    assert p["ExitTimeOut"] == 15 and p["ProcessType"] == "Background" and p["RunAtLoad"] is True
    assert "com.contorch.app" not in out and "~" not in out
    assert "StandardOutPath" not in p and "WorkingDirectory" not in p
    assert subprocess.run(["plutil", "-lint", "-"], input=out.encode(), capture_output=True).returncode == 0


def test_plist_bundled_fallback_program_and_lab_label(capsys):
    assert cli.main(["plist", "--bundled", "--program", "Contents/MacOS/contorch-python", "--bundle-id",
                     "com.contorch.labtest.app", "--label", "com.contorch.labtest.meeting-capture"]) == 0
    p = plistlib.loads(capsys.readouterr().out.encode())
    assert p["ProgramArguments"] == ["contorch-python", "-m", "meeting_capture.daemon"]
    assert p["Label"] == "com.contorch.labtest.meeting-capture"


def test_plist_without_bundled_is_a_usage_error(capsys):
    assert cli.main(["plist"]) == 2


def test_no_main_executable_call():
    """Registration is registrar's, in-process: nothing runs the app's main
    executable (in Phase 1 that would start a second menu bar)."""
    src = Path(cli.__file__).parent
    for py in src.rglob("*.py"):
        assert 'Contents/MacOS/Contorch"' not in py.read_text(encoding="utf-8"), py


# ---- registrar -----------------------------------------------------------------------

def test_registrar_maps_the_api_and_writes_the_record(tmp_path, monkeypatch):
    calls = []

    class Svc:
        def __init__(self):
            self.s = 0

        def status(self):
            return self.s

        def registerAndReturnError_(self, _):
            calls.append("register"); self.s = 1
            return True, None

        def unregisterAndReturnError_(self, _):
            calls.append("unregister"); self.s = 0
            return True, None
    svc = Svc()
    sm = types.SimpleNamespace(SMAppService=types.SimpleNamespace(
        agentServiceWithPlistName_=lambda name: calls.append(name) or svc))
    monkeypatch.setitem(sys.modules, "ServiceManagement", sm)
    monkeypatch.setattr(paths, "AGENT_RECORD", tmp_path / "agent.json")
    assert registrar.available()
    out = registrar.register("com.contorch.meeting-capture", {"app": "/A.app", "sysaudio": "/A.app/s"})
    assert out == {"ok": True, "error": None, "status_before": "not_registered", "status_after": "enabled"}
    rec = json.loads((tmp_path / "agent.json").read_text())
    assert rec == {"backend": "app", "label": "com.contorch.meeting-capture", "registered": True,
                   "app": "/A.app", "sysaudio": "/A.app/s"}
    assert registrar.status() == "enabled"
    assert registrar.unregister("com.contorch.meeting-capture")["status_after"] == "not_registered"
    assert not (tmp_path / "agent.json").exists()
    assert calls[0] == "com.contorch.meeting-capture.plist" and "register" in calls and "unregister" in calls


def test_registrar_without_pyobjc_says_so(monkeypatch):
    monkeypatch.setitem(sys.modules, "ServiceManagement", None)
    assert registrar.available() is False
    with pytest.raises(registrar.Unavailable):
        registrar.status()


# ---- the app backend ---------------------------------------------------------------------

def test_the_app_backend_needs_the_channel_and_the_bundled_plist(app, monkeypatch):
    a = supervisor.current()
    assert (a.backend, a.app, a.remote) == ("app", str(app.bundle), False)
    assert a.sysaudio == str(app.bundle / "Contents" / "Helpers" / "sysaudio")
    assert a.program == str(app.bundle / "Contents" / "MacOS" / "Contorch Recorder")
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")            # a brew CLI that happens to live in a bundle
    assert supervisor.current().backend == "none"
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    (app.bundle / "Contents" / "Library" / "LaunchAgents" / "com.contorch.meeting-capture.plist").unlink()
    assert supervisor.current().backend == "none"              # no bundled plist: no app backend


def test_start_registers_and_stop_unregisters(app, capfd):
    assert cli.main(["start", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["backend"] == "app" and doc["performed"] and doc["ok"]
    assert app.reg.calls == [("register", "com.contorch.meeting-capture")]
    rec = json.loads(paths.AGENT_RECORD.read_text())
    assert rec["app"] == str(app.bundle) and rec["version"] == "42" and rec["registered"] is True
    assert cli.main(["stop", "--json", "--reason", "quit"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["reason"] == "quit" and app.reg.calls[-1][0] == "unregister"
    assert not paths.AGENT_RECORD.exists()
    assert app.launchctl.verbs() == []                         # no launchctl verbs for the app's agent


def test_restart_kicks_the_sealed_agent_and_never_starts_a_stopped_one(app):
    assert not supervisor.restart()["performed"]
    app.launchctl.loaded = True
    assert supervisor.restart()["performed"]
    assert app.launchctl.verbs() == ["kickstart -k"]
    assert app.reg.calls == []


def test_a_failed_registration_is_reported(app, capfd):
    app.reg.fail = "Operation not permitted"
    assert cli.main(["start", "--json"]) == 1
    doc = json.loads(capfd.readouterr().out)
    assert doc["error"]["code"] == "register_failed" and "not permitted" in doc["error"]["message"]


def test_without_pyobjc_the_app_backend_reports_not_in_bundle(app, monkeypatch, capfd):
    def unavailable(*a):
        raise registrar.Unavailable("no pyobjc")
    monkeypatch.setattr(registrar, "_call", unavailable)
    assert cli.main(["start", "--json"]) == 1
    assert json.loads(capfd.readouterr().out)["error"]["code"] == "not_in_bundle"


def test_the_app_wont_register_over_a_running_legacy_agent_without_adopt(app, tmp_path, monkeypatch):
    _brew_plist(tmp_path, monkeypatch)
    app.launchctl.loaded = True
    app.launchctl.print_out = "{\n\tstate = running\n\tpid = 5\n}\n"   # a legacy job holds the label
    res = supervisor.start()
    assert res["error"]["code"] == "agent_elsewhere" and app.reg.calls == []


def test_a_brew_cli_sees_the_app_owns_the_recorder(app, tmp_path, monkeypatch, capfd):
    supervisor.start()
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    monkeypatch.setattr(sys, "executable", "/opt/homebrew/venv/bin/python3.12")
    a = supervisor.current()
    assert a.backend == "app" and a.remote and a.app == str(app.bundle)
    for verb in ("start", "stop", "uninstall", "install"):
        assert cli.main([verb, "--json"]) == 3, verb
        assert json.loads(capfd.readouterr().out)["error"]["code"] == "channel_conflict"


# ---- adopt: brew -> app -------------------------------------------------------------------

def test_install_adopt_no_load_takes_the_brew_agent_over(app, tmp_path, monkeypatch, capfd):
    plist, brew_sysaudio = _brew_plist(tmp_path, monkeypatch, {"MEETING_CAPTURE_SOURCE": "linein"})
    app.launchctl.loaded = True
    app.launchctl.print_out = "{\n\tstate = running\n\tpid = 5\n}\n"
    backup = tmp_path / "backups" / "2026"
    assert cli.main(["install", "--adopt", "--no-load", "--backup-dir", str(backup), "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["ok"] and doc["backend"] == "app" and doc["loaded"] is False
    assert not plist.exists() and (backup / plist.name).is_file()        # moved aside, kept
    assert config.read()["MEETING_CAPTURE_SOURCE"] == "linein"             # settings came along
    assert app.reg.calls == []                                             # --no-load: not registered yet
    rec = json.loads(paths.AGENT_RECORD.read_text())
    assert rec["registered"] is False and rec["sysaudio"] == str(app.bundle / "Contents/Helpers/sysaudio")
    assert app.launchctl.verbs() == ["bootout", "enable"]                  # the legacy job, then no override
    assert cli.main(["start", "--json"]) == 0                              # later: register
    assert app.reg.calls == [("register", "com.contorch.meeting-capture")]


def test_blocker4_after_adopt_every_entry_point_uses_the_apps_sysaudio(app, tmp_path, monkeypatch):
    """Review blocker 4: with a brew plist present and no MEETING_CAPTURE_SYSAUDIO
    in the environment, adopting as the app makes find_sysaudio() (and so
    `check --json`'s identity) the bundle's helper, whose TCC subject is the app."""
    plist, brew_sysaudio = _brew_plist(tmp_path, monkeypatch)
    monkeypatch.delenv(recorder.SYSAUDIO_ENV_VAR, raising=False)
    assert recorder.find_sysaudio() == brew_sysaudio                       # before: the brew pin
    assert supervisor.install(sys.executable, None, adopt=True, load=False)["ok"]
    bundle_helper = app.bundle / "Contents" / "Helpers" / "sysaudio"
    assert recorder.find_sysaudio() == bundle_helper
    # a Homebrew CLI on the same Mac (its wrapper exports its own copy) agrees
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    monkeypatch.setenv(recorder.SYSAUDIO_ENV_VAR, str(brew_sysaudio))
    monkeypatch.setattr(sys, "executable", "/opt/homebrew/venv/bin/python3.12")
    assert recorder.find_sysaudio() == bundle_helper
    try:
        from meeting_capture import tccspawn       # the check PR
    except ImportError:
        return
    assert tccspawn.tcc_subject(bundle_helper) == "com.contorch.app"


def test_blocker4_check_json_identity(app, tmp_path, monkeypatch, capfd):
    """The same contract through `check --json` (needs the check command)."""
    pytest.importorskip("meeting_capture.permissions")
    _brew_plist(tmp_path, monkeypatch)
    monkeypatch.delenv(recorder.SYSAUDIO_ENV_VAR, raising=False)
    helper = app.bundle / "Contents" / "Helpers" / "sysaudio"
    helper.write_text("#!/bin/sh\necho '{\"schema\":\"sysaudio.check/1\",\"screen_capture\":\"granted\","
                      "\"microphone\":\"granted\",\"os\":\"26.0\",\"arch\":\"arm64\",\"requested\":null}'\n")
    helper.chmod(0o755)
    assert supervisor.install(sys.executable, None, adopt=True, load=False)["ok"]
    assert cli.main(["check", "--json"]) == 0
    ident = json.loads(capfd.readouterr().out)["identity"]
    assert ident == {"helper": str(helper), "subject": "com.contorch.app"}


def test_adopt_never_passes_the_channel_guard(app, tmp_path, monkeypatch):
    _brew_plist(tmp_path, monkeypatch)
    marker = paths.HOME / ".contorch" / "channel.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"schema": "contorch.channel/1", "owner": "brew", "state": "adopting",
                                  "writers": ["app", "brew"], "op": {"id": "op-9", "kind": "adopt", "by": "app"},
                                  "blocked_message": "adopt in progress"}))
    assert supervisor.install(sys.executable, None, adopt=True, load=False)["error"]["code"] == "channel_conflict"
    monkeypatch.setenv("CONTORCH_OP", "op-9")
    assert supervisor.install(sys.executable, None, adopt=True, load=False)["ok"]


def test_uninstall_adopt_unregisters_from_inside_the_bundle(app, capfd):
    supervisor.start()
    assert cli.main(["uninstall", "--adopt", "--json"]) == 0
    assert app.reg.calls[-1][0] == "unregister" and not paths.AGENT_RECORD.exists()


def test_rollback_brew_takes_the_agent_back_after_the_app_unregistered(app, tmp_path, monkeypatch):
    supervisor.start()
    supervisor.uninstall(adopt=True)                          # the app's side of rollback
    supervisor.start()                                        # (re-registered, to leave a record)
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    monkeypatch.setattr(sys, "executable", "/brew/venv/python3.12")
    res = supervisor.install("/brew/venv/python3.12", "/opt/sysaudio", adopt=True, load=False)
    assert res["ok"] and res["backend"] == "launchctl" and not paths.AGENT_RECORD.exists()


# ---- heal ------------------------------------------------------------------------------------

def test_heal_does_nothing_when_the_registration_is_current(app, capfd):
    supervisor.start()
    app.launchctl.loaded = True
    app.launchctl.print_out = SM_PRINT.format(app=app.bundle, version="42")
    assert cli.main(["heal", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["performed"] is False and doc["reasons"] == []


@pytest.mark.parametrize("change,reason", [("moved", "moved"), ("version", "version"),
                                           ("spawn", "spawn_failed"), ("codesign", "codesigning")])
def test_heal_reregisters_a_stale_agent(app, tmp_path, monkeypatch, capfd, change, reason):
    supervisor.start()
    app.launchctl.loaded = True
    out = SM_PRINT.format(app=app.bundle, version="42")
    if change == "moved":
        rec = json.loads(paths.AGENT_RECORD.read_text())
        rec["app"] = "/Users/me/Downloads/Contorch.app"
        paths.AGENT_RECORD.write_text(json.dumps(rec))
    elif change == "version":
        out = SM_PRINT.format(app=app.bundle, version="41")
    elif change == "spawn":
        out = out.replace("state = running", "state = spawn failed")
    else:
        out += "\tlast exit reason = OS_REASON_CODESIGNING\n"
    app.launchctl.print_out = out
    assert cli.main(["heal", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert reason in doc["reasons"] and doc["performed"]
    assert [c[0] for c in app.reg.calls] == ["register", "unregister", "register"]


def test_heal_outside_the_app_is_a_no_op(capfd):
    assert cli.main(["heal", "--json"]) == 0
    assert json.loads(capfd.readouterr().out)["performed"] is False


# ---- where, skill ---------------------------------------------------------------------------

def test_where_json_schema(app, capfd):
    assert cli.main(["where", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["schema"] == "meeting-capture.where/1" and doc["ok"] and doc["channel"] == "app"
    assert doc["bundle"] == str(app.bundle)
    assert doc["sysaudio"] == str(app.bundle / "Contents" / "Helpers" / "sysaudio")
    assert doc["bin"]["meeting_capture"] == str(app.bundle / "Contents" / "Resources" / "bin" / "meeting-capture")
    assert doc["agent"]["backend"] == "app"
    assert Path(doc["skills"]["meeting"]["source"]).joinpath("SKILL.md").is_file()


def test_where_text(capsys):
    assert cli.main(["where"]) == 0
    assert "settings:" in capsys.readouterr().out


def test_skill_install_links_and_uninstall_removes(capfd):
    assert cli.main(["skill", "install", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    t = skill.target()
    assert doc == {"schema": "meeting-capture.skill/1", "ok": True, "path": str(t), "action": "linked"}
    assert (t / "SKILL.md").is_file() and (t / "bin" / "feed").is_file()
    assert cli.main(["skill", "install", "--json"]) == 0
    assert json.loads(capfd.readouterr().out)["action"] == "none"
    assert cli.main(["skill", "status", "--json"]) == 0
    assert json.loads(capfd.readouterr().out)["matches"] is True
    assert cli.main(["skill", "uninstall", "--json"]) == 0
    assert json.loads(capfd.readouterr().out)["action"] == "removed" and not t.exists()


def test_skill_keeps_a_users_own_copy(capfd):
    t = skill.target()
    t.mkdir(parents=True)
    (t / "SKILL.md").write_text("mine")
    for verb, expect in (("install", "kept_user_copy"), ("uninstall", "kept_user_copy")):
        assert cli.main(["skill", verb, "--json"]) == 0
        assert json.loads(capfd.readouterr().out)["action"] == expect
    assert (t / "SKILL.md").read_text() == "mine"


def test_skill_replaces_our_link_from_a_moved_install(tmp_path):
    t = skill.target()
    t.parent.mkdir(parents=True)
    t.symlink_to(tmp_path / "Old.app" / "site-packages" / "meeting_capture" / "skills" / "meeting")
    assert skill.install()["action"] == "linked" and Path(t.readlink()) == skill.source()


def test_the_skill_is_package_data():
    src = skill.source()
    assert (src / "SKILL.md").is_file()
    assert (src / "bin" / "feed").stat().st_mode & 0o111
