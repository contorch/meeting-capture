"""Tests for the transcription backends + key resolution.

We don't exercise the actual Gemini API in unit tests — that would require a
network call. Live verification uses `say`-generated audio through the real
transcriber (see repo knowledge for the A/B harness).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meeting_capture import transcriber as t


class TestDispatch:
    def test_transcribe_model_uses_interactions(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(t, "_transcribe_interactions", lambda p, m, r: seen.update(m=m, r=r) or "ok")
        monkeypatch.setattr(t, "_transcribe_gemini", lambda *a, **k: pytest.fail("should not hit generate_content"))
        assert t.transcribe(Path("/tmp/x.wav"), model="gemini-3.5-transcribe", role="me") == "ok"
        assert seen == {"m": "gemini-3.5-transcribe", "r": "me"}

    def test_flash_model_uses_generate_content(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(t, "_transcribe_interactions", lambda *a: pytest.fail("wrong backend"))
        monkeypatch.setattr(t, "_transcribe_gemini", lambda p, m, i=None, r="them": seen.update(m=m, r=r) or "ok")
        assert t.transcribe(Path("/tmp/x.wav"), model="gemini-2.5-flash", role="them") == "ok"
        assert seen == {"m": "gemini-2.5-flash", "r": "them"}

    def test_default_model_is_transcribe(self, monkeypatch):
        monkeypatch.delenv(t.ENV_GEMINI_MODEL, raising=False)
        assert t.resolve_model() == t.DEFAULT_GEMINI_MODEL
        assert t.is_transcribe_model(t.DEFAULT_GEMINI_MODEL)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(t.ENV_GEMINI_MODEL, "gemini-2.5-flash")
        assert t.resolve_model() == "gemini-2.5-flash"

    def test_live_model_rejected_for_batch(self):
        with pytest.raises(RuntimeError, match="streaming"):
            t.transcribe(Path("/tmp/x.wav"), model="gemini-3.5-transcribe-live")

    def test_falls_back_to_flash_when_transcribe_fails(self, monkeypatch):
        def boom(p, m, r):
            raise RuntimeError("503 preview hiccup")
        seen = {}
        monkeypatch.setattr(t, "_transcribe_interactions", boom)
        monkeypatch.setattr(t, "_transcribe_gemini", lambda p, m, i=None, r="them": seen.update(m=m) or "fallback text")
        assert t.transcribe(Path("/tmp/x.wav"), model="gemini-3.5-transcribe") == "fallback text"
        assert seen["m"] == t.FALLBACK_GEMINI_MODEL


class TestTranscriptionConfig:
    def test_me_channel_always_vocab_verbatim(self):
        cfg = t._transcription_config("me", ["Priya"], diarize=True)
        assert cfg == {"mode": {"type": "verbatim"}, "custom_vocabulary": ["Priya"]}

    def test_them_diarize_drops_vocab(self):
        cfg = t._transcription_config("them", ["Priya"], diarize=True)
        assert cfg == {"mode": {"type": "verbatim", "diarization_mode": "speaker", "timestamp_granularities": ["word"]}}

    def test_them_default_uses_vocab(self):
        cfg = t._transcription_config("them", ["Priya", "Chroma"], diarize=False)
        assert cfg["custom_vocabulary"] == ["Priya", "Chroma"]
        assert "diarization_mode" not in cfg["mode"]

    def test_no_vocab_no_key(self):
        assert "custom_vocabulary" not in t._transcription_config("me", [], diarize=False)

    def test_diarization_env(self, monkeypatch):
        monkeypatch.delenv(t.ENV_DIARIZE, raising=False)
        assert t.diarization_enabled() is False
        monkeypatch.setenv(t.ENV_DIARIZE, "1")
        assert t.diarization_enabled() is True


class TestVocabulary:
    def test_load_parses_comments_blanks_dupes(self, tmp_path):
        f = tmp_path / "vocab.txt"
        f.write_text("# header\nPriya\n\nChroma  # the vector db\nPriya\nJWT\n")
        assert t.load_vocabulary(f) == ["Priya", "Chroma", "JWT"]

    def test_missing_file_is_empty(self, tmp_path):
        assert t.load_vocabulary(tmp_path / "nope.txt") == []

    def test_capped_at_api_limit(self, tmp_path):
        f = tmp_path / "vocab.txt"
        f.write_text("\n".join(f"term{i}" for i in range(t.MAX_VOCAB_TERMS + 50)))
        assert len(t.load_vocabulary(f)) == t.MAX_VOCAB_TERMS


class TestDiarizedFormatting:
    def test_groups_consecutive_speakers(self):
        anns = [
            {"text": "can", "speaker": "spk:0"}, {"text": "we", "speaker": "spk:0"},
            {"text": "done.", "speaker": "spk:1"}, {"text": "tonight", "speaker": "spk:1"},
            {"text": "great", "speaker": "spk:0"},
        ]
        assert t.format_diarized(anns) == "[SPEAKER_1] can we\n[SPEAKER_2] done. tonight\n[SPEAKER_1] great"

    def test_empty(self):
        assert t.format_diarized([]) == ""

    def test_skips_blank_words(self):
        assert t.format_diarized([{"text": " ", "speaker": "spk:0"}, {"text": "hi", "speaker": "spk:0"}]) == "[SPEAKER_1] hi"


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
        assert ("API key" in msg) or ("google-genai" in msg.lower())
