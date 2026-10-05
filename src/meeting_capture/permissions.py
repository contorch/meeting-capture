"""The recorder's permissions, as one document: `meeting-capture check --json`.

macOS decides these for the recorder: how "them" is captured needs Screen &
System Audio Recording (sysaudio's sck backend, ScreenCaptureKit) or System
Audio Recording Only (its taps backend, Core Audio process taps), and the
Microphone covers "me" and every line-in input.
Only `sysaudio check` can read them as the recorder's helper sees them, so
this module starts it through tccspawn (its own responsible process, the same
identity as when it captures) and turns its answer into rows that other
programs show as they are: pipeline-monitor's menu and doctor, `contorch
setup`'s permission step, and the Phase 2 app. Nobody else re-derives a hint.

Schema `meeting-capture.permissions/1` (README "Contract"):

    {"schema", "ok", "channel", "error"?{code, message},
     "identity": {"helper": path|null, "subject": str|null},
     "permissions": [{"id": "screen_audio"|"system_audio"|"microphone", "status",
                      "required", "can_request", "hint", "settings_url"}],
     "backend": {"selected": "taps"|"sck", "setting": "auto"|"taps"|"sck",
                 "reason", "available": [..]|null},
     "requested": null|"screen_audio"|"system_audio"|"microphone"}

status: granted | not_granted (screen: macOS doesn't say "denied") | denied |
not_determined | restricted | unknown (no helper, or one too old to answer) |
unsupported (system_audio before macOS 14.2).
Exactly one of screen_audio / system_audio is required with source sck: the
one the selected backend needs (recorder.choose_backend). The system_audio
row and "backend" are additions within schema 1 (a reader that doesn't know
the row shows it by id or ignores it).
One microphone row in every channel: inside Contorch.app the meters, line-in
and sysaudio's mic are one "Contorch" row (contorch-macos tcc study).
"""
from __future__ import annotations

import json
import os
import subprocess

SCHEMA = "meeting-capture.permissions/1"
SYSAUDIO_SCHEMA = "sysaudio.check/1"

SCREEN = "screen_audio"
AUDIO = "system_audio"
MIC = "microphone"
ROWS = (SCREEN, AUDIO, MIC)
REQUEST_ARG = {SCREEN: "screen", AUDIO: "system_audio", MIC: "mic"}

SETTINGS_URL = {
    SCREEN: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    AUDIO: "x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture",
    MIC: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}
PANE = {
    SCREEN: "System Settings › Privacy & Security › Screen & System Audio Recording",
    AUDIO: "System Settings › Privacy & Security › Screen & System Audio Recording › System Audio Recording Only",
    MIC: "System Settings › Privacy & Security › Microphone",
}
TITLE = {SCREEN: "Screen & System Audio Recording", AUDIO: "System Audio Recording Only", MIC: "Microphone"}

CHECK_TIMEOUT_S = 30
# A request waits for the user to answer macOS's prompt.
REQUEST_TIMEOUT_S = 300


