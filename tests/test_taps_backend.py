"""The capture backend (sysaudio --backend taps|sck) from Python's side: which
one a session uses, the argv, and a whole capture session against a fake
sysaudio that speaks the real stdout protocol. Never the real sysaudio: no
prompt, no capture."""
import json
import sys
import time

import pytest

from meeting_capture import config, mic, recorder

# ---------------------------------------------------------------------------
# choose_backend: the rule, as a table

NEW = ["sck", "taps"]


def doc(screen="granted", audio="granted", backends=NEW):
    d = {"schema": "sysaudio.check/1", "screen_capture": screen, "microphone": "granted"}
    if backends is not None:
        d.update(system_audio=audio, backends=backends)
    return d


@pytest.mark.parametrize("setting,check,major,selected,flag", [
    # auto
    ("auto", doc(audio="granted"), 26, "taps", True),
    ("auto", doc(screen="granted", audio="not_determined"), 26, "sck", True),     # keeps today's grant
    ("auto", doc(screen="not_granted", audio="not_determined"), 15, "taps", True),  # fresh: narrower prompt
    ("auto", doc(screen="not_granted", audio="unknown"), 26, "taps", True),
    ("auto", doc(screen="not_granted", audio="denied"), 26, "taps", True),          # fails loudly, names taps
    ("auto", doc(screen="granted", audio="denied"), 26, "sck", True),
    ("auto", doc(audio="granted"), 14, "sck", True),                                 # auto: 15+ only
    ("auto", doc(audio="unsupported", backends=["sck"]), 26, "sck", True),           # no taps on this macOS
    ("auto", doc(backends=None), 26, "sck", False),                                  # old sysaudio: no flag
    ("auto", None, 26, "sck", False),                                                # couldn't ask
    # forced
    ("sck", doc(audio="granted"), 26, "sck", True),
    ("taps", doc(screen="granted", audio="not_determined"), 26, "taps", True),
    ("taps", doc(audio="granted"), 14, "taps", True),
    ("taps", doc(audio="unsupported", backends=["sck"]), 14, "sck", True),
    ("taps", doc(backends=None), 26, "sck", False),
    ("bogus", doc(audio="granted"), 26, "taps", True),                               # unknown = auto
])
def test_choose_backend(setting, check, major, selected, flag):
    plan = recorder.choose_backend(setting, check, major)
    assert (plan["selected"], plan["flag"]) == (selected, flag), plan
    assert plan["reason"]


def test_backend_setting_reads_the_env():
    assert recorder.backend_setting({}) == "auto"
    assert recorder.backend_setting({recorder.BACKEND_ENV_VAR: " TAPS "}) == "taps"
    assert recorder.backend_setting({recorder.BACKEND_ENV_VAR: "coreaudio"}) == "auto"


def test_backend_is_a_setting_not_a_locator():
    assert recorder.BACKEND_ENV_VAR in config.SETTINGS
    assert recorder.BACKEND_ENV_VAR not in config.LOCATORS


def test_capture_command():
    from pathlib import Path
    sa = Path("/x/sysaudio")
    taps = {"selected": "taps", "flag": True}
    assert recorder.capture_command(sa, 16000, True, taps) == \
        ["/x/sysaudio", "--sample-rate", "16000", "--mic", "--backend", "taps"]
    assert recorder.capture_command(sa, 16000, False, {"selected": "sck", "flag": False}) == \
        ["/x/sysaudio", "--sample-rate", "16000"]                      # an old sysaudio: argv unchanged
    assert recorder.capture_command(Path("/x/audiotee"), 16000, False, None) == \
        ["/x/audiotee", "--sample-rate", "16000", "--chunk-duration", str(recorder.CHUNK_DURATION)]


# ---------------------------------------------------------------------------
# The call gate never counts our own capture as a call


def test_own_capture_pids_are_excluded(monkeypatch):
    monkeypatch.setattr(mic, "_own_capture_pids", set())
    mic.register_own_capture(4242)
    mic.register_own_capture(None)        # a fake proc without a pid
    assert 4242 in mic._excluded_pids()
    mic.unregister_own_capture(4242)
    mic.unregister_own_capture(None)
    assert 4242 not in mic._excluded_pids()


def test_is_mic_active_skips_our_capture_child(monkeypatch):
    procs = {1: {"running": True, "bundle": "us.zoom.xos", "pid": 500},
             2: {"running": True, "bundle": None, "pid": 600},                 # sysaudio, taps backend
             3: {"running": True, "bundle": "com.contorch.meeting-capture.sysaudio", "pid": 700}}
    monkeypatch.setattr(mic, "_process_object_ids", lambda: list(procs))
    monkeypatch.setattr(mic, "_process_is_running_input", lambda o: procs[o]["running"])
    monkeypatch.setattr(mic, "_process_bundle_id", lambda o: procs[o]["bundle"])
    monkeypatch.setattr(mic, "_process_pid", lambda o: procs[o]["pid"])
    monkeypatch.setattr(mic, "_own_capture_pids", {600})
    assert mic.is_mic_active() is True               # Zoom
    procs[1]["running"] = False
    assert mic.is_mic_active() is False              # only our capture is left
    assert [p["excluded"] for p in mic.input_processes()] == [True, True]
    monkeypatch.setattr(mic, "_own_capture_pids", set())
    assert mic.is_mic_active() is True               # an unregistered pid with no bundle id counts


# ---------------------------------------------------------------------------
# A whole session against a fake sysaudio (same framing, same check)

