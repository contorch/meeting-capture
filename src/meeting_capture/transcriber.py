"""Transcription for meeting-capture audio chunks.

Two engines:

  * **On this Mac** ("apple") — Apple's SpeechAnalyzer / SpeechTranscriber,
    run by the signed ``sysaudio`` helper (``sysaudio transcribe``). macOS 26+
    on Apple silicon. Audio never leaves the Mac, no account or API key, one
    model per language family (English is usually already on the Mac; other
    languages download once from Apple). Ignores custom vocabulary.
  * **Gemini** ("gemini") — Google's hosted models (below). Needs an API key;
    each chunk is uploaded.

Which one runs is MEETING_CAPTURE_STT (in the launchd plist; `meeting-capture stt`):

  auto    (default) on this Mac when the helper says it is usable for the
          language (model installed); otherwise Gemini if an API key resolves;
          otherwise nothing — the daemon keeps the audio and retries later.
  apple   on this Mac only. NEVER uploads: when it can't run, the audio is
          parked and retried on-device later.
  gemini  always Gemini (the behaviour before on-device transcription).

MEETING_CAPTURE_LOCALE picks the on-device language (Gemini detects the
language itself). Unset, it follows the Mac: the first preferred language
(AppleLanguages, with AppleLocale's region when that language has none) that
the helper's probe lists as supported; otherwise en-US. When that fallback is
a guess — the Mac's language isn't English and can't be transcribed on this
Mac — auto prefers Gemini if a key is set (locale_choice(), resolve_backend()).
`meeting-capture stt|language` and the settings page always write the choice
they make, so an unset MEETING_CAPTURE_STT means nobody has picked an engine
yet: someone with a Gemini key whose auto setting now runs on this Mac gets
upgrade_notice() in status, doctor, `stt`, the settings page and the daemon
log until they do. This module is the only implementation of these rules:
pipeline-monitor (menu bar, `contorch setup|status|doctor`) asks for the
result with `meeting-capture stt --json` (engine_summary() plus live mode,
cli.transcription_summary(); the contract is in the README under "Contract")
instead of re-deriving it. The legacy MEETING_CAPTURE_TRANSCRIBER
(gemini|whisper) found in old plists is read as auto.

Helper contract (``sysaudio transcribe``; MEETING_CAPTURE_TRANSCRIBE_BIN
overrides the binary for development and tests):

  --probe [--locale L]   one JSON line; exit 0 usable now, 69 unusable here
                         (macOS < 26, Intel, locale unsupported), 75 supported
                         but the model is not installed
  --install --locale L   installs/reserves the model; exit 0 ok, 69
                         unsupported, 1 other error
  [--locale L] FILE      one JSON line {"text", "segments", "locale", "ms"};
                         exit 0 ok (no speech -> ""), 69/75 unavailable or
                         model missing/released, 70 this file is unreadable,
                         1 anything else
  Older sysaudio builds answer "unknown arg: transcribe" (exit 1): unavailable.

Gemini backends, chosen by model name:

  * ``gemini-3.5-transcribe`` (default) — Google's purpose-built speech-to-text
    model via the Interactions API. Verbatim by default, deterministic proper
    nouns via ``custom_vocabulary`` (``~/.meeting-capture/vocab.txt``), optional
    speaker diarization for the "them" channel (MEETING_CAPTURE_DIARIZE=1 —
    mutually exclusive with vocabulary, per the API).
  * Any other Gemini model (e.g. ``gemini-2.5-flash``) — general audio
    understanding via ``generate_content`` with a transcription prompt. Also the
    automatic fallback if the transcribe backend errors, so a preview-model
    hiccup never loses a chunk.

Gotcha preserved here for posterity: sending audio to a ``gemini-3.5-transcribe``
model through ``generate_content`` returns an EMPTY transcript while still
billing the audio tokens — hence the hard dispatch below.

Gemini needs a Google API key, resolved in order from:
  $GOOGLE_API_KEY, $GEMINI_API_KEY, or ~/.config/google/key (mode 600).
Override the model with MEETING_CAPTURE_GEMINI_MODEL. None of this is needed
when transcription runs on this Mac.

(A local mlx-whisper backend was removed: it ran on the GPU and its unbounded
MLX Metal buffer cache leaked tens of GB in a long-lived daemon. The on-device
engine runs in Apple's speech service, outside our process, one short-lived
helper process per chunk.)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import plistlib
import re
import subprocess
import threading
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .paths import VOCAB_FILE

log = logging.getLogger("meeting-capture.transcriber")

DEFAULT_GEMINI_MODEL = "gemini-3.5-transcribe"
FALLBACK_GEMINI_MODEL = "gemini-2.5-flash"

ENV_GEMINI_MODEL = "MEETING_CAPTURE_GEMINI_MODEL"
ENV_DIARIZE = "MEETING_CAPTURE_DIARIZE"
GEMINI_KEY_FILE = Path.home() / ".config" / "google" / "key"

REQUEST_TIMEOUT_MS = 60_000
MAX_VOCAB_TERMS = 1000

# --- engine choice --------------------------------------------------------------------

ENV_STT = "MEETING_CAPTURE_STT"
ENV_LOCALE = "MEETING_CAPTURE_LOCALE"
ENV_LEGACY_TRANSCRIBER = "MEETING_CAPTURE_TRANSCRIBER"
ENV_TRANSCRIBE_BIN = "MEETING_CAPTURE_TRANSCRIBE_BIN"

STT_CHOICES = ("auto", "apple", "gemini")
DEFAULT_STT = "auto"
DEFAULT_LOCALE = "en-US"

ENGINE_LABELS = {"apple": "On this Mac", "gemini": "Gemini", "none": "None"}
CHOICE_LABELS = {"auto": "Automatic", "apple": "On this Mac", "gemini": "Gemini"}

# sysexits.h codes the helper uses.
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_TEMPFAIL = 75

PROBE_TTL_S = 600.0          # re-probe at most every 10 minutes (cleared on 69/75)
PROBE_TIMEOUT_S = 20.0
INSTALL_TIMEOUT_S = 30 * 60.0
# Per-chunk helper timeout: max(30 s, 0.5 x the chunk's duration). Measured
# 8-170x realtime on an M3 Pro; 15x under heavy load — 0.5x is ample headroom.
APPLE_MIN_TIMEOUT_S = 30.0
APPLE_TIMEOUT_FACTOR = 0.5


class TranscriptionUnavailable(RuntimeError):
    """The engine can't run right now: no Gemini key, the on-device model is
    missing or was released, this Mac can't run it. Nothing is wrong with the
    audio — the daemon parks it and retries once an engine is available."""


class ChunkFailed(RuntimeError):
    """This one audio file can't be transcribed (the engine says so); the next
    may well work. The daemon parks it with an attempt count and quarantines
    it after a few tries."""


class AppleUnavailable(TranscriptionUnavailable):
    """On-device transcription is unusable (helper exit 69/75, a sysaudio that
    predates `transcribe`, the helper missing or its probe timing out)."""


class AppleChunkFailed(ChunkFailed):
    """The helper says this file is unreadable or undecodable (exit 70). The
    only on-device failure that is charged to the file straight away."""


class AppleError(RuntimeError):
    """The helper failed some other way: exit 1, no or unreadable output, a
    timeout, a crash (signal). That may be this file or Apple's speech service
    (broken after an OS update, wedged, an XPC interruption), so the daemon
    doesn't count it against the file until another chunk shows the engine
    working (daemon._Judge)."""


# --- generate_content backend prompts ------------------------------------------------

# Ask for a clean transcript with speaker labels when multiple voices are
# present. Returns empty for silent audio rather than hallucinated filler.
GEMINI_TRANSCRIBE_INSTRUCTION = (
    "Transcribe the audio. Return only the spoken text, nothing else. "
    "If multiple speakers are clearly distinguishable, prefix each "
    "speaker turn with [SPEAKER_1], [SPEAKER_2], etc. (consistent "
    "within this clip only — speaker IDs do NOT carry across clips). "
    "If the audio is silent, contains only background noise, or has no "
    "intelligible speech, return an empty string. Do not invent or "
    "filler-fill text. Do not add commentary, summary, or formatting "
    "beyond the speaker prefixes."
)

# Mic ("me") chunks are a single known speaker — the device owner talking into
# their own microphone — so speaker labels are noise there.
GEMINI_TRANSCRIBE_INSTRUCTION_ME = (
    "Transcribe the audio. It is a single speaker talking into their own "
    "microphone during a meeting. Return only the spoken text, nothing else. "
    "Do not add speaker labels. If the audio is silent, contains only "
    "background noise, or has no intelligible speech, return an empty "
    "string. Do not invent or filler-fill text. Do not add commentary, "
    "summary, or formatting."
)


# --- configuration ---------------------------------------------------------------------

def stt_choice(env=None) -> str:
    """auto | apple | gemini, from MEETING_CAPTURE_STT. Unset, unknown, or only
    the legacy MEETING_CAPTURE_TRANSCRIBER (gemini|whisper) set -> auto."""
    env = os.environ if env is None else env
    v = (env.get(ENV_STT) or "").strip().lower()
    return v if v in STT_CHOICES else DEFAULT_STT


def normalize_locale(text: str) -> str:
    """'hi_in' -> 'hi-IN', 'EN-us' -> 'en-US', 'hi-latn-in' -> 'hi-Latn-IN'."""
    parts = [p for p in text.strip().replace("_", "-").split("-") if p]
    if not parts:
        return ""
    out = [parts[0].lower()]
    for p in parts[1:]:
        if len(p) == 4 and p.isalpha():
            out.append(p.title())          # script subtag
        elif len(p) in (2, 3):
            out.append(p.upper())          # region subtag
        else:
            out.append(p)
    return "-".join(out)


@dataclass(frozen=True)
class LocaleChoice:
    locale: str          # the on-device language to use
    source: str          # "setting" (MEETING_CAPTURE_LOCALE) | "mac" (the Mac's language) | "default"
    mac: str = ""        # the Mac's preferred language, normalized ("" if unknown)

    @property
    def guessed(self) -> bool:
        """en-US only because the Mac's language — known, not English — can't
        be transcribed on this Mac: likely the wrong language for its meetings."""
        return (self.source == "default" and bool(self.mac)
                and self.mac.split("-")[0].lower() != "en")

    def describe(self) -> str:
        """Where the language comes from, for status/doctor/stt."""
        if self.source == "setting":
            return "chosen with `meeting-capture language`"
        if self.source == "mac":
            return "this Mac's language"
        if self.mac:
            return f"default — this Mac's language ({self.mac}) can't be transcribed on this Mac"
        return "default"


MAC_PREFS_TTL_S = 600.0       # like the probe: a changed Mac language shows up within 10 minutes
MAC_PREFS_RETRY_S = 60.0      # an unreadable read (e.g. right at login) is retried sooner
_mac_prefs_cache: list = []   # [(monotonic time, ttl, value)]


def _read_mac_preferences() -> Optional[tuple]:
    """(AppleLanguages, AppleLocale) from the user's global defaults — the
    Mac's preferred languages, most preferred first, and its region format,
    e.g. (("en-IN",), "en_IN"). Read-only. None if it can't be read."""
    try:
        r = subprocess.run(["/usr/bin/defaults", "export", "-g", "-"], capture_output=True,
                           timeout=5, stdin=subprocess.DEVNULL)
        data = plistlib.loads(r.stdout) if r.returncode == 0 and r.stdout else None
    except Exception:   # no `defaults` (not macOS), a timeout, an unparsable plist
        return None
    if not isinstance(data, dict):
        return None
    langs, region = data.get("AppleLanguages"), data.get("AppleLocale")
    return (tuple(str(x) for x in langs or () if isinstance(x, str)) if isinstance(langs, list) else (),
            region if isinstance(region, str) else "")


