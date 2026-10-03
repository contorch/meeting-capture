"""On-device transcription: engine resolution, the `sysaudio transcribe`
helper contract (via a fake helper — see conftest.FAKE_HELPER), and the
guarantee that the on-device setting never uploads."""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from meeting_capture import transcriber as t

_real_mac_preferences = t._mac_preferences          # conftest stubs it out for every test


def _wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


@pytest.fixture
def no_gemini(monkeypatch):
    """Any attempt to reach Gemini fails the test."""
    def boom(*_a, **_k):
        pytest.fail("Gemini was called")
    monkeypatch.setattr(t, "_client", boom)
    monkeypatch.setattr(t, "_transcribe_interactions", boom)
    monkeypatch.setattr(t, "_transcribe_gemini", boom)


# --- configuration ------------------------------------------------------------------

class TestConfig:
    def test_defaults(self):
        assert t.stt_choice({}) == "auto"
        assert t.stt_locale({}) == "en-US"

    def test_legacy_transcriber_setting_reads_as_auto(self):
        assert t.stt_choice({"MEETING_CAPTURE_TRANSCRIBER": "gemini"}) == "auto"
        assert t.stt_choice({"MEETING_CAPTURE_TRANSCRIBER": "whisper"}) == "auto"

    def test_unknown_choice_is_auto(self):
        assert t.stt_choice({"MEETING_CAPTURE_STT": "whisper"}) == "auto"
        assert t.stt_choice({"MEETING_CAPTURE_STT": " Apple "}) == "apple"

    @pytest.mark.parametrize("given, want", [
        ("hi_in", "hi-IN"), ("EN-us", "en-US"), ("hi-latn-in", "hi-Latn-IN"), ("  fr  ", "fr"), ("", ""),
    ])
    def test_normalize_locale(self, given, want):
        assert t.normalize_locale(given) == want

    @pytest.mark.parametrize("given, want", [
        ("hi-IN", "hi-IN"), ("HI_in", "hi-IN"), ("hi", "hi-IN"), ("en", "en-US"),
        ("es", None),            # ambiguous: es-ES or es-MX
        ("xx-YY", None),
    ])
    def test_match_locale(self, given, want):
        assert t.match_locale(given, ["en-GB", "en-US", "es-ES", "es-MX", "hi-IN"]) == want

    def test_match_locale_without_a_list_trusts_the_input(self):
        assert t.match_locale("de_de", []) == "de-DE"


# --- engine resolution matrix ------------------------------------------------------------

class TestResolution:
    def test_auto_uses_this_mac_when_the_model_is_installed(self, fake_helper, gemini_key):
        b = t.resolve_backend()
        assert (b.engine, b.choice, b.ready, b.locale) == ("apple", "auto", True, "en-US")

    def test_auto_needs_no_key_on_device(self, fake_helper):
        assert t.resolve_backend().engine == "apple"

    def test_auto_falls_back_to_gemini_only_with_a_key(self, fake_helper, monkeypatch):
        fake_helper.configure(probe_rc=69, reason="needs macOS 26")
        b = t.resolve_backend()
        assert (b.engine, b.ready) == ("none", False) and "needs macOS 26" in b.reason
        monkeypatch.setenv("GOOGLE_API_KEY", "k")
        b = t.resolve_backend()
        assert (b.engine, b.ready) == ("gemini", True) and "needs macOS 26" in b.reason

    def test_auto_with_model_not_installed(self, fake_helper, gemini_key):
        fake_helper.configure(installed=[])
        b = t.resolve_backend()
        assert b.engine == "gemini" and "isn't installed" in b.reason

    def test_auto_with_an_old_sysaudio(self, fake_helper):
        fake_helper.configure(old=True)
        b = t.resolve_backend()
        assert b.engine == "none" and "predates" in b.reason

    def test_auto_without_any_helper(self):
        b = t.resolve_backend()                     # conftest points at a missing helper
        assert b.engine == "none" and "can't run" in b.reason

    def test_apple_never_resolves_to_gemini(self, fake_helper, gemini_key, monkeypatch):
        monkeypatch.setenv(t.ENV_STT, "apple")
        fake_helper.configure(probe_rc=69)
        b = t.resolve_backend()
        assert (b.engine, b.ready) == ("apple", False)

    def test_gemini_choice(self, fake_helper, monkeypatch):
        monkeypatch.setenv(t.ENV_STT, "gemini")
        assert t.resolve_backend().ready is False                  # no key
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        b = t.resolve_backend()
        assert (b.engine, b.ready) == ("gemini", True)
        assert fake_helper.calls() == []                           # never probed

    def test_locale_setting_is_passed_to_the_helper(self, fake_helper, monkeypatch):
        monkeypatch.setenv(t.ENV_LOCALE, "hi_IN")
        b = t.resolve_backend()
        assert b.engine == "none" and b.locale == "hi-IN"           # supported, not installed
        assert fake_helper.calls()[-1] == ["transcribe", "--probe", "--locale", "hi-IN"]

    def test_explicit_env_wins_over_os_environ(self, fake_helper, monkeypatch):
        monkeypatch.setenv(t.ENV_STT, "gemini")
        b = t.resolve_backend(env={"MEETING_CAPTURE_STT": "apple"})
        assert b.choice == "apple" and b.engine == "apple"


