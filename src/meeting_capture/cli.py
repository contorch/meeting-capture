"""CLI: meeting-capture {start,stop,pause,resume,new,status,install,uninstall,run,mic,last,tail,doctor,
mode,source,stt,language,vocab,devices,ui,live,copilot}."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, store
from .mic import active_mic_name, is_mic_active, mic_name
from .paths import (
    AUDIO_DIR,
    LAUNCHD_LABEL,
    LAUNCHD_PLIST,
    LOG_FILE,
    PAUSE_FILE,
    PID_FILE,
    ensure_dirs,
)


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except ValueError:
        return None


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _last_transcript() -> dict | None:
    try:
        rows = store.recent(1)
    except Exception:
        return None
    return rows[0] if rows else None


def _last_chunk_log_line() -> str | None:
    if not LOG_FILE.exists():
        return None
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        if "chunk " in line and " -> " in line:
            return line.rstrip()
    return None


def cmd_status(args) -> int:
    if getattr(args, "json", False):
        # The one answer to "is a meeting being recorded right now?"
        # (state.py; README "Contract"). Read-only, exit 0 in every case.
        from . import jsonout, state
        with jsonout.reserved_stdout() as out:
            try:
                doc = state.status()
            except Exception as exc:   # a bug, not a state: still one document, recording unknown
                doc = {"schema": state.STATUS_SCHEMA, "ok": False, "recording": None,
                       "error": jsonout.error("internal", f"{type(exc).__name__}: {exc}")}
            jsonout.emit(doc, out)
        return 0
    ensure_dirs()
    pid = _read_pid()
    running = pid is not None and _is_running(pid)
    paused = PAUSE_FILE.exists()
    mic_on = is_mic_active()

    print(f"meeting-capture {__version__}")
    print(f"  daemon:           {'running (pid ' + str(pid) + ')' if running else 'stopped'}")
    print(f"  paused:           {paused}")
    if mic_on:
        print(f"  mic in use:       True ({active_mic_name() or 'unknown device'})")
    else:
        print(f"  mic in use:       False (default: {mic_name() or 'unknown device'})")
    if running and mic_on and not paused:
        print(f"  state:            ACTIVELY RECORDING")
    elif running and not paused:
        print(f"  state:            idle (waiting for mic to activate)")
    elif running and paused:
        print(f"  state:            paused")
    else:
        print(f"  state:            not running")

    last = _last_transcript()
    if last is not None:
        size_kb = len(last["body"].encode("utf-8")) / 1024
        age = time.time() - last["updated_at"]
        print(f"  last transcript:  {last['meeting_id']} ({size_kb:.1f} KB, {_format_age(age)})")
    else:
        print(f"  last transcript:  (none yet)")

    last_log = _last_chunk_log_line()
    if last_log is not None:
        print(f"  last chunk log:   {last_log}")

    print(f"  transcripts db:   {store.db_path()}")
    print(f"  log file:         {LOG_FILE}")
    print(f"  launchd:          {'installed' if LAUNCHD_PLIST.exists() else 'not installed'}")
    s = transcription_summary()
    print(f"  mode:             {_mode_line(s['live'])} "
          f"({'launchd plist' if LAUNCHD_PLIST.exists() else 'default'})")
    print(f"  transcription:    {_engine_line(s)}")
    print(f"  language:         {s['locale']} ({s['locale_why']})")
    if s.get("notice"):
        print(f"  note:             {_notice(s)}")
    key_needed = s["engine"] == "gemini" or s["live"]["requested"]
    print(f"  gemini key:       {'set' if s['gemini_key'] else 'not set'}"
          f"{'' if key_needed else ' (optional)'}")
    parked = _parked_line()
    if parked:
        print(f"  waiting audio:    {parked}")
    return 0


def _parked_line() -> str:
    from .daemon import parked_counts
    c = parked_counts()
    bits = []
    if c["queued"]:
        bits.append(f"{c['queued']} chunk(s) in the transcription queue")
    if c["parked"]:
        bits.append(f"{c['parked']} chunk(s) waiting to be transcribed")
    if c["quarantined"]:
        bits.append(f"{c['quarantined']} given up on (audio/failed/quarantine)")
    return "; ".join(bits)


def cmd_mic(_args) -> int:
    print(f"default input:       {mic_name() or '(none)'}")
    print(f"in use by other app: {is_mic_active()}")
    if is_mic_active():
        print(f"active device:       {active_mic_name() or '(unknown)'}")
    return 0


def cmd_last(_args) -> int:
    last = _last_transcript()
    if last is None:
        print("(no transcripts yet)", file=sys.stderr)
        return 1
    sys.stdout.write(last["body"])
    return 0


def cmd_tail(_args) -> int:
    if not LOG_FILE.exists():
        print(f"no log file at {LOG_FILE}", file=sys.stderr)
        return 1
    subprocess.run(["tail", "-f", str(LOG_FILE)])
    return 0


def cmd_doctor(_args) -> int:
    """Full health check — every prereq, every binary, every permission, every daemon state."""
    from .recorder import (
        MIC_ENV_VAR,
        find_audiotee,
        find_sysaudio,
        mic_capture_enabled,
        mic_capture_supported,
    )
    import platform

    failures = 0

    def _ok(label, value=""):
        suffix = f" — {value}" if value else ""
        print(f"  ✓ {label}{suffix}")

    def _fail(label, hint):
        nonlocal failures
        failures += 1
        print(f"  ✗ {label}")
        print(f"      → {hint}")

    print(f"meeting-capture {__version__} — doctor\n")

    print("System:")
    mac_ver = platform.mac_ver()[0]
    if mac_ver:
        major = int(mac_ver.split(".")[0])
        if major >= 13:
            _ok(f"macOS {mac_ver}")
        else:
            _fail(f"macOS {mac_ver} too old", "Need 13.0+ for ScreenCaptureKit. Update macOS.")

    print("\nBinaries:")
    sysaudio = find_sysaudio()
    if sysaudio is not None:
        _ok("sysaudio (SCK)", str(sysaudio))
        # Hardened-runtime binaries (Developer ID / notarized) MUST carry the
        # audio-input entitlement or macOS denies the mic silently — every
        # "Me:" line vanishes. The ad-hoc dev build has no runtime, so skip it.
        try:
            dv = subprocess.run(["codesign", "-dvv", str(sysaudio)],
                                capture_output=True, text=True, timeout=5)
            hardened = "runtime" in (dv.stderr + dv.stdout)
            if hardened:
                ent = subprocess.run(["codesign", "-d", "--entitlements", "-", "--xml", str(sysaudio)],
                                     capture_output=True, text=True, timeout=5)
                if "audio-input" in (ent.stdout + ent.stderr):
                    _ok("sysaudio mic entitlement", "audio-input present (hardened runtime)")
                else:
                    _fail("sysaudio hardened but missing mic entitlement",
                          "own-voice (Me:) capture will be denied silently. Re-sign with "
                          "--entitlements swift/sysaudio.entitlements (release pipeline handles this).")
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        _fail("sysaudio not built", "Run setup.sh from the repo root.")
    audiotee = find_audiotee()
    if audiotee is not None:
        print(f"  · audiotee (fallback) — {audiotee}")

    print("\nMic detection:")
    name = mic_name()
    if name:
        _ok(f"default input device", name)
    else:
        _fail("no input device detected", "Check System Settings -> Sound -> Input.")
    in_use = is_mic_active()
    print(f"  · mic in use right now: {in_use}{(' — ' + (active_mic_name() or '?')) if in_use else ''}")

    print("\nTwo-channel capture (own voice):")
    if mic_capture_enabled():
        _ok("mic capture enabled", "transcripts get **Me:** / **Them:** labels")
    elif not mic_capture_supported():
        print("  · mic capture unavailable — needs macOS 15+ (system audio only)")
    else:
        print(f"  · mic capture disabled via {MIC_ENV_VAR} (system audio only)")

    print("\nDaemon:")
    pid = _read_pid()
    if pid and _is_running(pid):
        _ok(f"daemon running (pid {pid})")
    else:
        _fail("daemon not running", "meeting-capture install   OR   meeting-capture start")
    if LAUNCHD_PLIST.exists():
        _ok(f"launchd plist installed", str(LAUNCHD_PLIST))
        result = subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL], capture_output=True, text=True
        )
        if result.returncode == 0:
            _ok("launchd service loaded")
        else:
            _fail("launchd service not loaded", f"launchctl load -w {LAUNCHD_PLIST}")
    else:
        _fail("launchd plist not installed", "meeting-capture install")

    print("\nPaths & data:")
    try:
        _ok("transcripts database", f"{store.db_path()} ({store.count()} meetings)")
    except Exception as exc:
        _fail(f"transcripts database unreadable: {exc}", f"check {store.db_path()}")
    if LOG_FILE.exists():
        size_kb = LOG_FILE.stat().st_size / 1024
        _ok(f"daemon log", f"{LOG_FILE} ({size_kb:.1f} KB)")
    else:
        print(f"  · no log file yet ({LOG_FILE}) — daemon hasn't run")

    print("\nTranscription:")
    from .transcriber import (
        ENV_GEMINI_MODEL, diarization_enabled, is_transcribe_model, load_vocabulary, resolve_model,
    )
    from .paths import VOCAB_FILE
    s = transcription_summary()
    apple = s["apple"]
    if s["ready"]:
        _ok("engine", _engine_line(s))
    else:
        _fail(f"no transcription engine can run ({s['reason']})",
              "on macOS 26+ / Apple silicon: `meeting-capture language en-US` installs the on-device "
              "model; otherwise add a Gemini key (~/.config/google/key). Audio is kept until then.")
    print(f"  · setting: {s['choice']} (`meeting-capture stt`), language: {s['locale']} "
          f"({s['locale_why']}; `meeting-capture language`)")
    if s.get("notice"):
        print(f"  ! {_notice(s)}")
    if apple["usable"]:
        _ok("on-device model", f"{apple['locale']} installed")
    elif apple["installable"]:
        print(f"  · on-device model for {s['locale']} not installed yet — `meeting-capture language {s['locale']}`")
    else:
        print(f"  · on this Mac: unavailable — {apple['reason']}")
    gemini_needed = s["engine"] == "gemini" or s["choice"] == "gemini"
    if s["gemini_key"]:
        _ok("Google API key", "found" + ("" if gemini_needed else " (optional; used for Gemini and live mode)"))
    elif gemini_needed:
        _fail("Google API key missing",
              "write it to ~/.config/google/key (mode 600) — the recorder runs under launchd and "
              "never sees GOOGLE_API_KEY / GEMINI_API_KEY from your shell")
    else:
        print("  · Google API key: not set (optional — only for Gemini transcription or live mode)")
    if gemini_needed:
        model = resolve_model()
        backend = "Interactions API (speech-to-text)" if is_transcribe_model(model) else "generate_content (prompted)"
        _ok("Gemini model", model + (f" (via {ENV_GEMINI_MODEL})" if ENV_GEMINI_MODEL in os.environ else "")
            + f" — {backend}")
        vocab = load_vocabulary()
        if vocab:
            _ok("custom vocabulary", f"{len(vocab)} terms from {VOCAB_FILE} (Gemini only)")
        else:
            print(f"  · no custom vocabulary yet — `meeting-capture vocab edit` (proper nouns, product names; Gemini only)")
        if diarization_enabled():
            print("  · diarization ON for the 'them' channel (vocabulary disabled there per API)")
    parked = _parked_line()
    if parked:
        print(f"  · {parked}")
    if s["live"]["requested"]:
        why = s["live"]["blocker"]
        if why:
            _fail(f"live mode requested, but the recorder runs batch: {why}",
                  f"{_live_fix(why)}; or `meeting-capture mode batch`")
        else:
            _ok("capture mode", "live — calls stream to Gemini (in-meeting copilot); "
                "the engine above only transcribes parked audio")
    else:
        print("  · capture mode: batch (`meeting-capture mode live` streams calls to Gemini for the copilot)")

    print("\nManual gates (cannot be checked from code):")
    print("  ?  Screen Recording TCC granted to the sysaudio binary itself (bin/sysaudio)")
    print("     System Settings -> Privacy & Security -> Screen & System Audio Recording")
    print("     (re-add it after a macOS update or a sysaudio rebuild — either")
    print("      invalidates the grant; the daemon log then shows 'declined TCCs')")
    print("  ?  Microphone TCC granted to sysaudio (own-voice capture; prompt fires on first session)")
    print("     System Settings -> Privacy & Security -> Microphone")
    print("  ?  Claude Code restarted (so the orchestrator MCP server is live)")

    print()
    if failures == 0:
        print("All automatic checks passed. Verify the manual gates above.")
        return 0
    else:
        print(f"{failures} issue(s) above. Fix and re-run `meeting-capture doctor`.")
        return 1


def cmd_copilot(args) -> int:
    """Watch the live meeting feed and whisper help from your past-meeting memory."""
    from .copilot import watch
    from .paths import LIVE_DIR
    feed = None
    if args.session:
        cand = LIVE_DIR / (args.session if args.session.endswith(".jsonl") else f"{args.session}.jsonl")
        if not cand.exists():
            print(f"no such feed: {cand}", file=sys.stderr)
            return 1
        feed = cand
    return watch(feed=feed, model=args.model)


def cmd_live(args) -> int:
    """Tail the live in-meeting transcript feed (finals; --interim for partials too)."""
    import json
    from .paths import LIVE_DIR
    ensure_dirs()
    feeds = sorted(LIVE_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if LIVE_DIR.exists() else []
    if not feeds:
        print("(no live feed yet — switch the daemon with `meeting-capture mode live`, then start a meeting)", file=sys.stderr)
        return 1
    feed = feeds[0]
    print(f"— live: {feed.name} —  (Ctrl-C to stop)")
    labels = {"me": "Me  ", "them": "Them"}
    proc = subprocess.Popen(["tail", "-n", "0", "-F", str(feed)], stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "interim" and not args.interim:
                continue
            who = labels.get(rec.get("role"), rec.get("role", "?"))
            mark = "  …" if rec.get("kind") == "interim" else ""
            print(f"[{rec.get('clock','')}] {who}  {rec.get('text','')}{mark}")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0


def cmd_vocab(args) -> int:
    """Show or edit the custom vocabulary fed to the transcription model."""
    from .paths import VOCAB_FILE
    from .transcriber import MAX_VOCAB_TERMS, load_vocabulary

    ensure_dirs()
    if args.action == "edit":
        if not VOCAB_FILE.exists():
            VOCAB_FILE.write_text(
                "# One term per line: names, products, jargon the transcriber should\n"
                "# spell correctly (e.g. Priya, Chroma, JWT). '#' starts a comment.\n"
                f"# Up to {MAX_VOCAB_TERMS} terms. Takes effect on the next chunk.\n"
                "# Used by Gemini transcription only (on-device transcription ignores it).\n",
                encoding="utf-8",
            )
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        subprocess.run([editor, str(VOCAB_FILE)])
    terms = load_vocabulary()
    print(f"{len(terms)} term(s) in {VOCAB_FILE}")
    for t in terms:
        print(f"  {t}")
    note = "note: the vocabulary applies to Gemini transcription only"
    try:
        if transcription_summary()["engine"] == "apple":
            note += " — transcription currently runs on this Mac, which ignores it"
    except Exception:
        pass
    print(note)
    return 0


def cmd_pause(_args) -> int:
    ensure_dirs()
    PAUSE_FILE.touch()
    print(f"paused (touch {PAUSE_FILE})")
    return 0


def cmd_resume(_args) -> int:
    from .meetings import request_new_meeting

    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        print("not paused")
        return 0
    request_new_meeting()   # whatever is recorded next is a new meeting
    print("resumed — the next speech starts a new transcript")
    return 0


def cmd_new(_args) -> int:
    from .meetings import request_new_meeting

    ensure_dirs()
    request_new_meeting()
    print("new meeting: speech from now on goes into a new transcript")
    return 0


def cmd_run(_args) -> int:
    from .daemon import run

    run()
    return 0


def cmd_start(args) -> int:
    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "load", "-w", str(LAUNCHD_PLIST)], check=False)
        print("started via launchd")
        return 0
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"already running (pid {pid})")
        return 0
    log = open(LOG_FILE, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "meeting_capture.daemon"],
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    print(f"started (pid {proc.pid})")
    return 0


def cmd_stop(_args) -> int:
    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "unload", "-w", str(LAUNCHD_PLIST)], check=False)
        print("stopped via launchd")
        return 0
    pid = _read_pid()
    if not pid or not _is_running(pid):
        print("not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    print(f"sent SIGTERM to pid {pid}")
    return 0


def _preserved_env() -> dict:
    """Collect MEETING_CAPTURE_* config to carry into the plist.

    Reinstalling regenerates the plist; without this, any backend choice (e.g.
    MEETING_CAPTURE_TRANSCRIBER=gemini) is silently dropped and the daemon falls
    back to the local-whisper default. That regression once put a daemon on the
    GPU for 16 days and leaked 24.5 GB of Metal buffers. Preserve such vars from
    (1) the existing plist and (2) the current environment (env wins).
    """
    env: dict[str, str] = {}
    if LAUNCHD_PLIST.exists():
        try:
            existing = plistlib.loads(LAUNCHD_PLIST.read_bytes())
            for k, v in (existing.get("EnvironmentVariables") or {}).items():
                if k.startswith("MEETING_CAPTURE_"):
                    env[k] = v
        except Exception:
            pass
    for k, v in os.environ.items():
        if k.startswith("MEETING_CAPTURE_"):
            env[k] = v
    return env


MODE_ENV_VAR = "MEETING_CAPTURE_MODE"
MODES = ("batch", "live")


def _plist_env() -> dict:
    if not LAUNCHD_PLIST.exists():
        return {}
    try:
        return dict(plistlib.loads(LAUNCHD_PLIST.read_bytes()).get("EnvironmentVariables") or {})
    except Exception:
        return {}


def _plist_mode() -> str:
    """Capture mode the launchd daemon is asked to run in ("batch" unless the
    plist says live). Whether live can actually run: live_mode_blocker()."""
    v = _plist_env().get(MODE_ENV_VAR, "batch").strip().lower()
    return v if v in MODES else "batch"


LINEIN_IS_BATCH = "the audio source is line-in, which always records in batch"


def live_mode_blocker() -> str | None:
    """Why the daemon, asked for live mode, runs batch instead (None: live can
    run) — live.live_blocker() for the daemon's configuration, plus line-in."""
    from .live import live_blocker
    if current_source()["source"] == "linein":
        return LINEIN_IS_BATCH
    return live_blocker(_daemon_env())


