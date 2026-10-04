"""The supervisor seam: the only code that starts, stops, restarts, installs
or removes the recorder agent (legacy launchd plist here), behind the channel
guard. launchctl is faked; nothing reaches the real launchd."""
import json
import plistlib
import subprocess

import pytest

from meeting_capture import cli, config, paths, supervisor

SM_PRINT = "gui/501/com.contorch.meeting-capture = {\n\tmanaged_by = com.apple.xpc.ServiceManagement\n\tstate = running\n\tpid = 77\n}\n"


class FakeLaunchctl:
    """Records launchctl calls; `loaded` decides what `list` and `print` answer."""
    def __init__(self, loaded=True, fail=(), print_out=None):
        self.calls, self.loaded, self.fail, self.print_out = [], loaded, set(fail), print_out

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "list":
            return subprocess.CompletedProcess(args, 0 if self.loaded else 113, '{\n\t"PID" = 42;\n};\n', "")
        if args[0] == "print":
            if self.print_out is not None:
                return subprocess.CompletedProcess(args, 0, self.print_out, "")
            if not self.loaded:
                return subprocess.CompletedProcess(args, 113, "", "Could not find service")
            return subprocess.CompletedProcess(args, 0, "{\n\tstate = running\n\tpid = 42\n"
                                                        "\tlast exit code = 0\n}\n", "")
        rc = 5 if args[0] in self.fail else 0
        return subprocess.CompletedProcess(args, rc, "", "err" if rc else "")

    def verbs(self):
        return [c[0] if c[0] != "kickstart" else " ".join(c[:2]) for c in self.calls
                if c[0] not in ("list", "print")]


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    plist.write_bytes(supervisor.plist_payload("/venv/python3.12", "/opt/sysaudio"))
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    fake = FakeLaunchctl()
    monkeypatch.setattr(supervisor, "_launchctl", fake)
    return plist, fake


def _marker(writers, owner="app", op=None, message="Contorch.app manages this Mac."):
    m = paths.HOME / ".contorch" / "channel.json"
    m.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema": "contorch.channel/1", "owner": owner, "state": "ok", "writers": writers,
           "blocked_message": message}
    if op:
        doc["op"] = op
    m.write_text(json.dumps(doc))
    return m


def test_nothing_installed():
    a = supervisor.current()
    assert a.backend == "none" and not a.installed and a.env == {}


def test_legacy_agent(legacy):
    plist, _ = legacy
    a = supervisor.current()
    assert (a.backend, a.program, a.sysaudio, a.plist) == ("launchctl", "/venv/python3.12", "/opt/sysaudio",
                                                           str(plist))


def test_restart_of_a_legacy_agent_reloads_the_plist(legacy):
    _, fake = legacy
    res = supervisor.restart()
    assert res["ok"] and res["performed"] and res["schema"] == "meeting-capture.agent/1"
    assert fake.verbs() == ["bootout", "bootstrap"]          # not kickstart -k: it keeps the old env


def test_restart_never_starts_a_stopped_recorder(legacy):
    _, fake = legacy
    fake.loaded = False
    res = supervisor.restart()
    assert res["ok"] and not res["performed"] and "stopped" in res["why"]
    assert fake.verbs() == []


def test_bootstrap_is_retried_while_the_old_process_exits(legacy, monkeypatch):
    _, fake = legacy
    answers = iter([5, 5, 0])
    real = fake.__call__

    def flaky(*args):
        if args[0] == "bootstrap":
            fake.calls.append(args)
            return subprocess.CompletedProcess(args, next(answers), "", "5: Input/output error")
        return real(*args)
    monkeypatch.setattr(supervisor, "_launchctl", flaky)
    monkeypatch.setattr(supervisor.time, "sleep", lambda s: None)
    assert supervisor.restart()["performed"]
    assert fake.verbs().count("bootstrap") == 3


def test_stop_and_start_persist_across_login(legacy):
    _, fake = legacy
    assert supervisor.stop(reason="quit")["reason"] == "quit"
    fake.loaded = False
    supervisor.start()
    assert fake.verbs() == ["disable", "bootout", "enable", "bootstrap"]


def test_start_of_a_loaded_job_kicks_it_without_killing(legacy):
    _, fake = legacy
    assert supervisor.start()["ok"]
    assert [c[0] for c in fake.calls if c[0] not in ("list", "print")] == ["enable", "kickstart"]
    assert all("-k" not in c for c in fake.calls)


def test_uninstall_leaves_no_disabled_override(legacy):
    plist, fake = legacy
    res = supervisor.uninstall()
    assert res["performed"] and not plist.exists()
    assert fake.verbs() == ["bootout", "enable"]