def _mac_preferences() -> tuple:
    """_read_mac_preferences(), cached; ((), "") while it can't be read."""
    now = time.monotonic()
    if _mac_prefs_cache and now - _mac_prefs_cache[0][0] < _mac_prefs_cache[0][1]:
        return _mac_prefs_cache[0][2]
    got = _read_mac_preferences()
    value = got if got is not None else ((), "")
    _mac_prefs_cache[:] = [(now, MAC_PREFS_TTL_S if got is not None else MAC_PREFS_RETRY_S, value)]
    return value


def mac_locale_candidates() -> list[str]:
    """Locales the Mac's settings point at, best first: its most preferred
    language (AppleLanguages[0]), then the region format (AppleLocale) when it
    is the same language — so "en" or "en-GB" with region en_IN also tries
    en-IN. A region in another language is ignored."""
    langs, region = _mac_preferences()
    region = normalize_locale(region.split("@")[0])        # en_IN@rg=inzzzz -> en-IN
    first = normalize_locale(langs[0]) if langs else ""
    if not first:
        return [region] if region else []
    out = [first]
    if region and region != first and region.split("-")[0] == first.split("-")[0]:
        out.append(region)
    return out


def _pick_supported(candidates: list[str], supported: list[str]) -> Optional[str]:
    """The first candidate the helper supports: exactly (any case), then
    without a script subtag (zh-Hans-CN -> zh-CN), then a bare language's only
    region (match_locale)."""
    by_lower = {s.lower(): s for s in supported}
    for c in candidates:
        parts = c.split("-")
        for want in (c, f"{parts[0]}-{parts[-1]}" if len(parts) == 3 else ""):
            if want and want.lower() in by_lower:
                return by_lower[want.lower()]
    for c in candidates:
        if "-" not in c:
            m = match_locale(c, supported)
            if m:
                return m
    return None


