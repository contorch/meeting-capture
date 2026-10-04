"""`meeting-capture check [--json] [--request …]`: the permissions document,
read from a fake `sysaudio check` (never the real one: no prompt, no capture)."""
import json
import plistlib
import sys

import pytest

from meeting_capture import cli, permissions, recorder, tccspawn

FAKE = r'''#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "check.json")))
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if cfg.get("old"):
    sys.stderr.write("unknown arg: %s\n" % sys.argv[1]); sys.exit(1)
if sys.argv[1:3] != ["check", "--json"]:
    sys.stderr.write("unexpected %r\n" % sys.argv[1:]); sys.exit(1)
req = sys.argv[4] if len(sys.argv) > 4 else None
print("noise on stderr", file=sys.stderr)
print(json.dumps({"schema": "sysaudio.check/1", "screen_capture": cfg["screen"],
                  "microphone": cfg["mic"], "os": "26.0", "arch": "arm64", "requested": req}))
'''


def _fake(dirpath, screen="granted", mic="granted", old=False):
    dirpath.mkdir(parents=True, exist_ok=True)
    exe = dirpath / "sysaudio"
    exe.write_text(FAKE.replace("{python}", sys.executable))
    exe.chmod(0o755)
    (dirpath / "check.json").write_text(json.dumps({"screen": screen, "mic": mic, "old": old}))
    return exe


def _calls(exe):
    log = exe.parent / "calls.jsonl"
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def brew_sysaudio(tmp_path, monkeypatch):
    exe = _fake(tmp_path / "opt" / "bin", screen="not_granted", mic="not_determined")
    monkeypatch.setenv(recorder.SYSAUDIO_ENV_VAR, str(exe))
    monkeypatch.setenv("CONTORCH_CHANNEL", "brew")
    return exe


@pytest.fixture
def app_sysaudio(tmp_path, monkeypatch):
    app = tmp_path / "Contorch.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.contorch.labtest.app"}))
    exe = _fake(app / "Contents" / "Helpers", screen="not_granted", mic="denied")
    monkeypatch.setenv(recorder.SYSAUDIO_ENV_VAR, str(exe))
    monkeypatch.setenv("CONTORCH_CHANNEL", "app")
    return exe


def _json(capfd, *argv):
    assert cli.main(["check", "--json", *argv]) == 0
    out = capfd.readouterr().out
    assert len(out.splitlines()) == 1, out          # one document, nothing else on stdout
    return json.loads(out)


def test_schema_and_rows_in_brew(brew_sysaudio, capfd):
    doc = _json(capfd)
    assert doc["schema"] == "meeting-capture.permissions/1" and doc["ok"] is True
    assert doc["channel"] == "brew" and doc["requested"] is None
    assert doc["identity"]["helper"] == str(brew_sysaudio)
    assert doc["identity"]["subject"] == str(brew_sysaudio.resolve())   # a bare binary: its path
    rows = {r["id"]: r for r in doc["permissions"]}
    assert list(rows) == ["screen_audio", "microphone"]
    for r in rows.values():
        assert set(r) == {"id", "status", "required", "can_request", "hint", "settings_url"}
        assert r["settings_url"].startswith("x-apple.systempreferences:")
    assert rows["screen_audio"]["status"] == "not_granted" and rows["screen_audio"]["can_request"]
    assert str(brew_sysaudio) in rows["screen_audio"]["hint"] and "⌘⇧G" in rows["screen_audio"]["hint"]
    assert rows["microphone"]["status"] == "not_determined" and rows["microphone"]["can_request"]
    assert _calls(brew_sysaudio) == [["check", "--json"]]           # read-only: no --request


