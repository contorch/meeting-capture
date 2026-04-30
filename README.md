# meeting-capture

Always-on, fully local meeting transcription daemon for macOS. Detects when another app is using your microphone (any video/audio call), captures system audio output via ScreenCaptureKit, transcribes it locally with mlx-whisper, and writes timestamped Markdown transcripts to `~/transcripts/`.

No driver, no kernel extension, no `sudo`, no reboot. One user-grantable Screen Recording permission.

Pairs with [context-orchestrator](https://github.com/stirredo/context-orchestrator), which auto-indexes the transcripts into a searchable vector store. The two are coupled only via the `~/transcripts/` directory; either runs independently.

## Requirements

- macOS 13.0 or later (ScreenCaptureKit)
- Apple Silicon (mlx-whisper)
- Python 3.10+
- Xcode command-line tools (`xcode-select --install`)
- Homebrew (for `ffmpeg`)

## Install

```bash
git clone https://github.com/stirredo/meeting-capture.git
cd meeting-capture
./setup.sh
```

`setup.sh` checks prerequisites, installs `ffmpeg`, builds the `sysaudio` Swift binary, creates a Python venv, registers the launchd auto-start agent, and triggers the macOS permission prompt.

After setup:

1. Open **System Settings → Privacy & Security → Screen & System Audio Recording**.
2. Add the parent terminal application (Warp, Terminal, iTerm, etc.) — macOS attributes the permission to the parent process, not to the CLI binary itself.
3. Restart that terminal once for the grant to take effect.

To verify the install:

```bash
.venv/bin/meeting-capture doctor
```

## Usage

The daemon runs in the background. Day-to-day there is nothing to do — when you join a Zoom / Teams / Meet / FaceTime / browser meeting, the daemon detects the mic activation within ~2 seconds, starts capturing, and writes the transcript as the meeting progresses. When you leave the call the daemon flushes the in-flight chunk and idles until the next meeting.

CLI commands for inspection and control:

| Command | Purpose |
|---|---|
| `meeting-capture status` | Daemon state, mic state, last transcript, last log line |
| `meeting-capture doctor` | Full health check of all prerequisites and components |
| `meeting-capture mic` | Show current microphone-activity state |
| `meeting-capture last` | Print the path of the most recent transcript |
| `meeting-capture tail` | Follow the daemon log |
| `meeting-capture pause` | Pause capture (creates `~/.meeting-capture/paused`) |
| `meeting-capture resume` | Resume capture |
| `meeting-capture install` | Install the launchd auto-start agent |
| `meeting-capture uninstall` | Remove the launchd auto-start agent |
| `meeting-capture start` / `stop` | Manual daemon control |
| `meeting-capture run` | Run daemon in the foreground (for debugging) |
| `meeting-capture vocab [edit\|clear\|path]` | Show / edit / clear the per-machine Whisper vocabulary bias |

## Architecture

```
mic activates                                     mic deactivates
     │                                                  │
     ▼                                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│  meeting-capture daemon (Python, launchd-managed)                │
│  - polls Core Audio HAL for mic activity every 2s                │
│  - while active: spawns sysaudio subprocess                      │
│  - reads PCM, splits on silence (≥3s gap, ≥8s min, ≤600s max)    │
│  - hands each chunk to mlx-whisper, appends text to .md file     │
│  - on mic-off: flushes in-flight buffer, terminates sysaudio     │
└──────────────────────────────────────────────────────────────────┘
            │                                          │
            ▼                                          ▼
   ┌──────────────────┐                    ┌────────────────────────┐
   │ sysaudio (Swift) │                    │ ~/transcripts/         │
   │ ScreenCaptureKit │                    │   meeting-{ISO}.md     │
   │ → int16 LE PCM   │                    │ (chunks appended       │
   │   on stdout      │                    │  during the meeting)   │
   └──────────────────┘                    └────────────────────────┘
```

A new transcript file is started whenever the gap between chunks exceeds 15 minutes. Mid-meeting mic mutes do not fragment the file. Raw audio chunks are deleted from disk after transcription.

## Files

- `~/transcripts/meeting-*.md` — final transcripts
- `~/.meeting-capture/daemon.log` — daemon log (rotated by macOS)
- `~/.meeting-capture/paused` — pause sentinel
- `~/.meeting-capture/audio/` — temporary chunk WAVs (deleted post-transcription)
- `~/Library/LaunchAgents/com.stirredo.meeting-capture.plist` — launchd agent
- `bin/sysaudio` — built audio-capture binary (gitignored)

## Transcription tuning

Defaults: `mlx-community/whisper-large-v3-turbo` with a generic technical-vocab `initial_prompt`. The prompt biases the model toward project-specific proper nouns and acronyms (e.g. internal product names, service acronyms) that no model size can recover on its own.

### Per-machine vocabulary

The recommended way to tune the prompt is the per-machine vocab file at `~/.meeting-capture/vocab.txt`. The daemon re-reads it on every chunk, so changes apply immediately — no restart needed.

```bash
meeting-capture vocab          # show the effective prompt and where it came from
meeting-capture vocab edit     # open the file in $EDITOR (seeded with the default on first edit)
meeting-capture vocab clear    # remove the file (fall back to env var or built-in default)
meeting-capture vocab path     # print the file path (useful for piping)
```

Keep it concise — Whisper truncates beyond ~224 tokens (~1000 chars). A natural-sounding sentence in the same register as the audio works best (`This is a meeting about <Project>, <Service>, <acronym1>, ...`). An empty file means "no prompt at all" — useful when biasing turns out to hurt on a particular machine.

### Resolution order

`transcribe()` resolves model and prompt in this order:

1. Explicit `initial_prompt=...` argument (programmatic use)
2. `~/.meeting-capture/vocab.txt`
3. `MEETING_CAPTURE_WHISPER_PROMPT` env var
4. Built-in default

Model resolution is `MEETING_CAPTURE_WHISPER_MODEL` env var, then default.

## Tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## Troubleshooting

If `meeting-capture doctor` reports everything green but no transcripts appear:

1. Verify the parent terminal has Screen Recording permission and was restarted after granting.
2. Check `~/.meeting-capture/daemon.log` for errors from the capture subprocess.
3. Confirm system audio is actually playing through the default output device (the daemon captures the system audio mixdown).
