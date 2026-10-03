"""`meeting-capture ui` — a settings page for the recording, in the browser.

Serves one page on 127.0.0.1 (random free port) and opens it. From the page:
choose the audio source (this Mac's call audio, or a USB interface such as a
UMC202HD/UMC404HD), pick the device and which input is the host ("Me") and
which the guests ("Them"), watch live per-input level meters while setting the
gain knobs, pause/resume, and see the latest transcript lines arrive.

Settings go through the same code as the CLI (cli.apply_source /
cli.apply_transcription → the launchd plist → daemon restart), so the page and
`meeting-capture source|stt|language` can't disagree. The Transcription
section picks the engine (on this Mac / Gemini / automatic) and the on-device
language; a language whose model isn't on the Mac yet is downloaded from
Apple first, while the page says so. No new dependencies: stdlib http.server; the meters use the
sounddevice package the line-in extra already installs. macOS lets several
processes read one input device, so metering works while the daemon records.

Every API call must carry the per-launch token (sent as a header the page
adds), so another website open in the browser can't drive this server.
"""
from __future__ import annotations

import json
import math
import secrets
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, store
from .paths import PAUSE_FILE

SILENCE_DB = -90.0
# The server exits when no page has called it for this long (the page polls
# every few seconds while open). Before, closing the tab left it running for
# days on old code, holding a stale view of the audio devices.
IDLE_EXIT_S = 10 * 60
REAPER_INTERVAL_S = 2.0


def _db(x: float) -> float:
    return 20 * math.log10(x) if x > 1e-9 else SILENCE_DB


