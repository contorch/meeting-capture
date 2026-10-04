"""`meeting-capture stt --json`: the contract pipeline-monitor reads instead of
re-implementing transcriber.py's rules (README "Contract"). Against the fake
helper; the real sysaudio is never run."""
from __future__ import annotations

import json
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from meeting_capture import cli
from meeting_capture import transcriber as t

# Every field of schema 1. Removing one, or changing what one means, is a new
# schema (cli.STT_JSON_SCHEMA) and a pipeline-monitor change; adding one isn't.
SCHEMA_1 = {
    "schema", "version", "agent_installed",
    "choice", "choice_label", "engine", "engine_label", "ready", "reason",
    "locale", "locale_source", "locale_why", "locale_guessed", "mac_language",
    "uploads", "gemini_fallback", "gemini_key", "notice", "live", "apple",
    "needs_model", "install_hint", "on_device_hint", "on_device_only_hint", "may_upload",
    "on_device_line", "on_device_for_mac_language",
}
APPLE_FIELDS = {"available", "usable", "installable", "installed", "reason", "locale", "supported",
                "installed_locales", "exit_code", "os", "arch", "helper"}


def _write_plist(path: Path, env: dict) -> None:
    path.write_bytes(plistlib.dumps({"Label": "com.contorch.meeting-capture", "EnvironmentVariables": env}))


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """An installed agent whose plist env the test sets: agent({...})."""
    plist = tmp_path / "agent.plist"
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    relaunches = []
    monkeypatch.setattr(cli, "_relaunch", lambda: relaunches.append(1))

    def _set(env: dict | None = None):
        _write_plist(plist, {"PATH": "/usr/bin", **(env or {})})
        relaunches.clear()
        return relaunches
    _set()
    return _set


@pytest.fixture
def mac(monkeypatch):
    def _set(*langs, region=""):
        monkeypatch.setattr(t, "_mac_preferences", lambda: (tuple(langs), region))
    return _set


