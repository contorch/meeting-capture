"""Settings page server: auth, state, applying the source, pause, meters."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from meeting_capture import store, ui


class FakeStream:
    def __init__(self, callback, channels, **_kw):
        self.cb, self.n = callback, channels

    def start(self):
        # input 1 loud (clipping), input 2 moderate, rest silent
        block = np.zeros((480, self.n), dtype="float32")
        block[:, 0] = 1.0
        if self.n > 1:
            block[:, 1] = 0.1
        self.cb(block, 480, None, None)

    def stop(self): pass
    def close(self): pass


class FakeSD:
    def query_devices(self, dev=None, kind=None):
        if dev is None and kind is None:
            return [{"name": "MacBook Pro Microphone", "max_input_channels": 1, "default_samplerate": 48000},
                    {"name": "UMC404HD 192k", "max_input_channels": 4, "default_samplerate": 48000}]
        return {"name": "UMC404HD 192k", "max_input_channels": 4, "default_samplerate": 48000}

    def InputStream(self, callback, channels, **kw):
        return FakeStream(callback, channels, **kw)


@pytest.fixture
def server(monkeypatch, tmp_path):
    from meeting_capture import linein
    monkeypatch.setattr(linein, "_import_sounddevice", lambda: FakeSD())
    monkeypatch.setattr(ui, "PAUSE_FILE", tmp_path / "paused")
    applied = []

    def fake_apply(**kw):
        applied.append(kw)
        if kw["source"] == "linein" and kw["me"] == kw["them"]:
            raise RuntimeError("me and them are both channel 0")
        return "source: line-in from 'UMC404HD 192k' — me = input 1, them = input 2"

    monkeypatch.setattr(ui, "state", lambda meter=None: {"version": "x", "daemon": {"installed": True}})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler("tok", ui.Meter(), apply_source=fake_apply))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, applied, tmp_path
    httpd.shutdown()


def call(url, body=None, token="tok"):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, r.read()


def test_every_route_needs_the_token(server):
    base, *_ = server
    for path in ("/", "/api/state", "/api/levels"):
        with pytest.raises(urllib.error.HTTPError) as e:
            call(base + path, token="wrong")
        assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/pause", {"paused": True}, token="")
    assert e.value.code == 403


def test_page_is_served_with_its_token(server):
    base, *_ = server
    status, body = call(base + "/?t=tok", token="")
    assert status == 200 and b'const T = "tok"' in body and b"Recording settings" in body


def test_apply_source_passes_choices_and_reports_errors(server):
    base, applied, _ = server
    status, body = call(base + "/api/source", {"source": "linein", "device": "UMC404HD 192k", "me": 0, "them": 1})
    assert status == 200 and json.loads(body)["ok"]
    assert applied[-1] == {"source": "linein", "device": "UMC404HD 192k", "me": 0, "them": 1}
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/source", {"source": "linein", "device": "UMC404HD 192k", "me": 0, "them": 0})
    assert e.value.code == 400 and "both channel 0" in json.loads(e.value.read())["error"]


def test_pause_and_resume_toggle_the_pause_file(server):
    base, _, tmp = server
    call(base + "/api/pause", {"paused": True})
    assert (tmp / "paused").exists()
    call(base + "/api/pause", {"paused": False})
    assert not (tmp / "paused").exists()


def test_levels_report_per_input_dbfs_and_clipping(server):
    base, *_ = server
    _, body = call(base + "/api/levels?device=UMC404HD%20192k&channels=4")
    lv = json.loads(body)
    assert lv["error"] == "" and len(lv["channels"]) == 4
    one, two, three = lv["channels"][:3]
    assert one["clipped"] and one["peak"] == 0.0
    assert not two["clipped"] and -21 < two["peak"] < -19       # 0.1 → −20 dBFS
    assert three["peak"] <= -89


def test_state_shape_with_real_helpers(monkeypatch, tmp_path):
    from meeting_capture import cli, linein
    monkeypatch.setattr(linein, "_import_sounddevice", lambda: FakeSD())
    monkeypatch.setattr("meeting_capture.paths.LAUNCHD_PLIST", tmp_path / "none.plist")
    store.append("meeting-2026-09-29T10-00-00", "[10:00:01] **Them:** hello from the guest\n\n")
    st = ui.state()
    assert st["daemon"]["installed"] is False
    assert st["source"]["source"] == "sck"
    assert [d["name"] for d in st["devices"]] == ["MacBook Pro Microphone", "UMC404HD 192k"]
    assert st["transcript"]["meeting_id"] == "meeting-2026-09-29T10-00-00"
    assert st["transcript"]["lines"] == ["[10:00:01] **Them:** hello from the guest"]


def test_second_ui_reopens_the_running_page(tmp_path, monkeypatch, capsys):
    from meeting_capture import mic, paths
    import meeting_capture.ui as u
    url_file = tmp_path / "ui.url"
    url_file.write_text("http://127.0.0.1:5555/?t=abc")
    monkeypatch.setattr(paths, "UI_URL_FILE", url_file)
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mic, "_excluded_pids", lambda: {1234})     # a live, fresh page
    opened = []
    monkeypatch.setattr(u.subprocess, "run", lambda cmd, check=False: opened.append(cmd))
    u.serve(open_browser=True)                                     # returns instead of serving
    assert opened == [["open", "http://127.0.0.1:5555/?t=abc"]]
    assert "already running" in capsys.readouterr().out


def test_meter_retries_once_after_a_device_rescan(monkeypatch):
    """A re-plugged interface can make the first open fail (-9986)."""
    from meeting_capture import linein
    calls = {"open": 0, "refresh": 0}

    class SD(FakeSD):
        def InputStream(self, callback, channels, **kw):
            calls["open"] += 1
            if calls["open"] == 1:
                raise RuntimeError("Error opening InputStream: Internal PortAudio error [PaErrorCode -9986]")
            return FakeStream(callback, channels, **kw)

    monkeypatch.setattr(linein, "_import_sounddevice", lambda: SD())
    monkeypatch.setattr(linein, "refresh_devices", lambda: calls.__setitem__("refresh", calls["refresh"] + 1) or True)
    m = ui.Meter()
    m.ensure("UMC404HD 192k", 4)
    snap = m.snapshot()
    assert snap["error"] == "" and len(snap["channels"]) == 4
    assert calls["open"] == 2 and calls["refresh"] >= 2      # before the first try and between tries


def test_server_exits_when_no_page_is_open(monkeypatch, tmp_path):
    import threading
    from meeting_capture import paths
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
    monkeypatch.setattr(paths, "UI_PID_FILE", tmp_path / "ui.pid")
    monkeypatch.setattr(paths, "UI_URL_FILE", tmp_path / "ui.url")
    from meeting_capture import mic
    monkeypatch.setattr(mic, "_excluded_pids", lambda: set())
    monkeypatch.setattr(ui, "REAPER_INTERVAL_S", 0.05)
    t = threading.Thread(target=ui.serve, kwargs={"open_browser": False, "idle_exit_s": 0.2}, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "server should shut itself down when idle"
    assert not (tmp_path / "ui.pid").exists() and not (tmp_path / "ui.url").exists()


@pytest.fixture
def stt_server(monkeypatch, tmp_path):
    applied = []

    def fake_apply(**kw):
        applied.append(kw)
        if kw.get("locale") == "xx-YY":
            raise RuntimeError("unsupported language 'xx-YY' — choose one of: en-US, hi-IN")
        return "transcription: On this Mac (stt=apple), language hi-IN — now On this Mac — nothing is uploaded; daemon restarted"

    monkeypatch.setattr(ui, "state", lambda meter=None: {"version": "x", "daemon": {"installed": True}})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler("tok", ui.Meter(), apply_transcription=fake_apply))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", applied
    httpd.shutdown()


def test_transcription_endpoint_needs_the_token(stt_server):
    base, applied = stt_server
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/transcription", {"stt": "apple"}, token="nope")
    assert e.value.code == 403 and applied == []


def test_transcription_endpoint_applies_engine_and_language(stt_server):
    base, applied = stt_server
    status, body = call(base + "/api/transcription", {"stt": "apple", "locale": "hi-IN"})
    assert status == 200 and "daemon restarted" in json.loads(body)["message"]
    assert applied[-1] == {"stt": "apple", "locale": "hi-IN"}
    call(base + "/api/transcription", {"stt": "gemini"})              # language unchanged: not sent
    assert applied[-1] == {"stt": "gemini", "locale": None}


def test_transcription_endpoint_reports_errors(stt_server):
    base, _ = stt_server
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/transcription", {"stt": "apple", "locale": "xx-YY"})
    assert e.value.code == 400 and "hi-IN" in json.loads(e.value.read())["error"]


def test_state_carries_the_transcription_settings(fake_helper, monkeypatch, tmp_path):
    from meeting_capture import cli, linein
    monkeypatch.setattr(linein, "_import_sounddevice", lambda: FakeSD())
    monkeypatch.setattr("meeting_capture.paths.LAUNCHD_PLIST", tmp_path / "none.plist")
    t = ui.state()["transcription"]
    assert (t["engine"], t["choice"], t["locale"], t["ready"]) == ("apple", "auto", "en-US", True)
    assert "hi-IN" in t["apple"]["supported"] and "en-US" in t["apple"]["installed_locales"]
    assert t["gemini_key"] is False and t["uploads"] is False
    assert t["notice"] is None and t["locale_why"] == "default"


def test_state_carries_the_upgrade_note(fake_helper, gemini_key, monkeypatch, tmp_path):
    from meeting_capture import cli, linein
    monkeypatch.setattr(linein, "_import_sounddevice", lambda: FakeSD())
    monkeypatch.setattr("meeting_capture.paths.LAUNCHD_PLIST", tmp_path / "none.plist")
    t = ui.state()["transcription"]
    assert t["engine"] == "apple" and "instead of Gemini" in t["notice"]


def test_page_has_the_transcription_section(server):
    base, *_ = server
    _, body = call(base + "/?t=tok", token="")
    for needle in (b"Transcription", b'value="apple"', b'value="gemini"', b'value="auto"',
                   b"On this Mac", b"Automatic", b'id="locale"', b"/api/transcription", b"romanized",
                   b'id="sttnotice"', b"t.notice"):
        assert needle in body


def test_new_meeting_button_requests_a_new_meeting(server, monkeypatch):
    from meeting_capture import meetings
    base, _, tmp = server
    monkeypatch.setattr(meetings, "NEW_MEETING_FILE", tmp / "new-meeting")
    status, body = call(base + "/api/new-meeting", {})
    assert status == 200 and "new transcript" in json.loads(body)["message"]
    assert (tmp / "new-meeting").exists()


def test_state_says_when_live_mode_is_requested_but_runs_batch(fake_helper, key_file, monkeypatch, tmp_path):
    import plistlib
    from meeting_capture import cli
    plist = tmp_path / "agent.plist"
    monkeypatch.setattr("meeting_capture.paths.LAUNCHD_PLIST", plist)
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"MEETING_CAPTURE_MODE": "live"}}))
    d = ui._daemon_state()
    assert d["mode"] == "live" and d["live_blocked"] == ""          # auto + key: live streams
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"MEETING_CAPTURE_MODE": "live",
                                                               "MEETING_CAPTURE_STT": "apple"}}))
    assert "never uploads" in ui._daemon_state()["live_blocked"]
