"""The line-in source when its interface is missing: state.json says so
(effective source + problem, since when), and — unless turned off — a call on
this Mac is recorded from this Mac's own call audio instead of nothing, then
the recorder goes back to line-in when the interface returns.

No audio hardware and no capture: a fake `sounddevice` whose device list is a
file the test edits, and the lifecycle tests' file-driven fake call."""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time

import pytest

from meeting_capture import cli, config, daemon, linein, paths, state, store

UMC = {"name": "UMC404HD 192k", "max_input_channels": 4, "default_samplerate": 48000.0}
BUILTIN = {"name": "MacBook Pro Microphone", "max_input_channels": 1, "default_samplerate": 48000.0}


def _wait(cond, timeout=20.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ---- the setting ----------------------------------------------------------------------------

@pytest.mark.parametrize("value,on", [(None, True), ("1", True), ("on", True), ("0", False), ("off", False),
                                      ("false", False), ("No", False)])
def test_fallback_is_on_unless_turned_off(monkeypatch, value, on):
    if value is not None:
        monkeypatch.setenv(linein.FALLBACK_ENV, value)
    assert linein.fallback_enabled() is on


def test_fallback_is_a_setting_in_the_env_file():
    assert linein.FALLBACK_ENV in config.SETTINGS
    assert cli.main(["config", "set", "linein_fallback", "0"]) == 0
    assert config.read()[linein.FALLBACK_ENV] == "0"
    config.apply()
    assert linein.fallback_enabled() is False


def test_problem_codes():
    assert linein.problem_code("no input device matching 'UMC404HD 192k'. Available: x") == "linein_device_missing"
    assert linein.problem_code("line-in capture needs PortAudio. Install with: …") == "linein_unavailable"


# ---- LineinWatch: probe, retry, back ----------------------------------------------------------

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_watch_reports_since_when_retries_every_30s_and_logs_the_return(caplog):
    caplog.set_level(logging.INFO, logger="meeting-capture")
    clock, present = Clock(), {"ok": False}

    def probe():
        if not present["ok"]:
            raise RuntimeError("no input device matching 'UMC404HD 192k'. Available: MacBook Pro Microphone")

    w = daemon.LineinWatch(probe, "UMC404HD 192k", clock=clock)
    assert w.ready() is False
    assert w.problem == {"code": "linein_device_missing", "device": "UMC404HD 192k", "since": 1000.0,
                         "message": "no input device matching 'UMC404HD 192k'. Available: MacBook Pro Microphone"}
    assert w.what() == "'UMC404HD 192k' not connected"
    clock.t += 10
    assert w.ready() is False                     # not probed again before the retry interval
    errors = [r for r in caplog.records if "line-in capture unavailable" in r.getMessage()]
    assert len(errors) == 1
    # the log line pipeline-monitor reads from older daemons is unchanged
    assert errors[0].getMessage().endswith("— retrying in 30s") and errors[0].levelname == "ERROR"
    clock.t += daemon.LINEIN_RETRY_SECONDS
    assert w.ready() is False
    assert w.problem["since"] == 1000.0           # the outage's start, not the last probe
    w.recheck_soon()
    present["ok"] = True
    clock.t += 1
    assert w.ready() is True
    assert w.problem is not None                  # listed again, but not "back" until a stream runs
    w.opened()
    assert w.problem is None
    assert any("'UMC404HD 192k' is back" in r.getMessage() for r in caplog.records)


# ---- state.json / status --json ----------------------------------------------------------------

def test_state_carries_the_configured_input_effective_source_and_problem():
    r = state.Recorder(source="linein", input={"device": "UMC404HD 192k", "me_channel": 0, "them_channel": 1},
                       clock=Clock())
    r.set("idle")
    prob = {"code": "linein_device_missing", "device": "UMC404HD 192k", "message": "m", "since": 990.0,
            "fallback": "armed"}
    r.source_state("sck", prob)
    d = json.loads(paths.STATE_FILE.read_text())
    assert d["source"] == "linein" and d["effective_source"] == "sck" and d["problem"] == prob
    assert d["input"] == {"device": "UMC404HD 192k", "me_channel": 0, "them_channel": 1}
    s = state.status()
    for k in ("source", "input", "effective_source", "linein_fallback", "problem"):
        assert s[k] == d[k], k
    mtime = paths.STATE_FILE.stat().st_mtime_ns
    r.source_state("sck", dict(prob))              # unchanged: not rewritten
    assert paths.STATE_FILE.stat().st_mtime_ns == mtime


def test_an_older_state_file_has_no_source_fields_and_status_still_answers():
    paths.STATE_FILE.write_text(json.dumps({"schema": state.STATE_SCHEMA, "pid": os.getpid(), "state": "idle",
                                            "since": 1.0, "meeting_id": None, "source": "linein",
                                            "updated_at": time.time()}))
    s = state.status()
    assert s["recording"] is False and "problem" not in s and "effective_source" not in s


@pytest.mark.parametrize("problem,expect", [
    (None, "line-in — UMC404HD 192k (Me in 1 · Them in 2)"),
    ({"code": "linein_device_missing", "fallback": "active"},
     "UMC404HD 192k not connected — recording this Mac's call audio instead"),
    ({"code": "linein_device_missing", "fallback": "armed"},
     "UMC404HD 192k not connected — this Mac's calls are recorded instead"),
    ({"code": "linein_device_missing", "fallback": "off"},
     "UMC404HD 192k not connected — not recording (line-in fallback is off)"),
])
def test_describe_source(problem, expect):
    doc = {"source": "linein", "input": {"device": "UMC404HD 192k", "me_channel": 0, "them_channel": 1},
           "problem": problem}
    assert state.describe_source(doc) == expect


def test_describe_source_from_settings_and_sck():
    assert state.describe_source({"source": "sck"}) == "this Mac's call audio"
    assert state.describe_source(None, {"source": "linein", "device": "UMC202HD", "me": 1, "them": 0}) == \
        "line-in — UMC202HD (Me in 2 · Them in 1)"


# ---- the daemon, end to end, with a fake interface --------------------------------------------

RUN = r'''
import json, os, sys, time, types, wave
from pathlib import Path

ctl = Path(sys.argv[1])

# A fake PortAudio: the device list is ctl/devices.json; a stream on a device
# that disappears raises like the real one. Nothing is ever captured.
sd = types.ModuleType("sounddevice")
class PortAudioError(Exception):
    pass
sd.PortAudioError = PortAudioError
def _devs():
    try:
        return json.loads((ctl / "devices.json").read_text())
    except (OSError, ValueError):
        return []
def query_devices(device=None, kind=None):
    devs = _devs()
    if device is None and kind is None:
        return devs
    if device is None:
        return devs[0]
    return devs[device]
sd.query_devices = query_devices
sd._terminate = sd._initialize = lambda: None
class RawInputStream:
    def __init__(self, samplerate, device, channels, dtype, blocksize):
        self.name, self.channels = _devs()[device]["name"], channels
    def start(self):
        (ctl / "linein-open").write_text("1")
    def read(self, n):
        time.sleep(0.05)
        if not any(d["name"] == self.name for d in _devs()):
            raise PortAudioError("Internal PortAudio error [PaErrorCode -9986]")
        return bytes(n * self.channels * 2), False
    def stop(self):
        pass
    def close(self):
        (ctl / "linein-open").unlink(missing_ok=True)
sd.RawInputStream = RawInputStream
sys.modules["sounddevice"] = sd

from meeting_capture import daemon
from meeting_capture.recorder import Chunk

daemon.STOP_GRACE_S = 0.5
daemon.LINEIN_RETRY_SECONDS = 0.5

def _wav(p, seconds=1.0):
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x01" * int(16000 * seconds))

def fake_stream(out_dir, should_record):
    """This Mac's call audio (sysaudio's place): one chunk per call."""
    started = time.time()
    (ctl / "sck-recording").write_text("1")
    while should_record():
        time.sleep(0.02)
    (ctl / "sck-recording").unlink(missing_ok=True)
    p = out_dir / f"chunk-{int(started)}-them.wav"
    _wav(p)
    yield Chunk(path=p, started_at=started, duration_seconds=1.0, role="them")

daemon.stream_chunks = fake_stream
daemon.is_mic_active = lambda: (ctl / "mic-on").exists()
daemon.default_devices_snapshot = lambda: {}
daemon.mic_name = lambda: "test mic"
daemon.mic_capture_enabled = lambda: False
daemon.check_and_maybe_exit = lambda: None
daemon.MIC_POLL_INTERVAL = 0.05
daemon.run()
'''


@pytest.fixture
def rig(tmp_path, fake_helper):
    home = tmp_path / "home"
    home.mkdir()
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    (ctl / "devices.json").write_text(json.dumps([BUILTIN]))       # the UMC is unplugged
    script = tmp_path / "run_daemon.py"
    script.write_text(RUN)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEETING_CAPTURE_", "CONTORCH_"))}
    env.update(HOME=str(home), CO_DB_PATH=str(tmp_path / "context.db"),
               MEETING_CAPTURE_TRANSCRIBE_BIN=str(fake_helper.path), MEETING_CAPTURE_LOCALE="en-US",
               MEETING_CAPTURE_STT="apple", MEETING_CAPTURE_MIC="0",
               MEETING_CAPTURE_SOURCE="linein", MEETING_CAPTURE_INPUT_DEVICE="UMC404HD 192k",
               MEETING_CAPTURE_ME_CHANNEL="0", MEETING_CAPTURE_THEM_CHANNEL="1")
    procs = []

    class R:
        pass
    r = R()
    r.ctl, r.tmp = ctl, tmp_path

    def start(**extra):
        e = dict(env, **extra)
        with open(tmp_path / "run.log", "w") as err:
            p = subprocess.Popen([sys.executable, str(script), str(ctl)], env=e, stderr=err,
                                 stdout=subprocess.DEVNULL)
        procs.append(p)
        return p

    def status():
        out = subprocess.run([sys.executable, "-m", "meeting_capture.cli", "status", "--json"],
                             capture_output=True, text=True, env=env, timeout=60)
        return json.loads(out.stdout)

    def plug(*devs):
        (ctl / "devices.json").write_text(json.dumps(list(devs)))

    def call(on: bool):
        if on:
            (ctl / "mic-on").write_text("1")
        else:
            (ctl / "mic-on").unlink(missing_ok=True)

    r.start, r.status, r.plug, r.call = start, status, plug, call
    r.log = lambda: (tmp_path / "run.log").read_text()
    r.transcripts = lambda: store.recent(10, path=tmp_path / "context.db") if (tmp_path / "context.db").exists() else []
    yield r
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


def _problem(s):
    return s.get("problem") or {}


def test_missing_interface_during_a_call_records_this_macs_audio_then_goes_back(rig, fake_helper):
    fake_helper.configure(text="words from the call")
    p = rig.start()
    # Idle, interface missing: says so, and that a call would be recorded from this Mac.
    _wait(lambda: _problem(rig.status()).get("fallback") == "armed", what="armed fallback")
    s = rig.status()
    assert s["recording"] is False and s["source"] == "linein" and s["effective_source"] == "sck"
    assert s["linein_fallback"] is True
    assert s["input"] == {"device": "UMC404HD 192k", "me_channel": 0, "them_channel": 1}
    assert _problem(s)["code"] == "linein_device_missing" and _problem(s)["device"] == "UMC404HD 192k"
    since = _problem(s)["since"]
    assert time.time() - since < 30

    # A call starts on this Mac: recorded from this Mac's call audio.
    rig.call(True)
    _wait(lambda: rig.status()["recording"] is True, what="recording")
    s = rig.status()
    assert _problem(s)["fallback"] == "active" and s["effective_source"] == "sck"
    assert _problem(s)["since"] == since                     # the same outage

    # The interface comes back mid-call: the call isn't split; it stays on this
    # Mac's audio until it ends, then the recorder is back on line-in.
    rig.plug(BUILTIN, UMC)
    time.sleep(1.0)
    assert (rig.ctl / "sck-recording").exists() and rig.status()["recording"] is True
    rig.call(False)
    _wait(lambda: rig.status().get("effective_source") == "linein" and not rig.status().get("problem"),
          what="back on line-in")
    _wait(lambda: (rig.ctl / "linein-open").exists(), what="line-in stream open")
    _wait(lambda: any("words from the call" in (t["body"] or "") for t in rig.transcripts()),
          what="the fallback chunk transcribed")
    _wait(lambda: "mic inactive — session ended" in rig.log(), what="the call's end line")
    rows = [t for t in rig.transcripts() if "words from the call" in (t["body"] or "")]
    assert len(rows) == 1

    log = rig.log()
    # Clear lines, and the pipeline-monitor contract lines are still there.
    assert "line-in capture unavailable: no input device matching 'UMC404HD 192k'" in log
    assert "'UMC404HD 192k' not connected — this Mac is in a call: recording this Mac's call audio instead" in log
    assert "mic active — starting recording session" in log
    assert "line-in fallback: the call ended" in log
    # the call's end is logged after its chunk, even though line-in started right away
    assert log.index("mic inactive — session ended") > log.index("chunk 1.0s [them]")
    assert "'UMC404HD 192k' is back" in log
    assert "line-in: listening on the interface" in log
    # No "listening" while the interface was missing (the 0.7 daemon logged it every 30 s).
    assert log.count("line-in: listening on the interface") == 1
    assert log.index("line-in: listening on the interface") > log.index("line-in fallback: the call ended")
    p.send_signal(signal.SIGTERM)
    assert p.wait(10) == 0


def test_with_the_fallback_off_a_call_is_not_recorded_and_status_says_so(rig):
    rig.start(MEETING_CAPTURE_LINEIN_FALLBACK="0")
    _wait(lambda: _problem(rig.status()).get("fallback") == "off", what="fallback off")
    rig.call(True)
    time.sleep(1.0)
    s = rig.status()
    assert s["recording"] is False and s["effective_source"] is None and s["linein_fallback"] is False
    assert not (rig.ctl / "sck-recording").exists()
    log = rig.log()
    assert "NOT recording" in log and "mic active" not in log
    assert log.count("this Mac is in a call — NOT recording") == 1      # once per outage


def test_unplugged_mid_stream_is_reported_and_the_fallback_takes_the_call(rig, fake_helper):
    rig.plug(BUILTIN, UMC)
    rig.start()
    _wait(lambda: (rig.ctl / "linein-open").exists(), what="line-in stream open")
    s = rig.status()
    assert s["effective_source"] == "linein" and not s.get("problem")
    rig.plug(BUILTIN)                                                # pulled out
    _wait(lambda: _problem(rig.status()).get("fallback") == "armed", what="problem reported")
    assert "line-in capture unavailable" in rig.log()
    rig.call(True)
    _wait(lambda: rig.status()["recording"] is True, what="fallback recording")


def test_configured_sck_reports_this_macs_call_audio(rig):
    rig.start(MEETING_CAPTURE_SOURCE="sck")
    _wait(lambda: rig.status().get("effective_source") == "sck", what="state")
    s = rig.status()
    assert s["source"] == "sck" and s["input"] is None and s["problem"] is None
