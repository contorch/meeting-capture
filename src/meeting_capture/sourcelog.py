"""Where the recorder's audio comes from, and whether that works — read back
from the daemon log, for `meeting-capture status` and `doctor`.

meeting-capture 0.7 has no state file: the daemon reports a line-in outage
only through its log (daemon.LineinWatch and the run loop), and
pipeline-monitor reads the same lines. They are a contract:

    INFO  meeting-capture daemon starting (pid=N, mic=…)
    INFO  SOURCE: line-in — …                                   (line-in configured)
    INFO  line-in fallback: on — … | off (MEETING_CAPTURE_LINEIN_FALLBACK) — …
    ERROR line-in capture unavailable: <message> — retrying in 30s   (every 30 s while it lasts)
    WARNING line-in: 'DEV' not connected — this Mac is in a call: recording this Mac's call audio instead …
    INFO  line-in: 'DEV' is connected again — staying on this Mac's call audio until the call ends
    INFO  line-in fallback: the call ended — looking for the interface again
    INFO  line-in: 'DEV' is back (unavailable for 5m) — recording from the interface again

    parse(lines, now) -> {"source": "linein"|"sck"|None, "fallback_setting": bool|None,
                          "problem": None | {code, message, since, fallback, returned?}}

`fallback` is "active" (this Mac's call audio is being recorded instead),
"armed" (a call would be), "off", or None for a 0.7.0 daemon (no fallback).
An outage is still on while its newest "unavailable" line is under
LOG_FRESH_S old (the daemon logs one every 30 s; nothing while paused), or
while a fallback call is being recorded.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from .paths import LOG_FILE

LOG_FRESH_S = 95
BLOCK = 256 * 1024
MAX_SCAN = 64 * 1024 * 1024
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_UNAVAILABLE = re.compile(r"line-in capture unavailable: (.*?)(?: — retrying in \d+s)?\s*$")


def _ts(line: str) -> float | None:
    m = _TS.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def lines_since_start(pid: int | None, path: Path | None = None) -> list[str]:
    """The log lines written since daemon `pid` started (its "daemon
    starting (pid=N" line), else the last BLOCK bytes."""
    p = path or LOG_FILE
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            needle = f"daemon starting (pid={pid},".encode() if pid else None
            pos, buf = size, b""
            while needle and pos > 0 and size - pos < MAX_SCAN:
                step = min(BLOCK, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                i = buf.rfind(needle)
                if i >= 0:
                    start = buf.rfind(b"\n", 0, i) + 1
                    return buf[start:].decode("utf-8", errors="ignore").splitlines()
            f.seek(max(0, size - BLOCK))
            return f.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def parse(lines: list[str], now: float | None = None) -> dict:
    now = time.time() if now is None else now
    source = fb_setting = None
    problem: dict | None = None
    last_err = None
    active = False
    for i, line in enumerate(lines):
        low = line.lower()
        if "daemon starting (pid=" in low:
            source = fb_setting = problem = last_err = None
            active = False
        elif "source: line-in" in low:
            source = "linein"
        elif " mode: " in low and source is None:
            source = "sck"
        elif "line-in fallback: on" in low:
            fb_setting = True
        elif "line-in fallback: off" in low:
            fb_setting = False
        elif "line-in capture unavailable" in low:
            ts = _ts(line)
            if ts is None:
                continue
            m = _UNAVAILABLE.search(line)
            msg = m.group(1).strip() if m else "unavailable"
            problem = {"code": "linein_device_missing" if msg.startswith("no input device matching")
                       else "linein_unavailable", "message": msg[:300],
                       "since": problem["since"] if problem else ts}
            last_err, active = ts, False
        elif problem is not None and "recording this mac's call audio instead" in low:
            active = True
        elif problem is not None and "is connected again — staying" in low:
            problem["returned"] = _ts(line) or now
        elif "line-in fallback: the call ended" in low:
            active = False
        elif "line-in:" in low and " is back " in low:
            problem, active = None, False
        elif "line-in: listening on the interface" in low:
            # a 0.7.0 daemon logs this before every attempt; the attempt failed
            # only if the "unavailable" line follows at once
            nxt = next((l for l in lines[i + 1:i + 3] if _TS.match(l)), "")
            if "line-in capture unavailable" not in nxt.lower():
                problem, active = None, False
        elif " info chunk " in low and fb_setting is None and source == "linein":
            problem = None                      # 0.7.0: line-in delivered audio (0.7.1 logs "is back";
                                                # its fallback call's chunks land after the call ended)
    if problem is not None and not active and (last_err is None or now - last_err > LOG_FRESH_S):
        problem = None                          # stale: not checked lately (paused, or the daemon died)
    if problem is not None:
        problem["fallback"] = ("active" if active else "armed" if fb_setting else "off") \
            if fb_setting is not None else None
    return {"source": source, "fallback_setting": fb_setting, "problem": problem}


def current(pid: int | None, path: Path | None = None, now: float | None = None) -> dict:
    try:
        return parse(lines_since_start(pid, path), now)
    except Exception:  # noqa: BLE001 — a status line must not break `status`
        return {"source": None, "fallback_setting": None, "problem": None}


def describe(configured: dict, problem: dict | None) -> str:
    """One line: where the recorder's audio comes from. `configured` is
    cli.current_source() ({source, device, me, them}). Never says
    "recording" for the configured source — that is the state line's job."""
    if configured.get("source") != "linein":
        return "this Mac's call audio"
    dev = configured.get("device") or "the default input"
    if not problem:
        return f"line-in — {dev} (Me in {int(configured.get('me', 0)) + 1} · Them in {int(configured.get('them', 1)) + 1})"
    if problem.get("returned") and problem.get("fallback") == "active":
        return f"{dev} is back — recording this Mac's call audio until the call ends"
    what = (f"{dev} not connected" if problem.get("code") == "linein_device_missing"
            else f"line-in unavailable ({problem.get('message') or problem.get('code')})")
    tail = {"active": "recording this Mac's call audio instead",
            "armed": "this Mac's calls are recorded instead",
            "off": "not recording (line-in fallback is off)"}.get(problem.get("fallback"), "not recording")
    return f"{what} — {tail}"
