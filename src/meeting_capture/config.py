"""meeting-capture's settings: ~/.meeting-capture/env.

One KEY=VALUE per line, '#' comments, an optional `export ` and optional
quotes: the format of ~/.context-orchestrator/env. Only MEETING_CAPTURE_*
keys are meeting-capture's. Other lines are kept as they are when the file is
written, and ignored when it is read.

Precedence. It is the same in every meeting-capture process:

    process environment  >  env file  >  built-in default

apply() runs first thing in the daemon and the CLI. It copies the file's values
into os.environ for every key the process environment doesn't already set, so
each existing os.environ reader (linein, live, recorder, transcriber, watchdog,
copilot) sees the file without knowing about it. `MEETING_CAPTURE_MODE=live
meeting-capture run` in a terminal still wins, as before.

Settings and locators are different things:
- SETTINGS are the user's choices. They live here.
- LOCATORS say where this install's binaries are (MEETING_CAPTURE_SYSAUDIO,
  _AUDIOTEE, _TRANSCRIBE_BIN, _VENV). They belong to the install channel: the
  Homebrew wrapper exports SYSAUDIO on every call, and the supervisor pins the
  recorder's copy. They never go in this file. update() refuses them, and the
  plist migration leaves them where they are. That is why the wrapper's export
  can't shadow or overwrite anything here.

Writers (`mode`, `source`, `stt`, `language`, the settings page, `config set`)
go through update(). It takes an exclusive lock, rewrites the file atomically,
and returns. The caller then restarts the recorder (supervisor.restart()).
The daemon reads its settings when it starts; there is no live reload.

The recorder's view (daemon_env) is the file with the agent's injected
environment on top. For a legacy plist that is its EnvironmentVariables, which
also make up the daemon's process environment, so the same precedence holds.
"""
from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from . import paths

PREFIX = "MEETING_CAPTURE_"

# User choices: stored in the env file, migrated out of an old plist.
SETTINGS = (
    "MEETING_CAPTURE_MODE",            # batch | live
    "MEETING_CAPTURE_SOURCE",          # sck | linein
    "MEETING_CAPTURE_INPUT_DEVICE",    # line-in device (name substring or index)
    "MEETING_CAPTURE_ME_CHANNEL",      # line-in, 0-based
    "MEETING_CAPTURE_THEM_CHANNEL",    # line-in, 0-based
    "MEETING_CAPTURE_LINEIN_FALLBACK", # line-in device missing during a call: record this Mac's audio (default 1)
    "MEETING_CAPTURE_STT",             # auto | apple | gemini
    "MEETING_CAPTURE_LOCALE",          # on-device language
    "MEETING_CAPTURE_MIC",             # 0 = system audio only
    "MEETING_CAPTURE_BACKEND",         # auto | taps | sck (how sysaudio captures "them")
    "MEETING_CAPTURE_DIARIZE",
    "MEETING_CAPTURE_GEMINI_MODEL",
    "MEETING_CAPTURE_COPILOT_MODEL",
    "MEETING_CAPTURE_MAX_FOOTPRINT_MB",
    "MEETING_CAPTURE_TRANSCRIBER",     # legacy (gemini|whisper) — read as stt=auto
)

# Where this install's binaries are: the install channel's, never the file's.
LOCATORS = (
    "MEETING_CAPTURE_SYSAUDIO",
    "MEETING_CAPTURE_AUDIOTEE",
    "MEETING_CAPTURE_TRANSCRIBE_BIN",
    "MEETING_CAPTURE_VENV",
)

# Left behind by removed features; dropped by the plist migration.
OBSOLETE = ("MEETING_CAPTURE_MENUBAR_BIN",)

# Process bookkeeping, never a setting (tccspawn's re-exec guard).
INTERNAL = ("MEETING_CAPTURE_OWN_RESPONSIBLE",)

_KEY_RE = re.compile(r"^MEETING_CAPTURE_[A-Z0-9_]+$")

# Keys apply() put into os.environ (the rest of os.environ came from the
# process environment and always wins).
_injected: set[str] = set()


# ------------------------------------------------------------------ format

