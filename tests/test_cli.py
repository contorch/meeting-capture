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


def test_last_transcript_returns_none_when_dir_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "TRANSCRIPTS_DIR", tmp_path)
    assert cli._last_transcript() is None


def test_last_transcript_returns_most_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "TRANSCRIPTS_DIR", tmp_path)
    older = tmp_path / "meeting-2026-01-01T00-00-00.md"
    newer = tmp_path / "meeting-2026-04-26T14-00-00.md"
    older.write_text("old")
    newer.write_text("new")
    import os
    past = time.time() - 3600
    os.utime(older, (past, past))
    assert cli._last_transcript() == newer


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


def test_cmd_mode_switch_relaunches(tmp_path, monkeypatch, capsys):
    plist = tmp_path / "agent.plist"
    _write_plist(plist, {})
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    calls = []
    monkeypatch.setattr(cli, "_relaunch", lambda: calls.append("relaunch"))
    assert cli.main(["mode", "live"]) == 0
    assert calls == ["relaunch"]
    assert _read_env(plist)["MEETING_CAPTURE_MODE"] == "live"
    assert "switched to live" in capsys.readouterr().out


def test_cmd_mode_noop_when_already_set(tmp_path, monkeypatch, capsys):
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
