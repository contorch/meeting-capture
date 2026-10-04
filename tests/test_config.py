"""~/.meeting-capture/env: format, precedence, writers, plist migration."""
import os
import plistlib

import pytest

from meeting_capture import cli, config, paths


def _plist(path, env, label="com.contorch.meeting-capture"):
    path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/venv/python3.12", "-m",
                                     "meeting_capture.daemon"], "EnvironmentVariables": env}))
    return path


def _plist_env(path):
    return plistlib.loads(path.read_bytes())["EnvironmentVariables"]


# ---- format -----------------------------------------------------------------------

def test_parse_reads_the_context_orchestrator_format():
    text = ("# comment\n\nexport MEETING_CAPTURE_MODE=live\nMEETING_CAPTURE_INPUT_DEVICE=UMC404HD 192k\n"
            "MEETING_CAPTURE_LOCALE='hi-IN'\nCO_EMBEDDING_MODEL=local\nnot a line\n"
            "MEETING_CAPTURE_STT=apple\nMEETING_CAPTURE_STT=gemini\n")
    assert config.parse(text) == {"MEETING_CAPTURE_MODE": "live",
                                  "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k",
                                  "MEETING_CAPTURE_LOCALE": "hi-IN",
                                  "MEETING_CAPTURE_STT": "gemini"}       # last one wins; CO_* ignored


def test_update_keeps_comments_and_foreign_lines_and_rewrites_in_place():
    paths.ENV_FILE.write_text("# mine\nMEETING_CAPTURE_MODE=live\nOTHER=1\nMEETING_CAPTURE_MODE=batch\n")
    config.update({"MEETING_CAPTURE_MODE": "batch", "MEETING_CAPTURE_STT": "apple"})
    assert paths.ENV_FILE.read_text() == ("# mine\nMEETING_CAPTURE_MODE=batch\nOTHER=1\n"
                                          "MEETING_CAPTURE_STT=apple\n")
    config.update(remove=("MEETING_CAPTURE_MODE",))
    assert "MEETING_CAPTURE_MODE" not in paths.ENV_FILE.read_text()


def test_update_round_trips_awkward_values():
    vals = {"MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k", "MEETING_CAPTURE_GEMINI_MODEL": "a#b=c",
            "MEETING_CAPTURE_COPILOT_MODEL": "  padded  "}
    assert config.update(vals) == vals


def test_update_refuses_locators_and_multiline_values():
    with pytest.raises(ValueError, match="not a setting"):
        config.update({"MEETING_CAPTURE_SYSAUDIO": "/opt/homebrew/opt/meeting-capture/bin/sysaudio"})
    with pytest.raises(ValueError, match="one line"):
        config.update({"MEETING_CAPTURE_MODE": "live\nMEETING_CAPTURE_SOURCE=linein"})
    with pytest.raises(ValueError):
        config.update({"GOOGLE_API_KEY": "x"})
    assert not paths.ENV_FILE.exists()


def test_update_keeps_the_files_permissions(tmp_path):
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=live\n")
    paths.ENV_FILE.chmod(0o600)
    config.update({"MEETING_CAPTURE_STT": "apple"})
    assert paths.ENV_FILE.stat().st_mode & 0o777 == 0o600


# ---- precedence: process env > file > default ----------------------------------------------

def test_apply_loads_the_file_under_the_process_environment(monkeypatch):
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=live\nMEETING_CAPTURE_STT=apple\n")
    monkeypatch.setenv("MEETING_CAPTURE_STT", "gemini")          # e.g. `MEETING_CAPTURE_STT=gemini meeting-capture run`
    loaded = config.apply()
    assert loaded == {"MEETING_CAPTURE_MODE": "live"}
    assert os.environ["MEETING_CAPTURE_MODE"] == "live"
    assert os.environ["MEETING_CAPTURE_STT"] == "gemini"


def test_the_existing_readers_see_the_file(monkeypatch):
    """No reader knows about the file: apply() is enough."""
    from meeting_capture import linein, live, recorder, transcriber, watchdog
    monkeypatch.delenv(transcriber.ENV_TRANSCRIBE_BIN, raising=False)
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=live\nMEETING_CAPTURE_SOURCE=linein\n"
                              "MEETING_CAPTURE_STT=apple\nMEETING_CAPTURE_MIC=0\n"
                              "MEETING_CAPTURE_MAX_FOOTPRINT_MB=999\nMEETING_CAPTURE_DIARIZE=1\n")
    config.apply()
    assert live.live_mode_enabled() and linein.linein_mode_enabled()
    assert transcriber.stt_choice() == "apple" and transcriber.diarization_enabled()
    assert recorder.mic_capture_enabled() is False
    assert int(os.environ[watchdog.ENV_MAX_FOOTPRINT_MB]) == 999