class TestProbe:
    def test_status_fields(self, fake_helper):
        st = t.apple_status()
        assert st.usable and st.installed and st.exit_code == 0
        assert "hi-IN" in st.supported and "en-US" in st.installed_locales
        d = st.as_dict()
        assert d["usable"] is True and d["installable"] is False

    def test_probe_is_cached(self, fake_helper):
        t.apple_status(); t.resolve_backend(); t.resolve_backend()
        assert len(fake_helper.calls()) == 1
        t.apple_status(refresh=True)
        assert len(fake_helper.calls()) == 2

    def test_probe_timeout_is_unavailable(self, fake_helper, monkeypatch):
        monkeypatch.setattr(t, "PROBE_TIMEOUT_S", 0.3)
        fake_helper.configure(probe_sleep=2)
        st = t.apple_status()
        assert not st.usable and "timed out" in st.reason

    def test_garbage_probe_output(self, fake_helper, tmp_path, monkeypatch):
        script = tmp_path / "garbage"
        script.write_text("#!/bin/sh\necho not json\nexit 3\n")
        script.chmod(0o755)
        monkeypatch.setenv(t.ENV_TRANSCRIBE_BIN, str(script))
        st = t.apple_status()
        assert not st.usable and "exit 3" in st.reason


# --- helper exit codes -> outcome classes ------------------------------------------------

