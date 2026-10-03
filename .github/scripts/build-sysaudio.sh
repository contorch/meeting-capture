#!/bin/bash
# Builds a universal (arm64 + x86_64) release sysaudio into ./sysaudio at the
# repo root. release-sysaudio.yml (tags) and the PR-time check in tests.yml
# both run this script, so they build with the same toolchain and flags.
#
# Select Xcode before running it (the workflows pin XCODE_APP).
set -euo pipefail
cd "$(dirname "$0")/../.."

sw_vers
xcode-select -p
# Cosmetic, and it can hit the same flaky NSFileHandle SIGABRT as the build.
# Outside the retry loop that would kill the job.
xcodebuild -version 2>/dev/null | head -2 || true
swift --version 2>&1 | head -1 || true

# `sysaudio transcribe` (SpeechAnalyzer, Transcribe.swift) only compiles in
# against the macOS 26 SDK. An older SDK still builds sysaudio, but that
# binary's transcribe always answers "unavailable", so refuse to build one.
SDK_VERSION=$(xcrun --sdk macosx --show-sdk-version)
echo "macOS SDK $SDK_VERSION"
if [ "${SDK_VERSION%%.*}" -lt 26 ]; then
    echo "FATAL: macOS SDK $SDK_VERSION is older than 26; select Xcode 26 or later"
    exit 1
fi

cd swift
LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT
# `swift build` intermittently SIGABRTs (exit 134) with an NSFileHandle crash
# on the GitHub runner. That is a known flaky toolchain bug, not our code, so
# retry a few times before giving up.
for attempt in 1 2 3; do
    if swift build -c release --arch arm64 --arch x86_64 >"$LOG" 2>&1; then
        tail -20 "$LOG"
        break
    fi
    tail -20 "$LOG"
    echo "swift build attempt $attempt failed (likely the flaky NSFileHandle SIGABRT), retrying"
    rm -rf .build/apple 2>/dev/null || true
    sleep 5
done
# The tail above can cut a diagnostic in half; list every compiler warning
# and error in full.
grep -E "(warning|error): " "$LOG" | grep -v -E "ld: warning: search path" | sort -u || true

# Universal products land in .build/apple/Products/Release (older toolchains)
# or .build/out/Products/Release (Swift 6.x). Never fall back to a
# single-arch build: fail if either arch is missing.
BIN=$(find .build -path "*/Products/Release/sysaudio" -type f | head -1)
[ -n "$BIN" ] || { echo "no universal sysaudio in .build"; exit 1; }
lipo -info "$BIN"
ARCHS=$(lipo -archs "$BIN")
case " $ARCHS " in *" arm64 "*) ;; *) echo "FATAL: sysaudio has no arm64 slice ($ARCHS)"; exit 1 ;; esac
case " $ARCHS " in *" x86_64 "*) ;; *) echo "FATAL: sysaudio has no x86_64 slice ($ARCHS)"; exit 1 ;; esac

# Both slices must carry the on-device transcription code. It is the only
# code that links Speech.framework (weakly, so macOS 13-15 still launch it).
for arch in arm64 x86_64; do
    LIBS=$(otool -arch "$arch" -L "$BIN")
    case "$LIBS" in
        *Speech.framework*) echo "$arch slice links Speech.framework" ;;
        *) echo "FATAL: the $arch slice does not link Speech.framework (transcribe not compiled in?)"; exit 1 ;;
    esac
done
vtool -show-build "$BIN" | grep -E "architecture|minos" || true

cp "$BIN" ../sysaudio
echo "built $(cd .. && pwd)/sysaudio"
