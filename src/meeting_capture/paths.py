from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".meeting-capture"
AUDIO_DIR = STATE_DIR / "audio"
# Chunks whose transcription failed (no key, quota, outage) wait here for a retry
# instead of being deleted — a recording made before the key was configured
# is otherwise gone for good.
FAILED_AUDIO_DIR = AUDIO_DIR / "failed"
LOG_FILE = STATE_DIR / "daemon.log"
PID_FILE = STATE_DIR / "daemon.pid"
PAUSE_FILE = STATE_DIR / "paused"
# PID of a running `meeting-capture ui` — its level meters open the input,
# and the mic-activity gate ignores that process.
UI_PID_FILE = STATE_DIR / "ui.pid"
UI_URL_FILE = STATE_DIR / "ui.url"   # the running page's URL (0600: carries its token)

LAUNCHD_LABEL = "com.contorch.meeting-capture"
LAUNCHD_PLIST = HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)

# Custom vocabulary for transcription (one term per line, '#' comments).
VOCAB_FILE = STATE_DIR / "vocab.txt"
# Live-mode transcript feeds (JSONL per session) for the in-meeting copilot.
LIVE_DIR = STATE_DIR / "live"
