"""Self-watchdog: exit if our own memory footprint runs away.

A long-lived daemon that leaks (e.g. the MLX/Metal GPU buffer leak that put a
meeting-capture daemon at 24.5 GB over 16 days) should fall on its sword and let
launchd KeepAlive respawn it clean, rather than drag the whole machine into swap.

The metric MUST be phys_footprint, not RSS. Both leaks we've seen were ~98%
swapped/compressed, so RSS read ~18-58 MB the entire time while the real
footprint was tens of GB. phys_footprint (the same number Activity Monitor and
`footprint`/`vmmap` report) counts the dirty memory the task owns including
compressed + swapped pages, so it actually tracks the leak.

We read it via the mach task_info(TASK_VM_INFO) call through ctypes — no
third-party dependency. macOS only; on any failure the watchdog no-ops (it will
never falsely kill the daemon).
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys

log = logging.getLogger("meeting_capture.watchdog")

# Self-exit above this footprint. Override via MEETING_CAPTURE_MAX_FOOTPRINT_MB.
# 2 GB is far above a healthy daemon (~20-80 MB in steady state) but well below
# the point where the machine starts thrashing.
ENV_MAX_FOOTPRINT_MB = "MEETING_CAPTURE_MAX_FOOTPRINT_MB"
DEFAULT_MAX_FOOTPRINT_MB = 2048

_TASK_VM_INFO = 22
# Offset of phys_footprint within task_vm_info (bytes). Layout is stable across
# macOS releases: 1 mach_vm_size_t + 2 integer_t + 17 mach_vm_size_t precede it.
_PHYS_FOOTPRINT_OFFSET = 144


def phys_footprint_bytes() -> int | None:
    """Current task phys_footprint in bytes, or None if unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mach_task_self = ctypes.c_uint.in_dll(libc, "mach_task_self_").value
        # Generous buffer; task_vm_info has more fields after phys_footprint.
        buf = (ctypes.c_byte * 512)()
        count = ctypes.c_uint(512 // ctypes.sizeof(ctypes.c_uint))
        kr = libc.task_info(
            mach_task_self,
            _TASK_VM_INFO,
            ctypes.byref(buf),
            ctypes.byref(count),
        )
        if kr != 0:
            return None
        return int(ctypes.c_uint64.from_buffer(buf, _PHYS_FOOTPRINT_OFFSET).value)
    except Exception:
        return None


def _limit_bytes() -> int:
    try:
        mb = int(os.environ.get(ENV_MAX_FOOTPRINT_MB, DEFAULT_MAX_FOOTPRINT_MB))
    except ValueError:
        mb = DEFAULT_MAX_FOOTPRINT_MB
    return mb * 1024 * 1024


def check_and_maybe_exit() -> None:
    """If our footprint exceeds the limit, log loudly and exit so launchd
    respawns us. No-ops when the footprint can't be read."""
    fp = phys_footprint_bytes()
    if fp is None:
        return
    limit = _limit_bytes()
    if fp >= limit:
        log.error(
            "phys_footprint %.0f MB exceeds limit %.0f MB — exiting for a clean "
            "launchd respawn (likely a memory leak)",
            fp / 1024 / 1024,
            limit / 1024 / 1024,
        )
        # Non-zero exit => launchd KeepAlive {SuccessfulExit: False} respawns us.
        sys.exit(1)
