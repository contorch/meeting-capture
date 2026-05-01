"""Tests for transcriber backend dispatch + Gemini key resolution.

We don't exercise the actual transcription APIs in unit tests — that would
require either a model download (whisper) or a network call (gemini).
Live verification of Gemini happens via a separate manual script that
generates a known audio sample with macOS `say` and round-trips it.
"""
from __future__ import annotations

import pytest

from meeting_capture import transcriber as t


class TestBackendDispatch:
    def test_default_backend_is_whisper(self, monkeypatch):
        monkeypatch.delenv(t.ENV_TRANSCRIBER, raising=False)
        # We just check the dispatch logic — actual mlx_whisper call is
        # mocked by patching _transcribe_whisper, so we don't need the model.
        called = {}

        def fake_whisper(p, m, ip):
            called["whisper"] = True
            return "fake whisper output"

        def fake_gemini(p, m):
            called["gemini"] = True
            return "fake gemini output"

        monkeypatch.setattr(t, "_transcribe_whisper", fake_whisper)
        monkeypatch.setattr(t, "_transcribe_gemini", fake_gemini)

        out = t.transcribe(__import__("pathlib").Path("/tmp/fake.wav"))
        assert out == "fake whisper output"
        assert called == {"whisper": True}

    def test_env_routes_to_gemini(self, monkeypatch):
        monkeypatch.setenv(t.ENV_TRANSCRIBER, "gemini")
        called = {}
        monkeypatch.setattr(t, "_transcribe_whisper", lambda *a, **k: (called.setdefault("w", True), "w")[1])
        monkeypatch.setattr(t, "_transcribe_gemini", lambda *a, **k: (called.setdefault("g", True), "g")[1])
        out = t.transcribe(__import__("pathlib").Path("/tmp/fake.wav"))
        assert out == "g"
        assert called == {"g": True}

    def test_env_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(t.ENV_TRANSCRIBER, "GEMINI")
        monkeypatch.setattr(t, "_transcribe_gemini", lambda *a, **k: "ok")
        assert t.transcribe(__import__("pathlib").Path("/tmp/fake.wav")) == "ok"

    def test_unknown_backend_falls_back_to_whisper(self, monkeypatch):
        monkeypatch.setenv(t.ENV_TRANSCRIBER, "alien-backend")
        monkeypatch.setattr(t, "_transcribe_whisper", lambda *a, **k: "w")
        assert t.transcribe(__import__("pathlib").Path("/tmp/fake.wav")) == "w"


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
            t._transcribe_gemini(__import__("pathlib").Path("/tmp/fake.wav"), None)
        msg = str(exc.value)
        # Either missing extra OR missing key — both have actionable hints
        assert ("API key" in msg) or ("gemini" in msg.lower())


class TestResolvedPrompt:
    """Existing whisper prompt resolution behavior is unchanged."""

    def test_default_built_in(self, monkeypatch, tmp_path):
        from meeting_capture import paths
        monkeypatch.setattr(paths, "VOCAB_FILE", tmp_path / "vocab.txt")
        monkeypatch.setattr(t, "VOCAB_FILE", tmp_path / "vocab.txt")
        monkeypatch.delenv(t.ENV_PROMPT, raising=False)
        prompt, source = t.resolved_prompt()
        assert prompt == t.DEFAULT_INITIAL_PROMPT
        assert "default" in source

    def test_vocab_file_overrides(self, monkeypatch, tmp_path):
        vocab = tmp_path / "vocab.txt"
        vocab.write_text("custom vocabulary here\n")
        monkeypatch.setattr(t, "VOCAB_FILE", vocab)
        prompt, source = t.resolved_prompt()
        assert prompt == "custom vocabulary here"
        assert "vocab file" in source