def _live_fix(why: str) -> str:
    from .live import LIVE_FIXES
    if why == LINEIN_IS_BATCH:
        return "`meeting-capture source sck` (this Mac's own call audio) allows live mode"
    return LIVE_FIXES.get(why, "see `meeting-capture doctor`")


def _mode_line(live: dict) -> str:
    """The capture mode as it really runs (transcription_summary()["live"]),
    for status: says so when live mode is asked for but the recorder runs batch."""
    if not live["requested"]:
        return "batch"
    if live["blocker"]:
        return f"live requested — running batch: {live['blocker']}"
    return "live — calls stream to Gemini"


def _set_plist_mode(mode: str) -> None:
    """Persist MEETING_CAPTURE_MODE in the launchd plist, touching nothing else.

    Live mode used to require `MEETING_CAPTURE_MODE=live meeting-capture run`
    in a terminal. That spawns sysaudio from the terminal's environment — a
    different binary path than the launchd daemon uses — so macOS treats it as
    a second app and asks for Screen Recording again; declining that prompt
    also revokes the grant the launchd daemon relies on. Switching the mode
    inside the plist keeps one daemon, one sysaudio, one TCC grant.
    """
    payload = plistlib.loads(LAUNCHD_PLIST.read_bytes())
    env = dict(payload.get("EnvironmentVariables") or {})
    if mode == "batch":
        env.pop(MODE_ENV_VAR, None)
    else:
        env[MODE_ENV_VAR] = mode
    payload["EnvironmentVariables"] = env
    LAUNCHD_PLIST.write_bytes(plistlib.dumps(payload))