FAKE = r'''#!{python}
import json, os, struct, sys, time
import math
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "cfg.json")))
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["check"]:
    d = {"schema": "sysaudio.check/1", "screen_capture": cfg["screen"], "microphone": "granted",
         "os": "26.0", "arch": "arm64", "requested": None}
    if cfg.get("new"):
        d.update(system_audio=cfg["audio"], backends=["sck", "taps"])
    print(json.dumps(d)); sys.exit(0)
if "--backend" in sys.argv and not cfg.get("new"):
    sys.stderr.write("unknown arg: --backend\n"); sys.exit(1)
rate = int(sys.argv[sys.argv.index("--sample-rate") + 1])
framed = "--mic" in sys.argv
out = sys.stdout.buffer
def pcm(seconds, amp, freq):
    n = int(rate * seconds)
    return struct.pack("<%dh" % n, *(int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))
def put(tag, data):
    # 100 ms payloads, like the taps backend
    step = rate // 10 * 2
    for i in range(0, len(data), step):
        p = data[i:i + step]
        out.write((tag + struct.pack("<I", len(p)) + p) if framed else p)
them = pcm(10, 0.3, 440) + pcm(4, 0, 440)
me = pcm(10, 0.2, 220) + pcm(4, 0, 220)
put(b"S", them)
if framed:
    put(b"M", me)
out.flush()
sys.stderr.write("sysaudio: stream started (backend %s), piping PCM to stdout\n"
                 % (sys.argv[sys.argv.index("--backend") + 1] if "--backend" in sys.argv else "default"))
sys.stderr.flush()
while True:
    put(b"S", pcm(1, 0, 440)); out.flush(); time.sleep(1)
'''


def _fake(dirpath, *, new=True, screen="not_granted", audio="granted"):
    dirpath.mkdir(parents=True, exist_ok=True)
    exe = dirpath / "sysaudio"
    exe.write_text(FAKE.replace("{python}", sys.executable))
    exe.chmod(0o755)
    (dirpath / "cfg.json").write_text(json.dumps({"new": new, "screen": screen, "audio": audio}))
    return exe


def _calls(exe):
    return [json.loads(l) for l in (exe.parent / "calls.jsonl").read_text().splitlines()]


def _session(tmp_path, exe, monkeypatch, *, mic_on=True, max_s=60):
    monkeypatch.setattr(recorder, "mic_capture_supported", lambda: True)
    monkeypatch.setattr(recorder, "_macos_major", lambda: 26)
    monkeypatch.setattr(recorder, "_last_logged", None)
    monkeypatch.setenv(recorder.MIC_ENV_VAR, "1" if mic_on else "0")
    monkeypatch.setattr(mic, "_own_capture_pids", set())
    out = tmp_path / "audio"
    out.mkdir()
    chunks, seen_pids = [], []
    deadline = time.time() + max_s

    def should_record():
        seen_pids.append(set(mic._own_capture_pids))
        want = {"them", "me"} if mic_on else {"them"}
        return time.time() < deadline and not want <= {c.role for c in chunks}

    for c in recorder.stream_chunks(out, should_record, capture_binary=exe):
        chunks.append(c)
    return chunks, seen_pids


def test_a_taps_session_records_both_sides(tmp_path, monkeypatch, capfd):
    exe = _fake(tmp_path / "bin", new=True, audio="granted")
    chunks, seen = _session(tmp_path, exe, monkeypatch)
    calls = _calls(exe)
    assert calls[0] == ["check", "--json"]                       # read-only, no prompt
    assert calls[1] == ["--sample-rate", "16000", "--mic", "--backend", "taps"]
    roles = sorted({c.role for c in chunks})
    assert roles == ["me", "them"]
    for c in chunks:
        assert c.path.exists() and 8 <= c.duration_seconds <= 14
    # The capture child was excluded from the call gate while it ran, and forgotten after.
    assert any(len(s) == 1 for s in seen)
    assert mic._own_capture_pids == set()
    err = capfd.readouterr().err
    assert "capture backend: taps — System Audio Recording Only is allowed" in err


def test_an_old_sysaudio_gets_the_old_argv(tmp_path, monkeypatch):
    exe = _fake(tmp_path / "bin", new=False)
    chunks, _ = _session(tmp_path, exe, monkeypatch, mic_on=False)
    assert _calls(exe)[1] == ["--sample-rate", "16000"]           # no --mic (off), no --backend
    assert [c.role for c in chunks] == ["them"]


def test_rollback_setting_passes_sck(tmp_path, monkeypatch):
    exe = _fake(tmp_path / "bin", new=True, audio="granted")
    monkeypatch.setenv(recorder.BACKEND_ENV_VAR, "sck")
    _session(tmp_path, exe, monkeypatch)
    assert _calls(exe)[1][-2:] == ["--backend", "sck"]


def test_live_mode_spawns_the_same_argv(tmp_path, monkeypatch):
    """live.py builds its command with the same helpers (no Gemini involved:
    only the spawn is checked)."""
    from meeting_capture import live
    exe = _fake(tmp_path / "bin", new=True, audio="granted")
    monkeypatch.setattr(recorder, "_macos_major", lambda: 26)
    monkeypatch.setattr(recorder, "mic_capture_supported", lambda: True)
    plan = live.capture_plan(exe)
    assert live.capture_command(exe, live.SAMPLE_RATE, True, plan)[-2:] == ["--backend", "taps"]
