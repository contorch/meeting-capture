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

    monkeypatch.setattr(ui, "state", lambda: {"version": "x", "daemon": {"installed": True}})
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
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "none.plist")
    store.append("meeting-2026-09-29T10-00-00", "[10:00:01] **Them:** hello from the guest\n\n")
    st = ui.state()
    assert st["daemon"]["installed"] is False
    assert st["source"]["source"] == "sck"
    assert [d["name"] for d in st["devices"]] == ["MacBook Pro Microphone", "UMC404HD 192k"]
    assert st["transcript"]["meeting_id"] == "meeting-2026-09-29T10-00-00"
    assert st["transcript"]["lines"] == ["[10:00:01] **Them:** hello from the guest"]
