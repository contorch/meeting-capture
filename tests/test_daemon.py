from pathlib import Path

from meeting_capture import daemon
from meeting_capture.recorder import Chunk


def test_session_path_contains_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "TRANSCRIPTS_DIR", tmp_path)
    started = 1714003200.0
    p = daemon._session_path(started)
    assert p.parent == tmp_path
    assert p.name.startswith("meeting-")
    assert p.suffix == ".md"


def test_append_creates_header_then_appends(tmp_path):
    transcript = tmp_path / "meeting-x.md"
    chunk = Chunk(path=Path("/tmp/x.wav"), started_at=1714003200.0, duration_seconds=5.0)
    daemon._append(transcript, chunk, "first line")
    daemon._append(transcript, chunk, "second line")
    text = transcript.read_text()
    assert text.count("# Meeting transcript") == 1
    assert "first line" in text
    assert "second line" in text


def test_append_skips_empty(tmp_path):
    transcript = tmp_path / "meeting-y.md"
    chunk = Chunk(path=Path("/tmp/x.wav"), started_at=1714003200.0, duration_seconds=5.0)
    daemon._append(transcript, chunk, "")
    assert not transcript.exists()


def test_append_labels_roles(tmp_path):
    transcript = tmp_path / "meeting-z.md"
    them = Chunk(path=Path("/tmp/a.wav"), started_at=1714003200.0, duration_seconds=5.0, role="them")
    me = Chunk(path=Path("/tmp/b.wav"), started_at=1714003210.0, duration_seconds=5.0, role="me")
    daemon._append(transcript, them, "how was the launch?")
    daemon._append(transcript, me, "shipped last night")
    text = transcript.read_text()
    assert "**Them:** how was the launch?" in text
    assert "**Me:** shipped last night" in text


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
