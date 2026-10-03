#!/bin/bash
# Smoke-tests a built sysaudio WITHOUT capturing anything. It never runs the
# capture path, which would need Screen Recording and the mic. It checks:
#   - the capture CLI still parses as before (--help; an unknown flag exits 1
#     with "unknown arg"),
#   - the `transcribe` exit-code contract that meeting_capture relies on
#     (see swift/Sources/sysaudio/Transcribe.swift),
#   - one real transcription of a `say` clip, when the runner can transcribe.
#
# usage: sysaudio-smoke.sh path/to/sysaudio [--try-install]
#   --try-install  if the en-US model is missing (probe exit 75), try to
#                  download it so the transcription path runs too. Failure to
#                  download is a warning, not an error.
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
# and sets RC. A hang shows up as 142 (SIGALRM).
run() {
    local secs="$1" out="$2" err="$3"
    shift 3
    set +e
    perl -e 'alarm shift; exec @ARGV or die "exec failed: $!\n"' "$secs" "$@" >"$out" 2>"$err"
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
    json_keys "$TMP/probe.json" available reason os arch locale installed supported installed_locales
check "built with the macOS 26 SDK (transcribe compiled in)" \
    not_in_file "built without the macOS 26 SDK" "$TMP/probe.json"

run 120 "$TMP/out" "$TMP/err" "$BIN" transcribe --probe --locale xx-XX
expect "transcribe --probe --locale xx-XX (unsupported)" 69
check "unsupported-locale probe JSON" json_keys "$TMP/out" available reason locale

if [ "$PROBE_RC" = 75 ] && [ "$TRY_INSTALL" = 1 ]; then
    echo "en-US model missing on this runner; trying --install (warning only)"
    run 600 "$TMP/install.json" "$TMP/install.err" "$BIN" transcribe --install --locale en-US
    cat "$TMP/install.json" || true
    tail -5 "$TMP/install.err" || true
    if [ "$RC" = 0 ]; then
        check "install JSON has every contract key" json_keys "$TMP/install.json" installed locale seconds
        run 120 "$TMP/probe.json" "$TMP/err" "$BIN" transcribe --probe --locale en-US
        PROBE_RC=$RC
        expect "probe after install" 0
    else
        echo "warning: --install exited $RC; skipping the transcription check"
    fi
fi

# With a usable model, a bad file is 70 and a clip transcribes (exit 0; 75 is
# tolerated because a CI VM may refuse the model for lack of resources, which
# is the contract's "model unavailable"). Otherwise every FILE run fails up
# front with the probe's own code (69/75).
WANT_BAD=$PROBE_RC
WANT_CLIP=$PROBE_RC
if [ "$PROBE_RC" = 0 ]; then
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
    run 300 "$TMP/clip.json" "$TMP/err" "$BIN" transcribe --locale en-US "$TMP/clip.wav"
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