class Meter:
    """Per-channel peak / RMS (dBFS) of one input device, updated from an
    audio callback. One at a time: asking for another device replaces it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream = None
        self._key: tuple | None = None
        self.levels: list[dict] = []
        self.error = ""
        self._last_used = 0.0

    def ensure(self, device: str | None, channels: int) -> None:
        key = (device or "", channels)
        with self._lock:
            self._last_used = time.time()
            if self._key == key and self._stream is not None:
                return
            self._close_locked()
            self._key = key
            self.error = ""
            self.levels = [{"peak": SILENCE_DB, "rms": SILENCE_DB, "clip": 0.0} for _ in range(channels)]
            try:
                from .linein import _import_sounddevice, refresh_devices, resolve_device
                sd = _import_sounddevice()
                refresh_devices()   # stream just closed: safe to re-scan
            except Exception as exc:
                self.error = str(exc)
                return
            # Two tries: a device that was unplugged and re-plugged can leave
            # PortAudio's view stale ("Internal PortAudio error -9986");
            # a fresh scan before the second try clears it.
            for attempt in (1, 2):
                try:
                    self._open_locked(sd, resolve_device, device, channels)
                    self.error = ""
                    return
                except Exception as exc:  # unplugged, no PortAudio, wrong name
                    self._stream = None
                    self.error = str(exc)
                    if attempt == 1:
                        refresh_devices()

    def _open_locked(self, sd, resolve_device, device: str | None, channels: int) -> None:
        dev = resolve_device(device) if device else None
        info = sd.query_devices(dev, "input") if dev is not None else sd.query_devices(kind="input")
        sr = int(info.get("default_samplerate") or 48000)
        n = min(channels, int(info["max_input_channels"]))
        self.levels = [{"peak": SILENCE_DB, "rms": SILENCE_DB, "clip": 0.0} for _ in range(n)]

        def cb(indata, _frames, _time, _status):
            now = time.time()
            for ch in range(n):
                col = indata[:, ch]
                peak = float(abs(col).max()) if len(col) else 0.0
                rms = float((col.astype("float64") ** 2).mean() ** 0.5) if len(col) else 0.0
                lv = self.levels[ch]
                lv["peak"], lv["rms"] = _db(peak), _db(rms)
                if peak >= 0.99:
                    lv["clip"] = now

        self._stream = sd.InputStream(device=dev, channels=n, samplerate=sr, dtype="float32",
                                      blocksize=max(256, sr // 30), callback=cb)
        self._stream.start()

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._last_used = now
            return {"error": self.error, "channels": [
                {"peak": round(l["peak"], 1), "rms": round(l["rms"], 1),
                 "clipped": now - l["clip"] < 2.0} for l in self.levels]}

    def idle(self) -> bool:
        with self._lock:
            return self._stream is None

    def close_if_idle(self, idle_s: float = 5.0) -> None:
        with self._lock:
            if self._stream is not None and time.time() - self._last_used > idle_s:
                self._close_locked()

    def _close_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        self._key = None


def _daemon_state() -> dict:
    from .cli import LAUNCHD_PLIST, _is_running, _plist_mode, _read_pid, live_mode_blocker

    from .mic import is_mic_active

    pid = _read_pid()
    try:
        in_call = is_mic_active()
    except Exception:
        in_call = False
    mode = _plist_mode() if LAUNCHD_PLIST.exists() else "batch"
    try:
        live_blocked = (live_mode_blocker() or "") if mode == "live" else ""
    except Exception:   # never break the page over it
        live_blocked = ""
    return {
        "installed": LAUNCHD_PLIST.exists(),
        "running": bool(pid and _is_running(pid)),
        "paused": PAUSE_FILE.exists(),
        "in_call": in_call,
        "mode": mode,
        # live requested but the recorder runs batch: why (empty otherwise)
        "live_blocked": live_blocked,
    }


def _latest_transcript(lines: int = 12) -> dict:
    try:
        rows = store.recent(1)
    except Exception as exc:
        return {"error": str(exc)}
    if not rows:
        return {"meeting_id": "", "lines": [], "age_s": None}
    r = rows[0]
    body = [ln for ln in r["body"].splitlines() if ln.strip() and not ln.startswith("#")]
    return {"meeting_id": r["meeting_id"], "lines": body[-lines:],
            "age_s": round(time.time() - r["updated_at"])}


def _transcription_state() -> dict:
    """Engine in use and why, the setting, the language, what this Mac can do
    on-device (supported / installed languages) and whether a Gemini key exists."""
    try:
        from .cli import transcription_summary
        return transcription_summary()
    except Exception as exc:   # never break the page over it
        return {"error": str(exc)}


def state(meter: "Meter | None" = None) -> dict:
    from .cli import current_source
    from .linein import list_input_devices, refresh_devices

    # Re-scan so an interface plugged in after the page opened shows up —
    # but never under an open meter stream (it re-scans when it reopens).
    if meter is None or meter.idle():
        refresh_devices()

    daemon = _daemon_state()
    return {
        "version": __version__,
        "daemon": daemon,
        "source": current_source() if daemon["installed"] else
                  {"source": "sck", "device": "", "me": 0, "them": 1},
        "devices": list_input_devices(),
        "transcription": _transcription_state(),
        "transcript": _latest_transcript(),
    }


class Activity:
    """When a page last talked to the server."""
    def __init__(self) -> None:
        self.last = time.time()

    def touch(self) -> None:
        self.last = time.time()

    def idle_for(self) -> float:
        return time.time() - self.last


def make_handler(token: str, meter: Meter, apply_source=None, activity: "Activity | None" = None,
                 apply_transcription=None):
    def _apply_source(**kw):
        from .cli import apply_source as real
        return (apply_source or real)(**kw)

    def _apply_transcription(**kw):
        from .cli import apply_transcription as real
        return (apply_transcription or real)(**kw)

    class Handler(BaseHTTPRequestHandler):
        server_version = "meeting-capture-ui"

        def log_message(self, *_a):  # quiet
            pass

        def _authed(self) -> bool:
            q = parse_qs(urlparse(self.path).query)
            got = self.headers.get("X-Token") or (q.get("t") or [""])[0]
            if secrets.compare_digest(got, token):
                if activity is not None:
                    activity.touch()
                return True
            self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return False

        def _json(self, obj, status=HTTPStatus.OK):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                if not self._authed():
                    return
                data = PAGE.replace("__TOKEN__", token).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/state":
                if self._authed():
                    self._json(state(meter))
            elif path == "/api/levels":
                if not self._authed():
                    return
                q = parse_qs(urlparse(self.path).query)
                device = (q.get("device") or [""])[0] or None
                channels = max(1, min(8, int((q.get("channels") or ["2"])[0])))
                meter.ensure(device, channels)
                self._json(meter.snapshot())
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if not self._authed():
                return
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/source":
                    msg = _apply_source(
                        source=body.get("source", "sck"),
                        device=body.get("device"),
                        me=None if body.get("me") is None else int(body["me"]),
                        them=None if body.get("them") is None else int(body["them"]),
                    )
                elif path == "/api/pause":
                    if body.get("paused"):
                        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        PAUSE_FILE.touch()
                        msg = "Paused"
                    else:
                        from .meetings import request_new_meeting
                        if PAUSE_FILE.exists():
                            PAUSE_FILE.unlink(missing_ok=True)
                            request_new_meeting()
                        msg = "Recording resumed — the next speech starts a new transcript"
                elif path == "/api/transcription":
                    # Blocks while a language's model downloads (the page
                    # says so); the threaded server keeps answering polls.
                    stt = body.get("stt")
                    locale = body.get("locale") or None
                    msg = _apply_transcription(stt=None if stt is None else str(stt),
                                               locale=None if locale is None else str(locale))
                elif path == "/api/new-meeting":
                    from .meetings import request_new_meeting
                    request_new_meeting()
                    msg = "New meeting — speech from now on goes into a new transcript"
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
            except (RuntimeError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "message": msg})

    return Handler


def serve(port: int = 0, open_browser: bool = True, idle_exit_s: float = IDLE_EXIT_S) -> None:
    import os
    from .mic import _excluded_pids
    from .paths import UI_PID_FILE, UI_URL_FILE, ensure_dirs

    ensure_dirs()
    # Already open (e.g. the menu bar item clicked twice): show that page.
    if _excluded_pids() and UI_URL_FILE.exists():
        existing = UI_URL_FILE.read_text().strip()
        print(f"meeting-capture settings: {existing} (already running)", flush=True)
        if open_browser:
            subprocess.run(["open", existing], check=False)
        return
    UI_PID_FILE.write_text(str(os.getpid()))
    token = secrets.token_urlsafe(18)
    meter = Meter()
    activity = Activity()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(token, meter, activity=activity))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={token}"
    UI_URL_FILE.write_text(url)
    UI_URL_FILE.chmod(0o600)

    def reaper():
        while True:
            time.sleep(REAPER_INTERVAL_S)
            meter.close_if_idle()
            if activity.idle_for() > idle_exit_s:
                print("no page open for a while — settings server exiting", flush=True)
                httpd.shutdown()   # serve_forever returns; cleanup runs below
                return
            try:
                UI_PID_FILE.touch()   # keeps the mic-gate exclusion fresh
            except OSError:
                pass
    threading.Thread(target=reaper, daemon=True).start()

    import signal

    def _stop(_sig, _frame):
        raise KeyboardInterrupt
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGHUP, _stop)

    print(f"meeting-capture settings: {url}", flush=True)
    print("Ctrl-C to stop.", flush=True)
    if open_browser:
        subprocess.run(["open", url], check=False)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        meter.close_if_idle(idle_s=-1)
        httpd.server_close()
        try:
            if UI_PID_FILE.read_text().strip() == str(os.getpid()):
                UI_PID_FILE.unlink()
                UI_URL_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def cmd_ui(args) -> int:
    serve(port=args.port, open_browser=not args.no_open)
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recording settings</title>
<style>
:root{
  --bg:#f4f5f3; --panel:#ffffff; --ink:#15181c; --muted:#5d6570; --line:#dde0e3;
  --accent:#0b6e79; --accent-ink:#ffffff; --me:#b4541a; --them:#0b6e79;
  --ok:#2f7d32; --warn:#b7791f; --bad:#b3261e; --meter-bg:#e9ecee;
  --mono: ui-monospace, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#121417; --panel:#1a1d21; --ink:#e7e9ec; --muted:#9aa2ad; --line:#2b3036;
  --accent:#3cbccb; --accent-ink:#081214; --me:#f08a4b; --them:#3cbccb;
  --ok:#6fcf73; --warn:#e6b450; --bad:#f07a70; --meter-bg:#23272c; color-scheme:dark;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 18px 60px;display:flex;flex-direction:column;gap:18px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{font-size:1.45rem;margin:0;letter-spacing:-.01em}
h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:0 0 12px;font-weight:600}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;font-weight:600;font-size:.9rem;border:1px solid var(--line)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
.pill.rec .dot{background:var(--bad);box-shadow:0 0 0 4px color-mix(in srgb,var(--bad) 25%,transparent)}
.pill.paused .dot{background:var(--warn)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}
.choice{border:1.5px solid var(--line);border-radius:8px;padding:12px 14px;cursor:pointer;display:flex;gap:10px;align-items:flex-start}
.choice:has(input:checked){border-color:var(--accent);background:color-mix(in srgb,var(--accent) 7%,transparent)}
.choice b{display:block}
.choice small{color:var(--muted)}
label.f{display:flex;flex-direction:column;gap:5px;font-size:.85rem;color:var(--muted);min-width:180px;flex:1}
select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:8px 10px}
button{font:inherit;font-weight:600;border-radius:7px;padding:9px 16px;border:1px solid var(--line);background:var(--bg);color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
button:focus-visible,select:focus-visible,.choice:focus-within{outline:2px solid var(--accent);outline-offset:2px}
.meters{display:flex;flex-direction:column;gap:14px}
.meter{display:grid;grid-template-columns:150px 1fr 70px;gap:12px;align-items:center}
.meter .name{font-weight:600}
.meter .name small{display:block;font-weight:400;color:var(--muted)}
.bar{position:relative;height:18px;background:var(--meter-bg);border-radius:4px;overflow:hidden}
.bar .fill{position:absolute;inset:0 auto 0 0;width:0;transition:width 80ms linear}
.bar .zone{position:absolute;top:0;bottom:0;border-left:1px dashed var(--muted);border-right:1px dashed var(--muted);opacity:.55}
.bar .peak{position:absolute;top:0;bottom:0;width:2px;background:var(--ink);opacity:.7}
.db{font-family:var(--mono);font-size:.85rem;text-align:right;color:var(--muted)}
.clip{color:var(--bad);font-weight:700}
.scale{display:grid;grid-template-columns:150px 1fr 70px;gap:12px;font-family:var(--mono);font-size:.72rem;color:var(--muted)}
.scale div:nth-child(2){position:relative;height:1.2em}
.scale span{position:absolute;transform:translateX(-50%);white-space:nowrap}
.scale span:last-child{transform:translateX(-100%)}
.hint{color:var(--muted);font-size:.88rem;margin:10px 0 0}
.msg{min-height:1.4em;font-size:.9rem;white-space:pre-line}
.choice:has(input:disabled){cursor:default}
.choice:has(input:disabled) span{opacity:.55}
.now{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin:0 0 12px;font-size:.92rem}
.now b{font-weight:600}
.tag{font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
.tag.local{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,transparent)}
.tag.cloud{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.tag.off{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 45%,transparent)}
.msg.err{color:var(--bad)} .msg.ok{color:var(--ok)}
pre{margin:0;font-family:var(--mono);font-size:.82rem;white-space:pre-wrap;line-height:1.55;max-height:260px;overflow:auto}
.me{color:var(--me)} .them{color:var(--them)}
.muted{color:var(--muted)}
@media (max-width:560px){.meter,.scale{grid-template-columns:1fr}.scale div:first-child,.scale div:last-child{display:none}}
@media (prefers-reduced-motion:reduce){.bar .fill{transition:none}}
</style></head>
<body><main>
<header>
  <h1>Recording settings</h1>
  <span id="status" class="pill"><span class="dot"></span><span id="statusText">Loading…</span></span>
</header>

<section>
  <h2>Where the audio comes from</h2>
  <div class="choices">
    <label class="choice"><input type="radio" name="source" value="linein" id="src-linein">
      <span><b>USB audio interface</b><small>e.g. Behringer UMC202HD / UMC404HD. This Mac isn't in the call; the interface carries both sides.</small></span></label>
    <label class="choice"><input type="radio" name="source" value="sck" id="src-sck">
      <span><b>This Mac's call audio</b><small>Records the call running on this Mac (system audio + its microphone) whenever the mic is in use.</small></span></label>
  </div>
</section>

<section id="iface">
  <h2>Interface and inputs</h2>
  <div class="row">
    <label class="f">Device<select id="device"></select></label>
    <label class="f"><span class="me">Host (“Me”) is on</span><select id="me"></select></label>
    <label class="f"><span class="them">Guests (“Them”) are on</span><select id="them"></select></label>
  </div>
  <div class="meters" id="meters" style="margin-top:18px"></div>
  <p class="hint">Talk into each source and turn that input's <b>Gain</b> knob until the bar's peaks land inside the dashed zone (−18 to −6 dBFS). If <span class="clip">CLIP</span> shows, turn it down or press <b>PAD</b>. The meter reads the interface directly; nothing here is recorded.</p>
</section>

<section>
  <div class="row" style="justify-content:space-between">
    <div class="msg" id="msg" role="status" aria-live="polite"></div>
    <div class="row">
      <button id="newmeeting" title="Speech from now on goes into a new transcript">Start new meeting</button>
      <button id="pause">Pause recording</button>
      <button class="primary" id="save">Save and restart recorder</button>
    </div>
  </div>
</section>

<section id="sttbox">
  <h2>Transcription</h2>
  <p class="now" id="sttnow"></p>
  <div class="choices">
    <label class="choice"><input type="radio" name="stt" value="apple" id="stt-apple">
      <span><b>On this Mac</b><small id="stt-apple-note">Apple's on-device speech recognition. Audio never leaves this Mac; no account or key.</small></span></label>
    <label class="choice"><input type="radio" name="stt" value="gemini" id="stt-gemini">
      <span><b>Gemini</b><small>Google's hosted model: uploads each chunk and needs a Google API key. Custom vocabulary and live mode.</small></span></label>
    <label class="choice"><input type="radio" name="stt" value="auto" id="stt-auto">
      <span><b>Automatic</b><small>On this Mac when it can; Gemini otherwise, only if a key is set.</small></span></label>
  </div>
  <div class="row" style="margin-top:14px;align-items:flex-end">
    <label class="f">Language (on this Mac)<select id="locale"></select></label>
    <button id="sttsave">Save transcription settings</button>
  </div>
  <p class="hint" id="stthint"></p>
  <div class="msg" id="sttmsg" role="status" aria-live="polite"></div>
</section>

<section>
  <h2>Latest transcript <span class="muted" id="tid"></span></h2>
  <pre id="transcript" class="muted">Nothing recorded yet.</pre>
</section>
<p class="muted" style="font-size:.8rem;text-align:center" id="ver"></p>
</main>
<script>
const T = "__TOKEN__";
const $ = id => document.getElementById(id);
let S = null, dirty = false;

async function api(path, body){
  const r = await fetch(path, body === undefined ? {headers:{"X-Token":T}} :
    {method:"POST", headers:{"X-Token":T,"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function say(text, kind){ const m=$("msg"); m.textContent=text; m.className="msg "+(kind||""); }

function selectedSource(){ return document.querySelector('input[name=source]:checked')?.value || "sck"; }
function deviceInfo(){ return (S?.devices||[]).find(d => d.name === $("device").value); }

function fillChannels(){
  const d = deviceInfo(), n = d ? d.channels : 2;
  for (const id of ["me","them"]) {
    const sel = $(id), keep = sel.value;
    sel.innerHTML = "";
    for (let i=0;i<n;i++) sel.add(new Option("Input "+(i+1), i));
    if (keep !== "" && keep < n) sel.value = keep;
  }
}
function render(first){
  const d = S.daemon, st = $("status");
  const live = S.source.source === "linein" || d.in_call;
  st.className = "pill " + (!d.installed || !d.running ? "" : d.paused ? "paused" : live ? "rec" : "");
  $("statusText").textContent = !d.installed ? "Recorder not installed" : !d.running ? "Recorder stopped"
    : d.paused ? "Paused" : S.source.source === "linein" ? "Listening on the interface"
    : d.in_call ? "Recording a call" : "Waiting for a call";
  $("pause").textContent = d.paused ? "Resume recording" : "Pause recording";
  $("pause").disabled = !d.installed;
  $("newmeeting").disabled = !d.installed || d.paused;
  $("ver").textContent = "meeting-capture " + S.version + (!d.installed ? "" :
    d.mode === "live" && d.live_blocked ? " · live mode requested, running batch" : " · " + d.mode + " mode");
  if (first || !dirty) {
    $("src-"+S.source.source).checked = true;
    const devSel = $("device");
    devSel.innerHTML = "";
    const multi = S.devices.filter(x => x.channels >= 2);
    for (const x of S.devices) {
      const o = new Option(`${x.name} — ${x.channels} input${x.channels>1?"s":""}`, x.name);
      if (x.channels < 2) o.disabled = true;
      devSel.add(o);
    }
    const want = S.source.device && S.devices.find(x => x.name.toLowerCase().includes(S.source.device.toLowerCase()));
    const guess = multi.find(x => /umc|behringer|focusrite|scarlett|audient|motu|presonus/i.test(x.name)) || multi[0];
    if (want || guess) devSel.value = (want || guess).name;
    fillChannels();
    $("me").value = S.source.me; $("them").value = S.source.them;
  }
  $("iface").style.display = selectedSource() === "linein" ? "" : "none";
  const t = S.transcript;
  $("tid").textContent = t.meeting_id ? "· " + t.meeting_id + (t.age_s != null ? " · updated " + ago(t.age_s) : "") : "";
  $("transcript").innerHTML = t.lines && t.lines.length ? t.lines.map(esc).map(l =>
    l.replace("**Me:**", '<b class="me">Me:</b>').replace("**Them:**", '<b class="them">Them:</b>')).join("\n") : "Nothing recorded yet.";
  buildMeters();
  renderStt(first);
}

let sttDirty = false, sttBusy = false;
const langNames = (() => { try { return new Intl.DisplayNames(["en"], {type:"language"}); } catch(e){ return null; } })();
function langName(code){
  if (code === "mul-IN") return "Indian languages (shared model)";
  try { return (langNames && langNames.of(code)) || code; } catch(e){ return code; }
}
function isIndic(code){ return /-IN$/.test(code) && !/^en-/.test(code); }
function renderStt(first){
  const t = S.transcription;
  if (!t || t.error) { $("sttnow").textContent = t && t.error ? "Can't read the transcription settings: " + t.error : ""; return; }
  const a = t.apple || {};
  const tag = t.engine === "apple" && t.ready ? '<span class="tag local">nothing uploaded</span>'
            : t.engine === "gemini" && t.ready ? '<span class="tag cloud">uploads to Google</span>'
            : '<span class="tag off">not transcribing — audio is kept</span>';
  const engine = t.engine === "apple" ? "On this Mac" + (t.ready ? " (" + langName(t.locale) + ")" : "")
               : t.engine === "gemini" ? "Gemini" : "Nothing yet";
  $("sttnow").innerHTML = `<span>Now: <b>${esc(engine)}</b></span>${tag}<span class="muted">${esc(t.reason)}</span>`;
  $("stt-apple").disabled = !a.available;
  $("stt-apple-note").textContent = a.available
    ? "Apple's on-device speech recognition. Audio never leaves this Mac; no account or key."
    : "Not available here — " + a.reason;
  if ((first || !sttDirty) && !sttBusy) {
    const r = $("stt-" + t.choice); if (r) r.checked = true;
    const sel = $("locale"); sel.innerHTML = "";
    const list = (a.supported && a.supported.length) ? a.supported.slice() : [t.locale];
    if (!list.includes(t.locale)) list.unshift(t.locale);
    const have = new Set(a.installed_locales || []);
    list.sort((x, y) => langName(x).localeCompare(langName(y)));
    for (const code of list) {
      sel.add(new Option(`${langName(code)} — ${code}${have.has(code) ? "" : " (downloads once)"}`, code));
    }
    sel.value = t.locale;
  }
  $("locale").disabled = !a.available || sttBusy;
  sttHint();
}
function sttHint(){
  const t = S.transcription, a = (t && t.apple) || {};
  const loc = $("locale").value, bits = [];
  if (a.available && loc && !(a.installed_locales || []).includes(loc))
    bits.push(`Saving downloads the speech model for ${langName(loc)} from Apple once (English ≈ 140 MB; the shared Indian-languages model ≈ 250 MB).`);
  if (isIndic(loc)) bits.push("Indian languages come out romanized (Latin script); mixed Hindi and English (“Hinglish”) lands in one transcript.");
  bits.push("Gemini API key: " + (t.gemini_key ? "set" : "not set") + (t.engine === "gemini" || t.choice === "gemini" ? "." : " (optional)."));
  if (S.daemon && S.daemon.mode === "live") bits.push(S.daemon.live_blocked
    ? "Live mode is requested, but " + S.daemon.live_blocked + ", so the recorder runs batch."
    : "Live mode is on: calls stream to Gemini (uploaded) whatever is chosen here, which then only transcribes parked audio.");
  $("stthint").textContent = bits.join(" ");
}
function sayStt(text, kind){ const m=$("sttmsg"); m.textContent=text; m.className="msg "+(kind||""); }
function ago(s){ return s<60? s+"s ago" : s<3600? Math.round(s/60)+" min ago" : Math.round(s/3600)+" h ago"; }
function esc(s){ return s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

function buildMeters(){
  const box = $("meters"), d = deviceInfo();
  const n = d ? d.channels : 0, sig = n + "|" + $("me").value + "|" + $("them").value;
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig; box.innerHTML = "";
  if (!n) { box.innerHTML = '<p class="muted">No multi-input device found. Plug the interface in (USB) and reload.</p>'; return; }
  for (let i=0;i<n;i++){
    const role = String(i) === $("me").value ? '<small class="me">Host (Me)</small>'
              : String(i) === $("them").value ? '<small class="them">Guests (Them)</small>' : '<small>not used</small>';
    box.insertAdjacentHTML("beforeend", `<div class="meter"><div class="name">Input ${i+1}${role}</div>
      <div class="bar" aria-label="Input ${i+1} level"><div class="zone" style="left:${pct(-18)}%;width:${pct(-6)-pct(-18)}%"></div>
      <div class="fill" id="f${i}"></div><div class="peak" id="p${i}"></div></div><div class="db" id="d${i}">—</div></div>`);
  }
  box.insertAdjacentHTML("beforeend", `<div class="scale"><div></div><div>${[-60,-40,-18,-6].map(d => `<span style="left:${pct(d)}%">${d === -60 ? "−60" : "−" + (-d)}</span>`).join("")}<span style="left:100%">0 dBFS</span></div><div></div></div>`);
}
function pct(db){ return Math.max(0, Math.min(100, (db + 60) / 60 * 100)); }
function colorFor(db){ return db > -6 ? "var(--bad)" : db >= -18 ? "var(--ok)" : db > -40 ? "var(--warn)" : "var(--muted)"; }

async function pollLevels(){
  try {
    const d = deviceInfo();
    if (S && selectedSource() === "linein" && d && document.visibilityState === "visible") {
      const L = await api(`/api/levels?device=${encodeURIComponent(d.name)}&channels=${d.channels}`);
      if (L.error) { $("meters").dataset.sig = ""; $("meters").innerHTML = `<p class="msg err">Can't read ${esc(d.name)}: ${esc(L.error)}</p>`; }
      L.channels.forEach((c, i) => {
        const f = $("f"+i); if (!f) return;
        f.style.width = pct(c.rms) + "%"; f.style.background = colorFor(c.peak);
        $("p"+i).style.left = "calc(" + pct(c.peak) + "% - 1px)";
        $("d"+i).innerHTML = c.clipped ? '<span class="clip">CLIP</span>' : (c.peak <= -89 ? "—" : c.peak.toFixed(0) + " dB");
      });
    }
  } catch(e) {}
  setTimeout(pollLevels, 1000/12);
}

async function refresh(first){
  try { S = await api("/api/state"); render(first); }
  catch(e){ say("Lost contact with the settings server — is `meeting-capture ui` still running?", "err"); }
}

document.querySelectorAll('input[name=source]').forEach(r => r.addEventListener("change", () => { dirty = true; render(false); }));
$("device").addEventListener("change", () => { dirty = true; fillChannels(); $("me").value = 0; $("them").value = 1; buildMeters(); });
for (const id of ["me","them"]) $(id).addEventListener("change", () => {
  dirty = true;
  const other = id === "me" ? "them" : "me";
  if ($(id).value === $(other).value) {           // keep them different: swap
    const opts = [...$(other).options].map(o => o.value).filter(v => v !== $(id).value);
    $(other).value = opts[0];
  }
  buildMeters();
});
$("save").addEventListener("click", async () => {
  const src = selectedSource();
  const body = src === "linein" ? {source:"linein", device:$("device").value, me:+$("me").value, them:+$("them").value} : {source:"sck"};
  $("save").disabled = true; say("Saving…");
  try { const r = await api("/api/source", body); dirty = false; say(r.message.replace(/^source: /, "Saved — "), "ok"); await refresh(true); }
  catch(e){ say(e.message, "err"); }
  finally { $("save").disabled = false; }
});
document.querySelectorAll('input[name=stt]').forEach(r => r.addEventListener("change", () => { sttDirty = true; sttHint(); }));
$("locale").addEventListener("change", () => { sttDirty = true; sttHint(); });
$("sttsave").addEventListener("click", async () => {
  const t = S.transcription || {}, a = t.apple || {};
  const body = {stt: document.querySelector('input[name=stt]:checked')?.value || "auto"};
  const loc = $("locale").value;
  if (a.available && loc && loc !== t.locale) body.locale = loc;
  const download = body.locale && !(a.installed_locales || []).includes(body.locale);
  const started = Date.now();
  const status = () => download
    ? `Downloading the speech model for ${langName(body.locale)} from Apple — this can take a minute… (${Math.round((Date.now() - started) / 1000)} s)`
    : "Saving and restarting the recorder…";
  sttBusy = true; $("sttsave").disabled = true; $("locale").disabled = true; sayStt(status());
  const tick = setInterval(() => sayStt(status()), 1000);
  try { const r = await api("/api/transcription", body); clearInterval(tick); sttDirty = false; sayStt(r.message, "ok"); }
  catch(e){ clearInterval(tick); sayStt(e.message, "err"); }
  finally { sttBusy = false; $("sttsave").disabled = false; await refresh(false); }
});
$("newmeeting").addEventListener("click", async () => {
  try { const r = await api("/api/new-meeting", {}); say(r.message, "ok"); await refresh(false); }
  catch(e){ say(e.message, "err"); }
});
$("pause").addEventListener("click", async () => {
  try { const r = await api("/api/pause", {paused: !S.daemon.paused}); say(r.message, "ok"); await refresh(false); }
  catch(e){ say(e.message, "err"); }
});
refresh(true); setInterval(() => refresh(false), 3000); pollLevels();
</script></body></html>
"""
