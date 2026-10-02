import pytest
from pathlib import Path

from meeting_capture import daemon, store
from meeting_capture.recorder import Chunk


def _body(meeting_id):
    row = store.get(meeting_id)
    return row["body"] if row else None


def test_session_id_contains_timestamp():
    sid = daemon._session_id(1714003200.0)
    assert sid.startswith("meeting-") and "T" in sid and not sid.endswith(".md")
    assert daemon._started_iso(sid).startswith(sid[len("meeting-"):len("meeting-") + 10])


def test_append_creates_header_then_appends(tmp_path):
    chunk = Chunk(path=Path("/tmp/x.wav"), started_at=1714003200.0, duration_seconds=5.0)
    daemon._append("meeting-x", chunk, "first line")
    daemon._append("meeting-x", chunk, "second line")
    text = _body("meeting-x")
    assert text.count("# Meeting transcript") == 1
    assert "first line" in text
    assert "second line" in text
    assert not (tmp_path / "transcripts").exists(), "no transcript files"


def test_append_skips_empty():
    chunk = Chunk(path=Path("/tmp/x.wav"), started_at=1714003200.0, duration_seconds=5.0)
    daemon._append("meeting-y", chunk, "")
    assert _body("meeting-y") is None


def test_append_labels_roles():
    them = Chunk(path=Path("/tmp/a.wav"), started_at=1714003200.0, duration_seconds=5.0, role="them")
    me = Chunk(path=Path("/tmp/b.wav"), started_at=1714003210.0, duration_seconds=5.0, role="me")
    daemon._append("meeting-z", them, "how was the launch?")
    daemon._append("meeting-z", me, "shipped last night")
    text = _body("meeting-z")
    assert "**Them:** how was the launch?" in text
    assert "**Me:** shipped last night" in text


def test_locked_db_queues_the_line_and_flushes_it_later(tmp_path, monkeypatch):
    import sqlite3
    real = store._write
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "_write", locked)
    daemon._append_text("meeting-q", "them", "said while the db was locked", started_at=1714003200.0)
    assert store.PENDING_FILE.exists()
    monkeypatch.setattr(store, "_write", real)
    daemon._append_text("meeting-q", "me", "and then this", started_at=1714003201.0)
    text = _body("meeting-q")
    assert text.index("while the db was locked") < text.index("and then this")
    assert not store.PENDING_FILE.exists()


def test_backoff_escalates_on_fast_failures_and_resets():
    b = daemon.FailureBackoff(fast_fail_s=10.0, base_s=5.0, max_s=40.0)
    assert b.record(0.2, 0) == 5.0
    assert b.record(0.2, 0) == 10.0
    assert b.record(0.2, 0) == 20.0
    assert b.record(0.2, 0) == 40.0
    assert b.record(0.2, 0) == 40.0  # capped
    assert b.failures == 5
    assert b.record(0.2, 1) == 0.0   # a chunk resets the streak
    assert b.failures == 0
    assert b.delay == 0.0


def test_backoff_ignores_long_sessions_without_chunks():
    b = daemon.FailureBackoff(fast_fail_s=10.0)
    assert b.record(45.0, 0) == 0.0  # quiet-but-alive session is not a failure
    assert b.failures == 0


