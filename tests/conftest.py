import json
import sys

import pytest

# A stand-in for `sysaudio transcribe` that follows the helper contract
# (transcriber.py docstring). Behaviour comes from helper.json beside it, so a
# test can change it mid-way; every invocation is logged to calls.jsonl.
FAKE_HELPER = r'''#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
cfg_path = os.path.join(here, "helper.json")
cfg = json.load(open(cfg_path))
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if not args or args[0] != "transcribe" or cfg.get("old"):
    sys.stderr.write("unknown arg: %s\n" % (args[0] if args else ""))
    sys.exit(1)
args = args[1:]
locale = "en-US"
if "--locale" in args:
    i = args.index("--locale"); locale = args[i + 1]; del args[i:i + 2]
supported, installed = cfg["supported"], cfg["installed"]
def out(o):
    print(json.dumps(o)); sys.stdout.flush()
if "--probe" in args:
    rc = cfg.get("probe_rc")
    if rc is None:
        rc = 69 if locale not in supported else (0 if locale in installed else 75)
    time.sleep(cfg.get("probe_sleep", 0))
    out({"available": rc == 0, "reason": cfg.get("reason", ""), "os": "26.0", "arch": "arm64",
         "locale": locale, "installed": locale in installed, "supported": supported,
         "installed_locales": installed})
    sys.exit(rc)
if "--install" in args:
    for line in cfg.get("install_stderr", []):   # download progress, as the real helper logs it
        sys.stderr.write(line + "\n"); sys.stderr.flush()
    rc = cfg.get("install_rc")
    if rc is None:
        rc = 0 if locale in supported else 69
    if rc == 0 and locale not in installed:
        cfg["installed"] = installed + [locale]
        json.dump(cfg, open(cfg_path, "w"))
    out({"installed": rc == 0, "locale": locale, "seconds": 0.1})
    sys.exit(rc)
path = args[-1]
time.sleep(cfg.get("sleeps", {}).get(os.path.basename(path), cfg.get("sleep", 0)))
if cfg.get("signal"):                      # the helper crashes
    os.kill(os.getpid(), cfg["signal"])
rc = cfg.get("transcribe_rc", {}).get(os.path.basename(path), cfg.get("default_rc", 0))
if rc == 0 and cfg.get("no_json"):         # exits 0 without a transcript
    sys.exit(0)
if rc == 0:
    text = cfg.get("texts", {}).get(os.path.basename(path), cfg.get("text", "hello from this mac"))
    out({"text": text, "segments": [{"start": 0.0, "end": 1.0, "text": text, "confidence": 0.9}],
         "locale": locale, "ms": 5})
elif rc != 1:
    out({"error": "fake failure %d" % rc})
sys.stderr.write("fake helper exit %d\n" % rc)
sys.exit(rc)
'''

SUPPORTED = ["en-AU", "en-GB", "en-IN", "en-US", "es-ES", "es-MX", "fr-FR", "hi-IN", "mul-IN", "ur-IN"]


class FakeHelper:
    def __init__(self, root):
        self.root = root
        self.path = root / "sysaudio"
        self.cfg_path = root / "helper.json"
        self.calls_path = root / "calls.jsonl"

    def configure(self, clear_cache=True, **updates):
        cfg = json.loads(self.cfg_path.read_text()) if self.cfg_path.exists() else {}
        cfg.update(updates)
        self.cfg_path.write_text(json.dumps(cfg))
        if clear_cache:
            from meeting_capture import transcriber
            transcriber.clear_apple_status_cache()
        return self

    def calls(self) -> list:
        if not self.calls_path.exists():
            return []
        return [json.loads(l) for l in self.calls_path.read_text().splitlines() if l.strip()]

    def transcribe_calls(self) -> list:
        return [c for c in self.calls() if "--probe" not in c and "--install" not in c]


@pytest.fixture
def fake_helper(tmp_path, monkeypatch):
    """A contract-following fake `sysaudio transcribe`: English installed,
    hi-IN and others installable."""
    root = tmp_path / "helper"
    root.mkdir()
    h = FakeHelper(root)
    h.path.write_text(FAKE_HELPER.replace("{python}", sys.executable))
    h.path.chmod(0o755)
    h.configure(supported=SUPPORTED, installed=["en-AU", "en-GB", "en-IN", "en-US"])
    from meeting_capture import transcriber
    monkeypatch.setenv(transcriber.ENV_TRANSCRIBE_BIN, str(h.path))
    return h


