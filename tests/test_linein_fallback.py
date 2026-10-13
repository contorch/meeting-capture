"""The line-in source when its interface is missing (0.7.1 backport): the
daemon log says so (and `meeting-capture status` reads it back — 0.7 has no
state file), and — unless turned off — a call on this Mac is recorded from
this Mac's own call audio instead of nothing, then the recorder goes back to
line-in when the interface returns.

No audio hardware and no capture: a fake `sounddevice` whose device list is a
file the test edits, and a file-driven fake call."""
from __future__ import annotations

import json
import logging
import os
import plistlib
import signal
import subprocess
import sys
import time

import pytest

from meeting_capture import cli, daemon, linein, sourcelog, store

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
    monkeypatch.delenv(linein.FALLBACK_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(linein.FALLBACK_ENV, value)
    assert linein.fallback_enabled() is on


def test_fallback_is_a_launchd_plist_setting(tmp_path, monkeypatch, capsys):
    plist = tmp_path / "agent.plist"
    plist.write_bytes(plistlib.dumps({"Label": "x", "EnvironmentVariables": {
        "MEETING_CAPTURE_SOURCE": "linein", "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k"}}))
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    calls = []
    monkeypatch.setattr(cli, "_relaunch", lambda: calls.append("relaunch"))
    assert cli.main(["linein-fallback"]) == 0
    assert capsys.readouterr().out.startswith("on")
    assert cli.main(["linein-fallback", "off"]) == 0
    env = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
    assert env[linein.FALLBACK_ENV] == "0" and env["MEETING_CAPTURE_SOURCE"] == "linein"
    assert linein.fallback_enabled(env) is False and calls == ["relaunch"]
    assert cli.main(["linein-fallback", "on"]) == 0
    assert linein.FALLBACK_ENV not in plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
    assert calls == ["relaunch", "relaunch"]
    # carried over when the agent is reinstalled
    cli.main(["linein-fallback", "off"])
    assert cli._preserved_env()[linein.FALLBACK_ENV] == "0"


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
    # the log line pipeline-monitor reads is unchanged
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


# ---- reading it back from the log -------------------------------------------------------------

T0 = "2026-10-06 11:15:"
REAL_070 = [  # what meeting-capture 0.7.0 logged on 2026-10-06 (no fallback)
    "2026-10-06 11:14:00,000 INFO meeting-capture daemon starting (pid=4242, mic=MacBook Pro Microphone)",
    "2026-10-06 11:14:00,001 INFO SOURCE: line-in — reading the USB audio interface continuously "
    "(not a call participant; chunks only on speech)",
    "2026-10-06 11:15:27,100 INFO line-in: listening on the interface",
    "2026-10-06 11:15:27,126 ERROR line-in capture unavailable: no input device matching 'UMC404HD 192k'. "
    "Available: External Microphone, MacBook Pro Microphone — retrying in 30s",
    "2026-10-06 11:15:57,198 INFO mic inactive — session ended",
    "2026-10-06 11:15:57,204 INFO line-in: listening on the interface",
    "2026-10-06 11:15:57,253 ERROR line-in capture unavailable: no input device matching 'UMC404HD 192k'. "
    "Available: External Microphone, MacBook Pro Microphone — retrying in 30s",
]


def _at(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def test_a_070_log_reads_as_not_recording_no_fallback():
    d = sourcelog.parse(REAL_070, now=_at("2026-10-06 11:16:10"))
    assert d["source"] == "linein" and d["fallback_setting"] is None
    p = d["problem"]
    assert p["code"] == "linein_device_missing" and p["fallback"] is None
    assert p["since"] == _at("2026-10-06 11:15:27")
    cfg = {"source": "linein", "device": "UMC404HD 192k", "me": 0, "them": 1}
    assert sourcelog.describe(cfg, p) == "UMC404HD 192k not connected — not recording"
    # stale (nothing logged for > 95 s: paused, or the daemon is gone): no claim
    assert sourcelog.parse(REAL_070, now=_at("2026-10-06 11:20:00"))["problem"] is None
    # a 0.7.0 attempt that opened (no "unavailable" right after "listening") ends it
    ok = REAL_070 + ["2026-10-06 11:16:27,204 INFO line-in: listening on the interface",
                     "2026-10-06 11:16:40,000 INFO audio devices changed: …"]
    assert sourcelog.parse(ok, now=_at("2026-10-06 11:16:45"))["problem"] is None


def test_a_fallback_calls_late_chunk_does_not_end_the_outage():
    lines = [
        "2026-10-06 11:14:00,000 INFO meeting-capture daemon starting (pid=1, mic=x)",
        "2026-10-06 11:14:00,001 INFO SOURCE: line-in — reading the USB audio interface continuously",
        "2026-10-06 11:14:00,002 INFO line-in fallback: on — if the interface is missing …",
        "2026-10-06 11:14:01,000 ERROR line-in capture unavailable: no input device matching 'U'. "
        "Available: x — retrying in 30s",
        "2026-10-06 11:14:02,000 WARNING line-in: 'U' not connected — this Mac is in a call: recording this "
        "Mac's call audio instead (line-in fallback; `meeting-capture linein-fallback off` turns it off)",
        "2026-10-06 11:14:02,001 INFO mic active — starting recording session",
    ]
    now = _at("2026-10-06 11:30:00")                    # a long call: no "unavailable" lines meanwhile
    assert sourcelog.parse(lines, now=now)["problem"]["fallback"] == "active"
    lines += ["2026-10-06 11:29:58,000 INFO line-in fallback: the call ended — looking for the interface again",
              "2026-10-06 11:29:58,100 ERROR line-in capture unavailable: no input device matching 'U'. "
              "Available: x — retrying in 30s",
              "2026-10-06 11:29:59,000 INFO chunk 30.0s [them] -> meeting-x (10 chars) via apple",
              "2026-10-06 11:29:59,001 INFO mic inactive — session ended"]
    p = sourcelog.parse(lines, now=now)["problem"]
    assert p["fallback"] == "armed" and p["since"] == _at("2026-10-06 11:14:01")


@pytest.mark.parametrize("problem,expect", [
    (None, "line-in — UMC404HD 192k (Me in 1 · Them in 2)"),
    ({"code": "linein_device_missing", "fallback": "active"},
     "UMC404HD 192k not connected — recording this Mac's call audio instead"),
    ({"code": "linein_device_missing", "fallback": "armed"},
     "UMC404HD 192k not connected — this Mac's calls are recorded instead"),
    ({"code": "linein_device_missing", "fallback": "off"},
     "UMC404HD 192k not connected — not recording (line-in fallback is off)"),
    ({"code": "linein_device_missing", "fallback": "active", "returned": 5.0},
     "UMC404HD 192k is back — recording this Mac's call audio until the call ends"),
])
def test_describe(problem, expect):
    assert sourcelog.describe({"source": "linein", "device": "UMC404HD 192k", "me": 0, "them": 1},
                              problem) == expect


def test_describe_sck():
    assert sourcelog.describe({"source": "sck", "device": "", "me": 0, "them": 1}, None) == "this Mac's call audio"


def test_only_the_current_daemons_lines_count(tmp_path):
    log = tmp_path / "daemon.log"
    old = REAL_070
    new = ["2026-10-06 11:30:00,000 INFO meeting-capture daemon starting (pid=5151, mic=x)",
           "2026-10-06 11:30:00,001 INFO MODE: batch — chunked transcription (default)"]
    log.write_text("\n".join(old + ["filler " * 40] * 20000 + new) + "\n")   # > one read block
    lines = sourcelog.lines_since_start(5151, log)
    assert lines[0].endswith("daemon starting (pid=5151, mic=x)")
    assert sourcelog.parse(lines, now=_at("2026-10-06 11:30:10")) == \
        {"source": "sck", "fallback_setting": None, "problem": None}
    assert sourcelog.lines_since_start(4242, log)[0].endswith("daemon starting (pid=4242, mic=MacBook Pro Microphone)")


def test_status_and_doctor_name_the_source_and_a_missing_interface(monkeypatch, capsys, tmp_path):
    from meeting_capture import recorder
    plist = tmp_path / "agent.plist"
    plist.write_bytes(plistlib.dumps({"Label": "x", "EnvironmentVariables": {
        "MEETING_CAPTURE_SOURCE": "linein", "MEETING_CAPTURE_INPUT_DEVICE": "UMC404HD 192k",
        "MEETING_CAPTURE_ME_CHANNEL": "0", "MEETING_CAPTURE_THEM_CHANNEL": "1"}}))
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    for name in ("is_mic_active", "active_mic_name", "mic_name"):
        monkeypatch.setattr(cli, name, lambda: None)
    monkeypatch.setattr(recorder, "find_sysaudio", lambda: None)
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli, "LOG_FILE", tmp_path / "daemon.log")
    monkeypatch.setattr(cli, "PAUSE_FILE", tmp_path / "paused")
    monkeypatch.setattr(cli, "PID_FILE", tmp_path / "daemon.pid")
    cli.main(["status"])                                       # no daemon: the settings
    assert "source:           line-in — UMC404HD 192k (Me in 1 · Them in 2)" in capsys.readouterr().out

    pid = os.getpid()                                         # "the daemon" is this process
    (tmp_path / "daemon.pid").write_text(str(pid))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    (tmp_path / "daemon.log").write_text("\n".join([
        f"{now},000 INFO meeting-capture daemon starting (pid={pid}, mic=x)",
        f"{now},001 INFO SOURCE: line-in — reading the USB audio interface continuously",
        f"{now},002 INFO line-in fallback: on — if the interface is missing during a call on this Mac, "
        "this Mac's call audio is recorded instead",
        f"{now},003 ERROR line-in capture unavailable: no input device matching 'UMC404HD 192k'. "
        "Available: MacBook Pro Microphone — retrying in 30s"]) + "\n")
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "source:           UMC404HD 192k not connected — this Mac's calls are recorded instead" in out
    assert "source problem:   since " in out and "no input device matching 'UMC404HD 192k'" in out
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "source: UMC404HD 192k not connected — this Mac's calls are recorded instead (since " in out


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
    env.pop("GOOGLE_API_KEY", None)
    env.pop("GEMINI_API_KEY", None)
    procs = []

    class R:
        pass
    r = R()
    r.ctl, r.tmp = ctl, tmp_path
    logf = tmp_path / "run.log"

    def start(**extra):
        e = dict(env, **extra)
        with open(logf, "w") as err:
            p = subprocess.Popen([sys.executable, str(script), str(ctl)], env=e, stderr=err,
                                 stdout=subprocess.DEVNULL)
        procs.append(p)
        r.pid = p.pid
        return p

    def status():
        """What `meeting-capture status` reads: the running daemon's log lines."""
        return sourcelog.current(r.pid, logf)

    def plug(*devs):
        (ctl / "devices.json").write_text(json.dumps(list(devs)))

    def call(on: bool):
        if on:
            (ctl / "mic-on").write_text("1")
        else:
            (ctl / "mic-on").unlink(missing_ok=True)

    r.start, r.status, r.plug, r.call = start, status, plug, call
    r.log = lambda: logf.read_text()
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
    assert s["source"] == "linein" and s["fallback_setting"] is True
    assert _problem(s)["code"] == "linein_device_missing"
    since = _problem(s)["since"]
    assert time.time() - since < 30
    assert not (rig.ctl / "sck-recording").exists()

    # A call starts on this Mac: recorded from this Mac's call audio.
    rig.call(True)
    _wait(lambda: (rig.ctl / "sck-recording").exists(), what="recording this Mac")
    _wait(lambda: _problem(rig.status()).get("fallback") == "active", what="fallback active")
    assert _problem(rig.status())["since"] == since                # the same outage
    time.sleep(1.5)                                                # > LINEIN_RETRY_SECONDS: no probe noise mid-call
    assert _problem(rig.status()).get("fallback") == "active"

    # The interface comes back mid-call: the call isn't split; it stays on this
    # Mac's audio until it ends, then the recorder is back on line-in.
    rig.plug(BUILTIN, UMC)
    _wait(lambda: _problem(rig.status()).get("returned"), what="the interface noted as back")
    assert (rig.ctl / "sck-recording").exists()
    cfg = {"source": "linein", "device": "UMC404HD 192k", "me": 0, "them": 1}
    assert sourcelog.describe(cfg, rig.status()["problem"]) == \
        "UMC404HD 192k is back — recording this Mac's call audio until the call ends"
    assert "is connected again — staying on this Mac's call audio until the call ends" in rig.log()
    rig.call(False)
    _wait(lambda: (rig.ctl / "linein-open").exists(), what="line-in stream open")
    _wait(lambda: rig.status()["problem"] is None, what="back on line-in")
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
    assert "line-in fallback: on" in log
    assert "line-in fallback: the call ended" in log
    # the call's end is logged after its chunk, even though line-in started right away
    assert log.index("mic inactive — session ended") > log.index("chunk 1.0s [them]")
    assert "'UMC404HD 192k' is back" in log
    # No "listening" while the interface was missing (0.7.0 logged it every 30 s).
    assert log.count("line-in: listening on the interface") == 1
    assert log.index("line-in: listening on the interface") > log.index("line-in fallback: the call ended")
    p.send_signal(signal.SIGTERM)
    assert p.wait(10) == 0


def test_with_the_fallback_off_a_call_is_not_recorded_and_status_says_so(rig):
    rig.start(MEETING_CAPTURE_LINEIN_FALLBACK="0")
    _wait(lambda: _problem(rig.status()).get("fallback") == "off", what="fallback off")
    rig.call(True)
    time.sleep(1.5)
    assert _problem(rig.status()).get("fallback") == "off"
    assert not (rig.ctl / "sck-recording").exists()
    log = rig.log()
    assert "line-in fallback: off (MEETING_CAPTURE_LINEIN_FALLBACK)" in log
    assert "NOT recording" in log and "mic active" not in log
    assert log.count("this Mac is in a call — NOT recording") == 1      # once per outage


def test_unplugged_mid_stream_is_reported_not_a_crash_and_the_fallback_takes_the_call(rig):
    rig.plug(BUILTIN, UMC)
    p = rig.start()
    _wait(lambda: (rig.ctl / "linein-open").exists(), what="line-in stream open")
    assert rig.status()["problem"] is None and rig.status()["source"] == "linein"
    rig.plug(BUILTIN)                                                # pulled out
    _wait(lambda: _problem(rig.status()).get("fallback") == "armed", what="problem reported")
    assert "PaErrorCode -9986" in rig.log() and "Traceback" not in rig.log()
    assert p.poll() is None                                          # still running
    rig.call(True)
    _wait(lambda: (rig.ctl / "sck-recording").exists(), what="fallback recording")


def test_configured_sck_is_unchanged(rig):
    p = rig.start(MEETING_CAPTURE_SOURCE="sck")
    _wait(lambda: "MODE: batch" in rig.log(), what="started")
    assert rig.status() == {"source": "sck", "fallback_setting": None, "problem": None}
    assert "line-in" not in rig.log()
    rig.call(True)
    _wait(lambda: (rig.ctl / "sck-recording").exists(), what="recording this Mac")
    rig.call(False)
    _wait(lambda: "mic inactive — session ended" in rig.log(), what="the call's end line")
    assert p.poll() is None
