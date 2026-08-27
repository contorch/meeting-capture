# meeting-capture

Always-on meeting transcription daemon for macOS. Detects when another app is using your microphone (any video/audio call), captures **both sides of the meeting** — system audio output (the other participants) and your own microphone — via ScreenCaptureKit, transcribes each side via Google's hosted Gemini audio models, and writes timestamped, speaker-attributed (`**Me:**` / `**Them:**`) Markdown transcripts to `~/transcripts/`.

No driver, no kernel extension, no `sudo`, no reboot. Two user-grantable permissions: Screen Recording (system audio) and Microphone (your voice; macOS 15+, optional — without it you get system audio only).

> **Note:** transcription is hosted (Gemini), so each audio chunk is sent to Google's API and a Google API key is required. A previous local mlx-whisper backend was removed — it ran on the GPU and its unbounded MLX Metal buffer cache leaked tens of GB in a long-lived daemon.

Pairs with [context-orchestrator](https://github.com/contorch/context-orchestrator), which auto-indexes the transcripts into a searchable vector store. The two are coupled only via the `~/transcripts/` directory; either runs independently.

## Requirements

- macOS 13.0 or later (ScreenCaptureKit); macOS 15.0+ for own-voice ("me") capture
- Python 3.10+
- Xcode command-line tools (`xcode-select --install`)
- A Google API key for Gemini — from `$GOOGLE_API_KEY`, `$GEMINI_API_KEY`, or `~/.config/google/key` (mode 600)

## Install

```bash
git clone https://github.com/contorch/meeting-capture.git
cd meeting-capture
./setup.sh
```

`setup.sh` checks prerequisites, builds the `sysaudio` Swift binary, creates a Python venv, registers the launchd auto-start agent, and triggers the macOS permission prompt.

After setup:

1. Open **System Settings → Privacy & Security → Screen & System Audio Recording**, click **+**, and add `bin/sysaudio` from the repo (⌘⇧G in the file dialog to type the path). Make sure it's enabled under the **System Audio Recording Only** list in the same pane too — system audio captures as silence until that grant lands. The daemon spawns sysaudio with TCC responsibility disclaimed, so permissions attach to the `sysaudio` binary itself — the same grant works under your terminal and under launchd, with no terminal-restart dance.
2. The first recording session pops a **Microphone** permission prompt titled "sysaudio" (for own-voice capture) — click Allow. Deny it (or skip it) and you get system-audio-only transcripts.
3. After rebuilding sysaudio (`setup.sh` or `swift build`), re-add it in step 1 — the ad-hoc code signature changes with each build, which invalidates the previous grant.

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

## Architecture

```
mic activates                                     mic deactivates
     │                                                  │
     ▼                                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│  meeting-capture daemon (Python, launchd-managed)                │
│  - polls Core Audio HAL for mic activity every 2s                │
│    (per-process attribution; our own capture is excluded)        │
│  - while active: spawns sysaudio subprocess                      │
│  - two channels: system audio = "them", microphone = "me"        │
│  - each channel splits on silence (≥3s gap, ≥8s min, ≤600s max)  │
│  - sends each chunk to Gemini, appends labeled text to .md file  │
│  - on mic-off: flushes in-flight buffers, terminates sysaudio    │
└──────────────────────────────────────────────────────────────────┘
            │                                          │
            ▼                                          ▼
   ┌────────────────────┐                  ┌────────────────────────┐
   │ sysaudio (Swift)   │                  │ ~/transcripts/         │
   │ ScreenCaptureKit   │                  │   meeting-{ISO}.md     │
   │ system out + mic   │                  │ [ts] **Them:** ...     │
   │ → framed int16 LE  │                  │ [ts] **Me:** ...       │
   │   PCM on stdout    │                  │ (appended live)        │
   └────────────────────┘                  └────────────────────────┘
```

A new transcript file is started whenever the gap between chunks exceeds 15 minutes. Mid-meeting mic mutes do not fragment the file. Raw audio chunks are deleted from disk after transcription.

### Two-channel (me/them) capture

On macOS 15+ `sysaudio` captures the microphone alongside system audio in the same ScreenCaptureKit stream (`--mic`; framed stdout protocol, both channels 16 kHz mono int16). Each channel runs through its own silence chunker, and transcript lines are labeled `**Me:**` (your mic) or `**Them:**` (system audio). Speaker attribution across the me/them boundary is therefore exact; multiple remote speakers within a "them" chunk still get best-effort `[SPEAKER_n]` labels from Gemini. Set `MEETING_CAPTURE_MIC=0` to opt out (system audio only). On macOS 13/14 the daemon runs system-audio-only automatically.

Mic-activity gating uses per-process Core Audio HAL attribution (`kAudioProcessPropertyIsRunningInput`) and ignores `com.apple.replayd`, ScreenCaptureKit's capture backend — otherwise the daemon's own mic capture would hold the "mic in use" gate open forever. Real meeting apps hold the mic under their own process, so gating is unaffected.

Note on echo: without headphones, your mic also picks up the other side from the speakers, so "me" chunks can contain "them" speech. Headphones (incl. AirPods) avoid this; OS-level echo cancellation is a possible future addition.

## Files

- `~/transcripts/meeting-*.md` — final transcripts
- `~/.meeting-capture/daemon.log` — daemon log (rotated by macOS)
- `~/.meeting-capture/paused` — pause sentinel
- `~/.meeting-capture/audio/` — temporary chunk WAVs (deleted post-transcription)
- `~/Library/LaunchAgents/com.contorch.meeting-capture.plist` — launchd agent
- `bin/sysaudio` — built audio-capture binary (gitignored)

## Transcription

Transcription is hosted on Google's Gemini. Default model: `gemini-3.5-transcribe` — Google's purpose-built speech-to-text model (~$0.005 per meeting-minute per channel at list prices), called through the Interactions API in verbatim mode. If it errors, the chunk automatically falls back to `gemini-2.5-flash` (prompted transcription, ~$0.0025/min) so nothing is lost. Override with `MEETING_CAPTURE_GEMINI_MODEL`; both models return an empty string for silence/noise rather than hallucinated filler.

### Live mode & the in-meeting copilot

By default the daemon runs in **batch** mode: it chunks audio and transcribes after each pause (cheapest, most robust). Set `MEETING_CAPTURE_MODE=live` and it streams to `gemini-3.5-transcribe-live` instead — ~1-second interim hypotheses and finalized utterances — which is what the in-meeting copilot needs. Finals still land in `~/transcripts/*.md` exactly as in batch mode; live *additionally* writes a per-session feed under `~/.meeting-capture/live/`.

Two panes during a meeting:

```bash
MEETING_CAPTURE_MODE=live meeting-capture run   # pane 1: the daemon, streaming
meeting-capture copilot                          # pane 2: whispers from your memory
```

`meeting-capture copilot` watches the live feed and, when the other side asks something you'd want help answering, retrieves from your **past meetings** and whispers the fact, decision, or number — with the meeting it came from — or stays silent when it has nothing useful. `meeting-capture live [--interim]` tails the raw transcript feed. Costs are higher in live mode (~$0.009/min per channel streaming, plus a cheap LLM call per copilot whisper), so it's opt-in.

### Vocabulary

Proper nouns are where transcription goes wrong. Put yours — names, products, jargon — in `~/.meeting-capture/vocab.txt` (one per line, up to 1,000; `meeting-capture vocab edit`) and the transcribe model spells them deterministically. Without it, "contorch" came back as "Concourse" in our tests; with it, never.

`MEETING_CAPTURE_DIARIZE=1` turns on speaker diarization for the `Them` channel (`[SPEAKER_n]` per turn). The API makes diarization and vocabulary mutually exclusive, so it's off by default — memory fidelity beats speaker labels within a channel; the Me/Them split is exact regardless.

### API key

A Google API key is required, resolved in this order:

1. `$GOOGLE_API_KEY`
2. `$GEMINI_API_KEY`
3. `~/.config/google/key` (mode 600)

`meeting-capture doctor` reports whether a key is found.

### Memory guardrail

The daemon self-exits (and launchd respawns it) if its `phys_footprint` exceeds `MEETING_CAPTURE_MAX_FOOTPRINT_MB` (default 2048) — a backstop against any runaway-memory regression. The check uses `phys_footprint`, not RSS, because leaked memory is often compressed/swapped and invisible to RSS.

## Tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Troubleshooting

If `meeting-capture doctor` reports everything green but no transcripts appear:

1. Verify the parent terminal has Screen Recording permission and was restarted after granting.
2. Check `~/.meeting-capture/daemon.log` for errors from the capture subprocess.
3. Confirm system audio is actually playing through the default output device (the daemon captures the system audio mixdown).