def _sysaudio_check(binary, request: str | None) -> tuple[dict | None, dict | None]:
    """(sysaudio.check/1 document, None) or (None, error)."""
    from . import tccspawn
    cmd = [str(binary), "check", "--json"]
    if request:
        cmd += ["--request", REQUEST_ARG[request]]
    try:
        r = tccspawn.run(cmd, timeout=REQUEST_TIMEOUT_S if request else CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, {"code": "helper_timeout", "message": f"{binary} check did not answer in time"}
    except OSError as exc:
        return None, {"code": "helper_failed", "message": f"could not run {binary}: {exc}"}
    if r.returncode != 0 and "unknown arg" in (r.stderr or ""):
        return None, {"code": "helper_too_old",
                      "message": f"{binary} predates `sysaudio check` (upgrade meeting-capture for one that has it)"}
    lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
    try:
        doc = json.loads(lines[-1])
    except (IndexError, ValueError):
        doc = None
    if r.returncode != 0 or not isinstance(doc, dict) or doc.get("schema") != SYSAUDIO_SCHEMA:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [f"exit {r.returncode}"]
        return None, {"code": "helper_failed", "message": f"{binary} check failed: {tail[0][:300]}"}
    return doc, None


def _required(source: str, mic_on: bool, backend: str = "sck") -> dict[str, bool]:
    """Which permissions the recorder needs with its current settings."""
    if source == "linein":
        # Line-in reads the interface in-process; sysaudio isn't used.
        return {SCREEN: False, AUDIO: False, MIC: True}
    return {SCREEN: backend != "taps", AUDIO: backend == "taps", MIC: mic_on}


def _hint(perm: str, status: str, channel: str, helper: str | None, source: str) -> str | None:
    if status == "granted":
        return None
    pane = PANE[perm]
    if perm == AUDIO and status == "unsupported":
        return "System Audio Recording Only needs macOS 14.2 or later; the recorder uses Screen & System Audio Recording."
    if channel == "app":
        if status in ("not_determined", "unknown") or (perm == SCREEN and status == "not_granted"):
            return f"Allow Contorch when macOS asks, or turn Contorch on in {pane}."
        return f"Turn Contorch on in {pane}."
    if perm == MIC and source == "linein":
        return (f"Line-in records in the recorder's own Python process: allow it in {pane} "
                "(a Homebrew Python has no microphone usage text, so macOS may deny it without asking).")
    if not helper:
        return "No sysaudio binary was found: run `meeting-capture doctor`."
    if perm == SCREEN:
        return (f"Turn sysaudio on in {pane}. If it isn't listed, press +, then ⌘⇧G and paste {helper} "
                "(again after a macOS update or a sysaudio rebuild).")
    if perm == AUDIO:
        if status in ("not_determined", "unknown"):
            return (f"Run `meeting-capture check --request system_audio` and allow sysaudio ({helper}) "
                    f"when macOS asks, or turn it on in {pane}.")
        return f"Turn sysaudio on in {pane}."
    if status == "not_determined":
        return f"macOS asks for sysaudio ({helper}) the first time it records your side; allow it."
    return f"Turn sysaudio on in {pane}."


def document(request: str | None = None, env: dict | None = None) -> dict:
    """The permissions document (see the module docstring) for the recorder's
    settings `env` (default: this process's). Never raises for a state; a
    missing or old helper is an `error` with status "unknown" rows."""
    from . import channel_guard, linein, recorder, tccspawn
    env = os.environ if env is None else env
    chan = channel_guard.channel()
    helper = recorder.find_sysaudio()
    helper_s = os.path.abspath(helper) if helper else None
    source = "linein" if (env.get(linein.SOURCE_ENV) or "").strip().lower() == "linein" else "sck"
    mic_on = ((env.get(recorder.MIC_ENV_VAR) or "1").strip().lower() not in ("0", "false", "no", "off")
              and recorder.mic_capture_supported())
    doc = {"schema": SCHEMA, "ok": True, "channel": chan, "requested": request,
           "identity": {"helper": helper_s, "subject": tccspawn.tcc_subject(helper) if helper else None}}
    statuses = {SCREEN: "unknown", AUDIO: "unknown", MIC: "unknown"}
    got = None
    if helper is None:
        doc["ok"] = False
        doc["error"] = {"code": "no_helper", "message": "no sysaudio binary found (meeting-capture doctor)"}
    else:
        got, err = _sysaudio_check(helper, request)
        if err:
            doc["ok"] = False
            doc["error"] = err
        else:
            statuses = {SCREEN: str(got.get("screen_capture") or "unknown"),
                        # A sysaudio that predates taps doesn't report it: unsupported here.
                        AUDIO: str(got.get("system_audio") or "unsupported"),
                        MIC: str(got.get("microphone") or "unknown")}
    plan = recorder.choose_backend(recorder.backend_setting(env), got, recorder._macos_major())
    doc["backend"] = {k: plan[k] for k in ("selected", "setting", "reason", "available")}
    required = _required(source, mic_on, plan["selected"])
    taps_here = "taps" in (plan["available"] or [])
    rows = []
    for perm in ROWS:
        st = statuses[perm]
        can = st == "not_determined" or (perm == SCREEN and st == "not_granted")
        if perm == AUDIO:
            # "unknown" here = TCC's preflight is missing: sysaudio can still ask.
            can = taps_here and st in ("not_determined", "unknown") and got is not None
        if perm == MIC and source == "linein" and chan != "app":
            can = False    # sysaudio's prompt grants sysaudio, not the recorder's Python
        if st == "unknown" and doc.get("error"):
            hint = f"Couldn't read this permission: {doc['error']['message']}"
        else:
            hint = _hint(perm, st, chan, helper_s, source)
        rows.append({"id": perm, "status": st, "required": required[perm], "can_request": can,
                     "hint": hint, "settings_url": SETTINGS_URL[perm]})
    doc["permissions"] = rows
    return doc


def text_lines(doc: dict) -> list[str]:
    """`meeting-capture check` without --json: the same document as text."""
    ident = doc["identity"]
    lines = [f"sysaudio:          {ident['helper'] or 'NOT FOUND'}"]
    if ident["subject"]:
        lines.append(f"  asks macOS as:   {ident['subject']}")
    if doc.get("error"):
        lines.append(f"  note:            {doc['error']['message']}")
    if doc.get("backend"):
        lines.append(f"capture backend:   {doc['backend']['selected']} — {doc['backend']['reason']}")
    for row in doc["permissions"]:
        need = "" if row["required"] else " (not needed with these settings)"
        lines.append(f"{TITLE[row['id']] + ':':<34} {row['status'].replace('_', ' ')}{need}")
        if row["hint"] and row["required"]:
            lines.append(f"  → {row['hint']}")
    return lines
