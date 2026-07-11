from pathlib import Path

from meeting_capture import paths


def test_paths_under_home():
    home = Path.home()
    assert paths.STATE_DIR == home / ".meeting-capture"
    assert paths.TRANSCRIPTS_DIR == home / "transcripts"
    assert paths.PAUSE_FILE == paths.STATE_DIR / "paused"
    assert paths.LAUNCHD_PLIST == home / "Library" / "LaunchAgents" / "com.contorch.meeting-capture.plist"


def test_ensure_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / ".meeting-capture")
    monkeypatch.setattr(paths, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(paths, "AUDIO_DIR", tmp_path / ".meeting-capture" / "audio")
    paths.ensure_dirs()
    assert (tmp_path / ".meeting-capture").is_dir()
    assert (tmp_path / "transcripts").is_dir()
    assert (tmp_path / ".meeting-capture" / "audio").is_dir()
