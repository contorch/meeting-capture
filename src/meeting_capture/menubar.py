"""The menu bar item's launchd agent (the item itself is Swift:
swift/Sources/menubar). It starts at login, shows the recorder's state, and
offers Pause/Resume and "Recording settings…". "Hide menu bar item" quits it
until the next login (KeepAlive only restarts it after a crash).
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import HOME, STATE_DIR

MENUBAR_ENV_VAR = "MEETING_CAPTURE_MENUBAR_BIN"
BINARY = "meeting-capture-menubar"
LABEL = "com.contorch.meeting-capture.menubar"
PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = STATE_DIR / "menubar.log"


def find_menubar() -> Path | None:
    """$MEETING_CAPTURE_MENUBAR_BIN (the brew wrapper sets it), bin/ beside a
    source checkout, the swift build output, then PATH."""
    env = os.environ.get(MENUBAR_ENV_VAR)
    if env and Path(env).is_file():
        return Path(os.path.abspath(env))
    pkg = Path(__file__).resolve().parent
    for ancestor in [pkg, *pkg.parents]:
        for c in (ancestor / "bin" / BINARY, ancestor / "swift" / ".build" / "release" / BINARY):
            if c.is_file():
                return c
        if (ancestor / ".git").exists():
            break
    found = shutil.which(BINARY)
    return Path(found) if found else None


def cli_path() -> str:
    """What the menu item runs for "Recording settings…": the stable
    `meeting-capture` entry point (the brew wrapper), not a venv path."""
    return (os.environ.get("MEETING_CAPTURE_CLI")
            or shutil.which("meeting-capture")
            or os.path.abspath(sys.argv[0]))


def plist_payload(binary: Path, cli: str) -> bytes:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(binary), "--cli", cli],
        "RunAtLoad": True,
        # Restart after a crash, but "Hide menu bar item" (exit 0) sticks.
        "KeepAlive": {"SuccessfulExit": False},
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    })


def install(quiet: bool = False) -> bool:
    binary = find_menubar()
    if binary is None:
        if not quiet:
            print(f"no {BINARY} binary found — build it with setup.sh (swift) or reinstall via brew",
                  file=sys.stderr)
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plist_payload(binary, cli_path()))
    subprocess.run(["launchctl", "unload", str(PLIST)], check=False, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "load", "-w", str(PLIST)], check=False)
    print(f"menu bar item installed ({binary}); it starts at login")
    return True


def uninstall() -> bool:
    if not PLIST.exists():
        return False
    subprocess.run(["launchctl", "unload", "-w", str(PLIST)], check=False, stderr=subprocess.DEVNULL)
    PLIST.unlink()
    print(f"removed {PLIST}")
    return True


def status() -> str:
    if not PLIST.exists():
        return "not installed"
    r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    return "running" if r.returncode == 0 and '"PID"' in r.stdout else "installed, not running"