def test_plist_payload_is_settings_free_with_an_exit_timeout():
    p = plistlib.loads(supervisor.plist_payload("/x/python", "/x/sysaudio", "dev"))
    assert p["ExitTimeOut"] == supervisor.EXIT_TIMEOUT_S > 10      # > the daemon's STOP_GRACE_S
    assert p["KeepAlive"] == {"SuccessfulExit": False, "Crashed": True}
    assert set(p["EnvironmentVariables"]) == {"PATH", "MEETING_CAPTURE_SYSAUDIO", "CONTORCH_CHANNEL"}
    assert p["EnvironmentVariables"]["CONTORCH_CHANNEL"] == "dev"
    assert supervisor.EXIT_TIMEOUT_S > __import__("meeting_capture.daemon", fromlist=["x"]).STOP_GRACE_S


@pytest.mark.parametrize("channel,expect", [("brew", "brew"), ("dev", "dev"), ("app", "dev"), (None, "dev")])
def test_install_records_the_channel_in_the_legacy_plist(legacy, monkeypatch, channel, expect):
    if channel:
        monkeypatch.setenv("CONTORCH_CHANNEL", channel)
    paths.LAUNCHD_PLIST.unlink()
    supervisor.install("/venv/python3.12", "/opt/sysaudio")
    env = plistlib.loads(paths.LAUNCHD_PLIST.read_bytes())["EnvironmentVariables"]
    assert env["CONTORCH_CHANNEL"] == expect


# ---- the channel guard and the job facts --------------------------------------------

@pytest.mark.parametrize("verb", ["start", "stop", "restart", "uninstall"])
def test_every_verb_refuses_when_another_channel_owns_the_mac(legacy, monkeypatch, capfd, verb):
    _, fake = legacy
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    _marker(["app"])
    assert cli.main([verb, "--json"]) == 3
    doc = json.loads(capfd.readouterr().out)
    assert doc["ok"] is False and doc["error"] == {"code": "channel_conflict",
                                                   "message": "Contorch.app manages this Mac."}
    assert fake.verbs() == []


def test_install_refuses_under_the_marker_and_writes_nothing(tmp_path, monkeypatch, capsys):
    fake = FakeLaunchctl(loaded=False)
    monkeypatch.setattr(supervisor, "_launchctl", fake)
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    _marker(["app"])
    assert cli.main(["install"]) == 3
    assert capsys.readouterr().err.strip() == "Contorch.app manages this Mac."
    assert not paths.LAUNCHD_PLIST.exists() and fake.verbs() == []


def test_a_writer_with_the_operations_token_may_act(legacy, monkeypatch):
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    _marker(["app", "brew"], op={"id": "op-1", "kind": "adopt", "by": "app"})
    assert supervisor.restart()["error"]["code"] == "channel_conflict"
    monkeypatch.setenv("CONTORCH_OP", "op-1")
    assert supervisor.restart()["performed"]


def test_an_smappservice_job_belongs_to_the_app_even_without_a_marker(legacy, monkeypatch):
    _, fake = legacy
    fake.print_out = SM_PRINT
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    res = supervisor.stop()
    assert res["error"]["code"] == "channel_conflict" and "Contorch.app" in res["error"]["message"]
    assert fake.verbs() == []


def test_install_over_another_installs_existing_interpreter_is_agent_elsewhere(tmp_path, monkeypatch):
    other = tmp_path / "brew-venv" / "python3.12"
    other.parent.mkdir()
    other.write_text("")
    plist = tmp_path / "agent.plist"
    plist.write_bytes(supervisor.plist_payload(str(other), "/opt/sysaudio"))
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    fake = FakeLaunchctl()
    monkeypatch.setattr(supervisor, "_launchctl", fake)
    res = supervisor.install(str(tmp_path / "dev-venv" / "python3.12"), "/opt/sysaudio")
    assert res["error"]["code"] == "agent_elsewhere" and str(other) in res["error"]["message"]
    assert fake.verbs() == []
    # the same interpreter (a reinstall), or one that no longer exists (a rebuilt venv): allowed
    assert supervisor.install(str(other), "/opt/sysaudio")["ok"]
    other.unlink()
    assert supervisor.install(str(tmp_path / "dev-venv" / "python3.12"), "/opt/sysaudio")["ok"]


# ---- CLI ------------------------------------------------------------------------------

def test_cli_verbs_json_documents(legacy, capfd):
    for verb in ("restart", "stop", "start"):
        assert cli.main([verb, "--json"]) == 0
        out = capfd.readouterr().out
        assert len(out.splitlines()) == 1
        doc = json.loads(out)
        assert doc["schema"] == "meeting-capture.agent/1" and doc["action"] == verb
        assert set(doc) >= {"ok", "action", "backend", "label", "program", "loaded", "performed"}
        assert doc["backend"] == "launchctl"


