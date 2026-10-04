"""Start privacy-sensitive helpers as their OWN responsible process.

macOS (tccd) checks a permission against the *responsible* process of the
caller. A plain fork/exec (subprocess.Popen) inherits the parent's
responsible process: sysaudio started from a terminal asks as the terminal,
started by Contorch.app's menu bar asks as Contorch, started by a launchd job
asks as that job. `posix_spawn` with the private-but-stable
`responsibility_spawnattrs_setdisclaim` (Chromium, Karabiner, Apple's own
tools) makes the child answer for itself, so its grant is the same whoever
started it. See contorch-macos design/phase1-tcc-identity-and-lab-gates.md.

Every sysaudio spawn (capture, live, transcribe --probe/--install/FILE,
check) goes through `spawn`/`run` here. Nothing else in meeting-capture may
start sysaudio. Not usable in a Mac App Store build (private symbol).
"""
from __future__ import annotations

import ctypes
import os
import select
import signal as _signal
import subprocess
import sys
import time
from typing import Optional, Sequence

PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL

# <spawn.h> (darwin)
_POSIX_SPAWN_SETSIGDEF = 0x0004
_POSIX_SPAWN_SETSIGMASK = 0x0008
_POSIX_SPAWN_CLOEXEC_DEFAULT = 0x4000
# CPython ignores SIGPIPE/SIGXFSZ and posix_spawn keeps ignored dispositions;
# Popen resets them (restore_signals=True), so must we, or an orphaned sysaudio
# whose reader died never gets SIGPIPE and keeps capturing.
_DEFAULT_SIGNALS = (_signal.SIGPIPE, _signal.SIGXFSZ)
# Set in a process that become_own_responsible() re-ran, so it never loops.
REEXEC_ENV = "MEETING_CAPTURE_OWN_RESPONSIBLE"

_libc = None


def _c():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    return _libc


def supported() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return hasattr(_c(), "responsibility_spawnattrs_setdisclaim")
    except OSError:
        return False


def responsible_pid(pid: Optional[int] = None) -> Optional[int]:
    """The pid macOS charges `pid`'s permission checks to (None if unknown)."""
    if sys.platform != "darwin":
        return None
    try:
        f = _c().responsibility_get_pid_responsible_for_pid
    except (OSError, AttributeError):
        return None
    f.argtypes, f.restype = [ctypes.c_int], ctypes.c_int
    r = f(os.getpid() if pid is None else pid)
    return r if r > 0 else None


def is_own_responsible() -> bool:
    r = responsible_pid()
    return r is None or r == os.getpid()


def _fdnum(spec, default: int) -> Optional[int]:
    """None = inherit `default`; int fd = use it."""
    if spec is None:
        return default
    if isinstance(spec, int) and spec >= 0:
        return spec
    if hasattr(spec, "fileno"):
        return spec.fileno()
    raise ValueError(f"unsupported stdio spec {spec!r}")