class TestHelperExitCodes:
    def test_ok(self, fake_helper, tmp_path):
        fake_helper.configure(text="  the launch moved to friday  ")
        assert t._transcribe_apple(_wav(tmp_path / "c.wav")) == "the launch moved to friday"
        assert fake_helper.transcribe_calls()[-1] == ["transcribe", "--locale", "en-US", str(tmp_path / "c.wav")]

    def test_no_speech_is_empty_text(self, fake_helper, tmp_path):
        fake_helper.configure(text="")
        assert t._transcribe_apple(_wav(tmp_path / "c.wav")) == ""

    @pytest.mark.parametrize("rc", [69, 75])
    def test_unavailable_or_model_released(self, fake_helper, tmp_path, rc):
        fake_helper.configure(default_rc=rc)
        t.apple_status()                                   # cached usable…
        with pytest.raises(t.AppleUnavailable):
            t._transcribe_apple(_wav(tmp_path / "c.wav"))
        n = len(fake_helper.calls())
        t.apple_status()                                   # …and the cache was cleared
        assert len(fake_helper.calls()) == n + 1

    def test_unreadable_file_is_a_chunk_failure(self, fake_helper, tmp_path):
        fake_helper.configure(default_rc=70)
        with pytest.raises(t.AppleChunkFailed, match="fake failure 70"):
            t._transcribe_apple(_wav(tmp_path / "c.wav"))

    def test_other_error(self, fake_helper, tmp_path):
        fake_helper.configure(default_rc=1)
        with pytest.raises(t.AppleError, match="exit 1"):
            t._transcribe_apple(_wav(tmp_path / "c.wav"))

    def test_old_sysaudio_is_unavailable(self, fake_helper, tmp_path):
        fake_helper.configure(old=True)
        with pytest.raises(t.AppleUnavailable, match="predates"):
            t._transcribe_apple(_wav(tmp_path / "c.wav"))

    def test_the_real_old_sysaudio_message(self, tmp_path, monkeypatch):
        """What a pre-`transcribe` sysaudio actually prints (Sysaudio.swift)."""
        old = tmp_path / "sysaudio"
        old.write_text('#!/bin/sh\necho "unknown arg: $1" >&2\nexit 1\n')
        old.chmod(0o755)
        monkeypatch.setenv(t.ENV_TRANSCRIBE_BIN, str(old))
        assert "predates" in t.apple_status().reason
        with pytest.raises(t.AppleUnavailable):
            t._transcribe_apple(_wav(tmp_path / "c.wav"))

    def test_timeout_is_not_charged_to_the_file(self, fake_helper, tmp_path, monkeypatch):
        """A hang is usually the speech service, not the file: AppleError (the
        daemon's judge decides), never AppleChunkFailed (counted at once)."""
        monkeypatch.setattr(t, "APPLE_MIN_TIMEOUT_S", 0.3)
        fake_helper.configure(sleep=3)
        with pytest.raises(t.AppleError, match="timed out") as e:
            t._transcribe_apple(_wav(tmp_path / "c.wav"))
        assert not isinstance(e.value, t.ChunkFailed)

    def test_a_crash_is_not_charged_to_the_file(self, fake_helper, tmp_path):
        import signal
        fake_helper.configure(signal=signal.SIGKILL)
        with pytest.raises(t.AppleError, match="signal 9") as e:
            t._transcribe_apple(_wav(tmp_path / "c.wav"))
        assert not isinstance(e.value, t.ChunkFailed)

    def test_exit_0_without_a_transcript_is_an_error(self, fake_helper, tmp_path):
        fake_helper.configure(no_json=True)
        with pytest.raises(t.AppleError, match="no transcript"):
            t._transcribe_apple(_wav(tmp_path / "c.wav"))

    def test_only_exit_70_is_charged_to_the_file(self):
        assert issubclass(t.AppleChunkFailed, t.ChunkFailed)
        assert not issubclass(t.AppleError, t.ChunkFailed)

    def test_timeout_scales_with_the_chunk(self, tmp_path, monkeypatch):
        seen = []

        def run(binary, args, timeout):
            seen.append(timeout)
            import subprocess
            return subprocess.CompletedProcess(args, 0, '{"text": "x"}', "")

        monkeypatch.setattr(t, "_run_helper", run)
        t._transcribe_apple(_wav(tmp_path / "short.wav", 10))
        t._transcribe_apple(_wav(tmp_path / "long.wav", 300))
        assert seen == [30.0, 150.0]

    def test_missing_helper_is_unavailable(self, tmp_path):
        with pytest.raises(t.AppleUnavailable):
            t._transcribe_apple(_wav(tmp_path / "c.wav"))


class TestInstall:
    def test_install_then_usable(self, fake_helper):
        assert t.apple_status("hi-IN").installable
        res = t.install_apple_model("hi_IN")
        assert res["installed"] and res["locale"] == "hi-IN"
        assert fake_helper.calls()[-1] == ["transcribe", "--install", "--locale", "hi-IN"]
        assert t.apple_status("hi-IN").usable                # cache was cleared

    def test_unsupported(self, fake_helper):
        with pytest.raises(t.AppleUnavailable):
            t.install_apple_model("xx-YY")

    def test_failure(self, fake_helper):
        fake_helper.configure(install_rc=1)
        with pytest.raises(t.AppleError, match="exit 1"):
            t.install_apple_model("hi-IN")


# --- transcribe(): dispatch and the never-upload guarantee -------------------------------