def _update_plist_env(set_: dict, remove: tuple = ()) -> None:
    """Set/remove launchd env keys, touching nothing else in the plist."""
    payload = plistlib.loads(LAUNCHD_PLIST.read_bytes())
    env = dict(payload.get("EnvironmentVariables") or {})
    for k in remove:
        env.pop(k, None)
    env.update(set_)
    payload["EnvironmentVariables"] = env
    LAUNCHD_PLIST.write_bytes(plistlib.dumps(payload))


def current_source() -> dict:
    """The launchd daemon's audio source settings, as the plist has them."""
    from .linein import DEVICE_ENV, ME_CHANNEL_ENV, SOURCE_ENV, THEM_CHANNEL_ENV

    env = _plist_env()
    return {
        "source": "linein" if env.get(SOURCE_ENV, "").strip().lower() == "linein" else "sck",
        "device": env.get(DEVICE_ENV) or "",
        "me": int(env.get(ME_CHANNEL_ENV, 0)),
        "them": int(env.get(THEM_CHANNEL_ENV, 1)),
    }


def apply_source(source: str, device: str | None = None, me: int | None = None,
                 them: int | None = None) -> str:
    """Switch the daemon's audio source and restart it. Validates the device
    and channel map BEFORE touching the plist. Returns a one-line summary;
    raises RuntimeError with a user-facing message. Shared by `source` and
    the settings page."""
    from .linein import DEVICE_ENV, ME_CHANNEL_ENV, SOURCE_ENV, THEM_CHANNEL_ENV, validate

    if not LAUNCHD_PLIST.exists():
        raise RuntimeError("no launchd agent installed — run `meeting-capture install` first")
    keys = (SOURCE_ENV, DEVICE_ENV, ME_CHANNEL_ENV, THEM_CHANNEL_ENV)
    if source == "sck":
        _update_plist_env({}, remove=keys)
        _relaunch()
        return "source: sck (this Mac's own call audio); daemon restarted"
    if source != "linein":
        raise RuntimeError(f"unknown source {source!r}")
    cur = current_source()
    me = cur["me"] if me is None else me
    them = cur["them"] if them is None else them
    device = (cur["device"] or None) if device is None else (device or None)
    info = validate(device, me, them)
    updates = {SOURCE_ENV: "linein", ME_CHANNEL_ENV: str(me), THEM_CHANNEL_ENV: str(them)}
    if device:
        updates[DEVICE_ENV] = device
    _update_plist_env(updates, remove=() if device else (DEVICE_ENV,))
    _relaunch()
    return f"source: line-in from {info['name']!r} — me = input {me + 1}, them = input {them + 1}"