def locale_choice(env=None, probe: bool = True) -> LocaleChoice:
    """The on-device language: MEETING_CAPTURE_LOCALE when set; else the
    Mac's language when the helper supports it (one cached probe for the
    supported list); else en-US. probe=False never runs the helper (the Mac's
    language is then only used when it is en-US)."""
    env = os.environ if env is None else env
    v = normalize_locale(env.get(ENV_LOCALE) or "")
    if v:
        return LocaleChoice(v, "setting")
    cands = mac_locale_candidates()
    if not cands:
        return LocaleChoice(DEFAULT_LOCALE, "default")
    mac = cands[0]
    if mac.lower() == DEFAULT_LOCALE.lower():
        return LocaleChoice(DEFAULT_LOCALE, "mac", mac)
    if not probe:
        return LocaleChoice(DEFAULT_LOCALE, "default", mac)
    # Any probe lists the supported locales. Probing the Mac's own locale
    # (when it names a region) also answers for it when it is picked — the
    # usual case — so the caller's apple_status() for it is cached: one helper
    # run, not two.
    picked = _pick_supported(cands, apple_status(mac if "-" in mac else DEFAULT_LOCALE).supported)
    if picked:
        return LocaleChoice(picked, "mac", mac)
    return LocaleChoice(DEFAULT_LOCALE, "default", mac)


def stt_locale(env=None) -> str:
    return locale_choice(env).locale


def match_locale(text: str, supported: list[str]) -> Optional[str]:
    """The supported locale the user meant, or None. Accepts any case and '_';
    a bare language ('hi', 'en') picks its only region, or en-US for English
    (plain 'en' would map to en-SG in Apple's own lookup). With no supported
    list to check against, the normalized input is returned as is."""
    want = normalize_locale(text)
    if not want:
        return None
    if not supported:
        return want
    by_lower = {s.lower(): s for s in supported}
    if want.lower() in by_lower:
        return by_lower[want.lower()]
    if "-" not in want:
        if want == "en" and "en-us" in by_lower:
            return by_lower["en-us"]
        cands = [s for s in supported if s.split("-")[0].lower() == want]
        if len(cands) == 1:
            return cands[0]
    return None


def helper_binary() -> Optional[Path]:
    """The `sysaudio transcribe` helper: MEETING_CAPTURE_TRANSCRIBE_BIN when set
    (even if it doesn't exist — tests rely on that), else the sysaudio the
    daemon records with."""
    override = os.environ.get(ENV_TRANSCRIBE_BIN)
    if override:
        return Path(override)
    from .recorder import find_sysaudio
    return find_sysaudio()


# --- on-device helper -------------------------------------------------------------------

@dataclass
class AppleStatus:
    available: bool                  # this Mac + locale can run on-device transcription
    installed: bool                  # the locale's model is on disk
    reason: str
    locale: str
    exit_code: Optional[int] = None  # the probe's exit code (None: not run / didn't finish)
    supported: list = field(default_factory=list)
    installed_locales: list = field(default_factory=list)
    os: str = ""
    arch: str = ""
    helper: str = ""

    @property
    def usable(self) -> bool:
        return self.exit_code == 0 and self.available and self.installed

    @property
    def installable(self) -> bool:
        """Supported here, model just not installed yet (probe exit 75)."""
        return self.exit_code == EX_TEMPFAIL

    def as_dict(self) -> dict:
        d = asdict(self)
        d["usable"], d["installable"] = self.usable, self.installable
        return d


