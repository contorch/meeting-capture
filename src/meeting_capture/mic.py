"""Microphone activity detection — proxy for 'we are in a call right now'.

Enumerates Core Audio HAL process objects (`kAudioHardwarePropertyProcessObjectList`,
macOS 14+) and reports the mic as active when a process OTHER than Apple's
`replayd` has audio input running (`kAudioProcessPropertyIsRunningInput`).
Catches Zoom, Teams, FaceTime, browser meetings, Slack calls, Discord,
etc. — anything that opens the mic, regardless of whether it goes through
AVCaptureSession or Core Audio directly.

Why replayd is excluded: ALL ScreenCaptureKit capture — including our own
sysaudio --mic (own-voice channel) — is attributed to `com.apple.replayd`,
not to the SCK client process. Without the exclusion, the daemon's own
recording session flips the mic-activity gate and the session never ends
(and replayd's input session even lingers a while after the SCK client
exits). Real meeting apps hold the mic under their own process, so gating
still catches them; other SCK-only consumers (Cluely, OBS, Loom) are
excluded too — correct, since "some tool is recording" is not "the user is
in a call". On macOS 13 (no process objects) we fall back to the device-wide
`kAudioDevicePropertyDeviceIsRunningSomewhere` — the same signal that drives
the orange menu-bar mic indicator — which is safe there because we never
open the mic ourselves on macOS < 15.

(We previously tried AVCaptureDevice.isInUseByAnotherApplication, which only
fires when another AVCaptureSession-using app holds the device. Almost no
real meeting app does — they all use Core Audio directly. So that approach
returned False even mid-Zoom-call. Lesson preserved as repo_knowledge.)

Implemented with ctypes against the system's CoreAudio framework so we have
no third-party dependency. macOS only.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import struct
import sys

_KAUDIO_OBJECT_SYSTEM_OBJECT = 1
_K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN = 0


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]


_K_HARDWARE_PROPERTY_DEVICES = _fourcc("dev#")
_K_HARDWARE_PROPERTY_DEFAULT_INPUT_DEVICE = _fourcc("dIn ")
_K_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE = _fourcc("dOut")
_K_HARDWARE_PROPERTY_PROCESS_OBJECT_LIST = _fourcc("prs#")
_K_DEVICE_PROPERTY_IS_RUNNING_SOMEWHERE = _fourcc("gone")
_K_DEVICE_PROPERTY_STREAM_CONFIGURATION = _fourcc("slay")
_K_OBJECT_PROPERTY_NAME = _fourcc("lnam")
_K_PROCESS_PROPERTY_BUNDLE_ID = _fourcc("pbid")
_K_PROCESS_PROPERTY_IS_RUNNING_INPUT = _fourcc("piri")
_K_SCOPE_GLOBAL = _fourcc("glob")
_K_SCOPE_INPUT = _fourcc("inpt")

# HAL process objects whose input activity does NOT mean "the user is in a
# call". replayd is ScreenCaptureKit's capture backend: our own sysaudio
# --mic session lands there, as do Cluely/Loom/OBS-style recorders.
_EXCLUDED_INPUT_BUNDLES = frozenset({"com.apple.replayd"})


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


def _load_coreaudio():
    if sys.platform != "darwin":
        return None
    path = ctypes.util.find_library("CoreAudio")
    if path is None:
        return None
    lib = ctypes.CDLL(path)
    lib.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(_AudioObjectPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    lib.AudioObjectGetPropertyData.restype = ctypes.c_int
    lib.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(_AudioObjectPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    lib.AudioObjectGetPropertyDataSize.restype = ctypes.c_int
    return lib


_CA = _load_coreaudio()


def _all_device_ids() -> list[int]:
    if _CA is None:
        return []
    addr = _AudioObjectPropertyAddress(
        _K_HARDWARE_PROPERTY_DEVICES,
        _K_SCOPE_GLOBAL,
        _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    size = ctypes.c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(
        _KAUDIO_OBJECT_SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)
    ) != 0:
        return []
    n = size.value // ctypes.sizeof(ctypes.c_uint32)
    ids = (ctypes.c_uint32 * n)()
    if _CA.AudioObjectGetPropertyData(
        _KAUDIO_OBJECT_SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size), ids
    ) != 0:
        return []
    return list(ids)


def _has_input_streams(device_id: int) -> bool:
    if _CA is None:
        return False
    addr = _AudioObjectPropertyAddress(
        _K_DEVICE_PROPERTY_STREAM_CONFIGURATION,
        _K_SCOPE_INPUT,
        _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    size = ctypes.c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size)
    ) != 0:
        return False
    # AudioBufferList = uint32 mNumberBuffers + array of AudioBuffer.
    # 8 bytes = header + zero buffers; anything more means at least one input buffer exists.
    return size.value > 8


def _is_device_running(device_id: int) -> bool:
    if _CA is None:
        return False
    addr = _AudioObjectPropertyAddress(
        _K_DEVICE_PROPERTY_IS_RUNNING_SOMEWHERE,
        _K_SCOPE_GLOBAL,
        _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    if _CA.AudioObjectGetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(val)
    ) != 0:
        return False
    return val.value == 1


def _cf_string_prop(object_id: int, selector: int) -> str | None:
    """Read a CFString-valued HAL property off any audio object."""
    if _CA is None:
        return None
    addr = _AudioObjectPropertyAddress(
        selector,
        _K_SCOPE_GLOBAL,
        _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    cf_str = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(cf_str))
    if _CA.AudioObjectGetPropertyData(
        object_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(cf_str)
    ) != 0 or not cf_str.value:
        return None
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_int
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    try:
        # kCFStringEncodingUTF8 = 0x08000100
        ptr = cf.CFStringGetCStringPtr(cf_str.value, 0x08000100)
        if ptr:
            return ptr.decode("utf-8", errors="replace")
        length = cf.CFStringGetLength(cf_str.value)
        buf = ctypes.create_string_buffer((length + 1) * 4)
        if cf.CFStringGetCString(cf_str.value, buf, len(buf), 0x08000100):
            return buf.value.decode("utf-8", errors="replace")
        return None
    finally:
        cf.CFRelease(cf_str.value)


def _device_name(device_id: int) -> str | None:
    return _cf_string_prop(device_id, _K_OBJECT_PROPERTY_NAME)


def _process_object_ids() -> list[int]:
    """HAL process objects (macOS 14+). Empty list on older macOS / query failure."""
    if _CA is None:
        return []
    addr = _AudioObjectPropertyAddress(
        _K_HARDWARE_PROPERTY_PROCESS_OBJECT_LIST,
        _K_SCOPE_GLOBAL,
        _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    size = ctypes.c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(
        _KAUDIO_OBJECT_SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)
    ) != 0:
        return []
    n = size.value // ctypes.sizeof(ctypes.c_uint32)
    if n == 0:
        return []
    ids = (ctypes.c_uint32 * n)()
    if _CA.AudioObjectGetPropertyData(
        _KAUDIO_OBJECT_SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size), ids
    ) != 0:
        return []
    return list(ids)


def _process_is_running_input(process_obj: int) -> bool:
    if _CA is None:
        return False
    addr = _AudioObjectPropertyAddress(
        _K_PROCESS_PROPERTY_IS_RUNNING_INPUT,
        _K_SCOPE_GLOBAL,
        _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    if _CA.AudioObjectGetPropertyData(
        process_obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(val)
    ) != 0:
        return False
    return val.value == 1


def _process_bundle_id(process_obj: int) -> str | None:
    return _cf_string_prop(process_obj, _K_PROCESS_PROPERTY_BUNDLE_ID)


def is_mic_active() -> bool:
    """True if a non-excluded process is currently running audio input.

    "In a call" gating signal: process-level attribution (macOS 14+) so our
    own SCK mic capture — attributed to com.apple.replayd — doesn't trip the
    gate; device-level fallback on older macOS.
    """
    procs = _process_object_ids()
    if procs:
        for obj in procs:
            if not _process_is_running_input(obj):
                continue
            if _process_bundle_id(obj) in _EXCLUDED_INPUT_BUNDLES:
                continue
            return True
        return False
    # Fallback (macOS 13 / query failure): device-wide check. Safe there
    # because we never open the mic ourselves on macOS < 15.
    for dev_id in _all_device_ids():
        if _has_input_streams(dev_id) and _is_device_running(dev_id):
            return True
    return False


def active_mic_name() -> str | None:
    """Localized name of the first input device currently in use, or None."""
    for dev_id in _all_device_ids():
        if _has_input_streams(dev_id) and _is_device_running(dev_id):
            return _device_name(dev_id)
    return None


def mic_name() -> str | None:
    """Localized name of any input device (the first one with input streams), for diagnostics."""
    for dev_id in _all_device_ids():
        if _has_input_streams(dev_id):
            return _device_name(dev_id)
    return None


def _default_device_id(prop_selector: int) -> int | None:
    if _CA is None:
        return None
    addr = _AudioObjectPropertyAddress(
        prop_selector, _K_SCOPE_GLOBAL, _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    if _CA.AudioObjectGetPropertyData(
        _KAUDIO_OBJECT_SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
        ctypes.byref(size), ctypes.byref(val),
    ) != 0 or val.value == 0:
        return None
    return val.value


def default_devices_snapshot() -> dict[str, str | None]:
    """Names of the current default input and output audio devices.

    The daemon polls this and logs a line when either name changes — useful
    for diagnosing recording dropouts that correlate with output-device
    switches (Bluetooth (re)connect, headphone unplug, virtual device
    insertion by other apps, etc.).
    """
    in_id = _default_device_id(_K_HARDWARE_PROPERTY_DEFAULT_INPUT_DEVICE)
    out_id = _default_device_id(_K_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE)
    return {
        "input": _device_name(in_id) if in_id else None,
        "output": _device_name(out_id) if out_id else None,
    }