def test_daemon_env_puts_the_agents_environment_over_the_file(tmp_path, monkeypatch):
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=batch\nMEETING_CAPTURE_STT=apple\n")
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", _plist(tmp_path / "a.plist", {"MEETING_CAPTURE_MODE": "live"}))
    monkeypatch.setenv("MEETING_CAPTURE_STT", "gemini")          # this shell: the recorder never sees it
    env = config.daemon_env()
    assert env["MEETING_CAPTURE_MODE"] == "live" and env["MEETING_CAPTURE_STT"] == "apple"
    src = config.sources()
    assert src["MEETING_CAPTURE_MODE"]["source"] == "agent"
    assert src["MEETING_CAPTURE_STT"] == {"value": "apple", "source": "file", "shell": "gemini"}
    assert src["MEETING_CAPTURE_LOCALE"] == {"value": None, "source": "default"}


def test_daemon_env_without_an_agent_is_this_process(monkeypatch):
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=live\n")
    monkeypatch.setenv("MEETING_CAPTURE_SOURCE", "linein")
    env = config.daemon_env()
    assert env["MEETING_CAPTURE_MODE"] == "live" and env["MEETING_CAPTURE_SOURCE"] == "linein"


def test_the_brew_wrappers_sysaudio_export_never_reaches_the_file(tmp_path, monkeypatch, capsys):
    """The wrapper exports MEETING_CAPTURE_SYSAUDIO on every call: it is a
    locator, so no CLI path writes it into the settings."""
    brew = tmp_path / "opt" / "sysaudio"; brew.parent.mkdir(); brew.write_text("")
    monkeypatch.setenv("MEETING_CAPTURE_SYSAUDIO", str(brew))
    assert cli.main(["mode", "batch"]) == 0
    assert cli.main(["config", "set", "MIC", "0"]) == 0
    assert "SYSAUDIO" not in paths.ENV_FILE.read_text()
    assert cli.main(["config", "set", "SYSAUDIO", "/x"]) == 2


# ---- migration out of the legacy plist ---------------------------------------------

def test_migration_moves_settings_and_leaves_locators(tmp_path, monkeypatch):
    plist = _plist(tmp_path / "a.plist", {"PATH": "/usr/bin", "MEETING_CAPTURE_SYSAUDIO": "/opt/sysaudio",
                                          "MEETING_CAPTURE_MODE": "live", "MEETING_CAPTURE_SOURCE": "linein",
                                          "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k",
                                          "MEETING_CAPTURE_MENUBAR_BIN": "/stale"})
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=batch\nMEETING_CAPTURE_STT=apple\n")
    moved = config.migrate_from_plist()
    assert moved == ["MEETING_CAPTURE_INPUT_DEVICE", "MEETING_CAPTURE_MODE", "MEETING_CAPTURE_SOURCE"]
    assert _plist_env(plist) == {"PATH": "/usr/bin", "MEETING_CAPTURE_SYSAUDIO": "/opt/sysaudio"}
    assert config.read() == {"MEETING_CAPTURE_MODE": "live",          # the plist's value was in effect
                             "MEETING_CAPTURE_STT": "apple",
                             "MEETING_CAPTURE_SOURCE": "linein",
                             "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k"}
    assert (paths.ENV_FILE.parent / "a.plist.before-env-file").is_file()
    assert config.migrate_from_plist() == []                         # idempotent


def test_a_crash_between_file_and_plist_is_finished_next_time(tmp_path, monkeypatch):
    plist = _plist(tmp_path / "a.plist", {"MEETING_CAPTURE_MODE": "live"})
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=live\n")       # step 1 done, step 2 not
    assert config.daemon_env()["MEETING_CAPTURE_MODE"] == "live"     # same value either way
    assert config.migrate_from_plist() == ["MEETING_CAPTURE_MODE"]
    assert "MEETING_CAPTURE_MODE" not in _plist_env(plist)


def test_every_writer_migrates_first_or_the_plist_would_override_it(tmp_path, monkeypatch):
    plist = _plist(tmp_path / "a.plist", {"MEETING_CAPTURE_MODE": "live"})
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    config.update(remove=("MEETING_CAPTURE_MODE",))
    assert "MEETING_CAPTURE_MODE" not in _plist_env(plist)
    assert "MEETING_CAPTURE_MODE" not in config.daemon_env()


def test_the_daemon_migrates_and_loads_before_reading_anything(tmp_path, monkeypatch):
    from meeting_capture import daemon, live
    plist = _plist(tmp_path / "a.plist", {"MEETING_CAPTURE_MODE": "live", "PATH": "/usr/bin"})
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    paths.ENV_FILE.write_text("MEETING_CAPTURE_STT=apple\n")
    assert daemon.load_settings() == ["MEETING_CAPTURE_MODE"]
    assert live.live_mode_enabled() and os.environ["MEETING_CAPTURE_STT"] == "apple"