class DisclaimedProc:
    """Popen-alike over posix_spawn with TCC responsibility disclaimed.

    stdin/stdout/stderr: PIPE, DEVNULL, None (inherit), an fd or a file.
    Exposes pid, args, stdin, stdout, stderr (binary, unbuffered), returncode,
    poll/wait/send_signal/terminate/kill/communicate like Popen.
    """

    def __init__(self, cmd: Sequence, *, stdin=DEVNULL, stdout=PIPE, stderr=None,
                 env: Optional[dict] = None, disclaim: bool = True) -> None:
        libc = _c()
        self.args = [os.fspath(c) for c in cmd]
        self.returncode: Optional[int] = None
        self.stdin = self.stdout = self.stderr = None
        attr, fa = ctypes.c_void_p(), ctypes.c_void_p()
        if libc.posix_spawnattr_init(ctypes.byref(attr)) != 0:
            raise OSError("posix_spawnattr_init failed")
        libc.posix_spawn_file_actions_init(ctypes.byref(fa))
        close_after: list[int] = []
        ours: dict[int, tuple[int, str]] = {}
        try:
            if disclaim and libc.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), 1) != 0:
                raise OSError("responsibility_spawnattrs_setdisclaim failed")
            sigset = ctypes.c_uint32(0)
            for s in _DEFAULT_SIGNALS:
                sigset.value |= 1 << (int(s) - 1)
            libc.posix_spawnattr_setsigdefault(ctypes.byref(attr), ctypes.byref(sigset))
            empty = ctypes.c_uint32(0)
            libc.posix_spawnattr_setsigmask(ctypes.byref(attr), ctypes.byref(empty))
            libc.posix_spawnattr_setflags(ctypes.byref(attr), ctypes.c_short(
                _POSIX_SPAWN_SETSIGDEF | _POSIX_SPAWN_SETSIGMASK | _POSIX_SPAWN_CLOEXEC_DEFAULT))
            for child_fd, spec in ((0, stdin), (1, stdout), (2, stderr)):
                if spec == PIPE:
                    r, w = os.pipe()
                    theirs, mine = (r, w) if child_fd == 0 else (w, r)
                    ours[child_fd] = (mine, "wb" if child_fd == 0 else "rb")
                    close_after.append(theirs)
                    libc.posix_spawn_file_actions_adddup2(ctypes.byref(fa), theirs, child_fd)
                elif spec == DEVNULL:
                    libc.posix_spawn_file_actions_addopen(
                        ctypes.byref(fa), child_fd, b"/dev/null",
                        os.O_RDONLY if child_fd == 0 else os.O_WRONLY, 0)
                else:
                    src = _fdnum(spec, child_fd)
                    if src == child_fd:   # keep it open across CLOEXEC_DEFAULT
                        libc.posix_spawn_file_actions_addinherit_np(ctypes.byref(fa), child_fd)
                    else:
                        libc.posix_spawn_file_actions_adddup2(ctypes.byref(fa), src, child_fd)
            argv = (ctypes.c_char_p * (len(self.args) + 1))(*[a.encode() for a in self.args], None)
            envd = os.environ if env is None else env
            items = [f"{k}={v}".encode() for k, v in envd.items()]
            envp = (ctypes.c_char_p * (len(items) + 1))(*items, None)
            pid = ctypes.c_int()
            rc = libc.posix_spawn(ctypes.byref(pid), self.args[0].encode(),
                                  ctypes.byref(fa), ctypes.byref(attr), argv, envp)
            if rc != 0:
                for mine, _ in ours.values():
                    os.close(mine)
                raise OSError(rc, f"posix_spawn {self.args[0]}: {os.strerror(rc)}")
            self.pid = pid.value
        finally:
            libc.posix_spawn_file_actions_destroy(ctypes.byref(fa))
            libc.posix_spawnattr_destroy(ctypes.byref(attr))
            for fd in close_after:
                os.close(fd)
        for child_fd, (mine, mode) in ours.items():
            f = os.fdopen(mine, mode, buffering=0)
            setattr(self, ("stdin", "stdout", "stderr")[child_fd], f)

    def poll(self) -> Optional[int]:
        if self.returncode is None:
            try:
                done, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return self.returncode
            if done == self.pid:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        if timeout is None:
            if self.returncode is None:
                _, status = os.waitpid(self.pid, 0)
                self.returncode = os.waitstatus_to_exitcode(status)
            return self.returncode
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.02)
        return self.returncode  # type: ignore[return-value]

    def send_signal(self, sig: int) -> None:
        if self.poll() is None:
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass

    def terminate(self) -> None:
        self.send_signal(_signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(_signal.SIGKILL)

    def communicate(self, timeout: Optional[float] = None) -> tuple[bytes, bytes]:
        """Read stdout and stderr to EOF without deadlock, then reap. On
        timeout the child is killed and TimeoutExpired raised (like run())."""
        if self.stdin is not None:
            self.stdin.close()
        streams = {f.fileno(): (name, f) for name, f in (("out", self.stdout), ("err", self.stderr)) if f}
        bufs = {"out": bytearray(), "err": bytearray()}
        deadline = None if timeout is None else time.monotonic() + timeout
        while streams:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                self.kill(); self.wait()
                raise subprocess.TimeoutExpired(self.args, timeout)
            ready, _, _ = select.select(list(streams), [], [], left)
            for fd in ready:
                chunk = os.read(fd, 65536)
                if chunk:
                    bufs[streams[fd][0]].extend(chunk)
                else:
                    streams.pop(fd)[1].close()
        self.wait(None if deadline is None else max(0.0, deadline - time.monotonic()))
        return bytes(bufs["out"]), bytes(bufs["err"])


def spawn(cmd: Sequence, *, stdin=DEVNULL, stdout=PIPE, stderr=None, env=None):
    """Start `cmd` as its own responsible process. Off macOS, or if the SPI is
    missing, a plain Popen (logged) so capture never stops for want of it."""
    if supported():
        try:
            return DisclaimedProc(cmd, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        except OSError as exc:
            print(f"disclaimed spawn of {cmd[0]} failed ({exc}); plain spawn instead — "
                  "macOS will attribute its permissions to this process's launcher",
                  file=sys.stderr, flush=True)
    return subprocess.Popen([os.fspath(c) for c in cmd], stdin=stdin, stdout=stdout,
                            stderr=stderr, env=env, bufsize=0)


def run(cmd: Sequence, *, timeout: Optional[float] = None, text: bool = True,
        env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """subprocess.run(cmd, capture_output=True, stdin=DEVNULL, timeout=…, text=…),
    with the child as its own responsible process."""
    p = spawn(cmd, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, env=env)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if isinstance(p, subprocess.Popen):   # DisclaimedProc already killed and reaped it
            p.kill(); p.wait()
        raise
    if text:
        out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    return subprocess.CompletedProcess(list(cmd), p.returncode, out, err)


def become_own_responsible(argv: Optional[Sequence[str]] = None) -> None:
    """If this process would be charged to someone else (a terminal, or the
    app that started it), re-run it disclaimed with the same stdio, forward
    signals, and exit with its status. No-op under launchd, when already
    disclaimed, or without the SPI.

    For entry points that open the microphone IN-PROCESS (sounddevice):
    `meeting-capture ui` (meters), `meeting-capture run` and
    `meeting-capture check --request microphone_linein`, so line-in meters,
    the probe and the recorder all answer to one identity."""
    if os.environ.get(REEXEC_ENV) == "1" or not supported() or is_own_responsible():
        return
    if argv is None:
        argv = [sys.executable, *getattr(sys, "orig_argv", sys.argv)[1:]]
    env = dict(os.environ, **{REEXEC_ENV: "1"})
    try:
        child = DisclaimedProc(argv, stdin=None, stdout=None, stderr=None, env=env)
    except OSError as exc:
        print(f"could not re-run as its own responsible process ({exc}); continuing",
              file=sys.stderr, flush=True)
        return
    for s in (_signal.SIGINT, _signal.SIGTERM, _signal.SIGHUP):
        _signal.signal(s, lambda sig, _f: child.send_signal(sig))
    rc = child.wait()
    raise SystemExit(rc if rc >= 0 else 128 - rc)


def tcc_subject(path) -> str:
    """Who macOS asks about when `path` runs disclaimed: the bundle id of the
    OUTERMOST `.app` around it (sysaudio in Contorch.app/Contents/Helpers is
    the app; the lab proved a nested helper app is charged to the outer one
    too), else the executable's real path (a bare Homebrew sysaudio is keyed by
    its path). Reads the bundle's Info.plist directly: inside the app's own
    interpreter NSBundle.mainBundle is the stub's embedded __info_plist, not
    the app."""
    import plistlib
    from pathlib import Path
    p = Path(os.path.abspath(os.fspath(path)))
    outer = next((a for a in reversed(p.parents) if a.suffix == ".app"), None)
    if outer is not None:
        try:
            ident = plistlib.loads((outer / "Contents" / "Info.plist").read_bytes()).get("CFBundleIdentifier")
        except Exception:
            ident = None
        if ident:
            return str(ident)
    return os.path.realpath(p)