def cmd_source(args) -> int:
    """Where audio comes from: `sck` (this Mac's own call audio, the default) or
    `linein` (a USB audio interface — for a separate Mac that isn't in the call)."""
    if args.source is None:
        cur = current_source()
        print(cur["source"])
        if cur["source"] == "linein":
            print(f"  device: {cur['device'] or '(system default input)'}")
            print(f"  me = channel {cur['me']}, them = channel {cur['them']}")
        return 0
    try:
        msg = apply_source(args.source, args.device, args.me, args.them)
    except RuntimeError as exc:
        print(f"can't use that input: {exc}", file=sys.stderr)
        if LAUNCHD_PLIST.exists():
            print("see the choices with `meeting-capture devices`", file=sys.stderr)
        return 1
    print(msg)
    if args.source == "linein":
        print("daemon restarted. It records whenever either input carries speech.")
    return 0


def cmd_ui(args) -> int:
    from .ui import cmd_ui as run_ui

    return run_ui(args)


def cmd_devices(_args) -> int:
    from .linein import list_input_devices

    devs = list_input_devices()
    if not devs:
        print("no input devices found — or the line-in extra is missing: "
              "pip install 'meeting-capture[linein]'", file=sys.stderr)
        return 1
    print("audio inputs (use a name with `meeting-capture source linein --device NAME`):")
    for d in devs:
        print(f"  {d['name']}  — {d['channels']} channel(s)")
    return 0