@pytest.fixture
def gemini_key(monkeypatch):
    """A key in this process's environment: what the daemon itself reads. The
    CLI asking on an installed agent's behalf doesn't count it (the launchd
    daemon never sees the shell) — use key_file for that."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    return "test-key"


@pytest.fixture
def key_file():
    """A key in ~/.config/google/key (a stand-in): seen by the daemon and by
    the CLI asking on its behalf."""
    from meeting_capture import transcriber
    transcriber.GEMINI_KEY_FILE.write_text("test-key-file\n")
    return "test-key-file"


@pytest.fixture(autouse=True)
def _isolated_transcript_db(tmp_path, monkeypatch):
    """Never touch the real ~/.context-orchestrator/context.db (or the
    daemon's state.json) from tests."""
    from meeting_capture import paths, store
    monkeypatch.setenv("CO_DB_PATH", str(tmp_path / "context.db"))
    monkeypatch.setattr(paths, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(store, "PENDING_FILE", tmp_path / "unsaved-lines.jsonl")


def _no_launchctl(*args):
    """Read-only questions answer "not loaded"; anything else fails the test."""
    import subprocess
    if args and args[0] in ("list", "print"):
        return subprocess.CompletedProcess(["launchctl", *args], 113, "", "Could not find service")
    pytest.fail(f"a test tried to run launchctl {' '.join(args)}")


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """The recorder's settings and agent live in tmp_path: never the real
    ~/.meeting-capture/env or ~/Library/LaunchAgents, never the real launchd.
    No MEETING_CAPTURE_* / CONTORCH_* from the developer's shell, and
    whatever a test's config.apply() puts into os.environ is undone."""
    import os
    from meeting_capture import config, paths, supervisor
    saved = dict(os.environ)
    for k in list(os.environ):
        if k.startswith(("MEETING_CAPTURE_", "CONTORCH_")):
            monkeypatch.delenv(k)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(paths, "ENV_FILE", state / "env")
    monkeypatch.setattr(paths, "ENV_LOCK", state / "env.lock")
    monkeypatch.setattr(paths, "PID_FILE", state / "daemon.pid")
    monkeypatch.setattr(paths, "LAUNCHD_PLIST", tmp_path / "no-agent.plist")
    monkeypatch.setattr(paths, "HOME", tmp_path / "home")
    monkeypatch.setattr(supervisor, "_launchctl", _no_launchctl)
    config._injected.clear()
    yield
    config._injected.clear()
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _isolated_transcription(tmp_path, monkeypatch):
    """Never run the real sysaudio (or Apple's speech model), never see the
    developer's Gemini key or transcription settings. Tests that need a
    helper point MEETING_CAPTURE_TRANSCRIBE_BIN at a fake one."""
    from meeting_capture import transcriber
    monkeypatch.setenv(transcriber.ENV_TRANSCRIBE_BIN, str(tmp_path / "no-transcribe-helper"))
    for var in (transcriber.ENV_STT, transcriber.ENV_LOCALE, transcriber.ENV_LEGACY_TRANSCRIBER,
                "GOOGLE_API_KEY", "GEMINI_API_KEY", "MEETING_CAPTURE_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(transcriber, "GEMINI_KEY_FILE", tmp_path / "no-gemini-key")
    # Nor the developer's Mac language (it picks the default on-device locale).
    monkeypatch.setattr(transcriber, "_mac_preferences", lambda: ((), ""))
    transcriber.clear_apple_status_cache()
    yield
    transcriber.clear_apple_status_cache()


@pytest.fixture(autouse=True)
def _no_real_sysaudio(monkeypatch):
    """Never find (and so never run) an installed sysaudio — e.g. Homebrew's on
    PATH — or one a developer's shell points at. Tests that need one set
    MEETING_CAPTURE_SYSAUDIO to a fake."""
    import shutil
    from meeting_capture import recorder
    monkeypatch.delenv(recorder.SYSAUDIO_ENV_VAR, raising=False)
    monkeypatch.delenv(recorder.AUDIOTEE_ENV_VAR, raising=False)
    monkeypatch.delenv("CONTORCH_CHANNEL", raising=False)
    monkeypatch.delenv("CONTORCH_OP", raising=False)
    real_which = shutil.which
    monkeypatch.setattr(recorder.shutil, "which",
                        lambda name, *a, **k: None if name in ("sysaudio", "audiotee") else real_which(name, *a, **k))
