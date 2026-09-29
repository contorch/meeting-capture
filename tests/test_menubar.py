import json
import plistlib

from meeting_capture import menubar, status


def test_status_file_is_written_atomically_and_only_on_change(tmp_path, monkeypatch):
    f = tmp_path / "state.json"
    monkeypatch.setattr(status, "STATE_FILE", f)
    r = status.Reporter("linein")
    r("idle")
    first = f.stat().st_mtime_ns
    r("idle")                                  # no change → no write
    assert f.stat().st_mtime_ns == first
    r("listening", "meeting-2026-09-29T10-00-00")
    got = status.read()
    assert got["state"] == "listening" and got["source"] == "linein"
    assert got["session"] == "meeting-2026-09-29T10-00-00" and got["pid"] > 0
    assert list(tmp_path.glob("*.tmp")) == []


def test_status_read_tolerates_missing_or_corrupt(tmp_path, monkeypatch):
    f = tmp_path / "state.json"
    monkeypatch.setattr(status, "STATE_FILE", f)
    assert status.read() is None
    f.write_text("{half")
    assert status.read() is None


def test_plist_runs_binary_with_cli_and_hide_sticks():
    p = plistlib.loads(menubar.plist_payload(menubar.Path("/opt/homebrew/bin/meeting-capture-menubar"),
                                             "/opt/homebrew/bin/meeting-capture"))
    assert p["ProgramArguments"] == ["/opt/homebrew/bin/meeting-capture-menubar", "--cli",
                                     "/opt/homebrew/bin/meeting-capture"]
    assert p["RunAtLoad"] is True
    assert p["KeepAlive"] == {"SuccessfulExit": False}   # "Hide menu bar item" exits 0 → stays hidden
    assert p["LimitLoadToSessionType"] == "Aqua"


def test_find_menubar_prefers_env(tmp_path, monkeypatch):
    b = tmp_path / "meeting-capture-menubar"
    b.write_text("#!/bin/sh\n")
    monkeypatch.setenv(menubar.MENUBAR_ENV_VAR, str(b))
    assert menubar.find_menubar() == b


def test_install_without_binary_is_a_quiet_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(menubar, "find_menubar", lambda: None)
    monkeypatch.setattr(menubar, "PLIST", tmp_path / "x.plist")
    assert menubar.install(quiet=True) is False
    assert not (tmp_path / "x.plist").exists()