def _relaunch() -> None:
    subprocess.run(["launchctl", "unload", "-w", str(LAUNCHD_PLIST)], check=False, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "load", "-w", str(LAUNCHD_PLIST)], check=False)


def cmd_mode(args) -> int:
    if args.mode is None:
        print(_plist_mode(), flush=True)   # stdout stays one word (scripts compare it)
        if _plist_mode() == "live":
            why = live_mode_blocker()
            if why:
                print(f"note: live mode is requested, but the recorder runs batch: {why}", file=sys.stderr)
        return 0
    if not LAUNCHD_PLIST.exists():
        print("no launchd agent installed — run `meeting-capture install` first", file=sys.stderr)
        return 1
    if args.mode == "live":
        from .live import live_blocker
        why = live_blocker(_daemon_env())
        if why:
            print(f"can't switch to live mode: {why}.\n{_live_fix(why)}, then `meeting-capture mode live`.",
                  file=sys.stderr)
            return 1
    current = _plist_mode()
    if args.mode == current:
        print(f"already in {current} mode")
        return 0
    _set_plist_mode(args.mode)
    _relaunch()
    print(f"switched to {args.mode} mode; daemon restarted via launchd")
    if args.mode == "live":
        print("calls now stream to Gemini (uploaded), whatever `meeting-capture stt` picks for batch")
        if current_source()["source"] == "linein":
            print(f"note: {LINEIN_IS_BATCH}; live applies once the source is sck again")
        print("tail the feed with `meeting-capture live`, or `meeting-capture copilot` for whispers")
    return 0


# --- transcription engine + language (launchd plist env, like mode/source) ---------------

def _daemon_env() -> dict:
    """The configuration the launchd daemon runs with: its plist's environment
    (meeting-capture's config store), or this shell's when no agent is installed."""
    return _plist_env() if LAUNCHD_PLIST.exists() else dict(os.environ)


# `meeting-capture stt --json` prints transcription_summary() as one JSON
# object. pipeline-monitor (menu bar, `contorch setup|status|doctor`) reads it
# instead of re-implementing transcriber.py's rules — README "Contract" lists
# the fields. Bump the schema when a field is removed or changes meaning (adding
# one doesn't), and change pipeline-monitor's transcription.py with it.
STT_JSON_SCHEMA = 1


def transcription_summary() -> dict:
    """How the daemon's configuration transcribes: transcriber.engine_summary()
    for its launchd plist env, plus live mode and whether the agent is
    installed. The one source for `stt` (its text and --json), status, doctor,
    the settings page and, through `stt --json`, pipeline-monitor."""
    from .transcriber import engine_summary
    s = engine_summary(_daemon_env())
    requested = _plist_mode() == "live"
    blocker = live_mode_blocker() if requested else None
    active = requested and blocker is None
    return {
        "schema": STT_JSON_SCHEMA,
        "version": __version__,
        "agent_installed": LAUNCHD_PLIST.exists(),
        **s,
        # Live mode streams every call to Gemini as it happens, whatever the
        # batch engine above is: active means audio leaves the Mac.
        "live": {"requested": requested, "active": active, "blocker": blocker},
        # The privacy answer: can meeting audio reach Google with nobody
        # changing a setting? Now (uploads, live) or as soon as on-device
        # transcription fails under auto with a key (gemini_fallback:
        # transcriber.transcribe() then sends the chunk to Gemini — after a
        # macOS update, a removed model, or one failed helper run). False
        # only when audio stays on this Mac whatever happens.
        "may_upload": bool(s["uploads"] or active or s["gemini_fallback"]),
    }


ENGINE_DESCRIPTIONS = {
    "apple": "On this Mac — nothing is uploaded",
    "gemini": "Gemini — each chunk is uploaded to Google",
    "none": "none — audio is kept until an engine can run",
}


