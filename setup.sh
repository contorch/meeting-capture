#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PY="$VENV/bin/python"

INSTALL_LAUNCHD=1
for arg in "$@"; do
    case "$arg" in
        --no-launchd) INSTALL_LAUNCHD=0 ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--no-launchd]

  --no-launchd    Build & install Python package, but skip launchd auto-start.
                  Use this if you want to run the daemon manually with
                  \`meeting-capture run\` instead of having it start at login.
EOF
            exit 0
            ;;
    esac
done

echo "Setting up meeting-capture..."

# ---------------------------------------------------------------------------
# Prereq check — fail loudly with exact next steps if anything's missing.
# ---------------------------------------------------------------------------
PREREQS_OK=1
fail() { echo "  ✗ $1"; PREREQS_OK=0; }
ok()   { echo "  ✓ $1"; }

echo ""
echo "Checking prerequisites..."

# macOS version >= 13 (ScreenCaptureKit)
OS_VERSION=$(sw_vers -productVersion)
OS_MAJOR=$(echo "$OS_VERSION" | cut -d. -f1)
if [ "$OS_MAJOR" -ge 13 ]; then
    ok "macOS $OS_VERSION (>= 13.0 required for ScreenCaptureKit)"
else
    fail "macOS $OS_VERSION is too old. Need 13.0+ for ScreenCaptureKit. Update macOS."
fi

# Python 3.10+
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        ok "Python $PY_VERSION"
    else
        fail "Python $PY_VERSION is too old. Need 3.10+. Install via Homebrew (\`brew install python@3.11\`) or python.org."
    fi
else
    fail "python3 not on PATH. Install via Homebrew (\`brew install python@3.11\`) or python.org."
fi

# git
if command -v git &>/dev/null; then
    ok "git $(git --version | awk '{print $3}')"
else
    fail "git not on PATH. Comes with Xcode CLI tools (\`xcode-select --install\`)."
fi

# Xcode CLI tools (Swift compiler)
if command -v swift &>/dev/null; then
    ok "Swift $(swift --version | head -1 | awk '{print $4}')"
else
    fail "swift not found. Install Xcode CLI tools: \`xcode-select --install\` (interactive)."
fi

if [ "$PREREQS_OK" -eq 0 ]; then
    echo ""
    echo "Prerequisites missing. Fix the items marked ✗ above and re-run."
    exit 1
fi
echo ""

# ---------------------------------------------------------------------------
# 2. Build sysaudio (ScreenCaptureKit)
# ---------------------------------------------------------------------------
mkdir -p "$SCRIPT_DIR/bin"
SYSAUDIO_BIN="$SCRIPT_DIR/bin/sysaudio"

echo "Building sysaudio (SCK, release)..."
(cd "$SCRIPT_DIR/swift" && swift build -c release --quiet)

SYSAUDIO_BUILT_DIR=$(cd "$SCRIPT_DIR/swift" && swift build -c release --show-bin-path)
SYSAUDIO_BUILT="$SYSAUDIO_BUILT_DIR/sysaudio"
if [ ! -x "$SYSAUDIO_BUILT" ]; then
    echo "sysaudio build failed — no binary at $SYSAUDIO_BUILT"
    exit 1
fi

cp "$SYSAUDIO_BUILT" "$SYSAUDIO_BIN"
codesign --force --sign - "$SYSAUDIO_BIN" 2>/dev/null || true
chmod +x "$SYSAUDIO_BIN"
echo "sysaudio binary: $SYSAUDIO_BIN"

# ---------------------------------------------------------------------------
# 3. venv + Python install
# ---------------------------------------------------------------------------
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
fi
echo "Installing meeting-capture Python package..."
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -e "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 4. Trigger the screen-recording permission prompt (one-time)
# ---------------------------------------------------------------------------
echo ""
echo "Triggering screen-recording permission prompt (approve in System Settings)..."
echo "  (Will exit after ~2 seconds.)"
"$SYSAUDIO_BIN" --sample-rate 16000 >/dev/null 2>&1 &
PROBE_PID=$!
sleep 2
kill "$PROBE_PID" 2>/dev/null || true
wait "$PROBE_PID" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5. Auto-install launchd agent (skipped with --no-launchd)
# ---------------------------------------------------------------------------
if [ "$INSTALL_LAUNCHD" -eq 1 ]; then
    echo ""
    echo "Installing launchd auto-start agent..."
    "$VENV/bin/meeting-capture" install
else
    echo ""
    echo "Skipping launchd install (--no-launchd flag)."
fi

# ---------------------------------------------------------------------------
# 6. Done
# ---------------------------------------------------------------------------
cat <<EOF

Done. Diagnostic:
  $VENV/bin/meeting-capture doctor   # full health check (binaries, permissions, daemon)
  $VENV/bin/meeting-capture status   # daemon + mic + last transcript

Permission setup (the only manual step):
  1. System Settings -> Privacy & Security -> Screen & System Audio Recording
  2. Add the parent terminal app (Warp / Terminal / iTerm) AND/OR add
     the binary $SYSAUDIO_BIN directly to the list
  3. Restart the terminal so the grant takes effect

Then you're done. Next time you join a Zoom/Teams/Meet/FaceTime call, the
daemon will detect mic activity within 2s and start capturing.

Transcripts land in ~/transcripts/ (picked up by context-orchestrator's transcript-watcher).
Pause:  touch ~/.meeting-capture/paused
Resume: rm    ~/.meeting-capture/paused
EOF