def test_permission_hint_names_binary(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(daemon, "find_sysaudio", lambda: Path("/x/bin/sysaudio"))
    hint = daemon._permission_hint()
    assert "/x/bin/sysaudio" in hint
    assert "Screen & System Audio Recording" in hint


def _wav(path, seconds=1.0, rate=16000):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


def test_park_failed_keeps_audio(tmp_path, monkeypatch):
    failed = tmp_path / "failed"
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", failed)
    src = tmp_path / "chunk-1714003200-them.wav"; _wav(src)
    chunk = Chunk(path=src, started_at=1714003200.0, duration_seconds=1.0, role="them")
    dest = daemon._park_failed(chunk, RuntimeError("no key"))
    assert dest == failed / "chunk-1714003200-them.wav"
    assert dest.exists() and not src.exists()


def test_retry_recovers_parked_chunks_into_sessions(tmp_path, monkeypatch):
    failed = tmp_path / "failed"; failed.mkdir()
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", failed)
    _wav(failed / "chunk-1714003200-them.wav", 2.0)
    _wav(failed / "chunk-1714003203-me.wav", 2.0)
    _wav(failed / "chunk-1714010000-them.wav", 2.0)   # > SESSION_GAP later → new session
    (failed / "not-a-chunk.wav").write_bytes(b"junk")
    said = {"them": "how was the launch?", "me": "shipped last night"}
    monkeypatch.setattr(daemon, "transcribe", lambda path, role: said[role])
    assert daemon.retry_failed_chunks() == 3
    assert sorted(p.name for p in failed.iterdir()) == ["not-a-chunk.wav"]
    sessions = sorted(r["meeting_id"] for r in store.recent())
    assert len(sessions) == 2
    first = _body(sessions[0])
    assert "**Them:** how was the launch?" in first and "**Me:** shipped last night" in first


def test_retry_stops_at_first_failure_and_keeps_the_rest(tmp_path, monkeypatch):
    failed = tmp_path / "failed"; failed.mkdir()
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", failed)
    _wav(failed / "chunk-1714003200-them.wav"); _wav(failed / "chunk-1714003205-me.wav")
    def boom(path, role): raise RuntimeError("still no key")
    monkeypatch.setattr(daemon, "transcribe", boom)
    assert daemon.retry_failed_chunks() == 0
    assert len(list(failed.glob("chunk-*.wav"))) == 2


def test_retry_noop_without_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "FAILED_AUDIO_DIR", tmp_path / "missing")
    assert daemon.retry_failed_chunks() == 0


def test_backlog_retry_runs_in_background_one_at_a_time(monkeypatch):
    import threading, time
    gate = threading.Event()
    calls = []

    def slow_retry():
        calls.append(1)
        gate.wait(5)
        return 0

    monkeypatch.setattr(daemon, "retry_failed_chunks", slow_retry)
    t0 = time.time()
    t = daemon.retry_failed_chunks_in_background()
    assert t is not None and time.time() - t0 < 1.0          # returns at once: recording isn't held up
    assert daemon.retry_failed_chunks_in_background() is None  # a second one doesn't pile on
    gate.set(); t.join(5)
    assert calls == [1]
    t2 = daemon.retry_failed_chunks_in_background()             # free again afterwards
    assert t2 is not None; t2.join(5)


# ---- start a new meeting on request ----------------------------------------

@pytest.fixture
def new_meeting_file(tmp_path, monkeypatch):
    from meeting_capture import meetings
    f = tmp_path / "new-meeting"
    monkeypatch.setattr(meetings, "NEW_MEETING_FILE", f)
    return f


def test_chunks_stay_in_the_meeting_without_a_request(new_meeting_file):
    s1 = daemon._next_session(None, 1000.0, 0.0)
    assert daemon._next_session(s1, 1060.0, 1050.0) == s1          # 10 s gap: same meeting
    assert daemon._next_session(s1, 1050.0 + 16 * 60, 1050.0) != s1  # 16 min gap: new one


def test_new_meeting_request_splits_at_the_click(new_meeting_file):
    from meeting_capture import meetings
    s1 = daemon._next_session(None, 1000.0, 0.0)
    meetings.request_new_meeting(at=1100.0)
    # A chunk that started before the click still belongs to the old meeting…
    assert daemon._next_session(s1, 1090.0, 1085.0) == s1
    assert new_meeting_file.exists()
    # …the first one starting after it opens the new meeting, once.
    s2 = daemon._next_session(s1, 1101.0, 1095.0)
    assert s2 != s1 and not new_meeting_file.exists()
    assert daemon._next_session(s2, 1120.0, 1110.0) == s2


def test_back_to_back_meetings_in_the_same_second_get_distinct_ids(new_meeting_file):
    from meeting_capture import meetings
    s1 = daemon._next_session(None, 1000.0, 0.0)
    meetings.request_new_meeting(at=1000.0)
    assert daemon._next_session(s1, 1000.2, 1000.1) != s1


def test_resume_and_new_commands_request_a_new_meeting(new_meeting_file, tmp_path, monkeypatch, capsys):
    from meeting_capture import cli
    monkeypatch.setattr(cli, "PAUSE_FILE", tmp_path / "paused")
    (tmp_path / "paused").touch()
    assert cli.main(["resume"]) == 0
    assert new_meeting_file.exists() and "new transcript" in capsys.readouterr().out
    new_meeting_file.unlink()
    assert cli.main(["resume"]) == 0 and not new_meeting_file.exists()   # wasn't paused: no-op
    assert cli.main(["new"]) == 0 and new_meeting_file.exists()
