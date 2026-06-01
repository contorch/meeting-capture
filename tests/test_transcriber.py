"""Tests for the Gemini transcription backend + key resolution.

We don't exercise the actual Gemini API in unit tests — that would require a
network call. Live verification happens via a separate manual script that
generates a known audio sample with macOS `say` and round-trips it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meeting_capture import transcriber as t


class TestTranscribeEntrypoint:
    def test_transcribe_calls_gemini(self, monkeypatch):
        called = {}

        def fake_gemini(p, m):
            called["gemini"] = (p, m)
            return "fake gemini output"

        monkeypatch.setattr(t, "_transcribe_gemini", fake_gemini)
        out = t.transcribe(Path("/tmp/fake.wav"))
        assert out == "fake gemini output"
        assert called["gemini"][0] == Path("/tmp/fake.wav")

    def test_model_override_passed_through(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(t, "_transcribe_gemini", lambda p, m: seen.setdefault("m", m) or "ok")
        t.transcribe(Path("/tmp/fake.wav"), model="gemini-2.5-pro")
        assert seen["m"] == "gemini-2.5-pro"


class TestGeminiKeyResolution:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        key_file = tmp_path / "key"
        key_file.write_text("from-file\n")
        monkeypatch.setattr(t, "GEMINI_KEY_FILE", key_file)
        monkeypatch.setenv("GOOGLE_API_KEY", "from-env")
        assert t._resolve_gemini_api_key() == "from-env"

    def test_gemini_api_key_env_also_works(self, monkeypatch, tmp_path):
        monkeypatch.setattr(t, "GEMINI_KEY_FILE", tmp_path / "no")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "alt-env")
        assert t._resolve_gemini_api_key() == "alt-env"

    def test_falls_back_to_file(self, monkeypatch, tmp_path):
        key_file = tmp_path / "key"
        key_file.write_text("file-key\n")
        monkeypatch.setattr(t, "GEMINI_KEY_FILE", key_file)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert t._resolve_gemini_api_key() == "file-key"

    def test_no_key_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(t, "GEMINI_KEY_FILE", tmp_path / "missing")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert t._resolve_gemini_api_key() is None


class TestGeminiBackendErrors:
    def test_missing_key_raises_with_clear_message(self, monkeypatch, tmp_path):
        monkeypatch.setattr(t, "GEMINI_KEY_FILE", tmp_path / "missing")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as exc:
            t._transcribe_gemini(Path("/tmp/fake.wav"), None)
        msg = str(exc.value)
        # Either missing package OR missing key — both have actionable hints.
        assert ("API key" in msg) or ("google-genai" in msg.lower())