class TestTranscribeDispatch:
    def test_auto_transcribes_on_this_mac(self, fake_helper, gemini_key, no_gemini, tmp_path):
        fake_helper.configure(text="on device words")
        assert t.transcribe(_wav(tmp_path / "c.wav"), role="me") == "on device words"
        assert t.last_backend() == "apple"

    def test_apple_never_uploads_when_unavailable(self, fake_helper, gemini_key, no_gemini, tmp_path, monkeypatch):
        monkeypatch.setenv(t.ENV_STT, "apple")
        fake_helper.configure(probe_rc=69)
        with pytest.raises(t.AppleUnavailable):
            t.transcribe(_wav(tmp_path / "c.wav"))
        assert fake_helper.transcribe_calls() == []

    def test_apple_never_uploads_when_the_model_is_released_mid_run(self, fake_helper, gemini_key, no_gemini,
                                                                   tmp_path, monkeypatch):
        monkeypatch.setenv(t.ENV_STT, "apple")
        fake_helper.configure(default_rc=75)
        with pytest.raises(t.AppleUnavailable):
            t.transcribe(_wav(tmp_path / "c.wav"))

    def test_apple_chunk_failure_is_not_retried_elsewhere(self, fake_helper, gemini_key, no_gemini, tmp_path):
        fake_helper.configure(default_rc=70)
        with pytest.raises(t.AppleChunkFailed):
            t.transcribe(_wav(tmp_path / "c.wav"))

    def test_auto_falls_back_to_gemini_when_the_model_disappears(self, fake_helper, gemini_key, tmp_path,
                                                                monkeypatch):
        assert t.apple_status().usable                       # cached: on this Mac
        # The model is released behind our back: the file gets 75, the re-probe "not installed".
        fake_helper.configure(clear_cache=False, default_rc=75, installed=[])
        used = []
        monkeypatch.setattr(t, "_transcribe_interactions", lambda p, m, r: used.append(m) or "cloud words")
        assert t.transcribe(_wav(tmp_path / "c.wav")) == "cloud words"
        assert used == [t.DEFAULT_GEMINI_MODEL] and t.last_backend() == t.DEFAULT_GEMINI_MODEL
        assert len(fake_helper.transcribe_calls()) == 1      # it did try on-device first

    def test_auto_without_on_device_or_key_is_unavailable(self, no_gemini, tmp_path):
        with pytest.raises(t.TranscriptionUnavailable):
            t.transcribe(_wav(tmp_path / "c.wav"))

    def test_gemini_choice_without_key_is_unavailable_not_an_error(self, fake_helper, tmp_path, monkeypatch):
        monkeypatch.setenv(t.ENV_STT, "gemini")
        with pytest.raises(t.TranscriptionUnavailable, match="API key"):
            t.transcribe(_wav(tmp_path / "c.wav"))

    def test_gemini_choice_uses_gemini(self, fake_helper, gemini_key, tmp_path, monkeypatch):
        monkeypatch.setenv(t.ENV_STT, "gemini")
        monkeypatch.setattr(t, "_transcribe_interactions", lambda p, m, r: "cloud")
        assert t.transcribe(_wav(tmp_path / "c.wav")) == "cloud"
        assert fake_helper.calls() == []

    def test_explicit_gemini_model_forces_gemini(self, fake_helper, gemini_key, tmp_path, monkeypatch):
        monkeypatch.setenv(t.ENV_STT, "apple")
        monkeypatch.setattr(t, "_transcribe_gemini", lambda p, m, i=None, r="them": "flash")
        assert t.transcribe(_wav(tmp_path / "c.wav"), model="gemini-2.5-flash") == "flash"
        assert t.last_backend() == "gemini-2.5-flash"
        assert fake_helper.calls() == []

    def test_explicit_apple_model(self, fake_helper, no_gemini, tmp_path, monkeypatch):
        monkeypatch.setenv(t.ENV_STT, "gemini")
        fake_helper.configure(text="forced local")
        assert t.transcribe(_wav(tmp_path / "c.wav"), model="apple") == "forced local"

    def test_no_key_does_not_fall_back_through_flash(self, tmp_path, monkeypatch, caplog):
        """A missing key is 'unavailable', not a preview-model hiccup."""
        with caplog.at_level("WARNING"):
            with pytest.raises(t.TranscriptionUnavailable):
                t.transcribe(_wav(tmp_path / "c.wav"), model="gemini-3.5-transcribe")
        assert "falling back" not in caplog.text


def test_engine_summary(fake_helper):
    s = t.engine_summary({"MEETING_CAPTURE_LOCALE": "en-IN"})
    assert s["engine"] == "apple" and s["ready"] and s["locale"] == "en-IN" and not s["uploads"]
    assert s["choice_label"] == "Automatic" and s["gemini_key"] is False
    assert "hi-IN" in s["apple"]["supported"]