def engine_description(s: dict) -> str:
    """ENGINE_DESCRIPTIONS for a transcription_summary(), hedged when on this
    Mac is not the whole story (gemini_fallback: auto with a key hands a chunk
    to Gemini when on-device fails)."""
    text = ENGINE_DESCRIPTIONS[s["engine"]]
    if s["engine"] == "apple" and s.get("gemini_fallback"):
        text = "On this Mac — uploaded to Gemini only if on-device transcription stops working"
    return text


def _notice(s: dict) -> str:
    from .transcriber import NOTICE_CLI_HINT
    return f"{s['notice']}. {NOTICE_CLI_HINT[0].upper()}{NOTICE_CLI_HINT[1:]}."


def _engine_line(s: dict) -> str:
    if s["engine"] == "apple" and s["ready"]:
        head = f"on this Mac ({s['locale']})"
    elif s["engine"] == "gemini" and s["ready"]:
        head = "Gemini (hosted)"
    elif s["engine"] == "none":
        head = "none"
    else:
        head = f"{s['engine_label']} (not ready)"
    return f"{head} [stt={s['choice']}] — {s['reason']}"


def _is_indic(locale: str) -> bool:
    return locale.endswith("-IN") and not locale.startswith("en-")


HINGLISH_NOTE = ("Indian languages come out romanized (Latin script): mixed Hindi and English "
                 "(\"Hinglish\") lands in one transcript.")


def apply_transcription(stt: str | None = None, locale: str | None = None, progress=None) -> str:
    """Set the transcription engine (auto|apple|gemini) and/or the on-device
    language in the launchd plist, then restart the daemon. A language, or
    switching to on-device, first installs that language's model through the
    helper (`sysaudio transcribe --install`). Everything is validated before
    the plist is touched; raises RuntimeError with a user-facing message.
    Shared by `stt`, `language` and the settings page."""
    from .transcriber import (
        CHOICE_LABELS, ENV_LEGACY_TRANSCRIBER, ENV_LOCALE, ENV_STT,
        STT_CHOICES, AppleError, TranscriptionUnavailable, apple_status, install_apple_model,
        match_locale, stt_choice, stt_locale,
    )

    say = progress or (lambda _msg: None)
    if not LAUNCHD_PLIST.exists():
        raise RuntimeError("no launchd agent installed — run `meeting-capture install` first")
    env = _plist_env()
    new_stt = stt_choice(env) if stt is None else str(stt).strip().lower()
    if new_stt not in STT_CHOICES:
        raise RuntimeError(f"unknown engine {stt!r} — choose auto, apple or gemini")
    new_locale = stt_locale(env)
    notes: list[str] = []

    if locale is not None:
        st = apple_status(new_locale, refresh=True)      # its supported list
        if not st.supported and not st.available:
            raise RuntimeError(f"on-device transcription isn't available on this Mac ({st.reason}); "
                               "the language setting only applies to it — Gemini detects the language itself")
        picked = match_locale(str(locale), st.supported)
        if picked is None:
            raise RuntimeError(f"unsupported language {locale!r} — choose one of: {', '.join(st.supported)}")
        new_locale = picked

    required = locale is not None or new_stt == "apple"
    if required or (stt is not None and new_stt == "auto"):
        st = apple_status(new_locale, refresh=True)
        if not st.available:
            if required:
                raise RuntimeError(f"on-device transcription isn't available: {st.reason}")
            notes.append(f"on this Mac isn't available ({st.reason})")
        elif locale is not None or not st.usable:
            if not st.usable:
                say(f"Downloading the on-device speech model for {new_locale} from Apple (one time)…")
            try:
                install_apple_model(new_locale, progress=progress)
            except (TranscriptionUnavailable, AppleError) as exc:
                if required:
                    raise RuntimeError(str(exc)) from exc
                notes.append(f"couldn't install the on-device model ({exc})")

    # Always written, auto included: an unset MEETING_CAPTURE_STT means nobody
    # has picked an engine yet (transcriber.upgrade_notice). A language is
    # written only when one is chosen — unset, it follows the Mac's language.
    sets: dict = {ENV_STT: new_stt}
    if locale is not None:
        sets[ENV_LOCALE] = new_locale
    _update_plist_env(sets, remove=(ENV_LEGACY_TRANSCRIBER,))
    _relaunch()

    s = transcription_summary()
    lines = [f"transcription: {CHOICE_LABELS[new_stt]} (stt={new_stt}), language {new_locale} — "
             f"now {engine_description(s)}; daemon restarted"]
    lines += notes
    if new_stt == "gemini" and not s["gemini_key"]:
        lines.append("no Google API key the recorder can see — write it to ~/.config/google/key "
                     "(a GOOGLE_API_KEY in your shell doesn't reach it); audio is kept until then")
    elif not s["ready"]:
        lines.append(f"not ready yet: {s['reason']}")
    if s["live"]["requested"]:
        why = s["live"]["blocker"]
        lines.append(f"live mode is on, but {why}, so the recorder runs batch" if why else
                     "live mode is on: calls stream to Gemini; this engine only transcribes "
                     "parked audio")
    if _is_indic(new_locale) and s["engine"] != "gemini":
        lines.append(HINGLISH_NOTE)
    return "\n".join(lines)


