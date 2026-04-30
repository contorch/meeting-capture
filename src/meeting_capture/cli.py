"""CLI: meeting-capture {start,stop,pause,resume,status,install,uninstall,run,mic,last,tail,doctor}."""
from __future__ import annotations

import argparse
import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .mic import active_mic_name, is_mic_active, mic_name
from .paths import (
    AUDIO_DIR,
    LAUNCHD_LABEL,
    LAUNCHD_PLIST,
    LOG_FILE,
    PAUSE_FILE,
    PID_FILE,
    TRANSCRIPTS_DIR,
    VOCAB_FILE,
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


def _last_transcript() -> Path | None:
    if not TRANSCRIPTS_DIR.exists():
        return None
    files = sorted(TRANSCRIPTS_DIR.glob("meeting-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


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


def cmd_status(_args) -> int:
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
        st = last.stat()
        size_kb = st.st_size / 1024
        age = time.time() - st.st_mtime
        print(f"  last transcript:  {last.name} ({size_kb:.1f} KB, {_format_age(age)})")
    else:
        print(f"  last transcript:  (none yet)")

    last_log = _last_chunk_log_line()
    if last_log is not None:
        print(f"  last chunk log:   {last_log}")

    print(f"  transcripts dir:  {TRANSCRIPTS_DIR}")
    print(f"  log file:         {LOG_FILE}")
    print(f"  launchd:          {'installed' if LAUNCHD_PLIST.exists() else 'not installed'}")
    return 0


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
    print(last)
    return 0


def cmd_tail(_args) -> int:
    if not LOG_FILE.exists():
        print(f"no log file at {LOG_FILE}", file=sys.stderr)
        return 1
    subprocess.run(["tail", "-f", str(LOG_FILE)])
    return 0


def cmd_doctor(_args) -> int:
    """Full health check — every prereq, every binary, every permission, every daemon state."""
    from shutil import which
    from .recorder import find_audiotee, find_sysaudio
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
    arch = platform.machine()
    if arch == "arm64":
        _ok("Apple Silicon (arm64)")
    else:
        _fail(f"arch is {arch}", "mlx-whisper requires arm64. No fix on Intel.")

    print("\nBinaries:")
    sysaudio = find_sysaudio()
    if sysaudio is not None:
        _ok("sysaudio (SCK)", str(sysaudio))
    else:
        _fail("sysaudio not built", "Run setup.sh from the repo root.")
    audiotee = find_audiotee()
    if audiotee is not None:
        print(f"  · audiotee (fallback) — {audiotee}")
    if which("ffmpeg"):
        _ok("ffmpeg", which("ffmpeg"))
    else:
        _fail("ffmpeg missing", "brew install ffmpeg  (mlx-whisper shells out to it)")

    print("\nMic detection:")
    name = mic_name()
    if name:
        _ok(f"default input device", name)
    else:
        _fail("no input device detected", "Check System Settings -> Sound -> Input.")
    in_use = is_mic_active()
    print(f"  · mic in use right now: {in_use}{(' — ' + (active_mic_name() or '?')) if in_use else ''}")

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
    if TRANSCRIPTS_DIR.exists():
        n = len(list(TRANSCRIPTS_DIR.glob("meeting-*.md")))
        _ok(f"transcripts dir", f"{TRANSCRIPTS_DIR} ({n} files)")
    else:
        _fail("transcripts dir missing", f"mkdir -p {TRANSCRIPTS_DIR}")
    if LOG_FILE.exists():
        size_kb = LOG_FILE.stat().st_size / 1024
        _ok(f"daemon log", f"{LOG_FILE} ({size_kb:.1f} KB)")
    else:
        print(f"  · no log file yet ({LOG_FILE}) — daemon hasn't run")

    print("\nTranscription:")
    from .transcriber import DEFAULT_MODEL, ENV_MODEL, resolved_prompt
    model = os.environ.get(ENV_MODEL, DEFAULT_MODEL)
    _ok("model", model + (f" (via {ENV_MODEL})" if ENV_MODEL in os.environ else ""))
    prompt, source = resolved_prompt()
    if prompt is None:
        print(f"  · vocabulary bias: none — {source}")
    else:
        print(f"  · vocabulary bias: {len(prompt)} chars — {source}")
        print(f"     edit with: meeting-capture vocab edit")

    print("\nManual gates (cannot be checked from code):")
    print("  ?  Screen Recording TCC granted to parent terminal (Warp/Terminal/iTerm)")
    print("     System Settings -> Privacy & Security -> Screen & System Audio Recording")
    print("  ?  Terminal restarted once after granting permission")
    print("  ?  Claude Code restarted (so the orchestrator MCP server is live)")

    print()
    if failures == 0:
        print("All automatic checks passed. Verify the manual gates above.")
        return 0
    else:
        print(f"{failures} issue(s) above. Fix and re-run `meeting-capture doctor`.")
        return 1


def cmd_pause(_args) -> int:
    ensure_dirs()
    PAUSE_FILE.touch()
    print(f"paused (touch {PAUSE_FILE})")
    return 0


def cmd_resume(_args) -> int:
    try:
        PAUSE_FILE.unlink()
        print("resumed")
    except FileNotFoundError:
        print("not paused")
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


def _plist_payload(python_exe: str) -> bytes:
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [python_exe, "-m", "meeting_capture.daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"),
        },
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
        print("  Permission attaches to the parent terminal/launcher (Warp, Terminal, iTerm, etc.).")
    else:
        print("sysaudio (SCK):    NOT FOUND")

    if audiotee is not None:
        print(f"audiotee (Tap):    {audiotee}  (fallback)")
        print("  Permission: System Settings -> Privacy & Security -> System Audio Recording Only.")
    else:
        print("audiotee (Tap):    not built (fine, fallback only)")
    return 0


def cmd_vocab(args) -> int:
    """Show, edit, or clear the per-machine vocabulary bias passed to Whisper."""
    from .transcriber import DEFAULT_INITIAL_PROMPT, resolved_prompt

    if args.action == "edit":
        VOCAB_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not VOCAB_FILE.exists():
            VOCAB_FILE.write_text(DEFAULT_INITIAL_PROMPT + "\n", encoding="utf-8")
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        return subprocess.run([editor, str(VOCAB_FILE)]).returncode

    if args.action == "clear":
        if VOCAB_FILE.exists():
            VOCAB_FILE.unlink()
            print(f"removed {VOCAB_FILE}")
        else:
            print(f"{VOCAB_FILE} does not exist; resolving from env / default")
        return 0

    if args.action == "path":
        print(VOCAB_FILE)
        return 0

    # Default: show
    prompt, source = resolved_prompt()
    print(f"source: {source}")
    print()
    if prompt is None:
        print("effective prompt: (none — Whisper will not be biased)")
    else:
        print("effective prompt:")
        print(prompt)
        if len(prompt) > 1000:
            print()
            print(
                f"warning: prompt is {len(prompt)} chars; Whisper truncates "
                f"beyond ~224 tokens (~1000 chars)."
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meeting-capture")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show daemon status").set_defaults(func=cmd_status)
    sub.add_parser("start", help="start the daemon").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the daemon").set_defaults(func=cmd_stop)
    sub.add_parser("pause", help="pause capture (creates pause file)").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="resume capture").set_defaults(func=cmd_resume)
    sub.add_parser("run", help="run daemon in foreground").set_defaults(func=cmd_run)
    sub.add_parser("install", help="install launchd auto-start agent").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="remove launchd agent").set_defaults(func=cmd_uninstall)
    sub.add_parser("check", help="verify audiotee is built and prompt audio-capture permission").set_defaults(func=cmd_check)
    sub.add_parser("mic", help="show current mic-activity state (the gate that triggers recording)").set_defaults(func=cmd_mic)
    sub.add_parser("last", help="print the path of the most recent transcript").set_defaults(func=cmd_last)
    sub.add_parser("tail", help="follow the daemon log").set_defaults(func=cmd_tail)
    sub.add_parser("doctor", help="full health check (binaries, permissions, daemon)").set_defaults(func=cmd_doctor)

    p_vocab = sub.add_parser(
        "vocab",
        help="show / edit / clear the per-machine Whisper vocabulary bias",
    )
    p_vocab.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "edit", "clear", "path"],
        help="show (default), edit (in $EDITOR), clear (delete file), path",
    )
    p_vocab.set_defaults(func=cmd_vocab)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
