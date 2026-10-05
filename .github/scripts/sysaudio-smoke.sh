#!/bin/bash
# Smoke-tests a built sysaudio WITHOUT capturing anything. It never runs the
# capture path, which would need Screen Recording and the mic. It checks:
#   - the capture CLI still parses as before (--help; an unknown flag exits 1
#     with "unknown arg"),
#   - `--backend sck|taps` parses (a bad value is a usage error); the taps
#     backend is never started (it would need System Audio Recording),
#   - `check --json` (sysaudio.check/1) reads the three permissions and the
#     available backends without asking,
#   - the `transcribe` exit-code contract that meeting_capture relies on
#     (see swift/Sources/sysaudio/Transcribe.swift),
#   - that a ready probe means this binary's bundle id holds a reservation for
#     the locale, not just that macOS has the model on disk,
#   - one real transcription of a `say` clip, when the runner can transcribe,
#     with no "unallocated locales" error from Speech in the unified log.
#
# A model that is on disk but not reserved (probe 75) gets reserved by the
# clip transcription below (or by --install with --try-install). That only adds
# a reservation for this binary's bundle id; it never downloads or releases one.
#
# usage: sysaudio-smoke.sh path/to/sysaudio [--try-install]
#   --try-install  if this binary can't use the en-US model yet (probe exit
#                  75: not on disk, or not reserved), run --install to download
#                  and/or reserve it, so the transcription path runs too.
#                  Failure to install is a warning, not an error.
#
# Bash 3.2 compatible (the macOS /bin/bash).
set -euo pipefail