def test_install_keeps_settings_and_writes_none_into_the_plist(tmp_path, monkeypatch, capsys):
    from meeting_capture import supervisor
    plist = _plist(tmp_path / "a.plist", {"PATH": "/usr/bin", "MEETING_CAPTURE_MODE": "live",
                                          "MEETING_CAPTURE_SOURCE": "linein"})
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    sysaudio = tmp_path / "sysaudio"; sysaudio.write_text("")
    monkeypatch.setenv("MEETING_CAPTURE_SYSAUDIO", str(sysaudio))
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    calls = []
    monkeypatch.setattr(supervisor, "_launchctl", lambda *a: calls.append(a) or
                        __import__("subprocess").CompletedProcess(a, 113 if a[0] in ("list", "print") else 0, "", ""))
    assert cli.main(["install"]) == 0
    payload = plistlib.loads(plist.read_bytes())
    env = payload["EnvironmentVariables"]
    assert set(env) == {"PATH", "MEETING_CAPTURE_SYSAUDIO", "CONTORCH_CHANNEL"}
    assert env["MEETING_CAPTURE_SYSAUDIO"] == str(sysaudio) and env["CONTORCH_CHANNEL"] == "brew"
    assert payload["ExitTimeOut"] == supervisor.EXIT_TIMEOUT_S
    assert config.read() == {"MEETING_CAPTURE_MODE": "live", "MEETING_CAPTURE_SOURCE": "linein"}
    assert [c[0] for c in calls if c[0] not in ("list", "print")] == ["enable", "bootout", "bootstrap"]
    out = capsys.readouterr().out
    assert "settings moved from the plist" in out and "mode, source" in out


def test_install_notes_shell_settings_it_does_not_keep(tmp_path, monkeypatch, capsys):
    from meeting_capture import supervisor
    monkeypatch.setattr(supervisor, "_launchctl", lambda *a: __import__("subprocess").CompletedProcess(
        a, 113 if a[0] in ("list", "print") else 0, "", ""))
    monkeypatch.setenv("MEETING_CAPTURE_MODE", "live")
    assert cli.main(["install"]) == 0
    assert "this shell sets MEETING_CAPTURE_MODE" in capsys.readouterr().out
    assert "MEETING_CAPTURE_MODE" not in config.read()
    assert "MEETING_CAPTURE_MODE" not in plistlib.loads(paths.LAUNCHD_PLIST.read_bytes())["EnvironmentVariables"]


# ---- concurrency, staleness ----------------------------------------------------------

def _writer(env_file, lock, i, n):
    from meeting_capture import config as c, paths as p
    p.ENV_FILE, p.ENV_LOCK = env_file, lock
    for j in range(n):
        c.update({f"MEETING_CAPTURE_T{i}": str(j)}, path=env_file)


def test_concurrent_writers_lose_nothing(tmp_path):
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    ps = [ctx.Process(target=_writer, args=(paths.ENV_FILE, paths.ENV_LOCK, i, 10)) for i in range(6)]
    [p.start() for p in ps]
    [p.join() for p in ps]
    assert config.read() == {f"MEETING_CAPTURE_T{i}": "9" for i in range(6)}


def test_restart_pending_after_a_hand_edit(tmp_path, monkeypatch):
    pid = tmp_path / "daemon.pid"
    monkeypatch.setattr(paths, "PID_FILE", pid)
    paths.ENV_FILE.write_text("MEETING_CAPTURE_MODE=live\n")
    pid.write_text("123")
    os.utime(paths.ENV_FILE, (1, 1))
    assert config.restart_pending() is False
    os.utime(paths.ENV_FILE, None)
    os.utime(pid, (1, 1))
    assert config.restart_pending() is True


def test_the_daemon_logs_what_it_moved_and_what_its_environment_overrides(tmp_path, monkeypatch, caplog):
    from meeting_capture import daemon

    class Started(Exception):
        pass

    plist = _plist(tmp_path / "a.plist", {"MEETING_CAPTURE_MODE": "live", "PATH": "/usr/bin"})
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", plist)
    paths.ENV_FILE.write_text("MEETING_CAPTURE_STT=apple\n")
    monkeypatch.setenv("MEETING_CAPTURE_STT", "gemini")          # e.g. a launchctl setenv
    monkeypatch.setattr(daemon, "ensure_dirs", lambda: None)
    monkeypatch.setattr(daemon, "_setup_logging", lambda: None)
    monkeypatch.setattr(daemon, "_another_daemon_running", lambda: (_ for _ in ()).throw(Started()))
    with caplog.at_level("INFO", logger="meeting-capture"), pytest.raises(Started):
        daemon.run()
    text = caplog.text
    assert "settings moved from the agent plist" in text and "MEETING_CAPTURE_MODE" in text
    assert "overridden by this process's environment: MEETING_CAPTURE_STT" in text


def test_internal_and_locator_keys_are_never_settings(monkeypatch):
    paths.ENV_FILE.write_text("MEETING_CAPTURE_OWN_RESPONSIBLE=1\nMEETING_CAPTURE_SYSAUDIO=/x\n"
                              "MEETING_CAPTURE_MIC=0\n")
    assert config.read() == {"MEETING_CAPTURE_MIC": "0"}
    with pytest.raises(ValueError):
        config.update({"MEETING_CAPTURE_OWN_RESPONSIBLE": "1"})
