"""The recorder's permissions, as one document: `meeting-capture check --json`.

macOS decides two things for the recorder: Screen & System Audio Recording
(ScreenCaptureKit, "them") and the Microphone ("me", and every line-in input).
Only `sysaudio check` can read them as the recorder's helper sees them, so
this module starts it through tccspawn (its own responsible process, the same
identity as when it captures) and turns its answer into rows that other
programs show as they are: pipeline-monitor's menu and doctor, `contorch
setup`'s permission step, and the Phase 2 app. Nobody else re-derives a hint.

Schema `meeting-capture.permissions/1` (README "Contract"):

    {"schema", "ok", "channel", "error"?{code, message},
     "identity": {"helper": path|null, "subject": str|null},
     "permissions": [{"id": "screen_audio"|"microphone", "status", "required",
                      "can_request", "hint", "settings_url"}],
     "requested": null|"screen_audio"|"microphone"}

status: granted | not_granted (screen: macOS doesn't say "denied") | denied |
not_determined | restricted | unknown (no helper, or one too old to answer).
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
MIC = "microphone"
REQUEST_ARG = {SCREEN: "screen", MIC: "mic"}

SETTINGS_URL = {
    SCREEN: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    MIC: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}
PANE = {
    SCREEN: "System Settings › Privacy & Security › Screen & System Audio Recording",
    MIC: "System Settings › Privacy & Security › Microphone",
}

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
                      "message": f"{binary} predates `sysaudio check` (meeting-capture 0.7 ships one that has it)"}
    lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
    try:
        doc = json.loads(lines[-1])
    except (IndexError, ValueError):
        doc = None
    if r.returncode != 0 or not isinstance(doc, dict) or doc.get("schema") != SYSAUDIO_SCHEMA:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [f"exit {r.returncode}"]
        return None, {"code": "helper_failed", "message": f"{binary} check failed: {tail[0][:300]}"}
    return doc, None


def _required(source: str, mic_on: bool) -> dict[str, bool]:
    """Which permissions the recorder needs with its current settings."""
    if source == "linein":
        # Line-in reads the interface in-process; ScreenCaptureKit isn't used.
        return {SCREEN: False, MIC: True}
    return {SCREEN: True, MIC: mic_on}


def _hint(perm: str, status: str, channel: str, helper: str | None, source: str) -> str | None:
    if status == "granted":
        return None
    pane = PANE[perm]
    if channel == "app":
        if status == "not_determined" or (perm == SCREEN and status == "not_granted"):
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
    required = _required(source, mic_on)
    doc = {"schema": SCHEMA, "ok": True, "channel": chan, "requested": request,
           "identity": {"helper": helper_s, "subject": tccspawn.tcc_subject(helper) if helper else None}}
    statuses = {SCREEN: "unknown", MIC: "unknown"}
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
                        MIC: str(got.get("microphone") or "unknown")}
    rows = []
    for perm in (SCREEN, MIC):
        st = statuses[perm]
        can = st == "not_determined" or (perm == SCREEN and st == "not_granted")
        if perm == MIC and source == "linein" and chan != "app":
            can = False    # sysaudio's prompt grants sysaudio, not the recorder's Python
        hint = (_hint(perm, st, chan, helper_s, source) if st != "unknown"
                else f"Couldn't read this permission: {doc['error']['message']}")
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
    names = {SCREEN: "Screen & System Audio Recording", MIC: "Microphone"}
    for row in doc["permissions"]:
        need = "" if row["required"] else " (not needed with these settings)"
        lines.append(f"{names[row['id']] + ':':<34} {row['status'].replace('_', ' ')}{need}")
        if row["hint"] and row["required"]:
            lines.append(f"  → {row['hint']}")
    return lines