def test_a_key_in_the_daemon_env_counts(fake_helper):
    """The CLI asks on the daemon's behalf: a key in the launchd plist's env is
    one the daemon will see (the CLI's own environment doesn't have it)."""
    env = {"MEETING_CAPTURE_STT": "gemini", "GOOGLE_API_KEY": "in-the-plist"}
    assert t.engine_summary(env)["gemini_key"] is True
    assert t.resolve_backend(env=env).ready
    assert t.engine_summary({})["gemini_key"] is False


# --- the default language follows the Mac; an upgrader from Gemini is told -----------------

@pytest.fixture
def mac(monkeypatch):
    """Set this Mac's language settings: mac("es-ES", region="es_ES")."""
    def _set(*langs, region=""):
        monkeypatch.setattr(t, "_mac_preferences", lambda: (tuple(langs), region))
    return _set


class TestMacLanguage:
    def test_reads_apple_languages_and_locale(self, monkeypatch):
        import plistlib, subprocess
        xml = plistlib.dumps({"AppleLanguages": ["hi-IN", "en-IN"], "AppleLocale": "en_IN@rg=inzzzz",
                              "Other": 1})
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, xml, b"")
        monkeypatch.setattr(t.subprocess, "run", fake_run)
        assert t._read_mac_preferences() == (("hi-IN", "en-IN"), "en_IN@rg=inzzzz")
        assert seen == [["/usr/bin/defaults", "export", "-g", "-"]]          # read-only

    def test_unreadable_defaults_mean_unknown(self, monkeypatch):
        def boom(cmd, **kw):
            raise FileNotFoundError("defaults")
        monkeypatch.setattr(t.subprocess, "run", boom)
        assert t._read_mac_preferences() is None

    def test_cached_but_a_failed_read_is_retried_sooner(self, monkeypatch):
        monkeypatch.setattr(t, "_mac_preferences", _real_mac_preferences)
        reads, clock = [], [1000.0]
        monkeypatch.setattr(t, "_mac_prefs_cache", [])
        monkeypatch.setattr(t.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(t, "_read_mac_preferences", lambda: reads.append(1) or (None if len(reads) == 1
                                                                                   else (("es-ES",), "")))
        assert t._mac_preferences() == ((), "")            # unreadable at login
        clock[0] += t.MAC_PREFS_RETRY_S + 1
        assert t._mac_preferences() == (("es-ES",), "")
        clock[0] += t.MAC_PREFS_TTL_S - 1
        assert t._mac_preferences() == (("es-ES",), "") and len(reads) == 2

    @pytest.mark.parametrize("langs, region, want", [
        (("es-ES",), "es_ES", ["es-ES"]),
        (("en",), "en_IN@rg=inzzzz", ["en", "en-IN"]),        # a bare language takes the region's country
        (("en-GB",), "en_IN", ["en-GB", "en-IN"]),
        (("en-US",), "hi_IN", ["en-US"]),                      # another language's region: not a candidate
        ((), "fr_FR", ["fr-FR"]),
        ((), "", []),
    ])
    def test_candidates(self, mac, langs, region, want):
        mac(*langs, region=region)
        assert t.mac_locale_candidates() == want


class TestDefaultLocale:
    def test_unknown_mac_language_is_en_us(self, fake_helper):
        assert t.locale_choice({}) == t.LocaleChoice("en-US", "default")

    def test_a_supported_mac_language_is_the_default(self, fake_helper, mac):
        mac("es-ES", region="es_ES")
        assert t.locale_choice({}) == t.LocaleChoice("es-ES", "mac", "es-ES")
        assert t.stt_locale({}) == "es-ES"

    def test_bare_english_takes_the_region(self, fake_helper, mac):
        mac("en", region="en_IN")
        assert t.stt_locale({}) == "en-IN"

    def test_a_script_subtag_is_dropped(self, fake_helper, mac):
        fake_helper.configure(supported=["en-US", "zh-CN"])
        mac("zh-Hans-CN", region="zh_CN")
        assert t.stt_locale({}) == "zh-CN"

    def test_the_setting_wins(self, fake_helper, mac):
        mac("es-ES")
        assert t.locale_choice({t.ENV_LOCALE: "fr_fr"}) == t.LocaleChoice("fr-FR", "setting")
        assert fake_helper.calls() == []

    def test_an_unsupported_mac_language_falls_back_and_says_so(self, fake_helper, mac):
        mac("pl-PL", region="pl_PL")
        lc = t.locale_choice({})
        assert lc == t.LocaleChoice("en-US", "default", "pl-PL") and lc.guessed
        assert "pl-PL" in lc.describe()

    def test_an_unsupported_english_variant_is_not_a_guess(self, fake_helper, mac):
        mac("en-NZ", region="en_NZ")
        lc = t.locale_choice({})
        assert lc.locale == "en-US" and not lc.guessed

    def test_without_on_device_support_it_is_en_us(self, fake_helper, mac):
        fake_helper.configure(old=True)
        mac("es-ES")
        assert t.locale_choice({}).locale == "en-US"

    def test_gemini_never_runs_the_helper_for_the_language(self, fake_helper, mac, gemini_key):
        mac("es-ES")
        b = t.resolve_backend(env={t.ENV_STT: "gemini", "GOOGLE_API_KEY": "k"})
        assert (b.engine, b.ready) == ("gemini", True) and fake_helper.calls() == []


UPGRADER = {"MEETING_CAPTURE_TRANSCRIBER": "gemini", "GOOGLE_API_KEY": "k"}   # a pre-on-device plist


class TestUpgradeFromGemini:
    def test_a_spanish_mac_transcribes_in_spanish(self, fake_helper, mac):
        """The review's case: a Gemini user on a Spanish Mac is no longer
        switched to the en-US model. Until es-ES is on the Mac, Gemini keeps
        going; once it is (the daemon downloads it), on this Mac in Spanish."""
        mac("es-ES", region="es_ES")
        s = t.engine_summary(dict(UPGRADER))
        assert (s["engine"], s["locale"], s["locale_source"]) == ("gemini", "es-ES", "mac")
        assert s["notice"] is None
        fake_helper.configure(installed=["en-US", "es-ES"])
        s = t.engine_summary(dict(UPGRADER))
        assert (s["engine"], s["locale"], s["ready"]) == ("apple", "es-ES", True)
        assert "on-device, es-ES — this Mac's language) instead of Gemini" in s["notice"]

    def test_a_mac_language_on_device_cannot_do_keeps_gemini(self, fake_helper, mac):
        mac("pl-PL", region="pl_PL")
        b = t.resolve_backend(env=dict(UPGRADER))
        assert (b.engine, b.ready) == ("gemini", True) and "pl-PL" in b.reason
        assert t.upgrade_notice(dict(UPGRADER)) is None

    def test_without_a_key_that_mac_still_transcribes_here_and_says_why(self, fake_helper, mac):
        mac("pl-PL", region="pl_PL")
        b = t.resolve_backend(env={})
        assert (b.engine, b.locale, b.ready) == ("apple", "en-US", True)
        assert "pl-PL" in b.reason and "meeting-capture language" in b.reason

    def test_on_device_only_stays_on_device(self, fake_helper, mac, gemini_key):
        mac("pl-PL")
        b = t.resolve_backend(env={t.ENV_STT: "apple", "GOOGLE_API_KEY": "k"})
        assert (b.engine, b.locale) == ("apple", "en-US")

    def test_an_english_mac_gets_the_notice(self, fake_helper):
        s = t.engine_summary(dict(UPGRADER))
        assert s["engine"] == "apple" and s["notice"].startswith(
            "transcription now runs on this Mac (on-device, en-US) instead of Gemini")
        assert "vocabulary" in s["notice"]

    @pytest.mark.parametrize("env", [
        {"MEETING_CAPTURE_TRANSCRIBER": "gemini"},                         # no key: never used Gemini
        dict(UPGRADER, MEETING_CAPTURE_STT="auto"),                       # picked auto
        dict(UPGRADER, MEETING_CAPTURE_STT="apple"),                      # picked on this Mac
        dict(UPGRADER, MEETING_CAPTURE_STT="gemini"),                     # picked Gemini
    ])
    def test_no_notice_once_an_engine_is_picked_or_without_a_key(self, fake_helper, env):
        assert t.engine_summary(env)["notice"] is None

    def test_no_notice_while_gemini_still_transcribes(self, fake_helper):
        fake_helper.configure(installed=[])
        assert t.engine_summary(dict(UPGRADER))["notice"] is None
