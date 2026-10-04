"""tccspawn: every sysaudio/line-in entry point runs as its own responsible process."""
import os
import signal
import subprocess
import sys

import pytest

from meeting_capture import tccspawn

darwin = pytest.mark.skipif(sys.platform != "darwin" or not tccspawn.supported(),
                            reason="responsibility SPI is darwin-only")
PY = sys.executable
RESP = ("import os,sys; sys.path[:0]=%r; from meeting_capture import tccspawn as t; "
        "print(t.responsible_pid()==os.getpid())" % (sys.path,))


@darwin
def test_disclaimed_child_is_its_own_responsible_process():
    r = tccspawn.run([PY, "-c", RESP], timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "True"


@darwin
def test_plain_popen_child_is_not_its_own_responsible_process_under_a_parent():
    # Sanity check of the premise; skip when the test runner is itself its
    # own responsible process (e.g. launched by launchd).
    if tccspawn.is_own_responsible():
        pytest.skip("runner is its own responsible process")
    r = subprocess.run([PY, "-c", RESP], capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == "False"


def test_run_captures_both_streams_and_exit_code():
    r = tccspawn.run(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], timeout=10)
    assert (r.returncode, r.stdout, r.stderr) == (3, "out\n", "err\n")


def test_run_timeout_kills_child():
    with pytest.raises(subprocess.TimeoutExpired):
        tccspawn.run(["/bin/sleep", "30"], timeout=0.5)


def test_large_output_does_not_deadlock():
    r = tccspawn.run([PY, "-c", "import sys; sys.stdout.write('x'*500000); sys.stderr.write('y'*500000)"],
                     timeout=30)
    assert len(r.stdout) == 500000 and len(r.stderr) == 500000


def test_children_get_default_sigpipe():
    # CPython ignores SIGPIPE and posix_spawn keeps ignored signals; a capture
    # child must die when its reader goes away (Popen's restore_signals).
    p = tccspawn.spawn(["/usr/bin/yes"], stdout=tccspawn.PIPE)
    p.stdout.read(16)
    p.stdout.close()
    assert p.wait(timeout=5) == -signal.SIGPIPE


def test_only_stdio_is_inherited():
    rfd, wfd = os.pipe()
    os.set_inheritable(wfd, True)
    try:
        r = tccspawn.run([PY, "-c", f"import os; os.fstat({wfd}); print('leaked')"], timeout=30)
        assert "leaked" not in r.stdout
    finally:
        os.close(rfd); os.close(wfd)


def test_terminate():
    p = tccspawn.spawn(["/bin/sleep", "30"], stdout=tccspawn.DEVNULL)
    p.terminate()
    assert p.wait(timeout=5) == -signal.SIGTERM


def test_fallback_to_popen_when_spawn_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(tccspawn, "DisclaimedProc", boom)
    p = tccspawn.spawn(["/bin/echo", "fallback"])
    assert p.stdout.read() == b"fallback\n"
    p.wait(timeout=5)


@darwin
def test_become_own_responsible_reexecs_once(tmp_path):
    script = tmp_path / "s.py"
    script.write_text(
        "import os,sys; sys.path[:0]=%r\n"
        "from meeting_capture import tccspawn as t\n"
        "t.become_own_responsible()\n"
        "print(os.environ.get(t.REEXEC_ENV), t.is_own_responsible())\n" % (sys.path,))
    r = subprocess.run([PY, str(script)], capture_output=True, text=True, timeout=30)
    expect = "None True" if tccspawn.is_own_responsible() else "1 True"
    assert r.stdout.strip() == expect, r.stderr
