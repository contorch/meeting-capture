"""Nothing in meeting-capture starts sysaudio except tccspawn: every capture,
transcribe and check run must be its own responsible process (TCC identity)."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "meeting_capture"
RAW = re.compile(r"subprocess\.(Popen|run|call|check_call|check_output)\(|os\.(posix_spawn|exec\w*|spawn\w*)\(")


def test_only_tccspawn_spawns_processes_that_could_be_sysaudio():
    offenders = []
    for py in sorted(SRC.glob("*.py")):
        if py.name == "tccspawn.py":
            continue
        lines = py.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines, 1):
            if RAW.search(line):
                # every remaining raw spawn names a fixed system tool (on its line or the next)
                call = line + " " + (lines[n] if n < len(lines) else "")
                if not re.search(r'\["(tail|codesign|launchctl|open|/usr/bin/defaults)"|\[editor,', call):
                    offenders.append(f"{py.name}:{n}: {line.strip()}")
    assert offenders == [], "spawn sysaudio through tccspawn.spawn/run:\n" + "\n".join(offenders)


def test_the_old_mic_only_disclaimed_spawn_stays_deleted():
    text = (SRC / "recorder.py").read_text(encoding="utf-8")
    assert "_DisclaimedProc" not in text and "disclaim=" not in text