def _parse_line(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    key, _, value = s.partition("=")
    key = key.strip().removeprefix("export ").strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key, value


def _format_value(value: str) -> str:
    # Raw unless the parser would change it: leading/trailing spaces or a
    # value wrapped in quotes are written inside double quotes.
    if value != value.strip() or (len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"):
        return f'"{value}"'
    return value


def parse(text: str) -> dict[str, str]:
    """MEETING_CAPTURE_* keys from env-file text (last occurrence wins)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        kv = _parse_line(line)
        if kv and kv[0].startswith(PREFIX) and kv[0] not in LOCATORS and kv[0] not in INTERNAL:
            out[kv[0]] = kv[1]
    return out


def read(path: Path | None = None) -> dict[str, str]:
    """The env file's values ({} when there is no file)."""
    try:
        return parse(Path(path or paths.ENV_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


# ------------------------------------------------------------------ readers

def apply(environ=None, path: Path | None = None) -> dict[str, str]:
    """Load the env file under the process environment (process env wins).
    Returns what was loaded. Safe to call more than once."""
    env = os.environ if environ is None else environ
    injected = _injected if environ is None else set()
    loaded = {}
    for k, v in read(path).items():
        if k in env and k not in injected:
            continue                       # set by the process environment: it wins
        env[k] = v
        loaded[k] = v
        injected.add(k)
    return loaded


def effective(environ=None, path: Path | None = None) -> dict[str, str]:
    """MEETING_CAPTURE_* as this process resolves them: file, then process env on top."""
    env = os.environ if environ is None else environ
    injected = _injected if environ is None else set()
    own = {k: v for k, v in env.items() if k.startswith(PREFIX) and k not in injected}
    return {**read(path), **own}


def overridden(environ=None, path: Path | None = None) -> list[str]:
    """Settings in the env file that this process's own environment overrides
    (the daemon logs them at start: a launchctl setenv, or a legacy plist that
    still carries settings, wins over the file)."""
    env = os.environ if environ is None else environ
    injected = _injected if environ is None else set()
    file_values = read(path)
    return sorted(k for k, v in file_values.items()
                  if k in env and k not in injected and env[k] != v)


def daemon_env() -> dict[str, str]:
    """The environment the recorder daemon runs with: the env file, overridden
    by what its agent injects (a legacy plist's EnvironmentVariables). When no
    agent is installed, it is this process's view (`meeting-capture run` here)."""
    from . import supervisor
    agent = supervisor.current()
    file_values = read()
    if agent.installed:
        return {**file_values, **agent.env}
    own = {k: v for k, v in os.environ.items() if k not in _injected}
    return {**file_values, **own}


def sources() -> dict[str, dict]:
    """Every setting with its value for the recorder and where it comes from:
    "agent" (its plist), "file", or "default" (unset). Also "shell", if this
    process's environment says something else (the recorder never sees it)."""
    from . import supervisor
    agent = supervisor.current()
    file_values = read()
    out = {}
    for key in SETTINGS:
        if agent.installed and key in agent.env:
            row = {"value": agent.env[key], "source": "agent"}
        elif key in file_values:
            row = {"value": file_values[key], "source": "file"}
        else:
            row = {"value": None, "source": "default"}
        shell = os.environ.get(key) if key not in _injected else None
        if shell is not None and shell != row["value"]:
            row["shell"] = shell
        out[key] = row
    return out


# ------------------------------------------------------------------ writers

@contextlib.contextmanager
def locked():
    """Exclusive lock around a read-modify-write of the env file (and the
    plist migration): the CLI, the settings page and the menu bar may write at
    once."""
    import fcntl
    lock = Path(paths.ENV_LOCK)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _check(key: str, value: str | None) -> None:
    if not _KEY_RE.match(key):
        raise ValueError(f"not a meeting-capture setting: {key!r}")
    if key in LOCATORS:
        raise ValueError(f"{key} says where this install's binaries are; it is not a setting "
                         "(the install channel pins it — see `meeting-capture install`)")
    if key in INTERNAL:
        raise ValueError(f"{key} is meeting-capture's own bookkeeping; it is not a setting")
    if value is not None and any(c in value for c in "\n\r\0"):
        raise ValueError(f"{key}: a value must be one line")


def _write_unlocked(set_: dict[str, str], remove=(), path: Path | None = None) -> dict[str, str]:
    p = Path(path or paths.ENV_FILE)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        mode = p.stat().st_mode & 0o777
    except OSError:
        lines, mode = [], 0o644
    pending = dict(set_)
    drop = set(remove) | set(set_)
    out = []
    for line in lines:
        kv = _parse_line(line)
        if kv and kv[0] in drop:
            if kv[0] in pending:            # first occurrence: rewrite in place
                out.append(f"{kv[0]}={_format_value(pending.pop(kv[0]))}")
            continue                        # later duplicates / removed keys: dropped
        out.append(line)
    if not lines:
        out.append("# meeting-capture settings — written by `meeting-capture` (mode, source, stt,")
        out.append("# language, config set) and the settings page. Restart the recorder after a")
        out.append("# hand edit: meeting-capture restart")
    for k, v in pending.items():
        out.append(f"{k}={_format_value(v)}")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return read(p)


def update(set_: dict[str, str] | None = None, remove=(), path: Path | None = None) -> dict[str, str]:
    """Set and/or remove settings in the env file, touching nothing else.
    Returns the file's values afterwards. Also updates this process's view
    (unless its environment overrides a key). The caller restarts the daemon."""
    set_ = {k: str(v) for k, v in (set_ or {}).items()}
    for k, v in set_.items():
        _check(k, v)
    for k in remove:
        _check(k, None)
    with locked():
        if path is None:
            _migrate_unlocked()            # or a setting left in the plist would override this write
        values = _write_unlocked(set_, remove, path)
    if path is None:
        for k in remove:
            if k in _injected:
                os.environ.pop(k, None)
                _injected.discard(k)
        for k, v in set_.items():
            if k not in os.environ or k in _injected:
                os.environ[k] = v
                _injected.add(k)
    return values


# ------------------------------------------------------------------ migration

def migrate_from_plist(plist: Path | None = None) -> list[str]:
    """One-time move of settings out of a legacy agent plist's
    EnvironmentVariables (the config store before the env file) into the env
    file. The plist's value wins over the file's, because it was the one in
    effect. Locators (SYSAUDIO, ...) and PATH stay in the plist; obsolete keys
    are dropped. A copy of the plist is kept once, beside the env file.

    The file is written before the plist, so a crash in between leaves the
    same values in both, and the next call finishes the job. Returns the
    settings moved ([] when there is nothing to do). The caller must reload
    the agent (supervisor.reload) when this returns keys: launchd keeps a
    loaded job's old environment until the plist is loaded again, and that
    environment would still override the file."""
    p = Path(plist or paths.LAUNCHD_PLIST)
    if not p.is_file():
        return []
    with locked():
        return _migrate_unlocked(p)


def _migrate_unlocked(plist: Path | None = None) -> list[str]:
    import plistlib
    p = Path(plist or paths.LAUNCHD_PLIST)
    if not p.is_file():
        return []
    try:
        payload = plistlib.loads(p.read_bytes())
    except Exception:
        return []
    env = dict(payload.get("EnvironmentVariables") or {})
    move = {k: str(v) for k, v in env.items()
            if k.startswith(PREFIX) and k not in LOCATORS and k not in OBSOLETE and k not in INTERNAL}
    obsolete = [k for k in env if k in OBSOLETE]
    if not move and not obsolete:
        return []
    backup = Path(paths.ENV_FILE).parent / f"{p.name}.before-env-file"
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(p.read_bytes())
    if move:
        _write_unlocked(move)
    for k in [*move, *obsolete]:
        env.pop(k, None)
    payload["EnvironmentVariables"] = env
    fd, tmp = tempfile.mkstemp(prefix=".plist.", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(plistlib.dumps(payload))
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return sorted(move)


def restart_pending() -> bool:
    """Has the env file changed since the running daemon started? (A hand
    edit, or a change made while the recorder was stopped.)"""
    try:
        started = Path(paths.PID_FILE).stat().st_mtime
        changed = Path(paths.ENV_FILE).stat().st_mtime
    except OSError:
        return False
    return changed > started