_probe_lock = threading.Lock()
_probe_cache: dict = {}


def clear_apple_status_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


def _last_json(stdout: str) -> Optional[dict]:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None
    return None


def _tail(text: str, n: int = 200) -> str:
    text = (text or "").strip()
    return text[-n:] if text else ""


def _is_old_helper(r: subprocess.CompletedProcess) -> bool:
    """A sysaudio from before `transcribe` existed rejects it as an argument."""
    return r.returncode != 0 and "unknown arg" in (r.stderr or "")


OLD_HELPER_REASON = ("this sysaudio predates on-device transcription — "
                     "upgrade meeting-capture (brew upgrade meeting-capture)")


def _run_helper(binary: Path, args: list[str], timeout: float,
                on_stderr=None) -> subprocess.CompletedProcess:
    """Run `binary transcribe ARGS` as its own responsible process (tccspawn),
    so --probe, --install and FILE all run as the same identity whoever calls
    them (onboarding, CLI, daemon). With `on_stderr`, each stderr line is also
    handed to it as the helper prints it (download progress)."""
    from . import tccspawn
    cmd = [str(binary), "transcribe", *args]
    if on_stderr is None:
        return tccspawn.run(cmd, timeout=timeout)
    proc = tccspawn.spawn(cmd, stdin=tccspawn.DEVNULL, stdout=tccspawn.PIPE, stderr=tccspawn.PIPE)
    out: list[bytes] = []
    err: list[str] = []

    def _stderr() -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace")
            err.append(line)
            try:
                on_stderr(line.rstrip("\n"))
            except Exception:   # a progress printer must never fail the install
                pass

    readers = [threading.Thread(target=_stderr, daemon=True),
               threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True)]
    for t in readers:
        t.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        for t in readers:
            t.join(timeout=5)
    return subprocess.CompletedProcess(cmd, rc, b"".join(out).decode("utf-8", "replace"), "".join(err))