BIN="${1:?usage: sysaudio-smoke.sh path/to/sysaudio [--try-install]}"
TRY_INSTALL=0
[ "${2:-}" = "--try-install" ] && TRY_INSTALL=1
case "$BIN" in /*) ;; *) BIN="$PWD/$BIN" ;; esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAILED=0
RC=0

fail() { echo "FAIL: $*"; FAILED=1; }
pass() { echo "ok:   $*"; }

# check NAME cmd...: pass when cmd succeeds, fail otherwise.
check() {
    local name="$1"
    shift
    if "$@"; then pass "$name"; else fail "$name"; fi
}

# run SECONDS OUT ERR cmd...: runs cmd with a timeout (macOS has no timeout(1))
# and sets RC. A hang shows up as 142 (SIGALRM). The command's pid (perl
# execs it, so the pid carries over) lands in $TMP/pid for the log check.
run() {
    local secs="$1" out="$2" err="$3"
    shift 3
    set +e
    perl -e 'open(my $f, ">", shift) or die "pid file: $!\n"; print $f $$; close $f;
             alarm shift; exec @ARGV or die "exec failed: $!\n"' "$TMP/pid" "$secs" "$@" >"$out" 2>"$err"
    RC=$?
    set -e
}

# expect NAME WANTED...: passes when RC is one of WANTED.
expect() {
    local name="$1"
    shift
    local w
    for w in "$@"; do
        if [ "$RC" = "$w" ]; then
            pass "$name (exit $RC)"
            return 0
        fi
    done
    fail "$name: exit $RC, wanted one of: $*"
    sed 's/^/      stderr: /' "$TMP/err" | head -5 || true
}

# json_keys FILE KEY...: the file holds exactly one JSON object line with these keys.
json_keys() {
    local f="$1"
    shift
    python3 - "$f" "$@" <<'PY'
import json, sys
path, keys = sys.argv[1], sys.argv[2:]
lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
if len(lines) != 1:
    sys.exit(f"expected one JSON line on stdout, got {len(lines)}")
obj = json.loads(lines[0])
missing = [k for k in keys if k not in obj]
if missing:
    sys.exit(f"missing keys {missing} in {lines[0][:300]}")
PY
}

# transcript_ok FILE: non-empty text and well-formed timed segments.
transcript_ok() {
    python3 - "$1" <<'PY'
import json, sys
d = json.loads(open(sys.argv[1], encoding="utf-8").read())
assert d["text"].strip(), "empty text for a speech clip"
assert d["segments"], "no segments"
for seg in d["segments"]:
    assert {"start", "end", "text", "confidence"} <= seg.keys(), seg
    assert 0 <= seg["start"] <= seg["end"], seg
    assert 0 <= seg["confidence"] <= 1, seg
PY
}

# probe_state FILE: ready | unreserved | missing | unavailable, from probe JSON.
# "unreserved": macOS has the model on disk (often the en-* models it holds
# itself) but this binary's bundle id has not reserved it.
probe_state() {
    python3 - "$1" <<'PY'
import json, sys
d = json.loads(open(sys.argv[1], encoding="utf-8").read())
loc = d.get("locale")
if not d.get("available"):
    print("unavailable")
elif loc not in d.get("installed_locales", []):
    print("missing")
elif loc in d.get("reserved_locales", []):
    print("ready")
else:
    print("unreserved")
PY
}

# ready_means_reserved FILE: a probe that says usable (exit 0) must report the
# locale as on disk AND reserved by this app. On disk alone is not enough:
# Speech logs "Cannot use modules with unallocated locales ... This will be an
# error in a future release!" for a locale the caller has not reserved.
ready_means_reserved() {
    python3 - "$1" <<'PY'
import json, sys
d = json.loads(open(sys.argv[1], encoding="utf-8").read())
loc = d["locale"]
assert d["installed"] is True, f"exit 0 but installed={d['installed']!r}"
assert loc in d["installed_locales"], f"exit 0 but {loc} not in installed_locales"
assert loc in d.get("reserved_locales", []), (
    f"exit 0 but {loc} not in reserved_locales {d.get('reserved_locales')}: "
    "the probe counted a model this app never reserved")
PY
}

# no_unallocated_log PID SINCE: Speech logged no "unallocated locales" error for
# that process, i.e. the analyzer never ran on a locale this app had not
# reserved. Skipped (warning) when the unified log can't be read.
no_unallocated_log() {
    local pid="$1" since="$2" hits
    sleep 2 # let logd persist the run's messages
    if ! hits=$(/usr/bin/log show --style compact --start "$since" \
        --predicate "processIdentifier == $pid AND eventMessage CONTAINS \"unallocated locales\"" 2>&1); then
        echo "warning: could not read the unified log; skipping the unallocated-locale check"
        return 0
    fi
    if printf '%s\n' "$hits" | grep -q "unallocated locales"; then
        printf '%s\n' "$hits" | grep "unallocated locales" | head -2 | sed 's/^/      log: /'
        return 1
    fi
}

stdout_empty() { [ ! -s "$1" ]; }
not_in_file() { ! grep -q "$1" "$2"; }

echo "== $BIN"
lipo -info "$BIN" || true

# --- capture CLI unchanged (parsing only; nothing is captured) ---
run 30 "$TMP/out" "$TMP/err" "$BIN" --help
expect "sysaudio --help" 0
check "sysaudio --help keeps its usage line" grep -q "Usage: sysaudio \[--sample-rate N\] \[--mic\]" "$TMP/out"
run 30 "$TMP/out" "$TMP/err" "$BIN" --no-such-flag
expect "sysaudio --no-such-flag" 1
check "capture arg parser still says 'unknown arg'" grep -q "unknown arg: --no-such-flag" "$TMP/err"
# --backend (sck | taps) is parsed before anything starts; a bad value is a
# usage error and captures nothing. The taps backend itself is never started
# here: it would need the System Audio Recording grant (M5 lab, step 16).
run 30 "$TMP/out" "$TMP/err" "$BIN" --help
check "sysaudio --help documents --backend sck|taps" grep -q -- "--backend sck|taps" "$TMP/out"
run 30 "$TMP/out" "$TMP/err" "$BIN" --backend coreaudio
expect "sysaudio --backend coreaudio (usage error)" 1
check "a bad --backend names the choices" grep -q "takes sck or taps" "$TMP/err"
check "a bad --backend prints nothing on stdout" stdout_empty "$TMP/out"

# --- check (reads the two permissions; never --request: no prompt in CI) ---
run 30 "$TMP/out" "$TMP/err" "$BIN" check --help
expect "check --help" 0
run 30 "$TMP/out" "$TMP/err" "$BIN" check --bogus
expect "check usage error" 1
check "check usage errors don't say 'unknown arg'" not_in_file "unknown arg" "$TMP/err"
run 60 "$TMP/check.json" "$TMP/err" "$BIN" check --json
expect "check --json" 0
cat "$TMP/check.json"
check "check JSON has every contract key" \
    json_keys "$TMP/check.json" schema screen_capture microphone system_audio backends os arch requested
check "check JSON words are the contract's" python3 - "$TMP/check.json" <<'PY'
import json, sys
d = json.loads(open(sys.argv[1], encoding="utf-8").read())
assert d["schema"] == "sysaudio.check/1", d
assert d["screen_capture"] in ("granted", "not_granted"), d
assert d["microphone"] in ("granted", "denied", "not_determined", "restricted"), d
assert d["system_audio"] in ("granted", "denied", "not_determined", "unknown", "unsupported"), d
assert d["requested"] is None, d
assert d["backends"][0] == "sck", d
major, minor = (int(x) for x in (d["os"].split(".") + ["0"])[:2])
assert ("taps" in d["backends"]) == ((major, minor) >= (14, 2)), d
assert (d["system_audio"] == "unsupported") == ("taps" not in d["backends"]), d
PY

# --- transcribe ---
run 30 "$TMP/out" "$TMP/err" "$BIN" transcribe --help
expect "transcribe --help" 0

run 30 "$TMP/out" "$TMP/err" "$BIN" transcribe --bogus
expect "transcribe usage error" 1
# Python reads "unknown arg" as "this sysaudio predates transcribe".
check "transcribe usage errors don't say 'unknown arg'" not_in_file "unknown arg" "$TMP/err"

run 120 "$TMP/probe.json" "$TMP/err" "$BIN" transcribe --probe --locale en-US
PROBE_RC=$RC
expect "transcribe --probe --locale en-US" 0 69 75
cat "$TMP/probe.json"
check "probe JSON has every contract key" \
    json_keys "$TMP/probe.json" available reason os arch locale installed supported installed_locales \
    reserved_locales
PROBE_STATE=$(probe_state "$TMP/probe.json" 2>/dev/null || echo "?")
echo "en-US model state for this binary: $PROBE_STATE"
if [ "$PROBE_RC" = 0 ]; then
    check "a ready probe means the locale is reserved by this app" ready_means_reserved "$TMP/probe.json"
fi
if [ "$PROBE_RC" = 75 ] && [ "$PROBE_STATE" != unreserved ] && [ "$PROBE_STATE" != missing ]; then
    fail "probe exit 75 but its JSON says $PROBE_STATE"
fi
check "built with the macOS 26 SDK (transcribe compiled in)" \
    not_in_file "built without the macOS 26 SDK" "$TMP/probe.json"

run 120 "$TMP/out" "$TMP/err" "$BIN" transcribe --probe --locale xx-XX
expect "transcribe --probe --locale xx-XX (unsupported)" 69
check "unsupported-locale probe JSON" json_keys "$TMP/out" available reason locale

if [ "$PROBE_RC" = 75 ] && [ "$TRY_INSTALL" = 1 ]; then
    echo "en-US model not usable by this binary ($PROBE_STATE); trying --install (warning only)"
    run 600 "$TMP/install.json" "$TMP/install.err" "$BIN" transcribe --install --locale en-US
    cat "$TMP/install.json" || true
    tail -5 "$TMP/install.err" || true
    if [ "$RC" = 0 ]; then
        check "install JSON has every contract key" json_keys "$TMP/install.json" installed locale seconds
        run 120 "$TMP/probe.json" "$TMP/err" "$BIN" transcribe --probe --locale en-US
        PROBE_RC=$RC
        expect "probe after install" 0
        PROBE_STATE=$(probe_state "$TMP/probe.json" 2>/dev/null || echo "?")
        if [ "$PROBE_RC" = 0 ]; then
            check "after --install the locale is reserved by this app" ready_means_reserved "$TMP/probe.json"
        fi
    else
        echo "warning: --install exited $RC; skipping the transcription check"
    fi
fi

# With a usable model, a bad file is 70 and a clip transcribes (exit 0; 75 is
# tolerated because a CI VM may refuse the model for lack of resources, which
# is the contract's "model unavailable"). A model that is on disk but not
# reserved counts as usable: a FILE run reserves it once the file opens.
# Otherwise every FILE run fails up front with the probe's own code (69/75).
WANT_BAD=$PROBE_RC
WANT_CLIP=$PROBE_RC
if [ "$PROBE_RC" = 0 ] || [ "$PROBE_STATE" = unreserved ]; then
    WANT_BAD=70
    WANT_CLIP="0 75"
fi
echo "not audio" >"$TMP/not-audio.wav"
run 120 "$TMP/out" "$TMP/err" "$BIN" transcribe --locale en-US "$TMP/not-audio.wav"
expect "transcribe a non-audio file" "$WANT_BAD"
check "a failed transcription prints nothing on stdout" stdout_empty "$TMP/out"

run 60 "$TMP/out" "$TMP/err" say -o "$TMP/clip.wav" --data-format=LEI16@16000 \
    "Good morning everyone. Let's review the pricing experiment for Canada."
if [ "$RC" = 0 ] && [ -s "$TMP/clip.wav" ]; then
    SINCE=$(date '+%Y-%m-%d %H:%M:%S')
    run 300 "$TMP/clip.json" "$TMP/err" "$BIN" transcribe --locale en-US "$TMP/clip.wav"
    CLIP_PID=$(cat "$TMP/pid")
    # shellcheck disable=SC2086  # WANT_CLIP is a list of codes
    expect "transcribe a say clip" $WANT_CLIP
    if [ "$PROBE_RC" = 0 ] && [ "$RC" = 75 ]; then
        echo "warning: the probe said ready but transcription reported the model unavailable (75):"
        sed 's/^/      stderr: /' "$TMP/err" | head -3 || true
    fi
    if [ "$RC" = 0 ]; then
        cat "$TMP/clip.json"
        check "transcript JSON has every contract key" json_keys "$TMP/clip.json" text segments locale ms
        check "transcript has text and timed segments" transcript_ok "$TMP/clip.json"
        check "Speech logged no 'unallocated locales' error for the clip" \
            no_unallocated_log "$CLIP_PID" "$SINCE"
        if [ "$PROBE_STATE" = unreserved ]; then
            # The clip run found the model on disk but unreserved, and reserved it.
            run 120 "$TMP/probe2.json" "$TMP/err" "$BIN" transcribe --probe --locale en-US
            expect "probe after the clip run reserved the on-disk model" 0
            if [ "$RC" = 0 ]; then
                check "the clip run left the locale reserved by this app" \
                    ready_means_reserved "$TMP/probe2.json"
            fi
        fi
    fi
else
    echo "warning: say could not render a clip on this runner; skipping the transcription check"
fi

# The x86_64 slice must route `transcribe` too (Intel Macs and Rosetta get 69).
ARCHS=$(lipo -archs "$BIN" 2>/dev/null || true)
case " $ARCHS " in *" x86_64 "*) HAS_X86=1 ;; *) HAS_X86=0 ;; esac
if [ "$HAS_X86" = 1 ] && arch -x86_64 /usr/bin/true 2>/dev/null; then
    run 120 "$TMP/out" "$TMP/err" arch -x86_64 "$BIN" transcribe --probe --locale en-US
    expect "x86_64 slice: transcribe --probe" 0 69 75
    cat "$TMP/out"
    run 30 "$TMP/out" "$TMP/err" arch -x86_64 "$BIN" --help
    expect "x86_64 slice: sysaudio --help" 0
else
    echo "note: Rosetta unavailable here; x86_64 slice not executed"
fi

if [ "$FAILED" != 0 ]; then
    echo "sysaudio smoke test FAILED"
    exit 1
fi
echo "sysaudio smoke test passed"
