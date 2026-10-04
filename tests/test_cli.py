import time
from pathlib import Path

import os

import pytest

from meeting_capture import cli


def test_format_age_seconds():
    assert cli._format_age(5) == "5s ago"


def test_format_age_minutes():
    assert cli._format_age(125) == "2m ago"


def test_format_age_hours():
    assert cli._format_age(7200) == "2h ago"


def test_format_age_days():
    assert cli._format_age(86400 * 3) == "3d ago"


def test_last_transcript_returns_none_when_db_empty():
    assert cli._last_transcript() is None


def test_last_transcript_returns_most_recent():
    from meeting_capture import store
    store.append("meeting-2026-01-01T00-00-00", "[00:00:01] old\n\n")
    time.sleep(0.01)
    store.append("meeting-2026-04-26T14-00-00", "[14:00:01] new\n\n")
    last = cli._last_transcript()
    assert last["meeting_id"] == "meeting-2026-04-26T14-00-00"
    assert "new" in last["body"]


def test_last_chunk_log_line_returns_none_when_no_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOG_FILE", tmp_path / "missing.log")
    assert cli._last_chunk_log_line() is None


def test_last_chunk_log_line_finds_chunk_line(tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text(
        "2026-04-26 14:00:00 INFO meeting-capture daemon starting\n"
        "2026-04-26 14:01:00 INFO chunk 30.0s -> meeting-x.md (1234 chars)\n"
        "2026-04-26 14:01:30 INFO mic inactive — session ended\n"
    )
    monkeypatch.setattr(cli, "LOG_FILE", log)
    line = cli._last_chunk_log_line()
    assert line is not None
    assert "chunk 30.0s" in line
    assert "1234 chars" in line


def test_last_chunk_log_line_skips_non_chunk_lines(tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text("INFO some other line\nINFO another line\n")
    monkeypatch.setattr(cli, "LOG_FILE", log)
    assert cli._last_chunk_log_line() is None


# --- mode: live/batch persisted in the launchd plist -------------------------------

def _write_plist(path: Path, env: dict) -> None:
    import plistlib
    path.write_bytes(plistlib.dumps({"Label": "x", "EnvironmentVariables": env}))


def _read_env(path: Path) -> dict:
    import plistlib
    return dict(plistlib.loads(path.read_bytes())["EnvironmentVariables"])


def test_plist_mode_defaults_to_batch_without_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "missing.plist")
    assert cli._plist_mode() == "batch"


def test_plist_mode_reads_live_from_plist(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    _write_plist(plist, {"PATH": "/usr/bin", "MEETING_CAPTURE_MODE": "live"})
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    assert cli._plist_mode() == "live"


def test_plist_mode_ignores_garbage_value(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    _write_plist(plist, {"MEETING_CAPTURE_MODE": "turbo"})
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    assert cli._plist_mode() == "batch"


def test_set_plist_mode_live_keeps_other_env(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    _write_plist(plist, {"PATH": "/usr/bin", "MEETING_CAPTURE_SYSAUDIO": "/x/sysaudio"})
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    cli._set_plist_mode("live")
    env = _read_env(plist)
    assert env["MEETING_CAPTURE_MODE"] == "live"
    assert env["MEETING_CAPTURE_SYSAUDIO"] == "/x/sysaudio"  # the TCC-granted path must survive
    assert env["PATH"] == "/usr/bin"


def test_set_plist_mode_batch_removes_key(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    _write_plist(plist, {"MEETING_CAPTURE_MODE": "live"})
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    cli._set_plist_mode("batch")
    assert "MEETING_CAPTURE_MODE" not in _read_env(plist)


def test_cmd_mode_switch_relaunches(tmp_path, monkeypatch, capsys, key_file):
    plist = tmp_path / "agent.plist"
    _write_plist(plist, {})
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    calls = []
    monkeypatch.setattr(cli, "_relaunch", lambda: calls.append("relaunch"))
    assert cli.main(["mode", "live"]) == 0
    assert calls == ["relaunch"]
    assert _read_env(plist)["MEETING_CAPTURE_MODE"] == "live"
    assert "switched to live" in capsys.readouterr().out


def test_cmd_mode_noop_when_already_set(tmp_path, monkeypatch, capsys, key_file):
    plist = tmp_path / "agent.plist"
    _write_plist(plist, {"MEETING_CAPTURE_MODE": "live"})
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    monkeypatch.setattr(cli, "_relaunch", lambda: pytest.fail("must not restart the daemon"))
    assert cli.main(["mode", "live"]) == 0
    assert "already in live" in capsys.readouterr().out


def test_cmd_mode_prints_current(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "missing.plist")
    assert cli.main(["mode"]) == 0
    assert capsys.readouterr().out.strip() == "batch"


def test_cmd_mode_requires_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "missing.plist")
    assert cli.main(["mode", "live"]) == 1
    assert "install" in capsys.readouterr().err


# --- install pins the sysaudio path into the plist ----------------------------------

def test_resolved_sysaudio_env_keeps_explicit_existing_path(tmp_path):
    binary = tmp_path / "sysaudio"
    binary.write_text("")
    env = cli._resolved_sysaudio_env({"MEETING_CAPTURE_SYSAUDIO": str(binary), "PATH": "/usr/bin"})
    assert env["MEETING_CAPTURE_SYSAUDIO"] == os.path.abspath(binary)
    assert env["PATH"] == "/usr/bin"


def test_resolved_sysaudio_env_replaces_dangling_path(tmp_path, monkeypatch):
    from meeting_capture import recorder
    found = tmp_path / "found" / "sysaudio"
    found.parent.mkdir()
    found.write_text("")
    monkeypatch.setattr(recorder, "find_sysaudio", lambda: found)
    env = cli._resolved_sysaudio_env({"MEETING_CAPTURE_SYSAUDIO": str(tmp_path / "gone")})
    assert env["MEETING_CAPTURE_SYSAUDIO"] == os.path.abspath(found)


def test_resolved_sysaudio_env_drops_key_when_nothing_found(tmp_path, monkeypatch):
    from meeting_capture import recorder
    monkeypatch.setattr(recorder, "find_sysaudio", lambda: None)
    env = cli._resolved_sysaudio_env({"MEETING_CAPTURE_SYSAUDIO": str(tmp_path / "gone")})
    assert "MEETING_CAPTURE_SYSAUDIO" not in env


def test_plist_payload_pins_sysaudio(tmp_path, monkeypatch):
    import plistlib
    from meeting_capture import recorder
    binary = tmp_path / "sysaudio"
    binary.write_text("")
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "no-agent.plist")
    monkeypatch.delenv("MEETING_CAPTURE_SYSAUDIO", raising=False)
    monkeypatch.setattr(recorder, "find_sysaudio", lambda: binary)
    payload = plistlib.loads(cli._plist_payload("/usr/bin/python3"))
    assert payload["EnvironmentVariables"]["MEETING_CAPTURE_SYSAUDIO"] == os.path.abspath(binary)


def test_resolved_sysaudio_env_keeps_symlink_path(tmp_path):
    """A brew-style opt/ symlink must be pinned as given, not followed into
    Cellar/<version>/ — that path dangles on the next upgrade."""
    real = tmp_path / "Cellar" / "0.3.0" / "bin"; real.mkdir(parents=True)
    (real / "sysaudio").write_bytes(b"")
    (tmp_path / "opt").symlink_to(tmp_path / "Cellar" / "0.3.0")
    given = tmp_path / "opt" / "bin" / "sysaudio"
    env = cli._resolved_sysaudio_env({"MEETING_CAPTURE_SYSAUDIO": str(given)})
    assert env["MEETING_CAPTURE_SYSAUDIO"] == str(given)
    assert "Cellar" not in env["MEETING_CAPTURE_SYSAUDIO"]


# ---- `meeting-capture source` (line-in) — against a temp plist, fake audio devices

class _FakeSD:
    DEVICES = [
        {"name": "MacBook Pro Microphone", "max_input_channels": 1},
        {"name": "UMC202HD 192k", "max_input_channels": 2},
    ]

    def query_devices(self, dev=None, kind=None):
        if dev is None and kind is None:
            return self.DEVICES
        if dev is None:
            return self.DEVICES[0]           # "system default input"
        return self.DEVICES[dev]


@pytest.fixture
def linein_env(tmp_path, monkeypatch):
    from meeting_capture import linein
    monkeypatch.setattr(linein, "_import_sounddevice", lambda: _FakeSD())
    plist = tmp_path / "agent.plist"
    _write_plist(plist, {"PATH": "/usr/bin", "MEETING_CAPTURE_SYSAUDIO": "/x/sysaudio"})
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    calls = []
    monkeypatch.setattr(cli, "_relaunch", lambda: calls.append("relaunch"))
    return plist, calls


def test_source_linein_writes_env_and_relaunches(linein_env, capsys):
    plist, calls = linein_env
    assert cli.main(["source", "linein", "--device", "umc202"]) == 0
    env = _read_env(plist)
    assert env["MEETING_CAPTURE_SOURCE"] == "linein"
    assert env["MEETING_CAPTURE_INPUT_DEVICE"] == "umc202"
    assert (env["MEETING_CAPTURE_ME_CHANNEL"], env["MEETING_CAPTURE_THEM_CHANNEL"]) == ("0", "1")
    assert env["MEETING_CAPTURE_SYSAUDIO"] == "/x/sysaudio"  # unrelated keys untouched
    assert calls == ["relaunch"]
    assert "UMC202HD 192k" in capsys.readouterr().out


def test_source_linein_rejects_bad_device_without_touching_plist(linein_env, capsys):
    plist, calls = linein_env
    before = plist.read_bytes()
    assert cli.main(["source", "linein", "--device", "focusrite"]) == 1
    assert plist.read_bytes() == before and calls == []
    assert "can't use that input" in capsys.readouterr().err


def test_source_linein_rejects_one_channel_device(linein_env, capsys):
    plist, calls = linein_env
    assert cli.main(["source", "linein", "--device", "MacBook Pro Microphone"]) == 1
    assert calls == []
    assert "needs 2" in capsys.readouterr().err


def test_source_linein_rejects_same_channel_for_both(linein_env, capsys):
    _, calls = linein_env
    assert cli.main(["source", "linein", "--device", "umc202", "--me", "1", "--them", "1"]) == 1
    assert calls == []
    assert "transcribed twice" in capsys.readouterr().err


def test_source_sck_removes_linein_keys_only(linein_env):
    plist, calls = linein_env
    cli.main(["source", "linein", "--device", "umc202"])
    assert cli.main(["source", "sck"]) == 0
    env = _read_env(plist)
    assert not any(k.startswith(("MEETING_CAPTURE_SOURCE", "MEETING_CAPTURE_INPUT", "MEETING_CAPTURE_ME_", "MEETING_CAPTURE_THEM_")) for k in env)
    assert env["MEETING_CAPTURE_SYSAUDIO"] == "/x/sysaudio"
    assert calls == ["relaunch", "relaunch"]


def test_source_shows_current_mapping(linein_env, capsys):
    cli.main(["source", "linein", "--device", "umc202", "--me", "1", "--them", "0"])
    capsys.readouterr()
    assert cli.main(["source"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("linein") and "umc202" in out and "me = channel 1, them = channel 0" in out


# ---- `meeting-capture stt` / `language` (plist env, like mode/source) --------------

@pytest.fixture
def agent(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    _write_plist(plist, {"PATH": "/usr/bin", "MEETING_CAPTURE_SYSAUDIO": "/x/sysaudio",
                         "MEETING_CAPTURE_TRANSCRIBER": "gemini"})
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    calls = []
    monkeypatch.setattr(cli, "_relaunch", lambda: calls.append("relaunch"))
    return plist, calls


def test_stt_shows_engine_reason_and_language(fake_helper, agent, capsys):
    assert cli.main(["stt"]) == 0
    out = capsys.readouterr().out
    assert "On this Mac — nothing is uploaded" in out
    assert "on-device model for en-US is installed" in out
    assert "setting:   auto" in out and "language:  en-US" in out
    assert "API key not set (optional)" in out
    assert "{" not in out                        # human text, not JSON


def test_stt_shows_why_nothing_runs(agent, capsys):
    assert cli.main(["stt"]) == 0                # no helper, no key
    out = capsys.readouterr().out
    assert out.startswith("engine:    none") and "no Gemini API key" in out


def test_stt_apple_writes_the_plist_and_restarts(fake_helper, agent, capsys):
    plist, calls = agent
    assert cli.main(["stt", "apple"]) == 0
    env = _read_env(plist)
    assert env["MEETING_CAPTURE_STT"] == "apple"
    assert "MEETING_CAPTURE_TRANSCRIBER" not in env              # legacy key dropped
    assert env["MEETING_CAPTURE_SYSAUDIO"] == "/x/sysaudio"      # TCC-pinned path untouched
    assert calls == ["relaunch"]
    assert "nothing is uploaded" in capsys.readouterr().out


def test_stt_apple_installs_a_missing_model_first(fake_helper, agent, capsys):
    plist, calls = agent
    fake_helper.configure(installed=[])
    assert cli.main(["stt", "apple"]) == 0
    assert ["transcribe", "--install", "--locale", "en-US"] in fake_helper.calls()
    assert _read_env(plist)["MEETING_CAPTURE_STT"] == "apple" and calls == ["relaunch"]
    assert "Downloading the on-device speech model for en-US" in capsys.readouterr().out


def test_stt_apple_refused_where_it_cannot_run(fake_helper, agent, capsys):
    plist, calls = agent
    fake_helper.configure(probe_rc=69, reason="needs macOS 26 or later on Apple silicon", supported=[])
    before = plist.read_bytes()
    assert cli.main(["stt", "apple"]) == 1
    assert plist.read_bytes() == before and calls == []
    assert "needs macOS 26" in capsys.readouterr().err


def test_stt_gemini_warns_without_a_key_and_auto_is_written_too(fake_helper, agent, capsys):
    plist, calls = agent
    assert cli.main(["stt", "gemini"]) == 0
    out = capsys.readouterr().out
    assert _read_env(plist)["MEETING_CAPTURE_STT"] == "gemini"
    assert "no Google API key the recorder can see" in out
    assert not any("--install" in c for c in fake_helper.calls())
    assert cli.main(["stt", "auto"]) == 0
    # Written, not removed: unset means nobody picked an engine (upgrade_notice).
    assert _read_env(plist)["MEETING_CAPTURE_STT"] == "auto"
    assert calls == ["relaunch", "relaunch"]


def test_stt_set_requires_install(fake_helper, capsys):
    assert cli.main(["stt", "apple"]) == 1
    assert "install" in capsys.readouterr().err


def test_language_installs_then_switches(fake_helper, agent, capsys):
    plist, calls = agent
    assert cli.main(["language", "hi_in"]) == 0
    assert ["transcribe", "--install", "--locale", "hi-IN"] in fake_helper.calls()
    assert _read_env(plist)["MEETING_CAPTURE_LOCALE"] == "hi-IN"
    assert calls == ["relaunch"]
    out = capsys.readouterr().out
    assert "romanized" in out and "hi-IN" in out


def test_language_bare_code_and_back_to_english(fake_helper, agent):
    plist, _ = agent
    assert cli.main(["language", "hi"]) == 0
    assert _read_env(plist)["MEETING_CAPTURE_LOCALE"] == "hi-IN"
    # A chosen en-US is kept: unset would follow the Mac's language instead.
    assert cli.main(["language", "en-US"]) == 0
    assert _read_env(plist)["MEETING_CAPTURE_LOCALE"] == "en-US"


def test_language_bad_input_lists_the_supported_ones(fake_helper, agent, capsys):
    plist, calls = agent
    before = plist.read_bytes()
    assert cli.main(["language", "klingon"]) == 1
    err = capsys.readouterr().err
    assert "unsupported language 'klingon'" in err and "hi-IN" in err and "en-GB" in err
    assert plist.read_bytes() == before and calls == []
    assert not any("--install" in c for c in fake_helper.calls())


def test_language_where_on_device_is_unavailable(fake_helper, agent, capsys):
    fake_helper.configure(old=True)
    assert cli.main(["language", "hi-IN"]) == 1
    assert "predates" in capsys.readouterr().err


def test_language_install_failure_leaves_the_plist_alone(fake_helper, agent, capsys):
    plist, calls = agent
    fake_helper.configure(install_rc=1)
    before = plist.read_bytes()
    assert cli.main(["language", "hi-IN"]) == 1
    assert plist.read_bytes() == before and calls == []


def test_language_show(fake_helper, agent, capsys):
    assert cli.main(["language"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("language:  en-US (installed on this Mac)")
    assert "supported: " in out and "hi-IN" in out


def test_mode_live_allowed_in_auto_even_when_batch_runs_on_this_mac(fake_helper, agent, key_file, capsys):
    """Live mode is an explicit choice to stream to Gemini: auto doesn't block it."""
    plist, calls = agent
    assert cli.transcription_summary()["engine"] == "apple"
    assert cli.main(["mode", "live"]) == 0
    assert _read_env(plist)["MEETING_CAPTURE_MODE"] == "live" and calls == ["relaunch"]
    assert "stream to Gemini" in capsys.readouterr().out


def test_mode_live_refused_when_on_device_only(fake_helper, agent, gemini_key, capsys):
    plist, calls = agent
    cli._update_plist_env({"MEETING_CAPTURE_STT": "apple"})
    before = plist.read_bytes()
    assert cli.main(["mode", "live"]) == 1
    err = capsys.readouterr().err
    assert "never uploads" in err and "stt auto" in err
    assert plist.read_bytes() == before and calls == []


def test_mode_live_refused_without_a_key(fake_helper, agent, capsys):
    plist, calls = agent
    before = plist.read_bytes()
    assert cli.main(["mode", "live"]) == 1
    assert "no Google API key" in capsys.readouterr().err
    assert plist.read_bytes() == before and calls == []


def test_mode_live_counts_a_key_in_the_daemon_env(fake_helper, agent):
    plist, calls = agent
    cli._update_plist_env({"GOOGLE_API_KEY": "from-the-plist"})
    assert cli.main(["mode", "live"]) == 0 and calls == ["relaunch"]


def test_mode_live_allowed_with_gemini(fake_helper, agent, key_file):
    plist, calls = agent
    cli._update_plist_env({"MEETING_CAPTURE_STT": "gemini"})
    assert cli.main(["mode", "live"]) == 0
    assert _read_env(plist)["MEETING_CAPTURE_MODE"] == "live" and calls == ["relaunch"]


def test_stt_apple_while_live_says_it_runs_batch(fake_helper, agent, capsys):
    cli._update_plist_env({"MEETING_CAPTURE_MODE": "live"})
    assert cli.main(["stt", "apple"]) == 0
    assert "runs batch" in capsys.readouterr().out


def _quiet_system(monkeypatch, tmp_path):
    """status/doctor without the mic HAL, codesign, launchctl or ~/.meeting-capture."""
    import subprocess
    from meeting_capture import daemon, recorder
    for name in ("is_mic_active", "active_mic_name", "mic_name"):
        monkeypatch.setattr(cli, name, lambda: None)
    monkeypatch.setattr(recorder, "find_sysaudio", lambda: None)
    monkeypatch.setattr(recorder, "find_audiotee", lambda: None)
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli, "LOG_FILE", tmp_path / "daemon.log")
    monkeypatch.setattr(cli, "PAUSE_FILE", tmp_path / "paused")
    monkeypatch.setattr(cli, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", tmp_path / "failed")
    monkeypatch.setattr(daemon, "AUDIO_DIR", tmp_path / "audio")
    real_run = subprocess.run

    def run(cmd, *a, **k):
        if cmd and cmd[0] in ("launchctl", "codesign"):
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", run)


def test_status_and_doctor_show_the_engine(fake_helper, agent, monkeypatch, capsys, tmp_path):
    _quiet_system(monkeypatch, tmp_path)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "transcription:    on this Mac (en-US) [stt=auto]" in out
    assert "gemini key:       not set (optional)" in out
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "✓ engine — on this Mac (en-US)" in out
    assert "✓ on-device model — en-US installed" in out
    assert "Google API key: not set (optional" in out
    assert "Google API key missing" not in out


def test_status_doctor_and_stt_say_when_live_is_requested_but_runs_batch(fake_helper, agent, gemini_key,
                                                                         monkeypatch, capsys, tmp_path):
    _quiet_system(monkeypatch, tmp_path)
    cli._update_plist_env({"MEETING_CAPTURE_MODE": "live", "MEETING_CAPTURE_STT": "apple"})
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "mode:             live requested — running batch: transcription is set to on this Mac only" in out
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "✗ live mode requested, but the recorder runs batch: transcription is set to on this Mac only" in out
    assert "stt auto" in out
    cli.main(["stt"])
    assert "live mode: requested, but transcription is set to on this Mac only" in capsys.readouterr().out
    assert cli.main(["mode"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "live\n" and "runs batch" in captured.err


def test_status_doctor_and_stt_when_live_runs(fake_helper, agent, key_file, monkeypatch, capsys, tmp_path):
    _quiet_system(monkeypatch, tmp_path)
    cli._update_plist_env({"MEETING_CAPTURE_MODE": "live"})
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "mode:             live — calls stream to Gemini" in out
    assert "gemini key:       set\n" in out                # needed for live, not "optional"
    cli.main(["doctor"])
    assert "✓ capture mode — live" in capsys.readouterr().out
    cli.main(["stt"])
    assert "live mode: on — calls stream to Gemini" in capsys.readouterr().out


def test_live_on_line_in_is_reported_as_batch(fake_helper, agent, gemini_key, monkeypatch, capsys, tmp_path):
    _quiet_system(monkeypatch, tmp_path)
    cli._update_plist_env({"MEETING_CAPTURE_MODE": "live", "MEETING_CAPTURE_SOURCE": "linein"})
    cli.main(["status"])
    assert "live requested — running batch: the audio source is line-in" in capsys.readouterr().out


def test_doctor_fails_when_nothing_can_transcribe(agent, monkeypatch, capsys, tmp_path):
    _quiet_system(monkeypatch, tmp_path)
    assert cli.main(["doctor"]) == 1
    assert "✗ no transcription engine can run" in capsys.readouterr().out


def test_vocab_says_it_is_for_gemini_only(fake_helper, agent, tmp_path, monkeypatch, capsys):
    from meeting_capture import paths
    monkeypatch.setattr(paths, "VOCAB_FILE", tmp_path / "vocab.txt")
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    assert cli.main(["vocab"]) == 0
    out = capsys.readouterr().out
    assert "Gemini transcription only" in out and "runs on this Mac" in out


# ---- upgrading from Gemini: say so, until an engine is picked ------------------------------

def test_an_upgrader_with_a_key_file_is_told_until_they_pick(fake_helper, agent, key_file,
                                                                  monkeypatch, capsys, tmp_path):
    """The plist of an install from before on-device transcription: the legacy
    TRANSCRIBER key, no STT, a Gemini key. Auto now runs on this Mac — say
    so in status, doctor and `stt` until they pick an engine or a language."""
    _quiet_system(monkeypatch, tmp_path)
    plist, _ = agent
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "note:             transcription now runs on this Mac (on-device, en-US) instead of Gemini" in out
    assert "`meeting-capture stt gemini`" in out and "`meeting-capture stt auto`" in out
    cli.main(["doctor"])
    assert "! transcription now runs on this Mac" in capsys.readouterr().out
    cli.main(["stt"])
    assert "note:      transcription now runs on this Mac" in capsys.readouterr().out
    assert cli.main(["stt", "auto"]) == 0                 # keep it: the note goes
    capsys.readouterr()
    cli.main(["status"])
    assert "note:" not in capsys.readouterr().out
    _write_plist(plist, {"MEETING_CAPTURE_TRANSCRIBER": "gemini"})
    assert cli.main(["language", "en-GB"]) == 0           # picking a language counts too
    assert _read_env(plist)["MEETING_CAPTURE_STT"] == "auto"
    capsys.readouterr()
    cli.main(["stt"])
    assert "note:" not in capsys.readouterr().out


def test_no_note_without_a_key_or_once_gemini_is_chosen(fake_helper, agent, monkeypatch, capsys, tmp_path):
    _quiet_system(monkeypatch, tmp_path)
    cli.main(["status"])                                  # never had Gemini: nothing changed for them
    assert "note:" not in capsys.readouterr().out
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    cli._update_plist_env({"MEETING_CAPTURE_STT": "gemini"})
    cli.main(["status"])
    assert "note:" not in capsys.readouterr().out


def test_status_and_stt_say_where_the_language_comes_from(fake_helper, agent, monkeypatch, capsys, tmp_path):
    from meeting_capture import transcriber
    _quiet_system(monkeypatch, tmp_path)
    monkeypatch.setattr(transcriber, "_mac_preferences", lambda: (("es-ES", "en-US"), "es_ES"))
    fake_helper.configure(installed=["en-US", "es-ES"])
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "transcription:    on this Mac (es-ES)" in out
    assert "language:         es-ES (this Mac's language)" in out
    cli.main(["stt"])
    assert "language:  es-ES   (this Mac's language;" in capsys.readouterr().out
    cli.main(["language"])
    assert "from:      this Mac's language" in capsys.readouterr().out


def test_status_counts_audio_a_stopped_daemon_left_in_the_queue(fake_helper, agent, monkeypatch, capsys,
                                                                tmp_path):
    _quiet_system(monkeypatch, tmp_path)
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "chunk-1714003200-them.wav").write_bytes(b"RIFF")
    cli.main(["status"])
    assert "waiting audio:    1 chunk(s) in the transcription queue" in capsys.readouterr().out


def test_the_package_version_is_the_installed_distributions():
    import importlib.metadata
    from meeting_capture import __version__
    assert __version__ == importlib.metadata.version("meeting-capture")