def _probe(binary: Path, locale: str) -> AppleStatus:
    base = {"locale": locale, "helper": str(binary)}
    try:
        r = _run_helper(binary, ["--probe", "--locale", locale], PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return AppleStatus(False, False, f"the on-device probe timed out after {PROBE_TIMEOUT_S:.0f}s", **base)
    except OSError as exc:
        return AppleStatus(False, False, f"can't run the transcription helper {binary}: {exc.strerror or exc}", **base)
    if _is_old_helper(r):
        return AppleStatus(False, False, OLD_HELPER_REASON, exit_code=r.returncode, **base)
    data = _last_json(r.stdout)
    if data is None:
        return AppleStatus(False, False,
                           f"on-device probe failed (exit {r.returncode}): {_tail(r.stderr) or 'no output'}",
                           exit_code=r.returncode, **base)
    rc = r.returncode
    loc = str(data.get("locale") or locale)
    installed = bool(data.get("installed", rc == 0))
    if rc == 0 and data.get("available", True) is not False:
        available, default = True, f"on-device model for {loc} is installed"
    elif rc == EX_TEMPFAIL:
        available, installed = True, False
        default = f"the on-device model for {loc} isn't installed yet (meeting-capture language {loc})"
    elif rc == EX_UNAVAILABLE:
        available, default = False, "on-device transcription needs macOS 26 or later on Apple silicon"
    else:
        available, default = False, f"on-device probe failed (exit {rc}): {_tail(r.stderr) or 'no detail'}"
    return AppleStatus(
        available=available, installed=installed,
        reason=str(data.get("reason") or default), locale=loc, exit_code=rc,
        supported=[str(x) for x in data.get("supported") or []],
        installed_locales=[str(x) for x in data.get("installed_locales") or []],
        os=str(data.get("os") or ""), arch=str(data.get("arch") or ""), helper=str(binary),
    )


def apple_status(locale: Optional[str] = None, refresh: bool = False) -> AppleStatus:
    """Can this Mac transcribe `locale` on-device right now? Probed with the
    helper and cached for PROBE_TTL_S (cleared when a chunk gets 69/75)."""
    locale = normalize_locale(locale) if locale else stt_locale()
    binary = helper_binary()
    if binary is None:
        return AppleStatus(False, False, "the on-device transcription helper (sysaudio) was not found", locale)
    key = (str(binary), locale)
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit is not None and not refresh and time.monotonic() - hit[0] < PROBE_TTL_S:
            return hit[1]
    st = _probe(binary, locale)
    with _probe_lock:
        now = time.monotonic()
        _probe_cache[key] = (now, st)
        if st.available and st.locale and normalize_locale(st.locale) != locale:
            # The helper resolved an equivalent (zh-Hans-CN -> zh-CN): same answer.
            _probe_cache[(str(binary), normalize_locale(st.locale))] = (now, st)
    return st


def _install_progress(progress):
    """The helper's stderr during --install, as lines for `progress`: its
    "<locale> model download N%" ticks (one a second) only when N reaches a
    new tenth, reservation notes and errors as they come. Its own
    "downloading…" line repeats what the caller already said."""
    last = [-1]

    def on_line(line: str) -> None:
        line = line.strip()
        if not line or line.startswith("downloading the on-device speech model"):
            return
        m = re.search(r" model download (\d+)%$", line)     # a tick, not "100%, installed"
        if m:
            tenth = int(m.group(1)) // 10
            if tenth == last[0] or tenth >= 10:
                return
            last[0] = tenth
        progress(f"  {line}")
    return on_line


def install_apple_model(locale: str, timeout: float = INSTALL_TIMEOUT_S, progress=None) -> dict:
    """Download (once) and reserve the on-device model for `locale`.
    Returns the helper's {"installed", "locale", "seconds"}. Raises
    AppleUnavailable (unsupported here) or AppleError (anything else).
    `progress`, if given, gets the download's progress lines as they come."""
    locale = normalize_locale(locale)
    binary = helper_binary()
    if binary is None:
        raise AppleUnavailable("the on-device transcription helper (sysaudio) was not found")
    try:
        r = _run_helper(binary, ["--install", "--locale", locale], timeout,
                        on_stderr=_install_progress(progress) if progress else None)
    except subprocess.TimeoutExpired as exc:
        raise AppleError(f"installing the on-device model for {locale} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise AppleUnavailable(f"can't run the transcription helper {binary}: {exc.strerror or exc}") from exc
    finally:
        clear_apple_status_cache()
    data = _last_json(r.stdout) or {}
    detail = data.get("error") or data.get("reason") or _tail(r.stderr)
    if _is_old_helper(r):
        raise AppleUnavailable(OLD_HELPER_REASON)
    if r.returncode == 0 and data.get("installed", True):
        return {"installed": True, "locale": str(data.get("locale") or locale),
                "seconds": float(data.get("seconds") or 0.0)}
    if r.returncode == EX_UNAVAILABLE:
        raise AppleUnavailable(detail or f"{locale} isn't supported for on-device transcription on this Mac")
    raise AppleError(f"installing the on-device model for {locale} failed (exit {r.returncode}): {detail or 'no detail'}")


def _audio_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 16000)
    except Exception:   # wave raises a bare RuntimeError on some malformed RIFF files
        try:
            return path.stat().st_size / 32000.0      # 16 kHz mono int16
        except OSError:
            return 0.0


def _apple_text(data: dict) -> str:
    text = data.get("text")
    if text is None:
        text = " ".join(str(s.get("text") or "").strip() for s in data.get("segments") or [] if isinstance(s, dict))
    return str(text or "").strip()


def _transcribe_apple(audio_path: Path, role: str = "them", locale: Optional[str] = None) -> str:
    """One chunk through `sysaudio transcribe`. Role doesn't matter on-device
    (no diarization, no vocabulary). Raises AppleUnavailable / AppleChunkFailed
    / AppleError — see the classes."""
    locale = normalize_locale(locale) if locale else stt_locale()
    binary = helper_binary()
    if binary is None:
        raise AppleUnavailable("the on-device transcription helper (sysaudio) was not found")
    timeout = max(APPLE_MIN_TIMEOUT_S, APPLE_TIMEOUT_FACTOR * _audio_seconds(audio_path))
    try:
        r = _run_helper(binary, ["--locale", locale, str(audio_path)], timeout)
    except subprocess.TimeoutExpired as exc:
        # A hang is far more often the speech service than the file: not ChunkFailed.
        raise AppleError(
            f"on-device transcription of {audio_path.name} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        clear_apple_status_cache()
        raise AppleUnavailable(f"can't run the transcription helper {binary}: {exc.strerror or exc}") from exc
    rc = r.returncode
    data = _last_json(r.stdout)
    detail = (data or {}).get("error") or (data or {}).get("reason") or _tail(r.stderr) or "no detail"
    if rc == 0:
        if data is None:
            raise AppleError(f"the helper printed no transcript for {audio_path.name}: {_tail(r.stderr) or 'no output'}")
        return _apple_text(data)
    if rc in (EX_UNAVAILABLE, EX_TEMPFAIL):
        clear_apple_status_cache()
        raise AppleUnavailable(f"on-device transcription unavailable (exit {rc}): {detail}")
    if rc == EX_SOFTWARE:
        raise AppleChunkFailed(f"on-device transcription couldn't read {audio_path.name}: {detail}")
    if _is_old_helper(r):
        clear_apple_status_cache()
        raise AppleUnavailable(OLD_HELPER_REASON)
    if rc < 0:
        raise AppleError(f"the transcription helper died (signal {-rc}) on {audio_path.name}")
    raise AppleError(f"on-device transcription failed (exit {rc}): {detail}")


# --- resolution ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Backend:
    engine: str      # "apple" | "gemini" | "none"
    choice: str      # what MEETING_CAPTURE_STT asks for: auto | apple | gemini
    reason: str
    locale: str
    ready: bool      # can transcribe right now

    @property
    def label(self) -> str:
        return ENGINE_LABELS.get(self.engine, self.engine)


KEY_ENV_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")


def gemini_key_present(env=None) -> bool:
    """Would Gemini find a key? The daemon itself passes nothing and reads its
    own environment, then the key file. `env` is the daemon's configuration
    when the CLI asks on its behalf (its launchd plist's env): a key there or
    in the key file counts, a key only in the CLI's own shell does not — the
    launchd daemon never sees that shell."""
    if env is None:
        return bool(_resolve_gemini_api_key())
    if any(str(env.get(v) or "").strip() for v in KEY_ENV_VARS):
        return True
    return bool(_key_file_value())


def resolve_backend(choice: Optional[str] = None, locale: Optional[str] = None, env=None) -> Backend:
    """Which engine transcribes the next chunk, and why (see the module doc)."""
    choice = (choice or stt_choice(env)).strip().lower()
    if choice not in STT_CHOICES:
        choice = DEFAULT_STT
    if choice == "gemini":     # never runs the helper
        locale = normalize_locale(locale) if locale else locale_choice(env, probe=False).locale
        if gemini_key_present(env):
            return Backend("gemini", choice, "chosen with `meeting-capture stt gemini`", locale, True)
        return Backend("gemini", choice, "chosen with `meeting-capture stt gemini`, but no Google API key is set "
                       "(GOOGLE_API_KEY / GEMINI_API_KEY / ~/.config/google/key)", locale, False)
    lc = LocaleChoice(normalize_locale(locale), "setting") if locale else locale_choice(env)
    locale = lc.locale
    if choice == "auto" and lc.guessed and gemini_key_present(env):
        # On this Mac would be en-US for someone whose Mac speaks another
        # language: Gemini, which detects the language, is the better guess.
        return Backend("gemini", choice, f"this Mac's language ({lc.mac}) can't be transcribed on this Mac, "
                       "so Gemini (it detects the language) transcribes; `meeting-capture language LOCALE` "
                       "picks an on-device language", locale, True)
    st = apple_status(locale)
    if st.usable:
        note = (f"; this Mac's language ({lc.mac}) isn't available on-device — "
                "`meeting-capture language LOCALE` to change") if lc.guessed else ""
        return Backend("apple", choice, st.reason + note, st.locale or locale, True)
    if choice == "apple":
        return Backend("apple", choice, st.reason, locale, False)
    if gemini_key_present(env):
        return Backend("gemini", choice, f"on this Mac isn't available ({st.reason}); using Gemini", locale, True)
    return Backend("none", choice, f"on this Mac isn't available ({st.reason}) and no Gemini API key is set",
                   locale, False)


def upgrade_notice(env=None, backend: Optional[Backend] = None) -> Optional[str]:
    """A note for someone with a Gemini key whose transcription now runs on
    this Mac without anyone having picked that: MEETING_CAPTURE_STT unset (the
    default for every install that predates on-device transcription; `stt`,
    `language` and the settings page always write it), a key present, and auto
    resolving to on this Mac. Shown until they pick an engine or a language."""
    env = os.environ if env is None else env
    if (env.get(ENV_STT) or "").strip() or not gemini_key_present(env):
        return None
    b = backend if backend is not None else resolve_backend(None, None, env)
    if b.engine != "apple" or not b.ready:
        return None
    lc = locale_choice(env)
    lang = b.locale + (" — this Mac's language" if lc.source == "mac" and lc.locale == b.locale else "")
    return (f"transcription now runs on this Mac (on-device, {lang}) instead of Gemini; custom "
            f"vocabulary and speaker labels are Gemini-only")


# How to act on upgrade_notice() from a terminal (the settings page has its own).
NOTICE_CLI_HINT = ("meetings in another language: `meeting-capture language LOCALE`; back to Gemini: "
                   "`meeting-capture stt gemini`; keep this and hide the note: `meeting-capture stt auto`")


def model_needed(b: Backend, lc: Optional[LocaleChoice] = None,
                 st: Optional[AppleStatus] = None) -> bool:
    """Is on-device transcription held up only by its language's model? The
    setting wants it (auto or apple — but not auto keeping Gemini for a Mac
    language on-device can't do, which needs no en-US model), it isn't
    running, and the helper says the model can be installed here (probe exit
    75: not on the Mac, or not reserved by this app yet). The daemon then
    downloads it by itself (at most once an hour); `meeting-capture language
    LOCALE` does it now. `lc` (locale_choice()) and `st` (apple_status(b.locale))
    are looked up only when needed, so a Gemini setting never runs the helper."""
    if b.choice == "gemini" or (b.engine == "apple" and b.ready):
        return False
    if b.engine == "gemini" and b.ready and (lc if lc is not None else locale_choice()).guessed:
        return False
    return (st if st is not None else apple_status(b.locale)).installable


def on_device_hint(b: Backend, lc: LocaleChoice, st: AppleStatus, key: bool) -> Optional[str]:
    """The one command that makes batch transcription run on this Mac under
    `auto` — with Gemini as its backup when a key exists — installing the
    language's model first when needed; None when it already does or this Mac
    can't. on_device_only_hint() is the never-uploads alternative."""
    if (b.choice == "auto" and b.engine == "apple" and b.ready) or not (st.usable or st.installable):
        return None
    if b.choice in ("gemini", "apple"):
        if lc.guessed and key:     # auto alone would keep Gemini for this Mac's language
            return f"meeting-capture stt auto --language {lc.locale}"
        return "meeting-capture stt auto"
    return f"meeting-capture language {lc.locale}"


def on_device_only_hint(b: Backend, st: AppleStatus) -> Optional[str]:
    """The one command that makes batch transcription run on this Mac and
    never upload — `stt apple`, which also installs the language's model when
    needed — or None when it already does or this Mac can't. Unlike
    on_device_hint() it drops auto's Gemini fallback (transcribe() hands a
    chunk to Gemini when on-device fails under auto with a key)."""
    if (b.choice == "apple" and b.engine == "apple" and b.ready) or not (st.usable or st.installable):
        return None
    return "meeting-capture stt apple"


def engine_summary(env=None) -> dict:
    """Everything the CLI and the settings page show about transcription, for
    the configuration in `env` (the launchd plist's, normally). One helper
    probe per language at most (apple_status() caches them)."""
    choice, lc = stt_choice(env), locale_choice(env)
    locale = lc.locale
    st = apple_status(locale)
    b = resolve_backend(choice, None, env)
    key = gemini_key_present(env)
    needs_model = model_needed(b, lc, st)
    return {
        "choice": choice, "choice_label": CHOICE_LABELS[choice],
        "engine": b.engine, "engine_label": b.label, "reason": b.reason, "ready": b.ready,
        "locale": locale, "locale_source": lc.source, "locale_why": lc.describe(),
        "locale_guessed": lc.guessed, "mac_language": lc.mac,
        "apple": st.as_dict(), "gemini_key": key,
        "uploads": b.engine == "gemini",
        "gemini_fallback": choice == "auto" and b.engine == "apple" and key,
        "needs_model": needs_model,
        "install_hint": f"meeting-capture language {locale}" if needs_model else None,
        "on_device_hint": on_device_hint(b, lc, st, key),
        "on_device_only_hint": on_device_only_hint(b, st),
        "notice": upgrade_notice(env, b),
    }


# --- model dispatch -----------------------------------------------------------------

def resolve_model(model: Optional[str] = None) -> str:
    return model or os.environ.get(ENV_GEMINI_MODEL, DEFAULT_GEMINI_MODEL)


def is_transcribe_model(model: str) -> bool:
    """True for the purpose-built batch speech-to-text models (Interactions API)."""
    return model.startswith("gemini-3.5-transcribe") and not model.endswith("-live")


def diarization_enabled() -> bool:
    """Diarize the 'them' channel? Off by default: the API makes it mutually
    exclusive with custom vocabulary, and proper-noun fidelity matters more
    for memory than speaker labels within a single channel."""
    return os.environ.get(ENV_DIARIZE, "0").strip().lower() in ("1", "true", "yes", "on")


def load_vocabulary(path: Path = VOCAB_FILE) -> list[str]:
    """Custom vocabulary terms (one per line, '#' comments), capped at the API limit."""
    if not path.exists():
        return []
    terms: list[str] = []
    seen: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            term = line.split("#", 1)[0].strip()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    except OSError:
        return []
    return terms[:MAX_VOCAB_TERMS]


_tls = threading.local()


def last_backend() -> Optional[str]:
    """What transcribed this thread's last successful transcribe() call:
    "apple" or the Gemini model name (for logging)."""
    return getattr(_tls, "backend", None)


def transcribe(
    audio_path: Path,
    model: Optional[str] = None,
    instruction: Optional[str] = None,
    role: str = "them",
) -> str:
    """Transcribe a single audio chunk with the engine resolve_backend() picks.

    Args:
        audio_path: WAV file (16kHz mono int16 expected).
        model: "apple" forces on-device; a Gemini model name forces Gemini with
            that model (A/B harness). Normally None: MEETING_CAPTURE_STT decides.
        instruction: prompt override for the Gemini generate_content backend only.
        role: "me" (own mic, single speaker) or "them" (system audio).

    Returns:
        Transcribed text. Empty string for silent / unintelligible audio.
        Diarized Gemini "them" chunks carry [SPEAKER_n] prefixes per speaker turn.

    Raises:
        TranscriptionUnavailable: no engine can run now (park the audio).
        ChunkFailed: the engine says this file can't be read (park it, count
            the attempt).
        Anything else (AppleError, Gemini/network errors): the file or the
            engine — can't tell from one failure.
    """
    _tls.backend = None
    if model == "apple":
        return _apple(audio_path, role)
    if model:
        return _gemini(audio_path, model, instruction, role)
    backend = resolve_backend()
    if backend.engine == "apple" and backend.ready:
        try:
            return _apple(audio_path, role, backend.locale)
        except AppleUnavailable as exc:
            if backend.choice != "auto":
                raise           # "apple" never uploads: the daemon parks it
            again = resolve_backend()      # cache was cleared: re-probes
            if again.engine != "gemini" or not again.ready:
                raise
            log.warning("on-device transcription became unavailable (%s) — auto mode falls back to Gemini", exc)
            return _gemini(audio_path, None, instruction, role)
    if backend.engine == "gemini" and backend.ready:
        return _gemini(audio_path, None, instruction, role)
    if backend.engine == "apple":
        raise AppleUnavailable(backend.reason)
    raise TranscriptionUnavailable(backend.reason)


def _apple(audio_path: Path, role: str, locale: Optional[str] = None) -> str:
    text = _transcribe_apple(audio_path, role, locale)
    _tls.backend = "apple"
    return text


def _gemini(audio_path: Path, model: Optional[str], instruction: Optional[str], role: str) -> str:
    """The Gemini path (key, vocabulary, diarization and rate limits live
    here and only here)."""
    model = resolve_model(model)
    if model.endswith("-live"):
        raise RuntimeError(
            f"{model} is a streaming model; batch transcription needs "
            "gemini-3.5-transcribe (or set MEETING_CAPTURE_MODE=live)."
        )
    # No key -> _client() raises TranscriptionUnavailable (resolve_backend()
    # already routes around a missing key; this covers an explicit model).
    if is_transcribe_model(model):
        try:
            text = _transcribe_interactions(audio_path, model, role)
            _tls.backend = model
            return text
        except TranscriptionUnavailable:
            raise           # no key: the fallback model can't help either
        except Exception as exc:
            # Preview model / API hiccup: never lose the chunk. Fall back to the
            # general audio model, which has carried this pipeline for months.
            log.warning(
                "%s failed (%s: %s) — falling back to %s for %s",
                model, type(exc).__name__, str(exc)[:160], FALLBACK_GEMINI_MODEL, audio_path.name,
            )
            model = FALLBACK_GEMINI_MODEL
    text = _transcribe_gemini(audio_path, model, instruction, role)
    _tls.backend = model
    return text


# --- shared client -------------------------------------------------------------------

def _missing_key_message() -> str:
    return ("Gemini transcription needs a Google API key. "
            "Set GOOGLE_API_KEY or GEMINI_API_KEY, or write the key to "
            f"{GEMINI_KEY_FILE} (mode 600).")


def _client():
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "meeting-capture requires the google-genai package (>=2.0). "
            "Install with: pip install -e ."
        ) from e

    api_key = _resolve_gemini_api_key()
    if not api_key:
        raise TranscriptionUnavailable(_missing_key_message())
    return genai.Client(api_key=api_key, http_options=_http_options(types)), types


def _http_options(types):
    """Bounded HTTP behaviour for every batch transcription call.

    * timeout: the SDK has no read timeout by default — a half-open TLS
      connection can wedge the daemon indefinitely on SSL_read.
    * attempts=1: the SDK retries 429/5xx on its own and honours Retry-After.
      A daily-quota 429 carries a Retry-After of hours, so one transcribe()
      call sat inside the SDK's retry loop for two days while the batch loop
      never reached its "session ended" line and the chunk was never parked.
      Fail fast instead; transcribe() already falls back to the flash model
      and the daemon parks the audio for a later retry.
    """
    return types.HttpOptions(
        timeout=REQUEST_TIMEOUT_MS,
        retry_options=types.HttpRetryOptions(attempts=1),
    )


# --- backend: Interactions API (gemini-3.5-transcribe) --------------------------------

def _transcription_config(role: str, vocab: list[str], diarize: bool) -> dict:
    """Per-channel config. Vocabulary and diarization/timestamps are mutually
    exclusive in the API; 'me' is one known speaker so it always takes vocab."""
    cfg: dict = {"mode": {"type": "verbatim"}}
    if role == "them" and diarize:
        cfg["mode"]["diarization_mode"] = "speaker"
        # Speaker annotations only populate alongside word-level timestamps.
        cfg["mode"]["timestamp_granularities"] = ["word"]
    elif vocab:
        cfg["custom_vocabulary"] = vocab
    return cfg


def _transcribe_interactions(audio_path: Path, model: str, role: str) -> str:
    client, _types = _client()
    if not hasattr(client, "interactions"):
        raise RuntimeError(
            f"{model} needs the Interactions API — upgrade with: pip install -U 'google-genai>=2.0'"
        )
    cfg = _transcription_config(role, load_vocabulary(), diarization_enabled())
    audio = {
        "type": "audio",
        "data": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
        "mime_type": "audio/wav",
    }
    interactions = _no_retries(client.interactions)
    interaction = interactions.create(
        model=model,
        input=[audio],
        generation_config={"transcription_config": cfg},
        timeout=REQUEST_TIMEOUT_MS / 1000,
    )
    if "diarization_mode" in cfg["mode"]:
        diarized = format_diarized(_collect_annotations(interaction))
        if diarized:
            return diarized
    return (getattr(interaction, "output_text", None) or "").strip()


def _no_retries(interactions):
    """The Interactions resource ignores the client's http retry options and
    retries 408/409/429/5xx up to 4 times with backoff, honouring
    Retry-After. With gemini-3.5-transcribe's 10-requests/minute limit the
    daemon's recording thread slept inside that loop while live line-in
    audio piled up unread (2026-09-30). One attempt only: a failure falls back
    to the general model, and failing that the audio is parked for a later
    retry — nothing is lost."""
    try:
        from google.genai._gaos import utils as _gaos_utils
        interactions.sdk_configuration.retry_config = _gaos_utils.RetryConfig("none", None, False)
    except Exception:   # older/newer SDK layout: keep its defaults
        log.debug("could not disable Interactions retries", exc_info=True)
    return interactions


def _collect_annotations(interaction) -> list[dict]:
    """Flatten word annotations ({text, speaker, start_offset, ...}) from an interaction."""
    out: list[dict] = []
    for step in getattr(interaction, "steps", None) or []:
        for content in getattr(step, "content", None) or []:
            for ann in getattr(content, "annotations", None) or []:
                if hasattr(ann, "model_dump"):
                    out.append(ann.model_dump(exclude_none=True))
                elif isinstance(ann, dict):
                    out.append(ann)
    return out


def format_diarized(annotations: list[dict]) -> str:
    """Group consecutive same-speaker words into '[SPEAKER_n] text' lines.

    The API labels speakers 'spk:0', 'spk:1', …; we keep the transcript
    convention already used by the prompt backend ([SPEAKER_1], [SPEAKER_2]).
    """
    lines: list[str] = []
    cur_spk: Optional[str] = None
    cur_words: list[str] = []
    labels: dict[str, int] = {}

    def flush() -> None:
        if cur_words and cur_spk is not None:
            n = labels.setdefault(cur_spk, len(labels) + 1)
            lines.append(f"[SPEAKER_{n}] " + " ".join(cur_words))

    for ann in annotations:
        word = (ann.get("text") or "").strip()
        if not word:
            continue
        spk = str(ann.get("speaker") or "spk:0")
        if spk != cur_spk:
            flush()
            cur_spk, cur_words = spk, []
        cur_words.append(word)
    flush()
    return "\n".join(lines)


# --- backend: generate_content (general audio models) ---------------------------------

def _transcribe_gemini(
    audio_path: Path,
    model: Optional[str],
    instruction: Optional[str] = None,
    role: str = "them",
) -> str:
    """General Gemini audio-understanding backend (prompted transcription)."""
    client, types = _client()
    model = model or FALLBACK_GEMINI_MODEL
    if instruction is None:
        instruction = GEMINI_TRANSCRIBE_INSTRUCTION_ME if role == "me" else GEMINI_TRANSCRIBE_INSTRUCTION
    response = client.models.generate_content(
        model=model,
        contents=[
            instruction,
            types.Part.from_bytes(data=audio_path.read_bytes(), mime_type="audio/wav"),
        ],
        config={"temperature": 0.0},
    )
    return (response.text or "").strip()


def _resolve_gemini_api_key() -> Optional[str]:
    """Look up the Gemini API key in env first, then ~/.config/google/key."""
    for var in KEY_ENV_VARS:
        v = os.environ.get(var)
        if v:
            return v.strip()
    return _key_file_value()


def _key_file_value() -> Optional[str]:
    if GEMINI_KEY_FILE.exists():
        try:
            return GEMINI_KEY_FILE.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None
