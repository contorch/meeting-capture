import pytest


@pytest.fixture(autouse=True)
def _isolated_transcript_db(tmp_path, monkeypatch):
    """Never touch the real ~/.context-orchestrator/context.db from tests."""
    from meeting_capture import store
    monkeypatch.setenv("CO_DB_PATH", str(tmp_path / "context.db"))
    monkeypatch.setattr(store, "PENDING_FILE", tmp_path / "unsaved-lines.jsonl")