def test_app_channel_hints_name_contorch_and_subject_is_the_bundle(app_sysaudio, capfd):
    doc = _json(capfd)
    assert doc["identity"]["subject"] == "com.contorch.labtest.app"
    rows = {r["id"]: r for r in doc["permissions"]}
    assert "Contorch" in rows["screen_audio"]["hint"] and "sysaudio" not in rows["screen_audio"]["hint"]
    assert rows["microphone"]["status"] == "denied" and rows["microphone"]["can_request"] is False
    assert rows["microphone"]["hint"].startswith("Turn Contorch on in")
    assert "Privacy_Microphone" in rows["microphone"]["settings_url"]
    assert len([r for r in doc["permissions"] if r["id"] == "microphone"]) == 1


def test_granted_rows_have_no_hint(tmp_path, monkeypatch, capfd):
    exe = _fake(tmp_path / "b", screen="granted", mic="granted")
    monkeypatch.setenv(recorder.SYSAUDIO_ENV_VAR, str(exe))
    doc = _json(capfd)
    assert all(r["status"] == "granted" and r["hint"] is None for r in doc["permissions"])


@pytest.mark.parametrize("flag,arg", [("screen_audio", "screen"), ("microphone", "mic")])
def test_request_is_passed_to_sysaudio(brew_sysaudio, capfd, flag, arg):
    doc = _json(capfd, "--request", flag)
    assert doc["requested"] == flag
    assert _calls(brew_sysaudio) == [["check", "--json", "--request", arg]]


def test_an_old_sysaudio_is_reported_not_crashed(tmp_path, monkeypatch, capfd):
    exe = _fake(tmp_path / "b", old=True)
    monkeypatch.setenv(recorder.SYSAUDIO_ENV_VAR, str(exe))
    doc = _json(capfd)
    assert doc["ok"] is False and doc["error"]["code"] == "helper_too_old"
    assert {r["status"] for r in doc["permissions"]} == {"unknown"}


def test_no_sysaudio_at_all(monkeypatch, capfd):
    monkeypatch.setattr(recorder, "find_sysaudio", lambda: None)
    doc = _json(capfd)
    assert doc["ok"] is False and doc["error"]["code"] == "no_helper"
    assert doc["identity"] == {"helper": None, "subject": None}
    assert cli.main(["check"]) == 1


def test_line_in_needs_no_screen_permission(brew_sysaudio, monkeypatch, capfd):
    monkeypatch.setenv("MEETING_CAPTURE_SOURCE", "linein")
    rows = {r["id"]: r for r in _json(capfd)["permissions"]}
    assert rows["screen_audio"]["required"] is False and rows["microphone"]["required"] is True
    assert rows["microphone"]["can_request"] is False      # brew: sysaudio's prompt isn't line-in's


def test_mic_off_makes_the_microphone_optional(brew_sysaudio, monkeypatch, capfd):
    monkeypatch.setenv(recorder.MIC_ENV_VAR, "0")
    rows = {r["id"]: r for r in _json(capfd)["permissions"]}
    assert rows["microphone"]["required"] is False and rows["screen_audio"]["required"] is True


def test_the_helper_runs_as_its_own_responsible_process(brew_sysaudio, monkeypatch, capfd):
    seen = []
    real = tccspawn.run
    monkeypatch.setattr(tccspawn, "run", lambda cmd, **kw: seen.append(cmd) or real(cmd, **kw))
    _json(capfd)
    assert seen == [[str(brew_sysaudio), "check", "--json"]]


def test_text_mode_renders_the_same_document(brew_sysaudio, capfd):
    assert cli.main(["check"]) == 0
    out = capfd.readouterr().out
    assert f"sysaudio:          {brew_sysaudio}" in out
    assert "Screen & System Audio Recording:" in out and "not granted" in out
    assert "⌘⇧G" in out


def test_tcc_subject_is_the_outermost_app(tmp_path):
    outer = tmp_path / "Outer.app"
    inner = outer / "Contents" / "Helpers" / "Inner.app" / "Contents" / "MacOS"
    inner.mkdir(parents=True)
    (outer / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.outer"}))
    (inner.parent / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.inner"}))
    assert tccspawn.tcc_subject(inner / "helper") == "com.example.outer"
    bare = tmp_path / "bin" / "sysaudio"
    assert tccspawn.tcc_subject(bare) == str(bare.resolve())