def stt_lines(s: dict) -> list[str]:
    """`meeting-capture stt` as text: transcription_summary(), the same dict
    `stt --json` prints."""
    live = s["live"]
    key_needed = s["engine"] == "gemini" or s["choice"] == "gemini" or live["requested"]
    lines = [
        f"engine:    {engine_description(s)}{'' if s['ready'] else ' (not ready)'}",
        f"why:       {s['reason']}",
        f"setting:   {s['choice']}   (meeting-capture stt auto|apple|gemini)",
        f"language:  {s['locale']}   ({s['locale_why']}; meeting-capture language LOCALE; on this Mac only)",
    ]
    if s["needs_model"]:
        lines.append(f"model:     the on-device model for {s['locale']} isn't set up for meeting-capture yet — "
                     f"`{s['install_hint']}` does it now (the running recorder also tries, at most once "
                     "an hour)")
    lines.append(f"gemini:    API key {'set' if s['gemini_key'] else 'not set'}"
                 f"{'' if key_needed else ' (optional)'}"
                 + (" — Gemini takes over if on-device transcription stops working "
                    "(`meeting-capture stt apple` never uploads)" if s["gemini_fallback"] else ""))
    if live["requested"]:
        lines.append(f"live mode: requested, but {live['blocker']} — running batch" if live["blocker"] else
                     "live mode: on — calls stream to Gemini (uploaded); the engine above only "
                     "transcribes parked audio")
    if s.get("notice"):
        lines.append(f"note:      {_notice(s)}")
    return lines


def cmd_stt(args) -> int:
    """Which engine transcribes: auto (on this Mac when it can, else Gemini if
    a key is set), apple (on this Mac only, never uploads) or gemini.

    Shown as text, or with --json as one JSON object on stdout (nothing else
    goes there; README "Contract"). Setting it is safe for another program to
    run: no prompts, progress as lines on stdout (flushed as they happen),
    errors on stderr; exit 0 = applied (the daemon restarted; the result may
    still not be ready — ask `stt --json`), 1 = refused or failed with the
    plist untouched, 2 = usage error."""
    if args.json:
        if args.engine is not None or args.language is not None:
            print("--json only shows the current state; set it without --json", file=sys.stderr)
            return 2
        try:
            s = transcription_summary()
        except Exception as exc:   # a bug, not a state: no half-written JSON on stdout
            print(f"can't work out the transcription state: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(s, sort_keys=True))
        return 0
    if args.engine is None and args.language is None:
        print("\n".join(stt_lines(transcription_summary())))
        return 0
    if not LAUNCHD_PLIST.exists():
        print("no launchd agent installed — run `meeting-capture install` first", file=sys.stderr)
        return 1
    try:
        msg = apply_transcription(stt=args.engine, locale=args.language,
                                  progress=lambda m: print(m, flush=True))
    except RuntimeError as exc:
        print(f"can't switch transcription: {exc}", file=sys.stderr)
        return 1
    print(msg)
    return 0


def cmd_language(args) -> int:
    """The on-device transcription language (installs its model first)."""
    if args.locale is None:
        s = transcription_summary()
        apple = s["apple"]
        if apple["usable"]:
            state = "installed on this Mac"
        elif apple["installable"]:
            state = f"model not installed yet — `meeting-capture language {s['locale']}`"
        else:
            state = f"on-device transcription unavailable: {apple['reason']}"
        print(f"language:  {s['locale']} ({state})")
        print(f"from:      {s['locale_why']}")
        if apple["supported"]:
            print(f"supported: {', '.join(apple['supported'])}")
        if apple["installed_locales"]:
            print(f"installed: {', '.join(apple['installed_locales'])}")
        if _is_indic(s["locale"]):
            print(HINGLISH_NOTE)
        return 0
    if not LAUNCHD_PLIST.exists():
        print("no launchd agent installed — run `meeting-capture install` first", file=sys.stderr)
        return 1
    try:
        msg = apply_transcription(locale=args.locale, progress=lambda m: print(m, flush=True))
    except RuntimeError as exc:
        print(f"can't use that language: {exc}", file=sys.stderr)
        return 1
    print(msg)
    return 0


def _resolved_sysaudio_env(env: dict) -> dict:
    """Pin the sysaudio path the agent will use, as an absolute path.

    The plist is the single source of truth for which sysaudio binary runs
    (recorder.plist_sysaudio explains why: one path, one Screen Recording
    grant). An explicit MEETING_CAPTURE_SYSAUDIO — from the shell, the brew
    wrapper, or a previous plist — is kept if it points at a real file;
    otherwise the path is resolved now so it is never left to whatever the
    daemon's environment happens to contain at launch.
    """
    from .recorder import SYSAUDIO_ENV_VAR, find_sysaudio

    env = dict(env)
    given = env.get(SYSAUDIO_ENV_VAR)
    # abspath, not resolve(): the brew wrapper hands us
    # /opt/homebrew/opt/meeting-capture/bin/sysaudio, and opt/ is a symlink into
    # Cellar/<version>/. Following it would pin a path that dangles on the next
    # `brew upgrade` — and it is the stable opt/ path the TCC grant lives on.
    if given and Path(given).is_file():
        env[SYSAUDIO_ENV_VAR] = os.path.abspath(given)
        return env
    env.pop(SYSAUDIO_ENV_VAR, None)
    found = find_sysaudio()
    if found is not None:
        env[SYSAUDIO_ENV_VAR] = os.path.abspath(found)
    return env


def _plist_payload(python_exe: str) -> bytes:
    env_vars = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"),
    }
    env_vars.update(_resolved_sysaudio_env(_preserved_env()))
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [python_exe, "-m", "meeting_capture.daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": env_vars,
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)


def cmd_install(_args) -> int:
    ensure_dirs()
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    payload = _plist_payload(sys.executable)
    LAUNCHD_PLIST.write_bytes(payload)
    subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], check=False, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "load", "-w", str(LAUNCHD_PLIST)], check=False)
    print(f"installed launchd agent at {LAUNCHD_PLIST}")
    print("daemon will auto-start at login.")
    recorded = _plist_env().get("MEETING_CAPTURE_SYSAUDIO")
    if recorded:
        print(f"sysaudio pinned to {recorded} — grant Screen Recording to that path, once.")
    else:
        print("warning: no sysaudio binary found to pin; run `meeting-capture doctor`.", file=sys.stderr)
    return 0


