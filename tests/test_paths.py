from pathlib import Path

from meeting_capture import paths


def test_paths_under_home():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fresh_paths", paths.__file__)
    fresh = importlib.util.module_from_spec(spec)   # conftest patches the live module
    spec.loader.exec_module(fresh)
    home = Path.home()
    assert fresh.STATE_DIR == home / ".meeting-capture"
    assert fresh.PAUSE_FILE == fresh.STATE_DIR / "paused"
    assert fresh.ENV_FILE == home / ".meeting-capture" / "env"
    assert fresh.LAUNCHD_PLIST == home / "Library" / "LaunchAgents" / "com.contorch.meeting-capture.plist"


def test_ensure_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / ".meeting-capture")
    monkeypatch.setattr(paths, "AUDIO_DIR", tmp_path / ".meeting-capture" / "audio")
    paths.ensure_dirs()
    assert (tmp_path / ".meeting-capture").is_dir()
    assert not (tmp_path / "transcripts").exists(), "transcripts live in the database"
    assert (tmp_path / ".meeting-capture" / "audio").is_dir()
