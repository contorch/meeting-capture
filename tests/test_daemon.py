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