def cmd_uninstall(_args) -> int:
    if not LAUNCHD_PLIST.exists():
        print("launchd agent not installed")
        return 0
    subprocess.run(["launchctl", "unload", "-w", str(LAUNCHD_PLIST)], check=False)
    LAUNCHD_PLIST.unlink()
    print(f"removed {LAUNCHD_PLIST}")
    return 0


def cmd_check(_args) -> int:
    from .recorder import find_audiotee, find_sysaudio

    sysaudio = find_sysaudio()
    audiotee = find_audiotee()

    if sysaudio is None and audiotee is None:
        print("No audio-capture binary found. Run setup.sh to build sysaudio.")
        return 1

    if sysaudio is not None:
        print(f"sysaudio (SCK):    {sysaudio}")
        print("  Permission: System Settings -> Privacy & Security -> Screen & System Audio Recording.")
        print("  Permission attaches to the sysaudio binary itself (spawned with TCC")
        print("  responsibility disclaimed) — re-add it after a macOS update or rebuild.")
    else:
        print("sysaudio (SCK):    NOT FOUND")

    if audiotee is not None:
        print(f"audiotee (Tap):    {audiotee}  (fallback)")
        print("  Permission: System Settings -> Privacy & Security -> System Audio Recording Only.")
    else:
        print("audiotee (Tap):    not built (fine, fallback only)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meeting-capture")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="show daemon status")
    status.add_argument("--json", action="store_true",
                        help="is a meeting being recorded right now? one JSON document "
                             "(meeting-capture.status/1; README: Contract)")
    status.set_defaults(func=cmd_status)
    sub.add_parser("start", help="start the daemon").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the daemon").set_defaults(func=cmd_stop)
    sub.add_parser("pause", help="pause capture (creates pause file)").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="resume capture (starts a new transcript)").set_defaults(func=cmd_resume)
    sub.add_parser("new", help="start a new meeting: speech from now on goes into a new transcript").set_defaults(func=cmd_new)
    sub.add_parser("run", help="run daemon in foreground").set_defaults(func=cmd_run)
    sub.add_parser("install", help="install launchd auto-start agent").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="remove launchd agent").set_defaults(func=cmd_uninstall)
    sub.add_parser("check", help="verify audiotee is built and prompt audio-capture permission").set_defaults(func=cmd_check)
    sub.add_parser("mic", help="show current mic-activity state (the gate that triggers recording)").set_defaults(func=cmd_mic)
    sub.add_parser("last", help="print the most recent transcript").set_defaults(func=cmd_last)
    sub.add_parser("tail", help="follow the daemon log").set_defaults(func=cmd_tail)
    sub.add_parser("doctor", help="full health check (binaries, permissions, daemon)").set_defaults(func=cmd_doctor)
    vocab = sub.add_parser("vocab", help="show or edit the transcription vocabulary (proper nouns; Gemini only)")
    vocab.add_argument("action", nargs="?", choices=["show", "edit"], default="show")
    vocab.set_defaults(func=cmd_vocab)
    mode = sub.add_parser("mode", help="show or switch the launchd daemon between batch and live capture")
    mode.add_argument("mode", nargs="?", choices=list(MODES), help="omit to print the current mode")
    mode.set_defaults(func=cmd_mode)
    source = sub.add_parser("source", help="show or switch where audio comes from: sck (this Mac) or linein (USB interface)")
    source.add_argument("source", nargs="?", choices=["sck", "linein"], help="omit to print the current source")
    source.add_argument("--device", help="input device name (substring) or index; see `meeting-capture devices`")
    source.add_argument("--me", type=int, help="0-based channel carrying your voice (default 0 = input 1)")
    source.add_argument("--them", type=int, help="0-based channel carrying the other side (default 1 = input 2)")
    source.set_defaults(func=cmd_source)
    stt = sub.add_parser("stt", help="show or switch the transcription engine: auto, apple (on this Mac, "
                                     "never uploads) or gemini")
    stt.add_argument("engine", nargs="?", choices=["auto", "apple", "gemini"],
                     help="omit to show the engine in use and why")
    stt.add_argument("--language", metavar="LOCALE",
                     help="also set the on-device language (like `meeting-capture language`), in one restart")
    stt.add_argument("--json", action="store_true",
                     help="print the current state as one JSON object (for other programs; README: Contract)")
    stt.set_defaults(func=cmd_stt)
    language = sub.add_parser("language", help="show or set the on-device transcription language "
                                               "(e.g. en-US, en-IN, hi-IN); installs its model")
    language.add_argument("locale", nargs="?", help="omit to show the current language and the supported ones")
    language.set_defaults(func=cmd_language)
    sub.add_parser("devices", help="list audio input devices (for line-in)").set_defaults(func=cmd_devices)
    ui = sub.add_parser("ui", help="open the recording settings page (source, interface inputs, levels) in the browser")
    ui.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    ui.add_argument("--no-open", action="store_true", help="print the URL instead of opening the browser")
    ui.set_defaults(func=cmd_ui)
    live = sub.add_parser("live", help="tail the live in-meeting transcript feed (`meeting-capture mode live`)")
    live.add_argument("--interim", action="store_true", help="also show low-latency partial hypotheses")
    live.set_defaults(func=cmd_live)
    copilot = sub.add_parser("copilot", help="watch the live meeting and whisper help from past-meeting memory")
    copilot.add_argument("--session", help="feed stem to watch (default: newest)")
    copilot.add_argument("--model", default=os.environ.get("MEETING_CAPTURE_COPILOT_MODEL", "gemini-2.5-flash"))
    copilot.set_defaults(func=cmd_copilot)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