def stt_json(capsys) -> dict:
    t.clear_apple_status_cache()            # each `stt --json` is a fresh process
    assert cli.main(["stt", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.endswith("}\n")      # one object, one line, nothing else
    return json.loads(out)


def test_the_shape_is_schema_1(fake_helper, agent, capsys):
    s = stt_json(capsys)
    assert set(s) == SCHEMA_1 and s["schema"] == cli.STT_JSON_SCHEMA == 1
    assert set(s["live"]) == {"requested", "active", "blocker"}
    assert APPLE_FIELDS <= set(s["apple"])
    assert s["version"] and s["agent_installed"] is True


def test_on_this_mac(fake_helper, agent, capsys):
    s = stt_json(capsys)
    assert (s["engine"], s["ready"], s["locale"], s["uploads"]) == ("apple", True, "en-US", False)
    assert s["live"] == {"requested": False, "active": False, "blocker": None}
    assert s["needs_model"] is False and s["install_hint"] is None and s["on_device_hint"] is None
    assert s["gemini_key"] is False and s["gemini_fallback"] is False


def test_a_dutch_mac_with_a_key_uploads_and_says_so(fake_helper, agent, mac, key_file, capsys):
    """The case pipeline-monitor's mirror got wrong: Dutch isn't an on-device
    language, there is a key and nobody picked an engine, so auto transcribes
    with Gemini — the JSON says uploads, not "never leaves this Mac"."""
    mac("nl-NL", region="nl_NL")
    s = stt_json(capsys)
    assert (s["choice"], s["engine"], s["ready"], s["uploads"]) == ("auto", "gemini", True, True)
    assert (s["locale"], s["locale_source"], s["locale_guessed"], s["mac_language"]) == \
        ("en-US", "default", True, "nl-NL")
    assert s["apple"]["usable"] and s["needs_model"] is False and s["install_hint"] is None
    assert s["on_device_hint"] == "meeting-capture language en-US"
    agent({"MEETING_CAPTURE_STT": "gemini"})
    assert stt_json(capsys)["on_device_hint"] == "meeting-capture stt auto --language en-US"


def test_the_on_device_hint_does_what_it_says(fake_helper, agent, mac, key_file, capsys):
    mac("nl-NL", region="nl_NL")
    for env in ({}, {"MEETING_CAPTURE_STT": "gemini"}, {"MEETING_CAPTURE_STT": "apple"}):
        relaunches = agent(env)
        hint = stt_json(capsys)["on_device_hint"]
        argv = shlex.split(hint)
        assert argv[0] == "meeting-capture" and cli.main(argv[1:]) == 0 and relaunches == [1]
        capsys.readouterr()
        s = stt_json(capsys)
        assert (s["engine"], s["ready"], s["uploads"], s["on_device_hint"]) == ("apple", True, False, None)


def test_without_a_key_the_dutch_mac_transcribes_here_in_english(fake_helper, agent, mac, capsys):
    mac("nl-NL", region="nl_NL")
    s = stt_json(capsys)
    assert (s["engine"], s["locale"], s["uploads"]) == ("apple", "en-US", False)
    assert "nl-NL" in s["reason"]


def test_needs_model_and_the_install_hint(fake_helper, agent, capsys):
    fake_helper.configure(installed=[])
    s = stt_json(capsys)
    assert (s["engine"], s["ready"], s["needs_model"]) == ("none", False, True)
    assert s["install_hint"] == s["on_device_hint"] == "meeting-capture language en-US"
    assert s["apple"]["installable"] and not s["apple"]["usable"]
    argv = shlex.split(s["install_hint"])
    assert cli.main(argv[1:]) == 0
    capsys.readouterr()
    s = stt_json(capsys)
    assert (s["engine"], s["needs_model"], s["install_hint"]) == ("apple", False, None)


def test_a_missing_model_with_a_key_uploads_meanwhile(fake_helper, agent, mac, key_file, capsys):
    mac("es-ES", region="es_ES")                    # supported, not installed yet
    s = stt_json(capsys)
    assert (s["engine"], s["uploads"], s["needs_model"]) == ("gemini", True, True)
    assert s["install_hint"] == "meeting-capture language es-ES"


def test_gemini_chosen_needs_no_model(fake_helper, agent, key_file, capsys):
    fake_helper.configure(installed=[])
    agent({"MEETING_CAPTURE_STT": "gemini"})
    s = stt_json(capsys)
    assert (s["engine"], s["uploads"], s["needs_model"]) == ("gemini", True, False)
    assert s["on_device_hint"] == "meeting-capture stt auto"


def test_where_on_device_cannot_run(fake_helper, agent, capsys):
    fake_helper.configure(old=True)
    s = stt_json(capsys)
    assert s["engine"] == "none" and not s["apple"]["available"] and "predates" in s["apple"]["reason"]
    assert s["needs_model"] is False and s["on_device_hint"] is None


def test_with_a_key_auto_says_gemini_may_take_over(fake_helper, agent, key_file, capsys):
    s = stt_json(capsys)
    assert (s["engine"], s["uploads"], s["gemini_fallback"]) == ("apple", False, True)
    agent({"MEETING_CAPTURE_STT": "apple"})
    assert stt_json(capsys)["gemini_fallback"] is False


def test_may_upload_is_the_privacy_answer(fake_helper, agent, key_file, capsys):
    """The review's case: stt unset, a key in the key file, an English Mac,
    on-device ready. Nothing uploads now, but transcribe() hands a chunk to
    Gemini the moment on-device fails, so it is not "never leaves this Mac"."""
    s = stt_json(capsys)
    assert (s["engine"], s["uploads"], s["live"]["active"]) == ("apple", False, False)
    assert s["gemini_fallback"] is True and s["may_upload"] is True
    assert s["on_device_hint"] is None and s["on_device_only_hint"] == "meeting-capture stt apple"
    agent({"MEETING_CAPTURE_STT": "apple"})                     # on this Mac only
    s = stt_json(capsys)
    assert (s["gemini_fallback"], s["may_upload"], s["on_device_only_hint"]) == (False, False, None)
    assert s["on_device_hint"] == "meeting-capture stt auto"   # on this Mac, Gemini as backup
    agent({"MEETING_CAPTURE_STT": "apple", "MEETING_CAPTURE_MODE": "live"})
    assert stt_json(capsys)["may_upload"] is False             # apple refuses live
    agent({"MEETING_CAPTURE_MODE": "live"})
    assert stt_json(capsys)["may_upload"] is True


def test_may_upload_without_a_key_is_false(fake_helper, agent, capsys):
    s = stt_json(capsys)
    assert (s["engine"], s["gemini_fallback"], s["may_upload"]) == ("apple", False, False)
    agent({"MEETING_CAPTURE_STT": "gemini"})                   # gemini, no key: would upload once one exists
    assert stt_json(capsys)["may_upload"] is True


@pytest.mark.parametrize("env, uploads", [({"MEETING_CAPTURE_STT": "apple"}, False), ({}, True)])
def test_may_upload_matches_what_transcribe_does(fake_helper, agent, key_file, monkeypatch, capsys,
                                                 env, uploads):
    """Pin the field to the daemon's behaviour, not to a formula: on-device
    ready, then the helper breaks (a macOS update, a removed model, a failed
    fork). With may_upload false transcribe() keeps the chunk; with it true
    the chunk goes to Gemini with nobody changing a setting."""
    agent(env)
    s = stt_json(capsys)
    assert (s["engine"], s["ready"], s["uploads"], s["may_upload"]) == ("apple", True, False, uploads)
    for k, v in env.items():                       # the daemon runs with its plist env
        monkeypatch.setenv(k, v)
    sent = []
    monkeypatch.setattr(t, "_gemini", lambda *a, **k: sent.append(1) or "gemini text")

    def broken(*a, **k):
        fake_helper.configure(old=True)            # the re-probe fails too
        t.clear_apple_status_cache()
        raise t.AppleUnavailable("helper vanished")
    monkeypatch.setattr(t, "_transcribe_apple", broken)
    t.clear_apple_status_cache()
    try:
        t.transcribe(Path("/nonexistent.wav"))
    except (t.AppleUnavailable, t.TranscriptionUnavailable):
        pass
    assert bool(sent) is uploads


def test_the_on_device_only_hint_does_what_it_says(fake_helper, agent, key_file, capsys):
    for env, installed in (({}, ["en-US"]), ({"MEETING_CAPTURE_STT": "gemini"}, []), ({}, [])):
        fake_helper.configure(installed=installed)
        relaunches = agent(env)
        argv = shlex.split(stt_json(capsys)["on_device_only_hint"])
        assert argv[:3] == ["meeting-capture", "stt", "apple"]
        assert cli.main(argv[1:]) == 0 and relaunches == [1]
        capsys.readouterr()
        s = stt_json(capsys)
        assert (s["engine"], s["ready"], s["may_upload"], s["on_device_only_hint"]) == ("apple", True, False, None)


def test_the_text_hedges_like_the_json(fake_helper, agent, key_file, capsys):
    assert cli.main(["stt"]) == 0
    text = capsys.readouterr().out
    assert "nothing is uploaded" not in text and "only if on-device transcription stops working" in text
    agent({"MEETING_CAPTURE_STT": "apple"})
    assert cli.main(["stt"]) == 0
    assert "On this Mac — nothing is uploaded" in capsys.readouterr().out


@pytest.mark.parametrize("env, active, blocker", [
    ({"MEETING_CAPTURE_MODE": "live"}, True, None),                           # auto streams too
    ({"MEETING_CAPTURE_MODE": "live", "MEETING_CAPTURE_STT": "apple"}, False, "never uploads"),
    ({"MEETING_CAPTURE_MODE": "live", "MEETING_CAPTURE_SOURCE": "linein"}, False, "line-in"),
])
def test_live_mode(fake_helper, agent, key_file, capsys, env, active, blocker):
    agent(env)
    s = stt_json(capsys)
    assert s["live"]["requested"] is True and s["live"]["active"] is active
    assert (s["live"]["blocker"] is None) if blocker is None else (blocker in s["live"]["blocker"])
    assert s["engine"] == "apple" and s["uploads"] is False      # batch is separate from live


def test_a_key_only_in_the_callers_shell_is_not_the_daemons(fake_helper, agent, gemini_key, capsys):
    """pipeline-monitor runs `stt --json` from wherever it runs (a terminal
    with GOOGLE_API_KEY exported); the launchd daemon never sees that key."""
    agent({"MEETING_CAPTURE_MODE": "live"})
    s = stt_json(capsys)
    assert s["gemini_key"] is False
    assert s["live"]["active"] is False and "no Google API key" in s["live"]["blocker"]
    agent({"MEETING_CAPTURE_MODE": "live", "GOOGLE_API_KEY": "in-the-plist"})
    assert stt_json(capsys)["live"]["active"] is True


def test_without_an_agent_it_describes_this_shell(fake_helper, gemini_key, capsys):
    s = stt_json(capsys)
    assert s["agent_installed"] is False and s["gemini_key"] is True
    assert s["live"]["requested"] is False


def test_one_probe_per_language(fake_helper, agent, mac, capsys):
    stt_json(capsys)
    assert [c for c in fake_helper.calls() if "--probe" in c] == [
        ["transcribe", "--probe", "--locale", "en-US"]]
    fake_helper.calls_path.unlink()
    mac("es-ES", region="es_ES")
    stt_json(capsys)                                    # the Mac's language: its probe lists the rest
    assert [c[-1] for c in fake_helper.calls() if "--probe" in c] == ["es-ES"]
    fake_helper.calls_path.unlink()
    mac("pl-PL", region="pl_PL")
    stt_json(capsys)                                    # not on-device: then en-US's status
    assert [c[-1] for c in fake_helper.calls() if "--probe" in c] == ["pl-PL", "en-US"]
    fake_helper.calls_path.unlink()
    agent({"MEETING_CAPTURE_STT": "gemini", "MEETING_CAPTURE_LOCALE": "fr-FR"})
    stt_json(capsys)
    assert [c[-1] for c in fake_helper.calls() if "--probe" in c] == ["fr-FR"]


def test_text_and_json_come_from_the_same_function(fake_helper, agent, monkeypatch, capsys):
    real = cli.transcription_summary
    calls = []

    def spy():
        calls.append(1)
        return dict(real(), reason="SENTINEL reason", needs_model=True,
                    install_hint="meeting-capture language xx-XX")
    monkeypatch.setattr(cli, "transcription_summary", spy)
    assert cli.main(["stt"]) == 0
    text = capsys.readouterr().out
    assert "why:       SENTINEL reason" in text and "`meeting-capture language xx-XX`" in text
    assert cli.main(["stt", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "SENTINEL reason"
    assert calls == [1, 1]


def test_json_cannot_be_combined_with_a_change(fake_helper, agent, capsys):
    relaunches = agent()
    assert cli.main(["stt", "apple", "--json"]) == 2
    assert cli.main(["stt", "--json", "--language", "hi-IN"]) == 2
    assert relaunches == [] and capsys.readouterr().out == ""


def test_a_failure_is_exit_1_with_nothing_on_stdout(fake_helper, agent, monkeypatch, capsys):
    def boom():
        raise ValueError("broken")
    monkeypatch.setattr(cli, "transcription_summary", boom)
    assert cli.main(["stt", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "broken" in captured.err


def test_the_json_is_ascii(fake_helper, agent, mac, key_file, capsys):
    mac("nl-NL")
    t.clear_apple_status_cache()
    cli.main(["stt", "--json"])
    out = capsys.readouterr().out
    assert out.isascii() and "—" in json.loads(out)["locale_why"]


# --- setting it from another program -------------------------------------------------------

def test_stt_with_a_language_sets_both_in_one_restart(fake_helper, agent, capsys):
    relaunches = agent({"MEETING_CAPTURE_STT": "gemini"})
    assert cli.main(["stt", "auto", "--language", "hi"]) == 0
    env = plistlib.loads(cli.LAUNCHD_PLIST.read_bytes())["EnvironmentVariables"]
    assert (env["MEETING_CAPTURE_STT"], env["MEETING_CAPTURE_LOCALE"]) == ("auto", "hi-IN")
    assert relaunches == [1]


def test_download_progress_streams_as_stdout_lines(fake_helper, agent, capsys):
    ticks = [f"de-DE model download {p}%" for p in (0, 3, 9, 10, 15, 47, 99, 100)]
    fake_helper.configure(supported=["de-DE", "en-US"], installed=["en-US"],
                          install_stderr=["downloading the on-device speech model for de-DE…",
                                          "reserved the de-DE model for this app", *ticks,
                                          "de-DE model download 100%, installed"])
    assert cli.main(["language", "de-DE"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Downloading the on-device speech model for de-DE from Apple (one time)…"
    assert out[1:6] == ["  reserved the de-DE model for this app", "  de-DE model download 0%",
                        "  de-DE model download 10%", "  de-DE model download 47%",
                        "  de-DE model download 99%"]
    assert out[6] == "  de-DE model download 100%, installed"
    assert out[7].startswith("transcription: Automatic (stt=auto), language de-DE")


def test_refusals_are_exit_1_on_stderr_with_the_plist_untouched(fake_helper, agent, capsys):
    agent()
    before = cli.LAUNCHD_PLIST.read_bytes()
    assert cli.main(["language", "klingon"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "unsupported language" in captured.err
    assert cli.LAUNCHD_PLIST.read_bytes() == before


def test_as_a_separate_process(fake_helper, tmp_path):
    """What pipeline-monitor actually runs: a fresh process, a scratch HOME
    with an agent plist, stdout piped. stdout must be the JSON and nothing
    else (no import-time chatter)."""
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    _write_plist(home / "Library" / "LaunchAgents" / "com.contorch.meeting-capture.plist",
                 {"MEETING_CAPTURE_LOCALE": "en-GB", "MEETING_CAPTURE_MODE": "live"})
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "MEETING_CAPTURE_TRANSCRIBE_BIN": str(fake_helper.path), "GOOGLE_API_KEY": "shell-only"}
    r = subprocess.run([sys.executable, "-m", "meeting_capture.cli", "stt", "--json"],
                       capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert (s["engine"], s["locale"], s["agent_installed"], s["gemini_key"]) == ("apple", "en-GB", True, False)
    assert s["live"]["active"] is False
    assert not (home / ".meeting-capture").exists()                   # read-only: creates nothing


# --- the on-device line setup shows as it is (#26 follow-up) ----------------------------------

def test_on_device_line_on_a_mac_whose_language_runs_on_device(fake_helper, agent, mac, capsys):
    mac("en-IN", region="en_IN")
    s = stt_json(capsys)
    assert s["on_device_line"].startswith("This Mac can transcribe meetings itself") and "en-IN" in s["on_device_line"]
    assert s["on_device_for_mac_language"] is True


def test_on_device_line_never_claims_a_language_the_mac_doesnt_speak(fake_helper, agent, mac, key_file, capsys):
    """A Polish Mac with a key: auto keeps Gemini (it detects the language),
    so the line must not say "This Mac can transcribe meetings" first."""
    mac("pl-PL", region="pl_PL")
    s = stt_json(capsys)
    assert s["engine"] == "gemini" and s["locale_guessed"] is True and s["apple"]["usable"] is True
    assert "can transcribe meetings itself" not in s["on_device_line"]
    assert "pl-PL" in s["on_device_line"] and "meeting-capture language en-US" in s["on_device_line"]
    assert s["on_device_for_mac_language"] is False


def test_on_device_line_where_it_cant_run(fake_helper, agent, capsys):
    fake_helper.configure(probe_rc=69, reason="needs macOS 26 or later on Apple silicon", supported=[])
    s = stt_json(capsys)
    assert s["on_device_line"].startswith("On-device transcription isn't available on this Mac: needs macOS 26")
    assert s["on_device_for_mac_language"] is False


def test_on_device_line_when_the_model_still_has_to_download(fake_helper, agent, mac, capsys):
    mac("hi-IN", region="en_IN")
    s = stt_json(capsys)
    assert "once Apple's speech model is downloaded" in s["on_device_line"] and "hi-IN" in s["on_device_line"]


# --- stt --check-key ---------------------------------------------------------------------------

class _FakeModels:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def get(self, model):
        self.calls.append(model)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return {"name": model}


@pytest.fixture
def genai_stub(monkeypatch):
    """google.genai.Client stubbed: no network, records the key it got."""
    from google import genai
    seen = {}

    def install(outcome):
        models = _FakeModels(outcome)

        class Client:
            def __init__(self, api_key=None, http_options=None):
                seen["key"] = api_key
                seen["attempts"] = http_options.retry_options.attempts
                self.models = models
        monkeypatch.setattr(genai, "Client", Client)
        return models
    install.seen = seen
    return install


def _client_error(code, message):
    from google.genai import errors
    return errors.ClientError(code, {"error": {"code": code, "message": message, "status": "X"}})


def test_check_key_accepted(fake_helper, agent, key_file, genai_stub, capsys):
    models = genai_stub(None)
    t.clear_apple_status_cache()
    assert cli.main(["stt", "--json", "--check-key"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["key_check"] == {"key": "accepted", "message": None}
    assert models.calls == [t.KEY_CHECK_MODEL] and genai_stub.seen["key"] == key_file
    assert genai_stub.seen["attempts"] == 1                       # no retry loop on a 429


def test_check_key_rejected(fake_helper, agent, key_file, genai_stub, capsys):
    genai_stub(_client_error(400, "API key not valid. Please pass a valid API key."))
    assert cli.main(["stt", "--check-key"]) == 1
    assert "REJECTED" in capsys.readouterr().out
    t.clear_apple_status_cache()
    assert cli.main(["stt", "--json", "--check-key"]) == 0
    assert json.loads(capsys.readouterr().out)["key_check"]["key"] == "rejected"


def test_check_key_missing_never_calls_google(fake_helper, agent, genai_stub, gemini_key, capsys):
    """A key only in the caller's shell doesn't count: the recorder never sees it."""
    models = genai_stub(None)
    t.clear_apple_status_cache()
    assert cli.main(["stt", "--json", "--check-key"]) == 0
    assert json.loads(capsys.readouterr().out)["key_check"]["key"] == "missing"
    assert models.calls == []


@pytest.mark.parametrize("outcome,expect", [
    (OSError("network is unreachable"), "unreachable"),
    ("500", "unreachable"),
    ("429", "accepted"),
    ("404", "accepted"),
])
def test_check_key_other_outcomes(fake_helper, agent, key_file, genai_stub, capsys, outcome, expect):
    from google.genai import errors
    if outcome == "500":
        outcome = errors.ServerError(500, {"error": {"code": 500, "message": "internal", "status": "X"}})
    elif outcome in ("429", "404"):
        outcome = _client_error(int(outcome), "quota" if outcome == "429" else "model not found")
    genai_stub(outcome)
    t.clear_apple_status_cache()
    assert cli.main(["stt", "--json", "--check-key"]) == 0
    assert json.loads(capsys.readouterr().out)["key_check"]["key"] == expect


def test_without_check_key_there_is_no_key_check_and_no_call(fake_helper, agent, key_file, genai_stub, capsys):
    models = genai_stub(None)
    s = stt_json(capsys)
    assert "key_check" not in s and models.calls == []


def test_version_flag(capsys):
    from meeting_capture import __version__
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"meeting-capture {__version__}"
