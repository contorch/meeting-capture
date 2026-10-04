"""state.json and `meeting-capture status --json`: the one answer to "is a
meeting being recorded right now?" (recording: true | false | null)."""
import json
import os
import subprocess
import sys

import pytest

from meeting_capture import cli, paths, state

STATUS_KEYS = {"schema", "ok", "recording", "state", "since", "meeting_id", "pid", "stale"}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _doc():
    return json.loads(paths.STATE_FILE.read_text())


def test_transitions_are_written_atomically_with_the_schema():
    clock = Clock()
    r = state.Recorder(source="sck", clock=clock)
    r.set("idle")
    d = _doc()
    assert d == {"schema": "meeting-capture.state/1", "pid": os.getpid(), "state": "idle", "since": 1000.0,
                 "meeting_id": None, "source": "sck", "updated_at": 1000.0}
    clock.t = 1005
    r.set("recording")
    r.meeting("meeting-2026-10-04T10-00-00")
    d = _doc()
    assert (d["state"], d["since"], d["meeting_id"]) == ("recording", 1005, "meeting-2026-10-04T10-00-00")
    clock.t = 1010
    r.set("paused")
    assert (_doc()["state"], _doc()["meeting_id"]) == ("paused", None)
    assert not list(paths.STATE_FILE.parent.glob(".state.*"))          # no temp files left


def test_an_unchanged_state_is_not_rewritten_and_heartbeats_are_throttled():
    clock = Clock()
    r = state.Recorder(clock=clock)
    r.set("idle")
    mtime = paths.STATE_FILE.stat().st_mtime_ns
    r.set("idle")
    r.heartbeat()                                     # idle: no heartbeat
    assert paths.STATE_FILE.stat().st_mtime_ns == mtime
    r.set("recording")
    clock.t += state.HEARTBEAT_S - 1
    r.heartbeat()
    assert _doc()["updated_at"] == 1000.0
    clock.t += 2
    r.heartbeat()
    assert _doc()["updated_at"] == clock.t and _doc()["since"] == 1000.0


def test_status_without_a_state_file_cannot_tell():
    s = state.status()
    assert set(s) >= STATUS_KEYS and s["schema"] == "meeting-capture.status/1"
    assert s["recording"] is None and s["reason"] == "no_state" and s["ok"] is True


def test_status_idle_paused_and_recording():
    clock = Clock()
    r = state.Recorder(clock=clock)
    r.set("idle")
    assert state.status(now=clock.t)["recording"] is False
    r.set("paused")
    assert state.status(now=clock.t)["recording"] is False
    r.set("recording")
    r.meeting("meeting-x")
    s = state.status(now=clock.t + 10)
    assert (s["recording"], s["meeting_id"], s["state"], s["stale"]) == (True, "meeting-x", "recording", False)


def test_a_stale_heartbeat_while_recording_cannot_tell():
    clock = Clock()
    r = state.Recorder(clock=clock)
    r.set("recording")
    s = state.status(now=clock.t + state.STALE_S + 1)
    assert s["recording"] is None and s["stale"] is True and s["reason"] == "stale_heartbeat"
    # an old idle state is still idle: only recording heartbeats
    r.set("idle")
    assert state.status(now=clock.t + 10 * state.STALE_S)["recording"] is False


def test_a_killed_daemon_cannot_tell():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    state.write({"schema": state.STATE_SCHEMA, "pid": p.pid, "state": "recording", "since": 1.0,
                 "meeting_id": "m", "source": "sck", "updated_at": 9e12})
    s = state.status()
    assert s["recording"] is None and s["reason"] == "daemon_not_running" and s["pid"] == p.pid


def test_garbage_is_unknown_not_a_crash():
    paths.STATE_FILE.write_text("{not json")
    assert state.status()["recording"] is None
    state.write({"schema": state.STATE_SCHEMA, "pid": os.getpid(), "state": "dancing", "updated_at": 0})
    assert state.status()["reason"] == "unknown_state"


def test_clear_removes_only_our_own_file():
    r = state.Recorder()
    r.set("idle")
    r.clear()
    assert not paths.STATE_FILE.exists()
    state.write({"schema": state.STATE_SCHEMA, "pid": 1, "state": "idle", "updated_at": 0})
    r.clear()
    assert paths.STATE_FILE.exists()


def test_a_full_disk_never_stops_the_recorder(monkeypatch):
    monkeypatch.setattr(state, "write", lambda doc: (_ for _ in ()).throw(OSError(28, "No space left")))
    state.Recorder().set("recording")                  # no exception


def test_cli_status_json_is_one_document_exit_0(capfd):
    state.Recorder().set("recording")
    assert cli.main(["status", "--json"]) == 0
    out = capfd.readouterr().out
    assert len(out.splitlines()) == 1
    doc = json.loads(out)
    assert doc["schema"] == "meeting-capture.status/1" and doc["recording"] is True
    assert set(doc) >= STATUS_KEYS


def test_cli_status_json_with_nothing_running(capfd):
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capfd.readouterr().out)["recording"] is None


def test_cli_status_text_is_unchanged_by_json(monkeypatch, capsys, tmp_path):
    from test_cli import _quiet_system
    _quiet_system(monkeypatch, tmp_path)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("meeting-capture ") and "{" not in out.splitlines()[1]


def test_as_a_separate_process_in_a_scratch_home(tmp_path):
    """What pipeline-monitor runs: a fresh process, stdout is the JSON only,
    and a status read creates nothing."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-m", "meeting_capture.cli", "status", "--json"],
                       capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["recording"] is None
    assert not (home / ".meeting-capture").exists()