def test_cli_stop_reason_is_echoed(legacy, capfd):
    assert cli.main(["stop", "--json", "--reason", "update"]) == 0
    assert json.loads(capfd.readouterr().out)["reason"] == "update"


def test_cli_start_json_without_an_agent(capfd):
    assert cli.main(["start", "--json"]) == 1
    assert json.loads(capfd.readouterr().out)["error"]["code"] == "not_installed"


def test_cli_restart_text(legacy, capsys):
    assert cli.main(["restart"]) == 0
    assert "recorder restarted" in capsys.readouterr().out


def test_a_setting_change_restarts_but_never_resumes_a_stopped_recorder(legacy, key_file, capsys):
    _, fake = legacy
    fake.loaded = False                       # "Stop everything" earlier
    assert cli.main(["mode", "live"]) == 0
    assert "stopped and uses it when it starts" in capsys.readouterr().out
    assert fake.verbs() == [] and config.read()["MEETING_CAPTURE_MODE"] == "live"


def test_cli_config_json(legacy, capfd):
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=live\n")
    assert cli.main(["config", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["schema"] == "meeting-capture.config/1" and doc["ok"] is True
    assert doc["agent"]["backend"] == "launchctl"
    assert doc["settings"]["mode"] == {"value": "live", "source": "file"}
    assert doc["settings"]["locale"] == {"value": None, "source": "default"}
    assert doc["watch_paths"] == [str(paths.ENV_FILE), str(paths.LAUNCHD_PLIST)]
    assert doc["overridden"] == []


def test_config_json_flags_settings_an_old_plist_still_overrides(tmp_path, monkeypatch, capfd):
    plist = tmp_path / "agent.plist"
    plist.write_bytes(plistlib.dumps({"Label": paths.LAUNCHD_LABEL, "ProgramArguments": ["/p"],
                                      "EnvironmentVariables": {"MEETING_CAPTURE_MODE": "live"}}))
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=batch\n")
    assert cli.main(["config", "--json"]) == 0
    doc = json.loads(capfd.readouterr().out)
    assert doc["settings"]["mode"] == {"value": "live", "source": "agent"}
    assert doc["overridden"] == ["mode"]


def test_config_set_and_unset_an_advanced_setting(legacy, capsys):
    assert cli.main(["config", "set", "mic", "0"]) == 0
    assert config.read()["MEETING_CAPTURE_MIC"] == "0"
    assert "recorder restarted" in capsys.readouterr().out
    assert cli.main(["config", "unset", "MEETING_CAPTURE_MIC"]) == 0
    assert "MEETING_CAPTURE_MIC" not in config.read()


def test_config_set_refuses_settings_that_have_a_checked_command(capsys):
    assert cli.main(["config", "set", "SOURCE", "linein"]) == 2
    assert "meeting-capture source" in capsys.readouterr().err
    assert cli.main(["config", "set", "nonsense", "1"]) == 2
    assert cli.main(["config", "set", "own_responsible", "1"]) == 2
    assert not paths.ENV_FILE.exists()


def test_config_text_shows_sources(monkeypatch, capsys):
    paths.ENV_FILE.write_text("MEETING_CAPTURE_STT=apple\n")
    monkeypatch.setenv("MEETING_CAPTURE_STT", "gemini")
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "stt" in out and "apple  (file)" in out and "your shell says gemini" in out


def test_status_and_doctor_name_the_settings_file(legacy, monkeypatch, capsys, tmp_path):
    from test_cli import _quiet_system
    _quiet_system(monkeypatch, tmp_path)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "recorder agent:   launchctl" in out and f"settings:         {paths.ENV_FILE}" in out
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "recorder agent installed (launchctl)" in out and f"settings: {paths.ENV_FILE}" in out


def test_the_old_plist_config_store_stays_deleted():
    """Settings moved to ~/.meeting-capture/env; the agent is driven only by
    supervisor.py (one restart path, never `kickstart -k` on a legacy plist)."""
    import re
    from pathlib import Path
    src = Path(cli.__file__).parent
    cli_text = (src / "cli.py").read_text(encoding="utf-8")
    for gone in ("_preserved_env", "_plist_env", "_plist_mode", "_set_plist_mode", "_update_plist_env",
                 "_relaunch", "_resolved_sysaudio_env", "_plist_payload", "plist_sysaudio"):
        assert not re.search(rf"\b{gone}\b", cli_text), gone
    for py in src.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        if py.name != "supervisor.py":
            assert '"launchctl"' not in text, f"{py.name} runs launchctl itself"
        assert '"kickstart", "-k"' not in text, py.name
