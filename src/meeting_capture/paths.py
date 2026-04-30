from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".meeting-capture"
TRANSCRIPTS_DIR = HOME / "transcripts"
AUDIO_DIR = STATE_DIR / "audio"
LOG_FILE = STATE_DIR / "daemon.log"
PID_FILE = STATE_DIR / "daemon.pid"
PAUSE_FILE = STATE_DIR / "paused"
VOCAB_FILE = STATE_DIR / "vocab.txt"

LAUNCHD_LABEL = "com.stirredo.meeting-capture"
LAUNCHD_PLIST = HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
