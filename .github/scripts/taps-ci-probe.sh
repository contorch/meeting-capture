#!/bin/bash
# INFORMATIONAL ONLY (CI runners; never a developer's Mac): runs the real
# `sysaudio --backend taps` for a few seconds on a GitHub macOS runner while
# afplay plays a tone this script generated, and reports what came out:
# whether the tap and its aggregate device could be built and started at all
# on this OS, whether IO runs (frames keep flowing even when nothing plays),
# and whether the tone is audible in them (it is only if the runner's TCC
# allows System Audio Recording for this process; a refusal is silence).
#
# It never fails the job (the workflow step is continue-on-error too): the
# real verdict on taps is the M5 lab (protocol step 16), on a person's Mac.
#
# usage: taps-ci-probe.sh path/to/sysaudio
# Bash 3.2 compatible.
set -uo pipefail

if [ "${GITHUB_ACTIONS:-}" != "true" ]; then
    echo "taps-ci-probe.sh captures system audio: it only runs on GitHub Actions runners" >&2
    exit 2
fi
BIN="${1:?usage: taps-ci-probe.sh path/to/sysaudio}"
case "$BIN" in /*) ;; *) BIN="$PWD/$BIN" ;; esac
TMP=$(mktemp -d)
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$TMP"' EXIT

echo "== taps probe on $(sw_vers -productVersion) ($(uname -m))"
"$BIN" check --json || true
system_profiler SPAudioDataType 2>/dev/null | sed -n '1,40p' || true

python3 - "$TMP/tone.wav" <<'PY'
import math, struct, sys, wave
rate, secs = 48000, 12
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(b"".join(struct.pack("<h", int(0.4 * 32767 * math.sin(2 * math.pi * 440 * i / rate)))
                           for i in range(rate * secs)))
PY

# 1) nothing playing, 8 s; 2) a tone playing, 6 s. One sysaudio run each.
run_probe() {
    local secs="$1" out="$2" err="$3"
    perl -e 'alarm shift; exec @ARGV' "$secs" "$BIN" --backend taps --sample-rate 16000 >"$out" 2>"$err"
    echo "exit $? (142 = stopped by the probe's timer, i.e. it was still running)"
    sed 's/^/    stderr: /' "$err" | head -20
}

echo "-- silence (8 s)"
run_probe 8 "$TMP/silence.pcm" "$TMP/silence.err"
echo "-- tone"
afplay "$TMP/tone.wav" &
sleep 1
run_probe 6 "$TMP/tone.pcm" "$TMP/tone.err"

python3 - "$TMP/silence.pcm" "$TMP/tone.pcm" <<'PY'
import math, struct, sys
for path in sys.argv[1:]:
    data = open(path, "rb").read()
    n = len(data) // 2
    s = struct.unpack("<%dh" % n, data[: n * 2]) if n else ()
    rms = math.sqrt(sum(v * v for v in s) / n) / 32768 if n else 0.0
    peak = max((abs(v) for v in s), default=0) / 32768
    print(f"{path.rsplit('/', 1)[-1]}: {n / 16000:.2f} s of audio, rms {rms:.4f}, peak {peak:.4f}")
PY
exit 0
