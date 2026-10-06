# meeting-capture

Always-on meeting transcription daemon for macOS. Detects when another app is using your microphone (any video/audio call), captures **both sides of the meeting** — system audio output (the other participants) and your own microphone — via ScreenCaptureKit, transcribes each side — on the Mac itself (macOS 26+, Apple silicon) or with Google's hosted Gemini models — and stores timestamped, speaker-attributed (`**Me:**` / `**Them:**`) transcripts in the contorch database (`~/.context-orchestrator/context.db`, table `transcripts`) — no transcript files.

No driver, no kernel extension, no `sudo`, no reboot. Two user-grantable permissions: Screen Recording (system audio) and Microphone (your voice; macOS 15+, optional — without it you get system audio only).

> **Note:** on macOS 26+ with Apple silicon, transcription runs **on this Mac** by default (Apple's on-device speech recognition) — no audio leaves the machine and no API key is needed. Gemini is optional: it is used only when you choose it, or automatically when on-device transcription isn't available and a Google API key is set. See [Transcription](#transcription).

Pairs with [context-orchestrator](https://github.com/contorch/context-orchestrator), which indexes each meeting into a searchable vector store and serves the full text (`get_transcript`). The two are coupled only via that SQLite table (the same `CREATE TABLE` on both sides; `CO_DB_PATH` overrides the location); either runs independently.

## Requirements

- macOS 15.0 or later (ScreenCaptureKit with the microphone, for both sides of a call)
- Python 3.10+
- Xcode command-line tools (`xcode-select --install`)
- For on-device transcription: macOS 26+ on Apple silicon (nothing else — no key, no account)
- Otherwise (Intel, macOS < 26, or by choice): a Google API key for Gemini — from `$GOOGLE_API_KEY`, `$GEMINI_API_KEY`, or `~/.config/google/key` (mode 600)

## Install

```bash
git clone https://github.com/contorch/meeting-capture.git
cd meeting-capture
./setup.sh
```

`setup.sh` checks prerequisites, builds the `sysaudio` Swift binary, creates a Python venv, registers the launchd auto-start agent, and triggers the macOS permission prompt.

After setup:

1. Open **System Settings → Privacy & Security → Screen & System Audio Recording**, click **+**, and add `bin/sysaudio` from the repo (⌘⇧G in the file dialog to type the path). Make sure it's enabled under the **System Audio Recording Only** list in the same pane too — system audio captures as silence until that grant lands. Every sysaudio run (capture, `transcribe`, `check`) is started with TCC responsibility disclaimed, so permissions attach to the `sysaudio` binary itself — the same grant works under your terminal and under launchd, with no terminal-restart dance. `meeting-capture check` shows both permissions as sysaudio sees them.
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
| `meeting-capture check [--request screen_audio\|system_audio\|microphone]` | The recorder's permissions as sysaudio sees them (and which capture backend they select), and how to fix each; `--json` for other programs (see [Contract](#contract)) |
| `meeting-capture mic` | Show current microphone-activity state |
| `meeting-capture last` | Print the path of the most recent transcript |
| `meeting-capture tail` | Follow the daemon log |
| `meeting-capture pause` | Pause capture (creates `~/.meeting-capture/paused`) |
| `meeting-capture resume` | Resume capture |
| `meeting-capture new` | Start a new meeting (speech from now on goes into a new transcript) |
| `meeting-capture stt [auto\|apple\|gemini] [--language L]` | Show the transcription engine in use and why, or switch it (restarts the daemon); `--json` prints the state for other programs (see [Contract](#contract)) |
| `meeting-capture stt --check-key` | Ask Google whether the recorder's Gemini key is accepted (exit 0 accepted, 1 otherwise) |
| `meeting-capture --version` | Print the version |
| `meeting-capture language [LOCALE]` | Show or set the on-device language (installs its model first) |
| `meeting-capture ui` | Settings page: audio source, interface inputs and levels, transcription engine and language |
| `meeting-capture config [set\|unset KEY VALUE]` | The recorder's settings (`~/.meeting-capture/env`) and where each comes from; `--json` for other programs |
| `meeting-capture install` / `uninstall` | Install / remove the recorder's launchd agent (starts at login) |
| `meeting-capture start` / `stop` / `restart` | Start the recorder (and at login), stop it (stays stopped at login), restart it to apply a hand edit of the settings; `--json` for other programs |
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
│  - queues each chunk; one worker thread transcribes it (on this  │
│    Mac or Gemini) and appends labeled text to the DB             │
│  - on mic-off: flushes in-flight buffers, terminates sysaudio    │
└──────────────────────────────────────────────────────────────────┘
            │                                          │
            ▼                                          ▼
   ┌────────────────────┐                  ┌────────────────────────┐
   │ sysaudio (Swift)   │                  │ context.db transcripts │
   │ ScreenCaptureKit   │                  │   row meeting-{ISO}    │
   │ system out + mic   │                  │ [ts] **Them:** ...     │
   │ → framed int16 LE  │                  │ [ts] **Me:** ...       │
   │   PCM on stdout    │                  │ (appended live)        │
   └────────────────────┘                  └────────────────────────┘
```

A new transcript (row) is started whenever the gap between chunks exceeds 15 minutes. Mid-meeting mic mutes do not fragment it. If the database can't be written (locked, disk full), lines queue in `~/.meeting-capture/unsaved-lines.jsonl` and are written ahead of the next line. Raw audio chunks are deleted from disk after transcription.

Capture never waits for transcription: the capture loop decides each chunk's meeting and hands it to a bounded queue, and a single worker thread transcribes the chunks in order. A chunk that can't be transcribed is never deleted — it is kept in `~/.meeting-capture/audio/failed/` (with a small `.json` note of its meeting and attempts) and retried when the worker is idle: after the engine comes back if it was unavailable (no key, on-device model missing), or after the next session for other errors. A file the engine rejects as unreadable counts a failed attempt at once; any other failure (a Gemini or network error, or the on-device helper failing, hanging or crashing) counts only once the next chunk shows the engine working, so an outage or a broken speech service never uses up attempts. A file that fails on its own three times moves to `audio/failed/quarantine/` so it can't hold up the rest (move it back to retry).

### Two-channel (me/them) capture

On macOS 15+ `sysaudio` captures the microphone alongside system audio in the same ScreenCaptureKit stream (`--mic`; framed stdout protocol, both channels 16 kHz mono int16). Each channel runs through its own silence chunker, and transcript lines are labeled `**Me:**` (your mic) or `**Them:**` (system audio). Speaker attribution across the me/them boundary is therefore exact; multiple remote speakers within a "them" chunk still get best-effort `[SPEAKER_n]` labels when Gemini transcribes. Set `MEETING_CAPTURE_MIC=0` to opt out (system audio only). On macOS 13/14 the daemon runs system-audio-only automatically.

Mic-activity gating uses per-process Core Audio HAL attribution (`kAudioProcessPropertyIsRunningInput`) and ignores `com.apple.replayd`, ScreenCaptureKit's capture backend, and the recorder's own sysaudio child (by pid; the taps backend opens the mic under sysaudio's own process) — otherwise the daemon's own mic capture would hold the "mic in use" gate open forever. Real meeting apps hold the mic under their own process, so gating is unaffected. `meeting-capture mic` lists every process holding an input.

### Capture backends: ScreenCaptureKit or Core Audio taps

`sysaudio --backend sck|taps` picks how the other side of the call is captured; the stdout protocol (raw, or `S`/`M` frames of 16 kHz mono int16) is the same for both, so everything downstream is unchanged.

| | `sck` (ScreenCaptureKit) | `taps` (Core Audio process taps) |
|---|---|---|
| macOS | 13+ (mic: 15+) | 14.2+ |
| "Them" permission | Screen & System Audio Recording (macOS re-confirms it about monthly) | System Audio Recording Only (`NSAudioCaptureUsageDescription`), no re-confirmation |
| "Me" | SCK's microphone capture | the default input through the HAL |
| Device changes | ScreenCaptureKit's | rebuilt by sysaudio on output/input switches, rate changes, a device dying, IO stalls (sleep/wake); gives up (exit 2, the recorder respawns it) rather than hang |

`MEETING_CAPTURE_BACKEND` = `auto` (default) \| `taps` \| `sck` (`meeting-capture config set backend sck` is the rollback). **auto** uses taps on macOS 15+ when sysaudio has them and System Audio Recording Only is allowed — or when Screen & System Audio Recording isn't (a new install is only ever asked for the narrower permission); a Mac already recording with the screen grant stays on sck, with no new prompt, until `meeting-capture check --request system_audio` (or setup) allows system audio. A sysaudio that predates `--backend` is run exactly as before. The daemon log names the choice once per change (`capture backend: taps — …`), and `check --json` reports it (`backend`). While System Audio Recording Only is still undecided, macOS holds the tap's start until its prompt is answered, so the recorder then waits up to 5 minutes for the first audio instead of the usual 30 s; setup asks first (`check --request system_audio`) so this is rare.

Note on echo: without headphones, your mic also picks up the other side from the speakers, so "me" chunks can contain "them" speech. Headphones (incl. AirPods) avoid this; OS-level echo cancellation is a possible future addition.

## Files

- `~/.context-orchestrator/context.db` — transcripts (`meeting-capture last` prints the latest; contorch's `get_transcript` / `contorch-transcripts show` any)
- `~/.meeting-capture/daemon.log` — daemon log (rotated by macOS); under launchd the daemon opens it itself, so sysaudio's lines land there too
- `~/.meeting-capture/state.json` — what the recorder is doing (idle / recording / paused), for `meeting-capture status --json`
- `~/.meeting-capture/paused` — pause sentinel
- `~/.meeting-capture/audio/` — temporary chunk WAVs (deleted post-transcription); `audio/failed/` holds audio waiting for a retry, `audio/failed/quarantine/` files that failed three times
- `~/.meeting-capture/env` — the recorder's settings (`KEY=VALUE`, see [Settings](#settings))
- `~/.meeting-capture/agent.json` — written when Contorch.app registers its bundled recorder agent (which app, which sysaudio); removed when it unregisters
- `~/Library/LaunchAgents/com.contorch.meeting-capture.plist` — launchd agent (no settings: the program, `PATH`, the pinned sysaudio and `CONTORCH_CHANNEL`)
- `bin/sysaudio` — built audio-capture binary (gitignored). It also carries the on-device speech-to-text helper, `sysaudio transcribe` (`--help` lists its flags; macOS 26+ on Apple silicon). That helper is compiled in only when sysaudio is built with the macOS 26 SDK (Xcode or command-line tools 26+). An older toolchain still builds capture, and its `transcribe` reports "unavailable".

## Transcription

Two engines, chosen with `meeting-capture stt` (or the settings page) and stored in `~/.meeting-capture/env` as `MEETING_CAPTURE_STT`:

| Setting | What happens |
|---|---|
| `auto` (default) | **On this Mac** when it can (macOS 26+, Apple silicon, the language's model installed); otherwise Gemini if a Google API key is set; otherwise the audio is kept and transcribed later. |
| `apple` | **On this Mac only.** Never uploads anything: if on-device transcription can't run, the audio is kept and retried on-device later. |
| `gemini` | Always Gemini (hosted; each chunk is uploaded to Google; needs a key). |

```bash
meeting-capture stt             # engine in use, why, and the language
meeting-capture stt apple       # on-device only (restarts the daemon)
meeting-capture language        # current language + the supported ones
meeting-capture language hi-IN  # downloads that language's model from Apple once, then switches
```

**On this Mac.** Apple's on-device speech recognition (SpeechAnalyzer / SpeechTranscriber), run by the same signed `sysaudio` binary that captures audio (`sysaudio transcribe`). No account, no key, no Speech Recognition permission prompt, and nothing leaves the Mac. The model runs in Apple's speech service, outside the daemon, about 8–150× faster than real time on Apple silicon. In our tests its accuracy on clean English meeting speech matched `gemini-2.5-flash`; it ignores the custom vocabulary (proper nouns are its weak spot), is weaker in noise, and can't diarize multiple remote speakers. If the language's model isn't on the Mac yet, the daemon downloads it from Apple once (English ≈ 140 MB is usually already there; `meeting-capture language` does it up front).

Languages (`MEETING_CAPTURE_LOCALE`; until you pick one, **your Mac's language** — the first preferred language in System Settings → General → Language & Region, when on-device transcription supports it; else `en-US`): English (US, UK, India, Australia, …), French, German, Spanish, Italian, Portuguese, Japanese, Korean, Chinese and the Indian languages — `meeting-capture language` lists exactly what this Mac supports. The English variants share one model (en-IN transcribes like en-US). **Hindi:** `hi-IN` downloads one shared Indian-languages model (≈ 250 MB, also covering Bengali, Tamil, Telugu, Marathi, Urdu, …) and its output is **romanized** — Hindi comes out in Latin script ("Hinglish"), and mixed Hindi/English speech works in one transcript (English-locale models mangle the Hindi words). For Devanagari output, use Gemini.

**Gemini.** Default model: `gemini-3.5-transcribe` — Google's purpose-built speech-to-text model (~$0.005 per meeting-minute per channel at list prices), called through the Interactions API in verbatim mode. If it errors, the chunk automatically falls back to `gemini-2.5-flash` (prompted transcription, ~$0.0025/min) so nothing is lost. Override with `MEETING_CAPTURE_GEMINI_MODEL`; both models return an empty string for silence/noise rather than hallucinated filler. Gemini detects the language by itself, honours the custom vocabulary, and is required for live mode.

**Upgrading from Gemini.** An install from before on-device transcription has no `MEETING_CAPTURE_STT`, so it is `auto`: once the language's model is on the Mac, transcription moves from Gemini to this Mac, in your Mac's language. If your Mac's language isn't English and on-device transcription can't do it, `auto` keeps using Gemini (which detects the language) as long as a key is set. Until you pick an engine or a language (`meeting-capture stt …`, `meeting-capture language …`, or saving on the settings page), `status`, `doctor`, `stt`, the settings page and the daemon log carry a note saying transcription now runs on this Mac and how to change it; `meeting-capture stt auto` keeps it and clears the note. Meetings in a language other than your Mac's: `meeting-capture language LOCALE` (e.g. `hi-IN` for Hinglish) or `meeting-capture stt gemini`.

`meeting-capture status` and `doctor` show the engine in use and why, the language and where it comes from, whether its model is installed, whether a Gemini key exists (optional unless Gemini is used), and how much audio is waiting. Each transcribed chunk's log line ends with the engine (`… (N chars) via apple`).

A chunk keeps a small note naming its meeting (`chunk-….json`, beside the audio) from the moment it is queued for transcription until it is transcribed, so audio left untranscribed when the daemon is restarted — by `stt`, `language`, `mode`, the settings page or a `brew upgrade` — goes back into its own meeting at the next start.

### Live mode & the in-meeting copilot

By default the daemon runs in **batch** mode: it chunks audio and transcribes after each pause (cheapest, most robust). Live mode streams the call to Gemini, so it needs a Google API key, and choosing it is choosing to upload: it runs with `stt auto` (the default) too, even where batch would transcribe on this Mac. Only `stt apple` (on this Mac only, never uploads) refuses it — `mode live` says so, and a live setting then runs batch, which `status`, `doctor`, `stt` and the settings page all show. Run `meeting-capture mode live` once and the launchd daemon streams to `gemini-3.5-transcribe-live` instead — ~1-second interim hypotheses and finalized utterances — which is what the in-meeting copilot needs. Finals still land in the meeting's transcript row exactly as in batch mode; live *additionally* writes a per-session feed under `~/.meeting-capture/live/`.

Switch once, then one pane during a meeting:

```bash
meeting-capture mode live        # saves the setting and restarts the recorder
meeting-capture copilot          # during the call: whispers from your memory
meeting-capture mode batch       # back to chunked transcription
```

Keep using the launchd daemon for live mode rather than `MEETING_CAPTURE_MODE=live meeting-capture run` in a terminal: a terminal-spawned sysaudio is a different binary path to macOS, so it asks for Screen Recording again, and declining that prompt also revokes the grant the daemon depends on.

`meeting-capture copilot` watches the live feed and, when the other side asks something you'd want help answering, retrieves from your **past meetings** and whispers the fact, decision, or number — with the meeting it came from — or stays silent when it has nothing useful. Inside Claude Code there's a richer surface: `meeting-capture skill install` links the `/meeting` skill into `~/.claude/skills/meeting` (a folder of your own there is left alone; `skill uninstall` removes the link) and type `/meeting` mid-call — Claude reads the same feed but searches your *entire* contorch memory (notes, tasks, repo knowledge, every meeting), not just transcripts. `/loop 15s /meeting` watches continuously. `meeting-capture live [--interim]` tails the raw transcript feed. Costs are higher in live mode (~$0.009/min per channel streaming, plus a cheap LLM call per copilot whisper), so it's opt-in.

### Settings page

`meeting-capture ui` — or **Recording settings…** in the Contorch menu bar ([pipeline-monitor](https://github.com/contorch/pipeline-monitor)) — opens a settings page in the browser (served from this Mac on 127.0.0.1 only; nothing to install). Choose where audio comes from — this Mac's call audio, or a USB interface such as a Behringer UMC202HD/UMC404HD — pick the device and which input is the host ("Me") and which the guests ("Them"), and watch live level meters for every input while you set the interface's gain knobs (aim for peaks between −18 and −6 dBFS; a CLIP flag means turn down or press PAD). Save restarts the recorder with the new settings — the same thing `meeting-capture source linein --device … --me … --them …` does. The **Transcription** section shows the engine in use and why, switches between On this Mac / Gemini / Automatic, and picks the on-device language from the ones this Mac supports (a language whose model isn't installed is downloaded from Apple first; the page shows the download while it runs) — the same as `meeting-capture stt` / `language`. The page also pauses/resumes recording and shows the latest transcript lines as they arrive. Metering from the page never counts as "a call started".

### Settings

The recorder's settings live in `~/.meeting-capture/env`: one `MEETING_CAPTURE_*=value` per line, `#` comments, the format of `~/.context-orchestrator/env`. `mode`, `source`, `stt`, `language`, the settings page and `meeting-capture config set` write it (locked and atomic, so they can't lose each other's changes) and restart the recorder; after a hand edit, run `meeting-capture restart`. Precedence is the same in every meeting-capture process: **the process's own environment > the file > the default** — so `MEETING_CAPTURE_MODE=live meeting-capture run` still works, while a value exported in your shell never reaches the launchd recorder (`meeting-capture config` shows where each value comes from, and which ones your shell sets differently).

Paths to binaries are not settings: `MEETING_CAPTURE_SYSAUDIO` (exported by the Homebrew wrapper on every call), `_AUDIOTEE`, `_TRANSCRIBE_BIN` and `_VENV` are never stored in the file. `install` pins the recorder's sysaudio in its plist, and every command uses that copy — one binary, one Screen Recording grant.

Before 0.8 the settings lived in the launchd plist's `EnvironmentVariables`. The recorder (at start), `install` and every writer move them into the file once, keeping a copy of the old plist as `~/.meeting-capture/com.contorch.meeting-capture.plist.before-env-file`. Going back to an older meeting-capture: copy that file back to `~/Library/LaunchAgents/` (an old version reads only the plist and would otherwise run with defaults).

Restarts never start a recorder you stopped (`meeting-capture stop`, or "Stop everything" in the menu bar): a setting saved while it is stopped applies when it starts. A legacy launchd restart is `bootout` + `bootstrap` (never `launchctl kickstart -k`, which restarts from launchd's cached copy of the plist), and the agent's `ExitTimeOut` (15 s) lets the recorder finish the chunk in hand.

### Vocabulary

Proper nouns are where transcription goes wrong. Put yours — names, products, jargon — in `~/.meeting-capture/vocab.txt` (one per line, up to 1,000; `meeting-capture vocab edit`) and the Gemini transcribe model spells them deterministically. The vocabulary applies to Gemini only; on-device transcription ignores it. Without it, "contorch" came back as "Concourse" in our tests; with it, never.

`MEETING_CAPTURE_DIARIZE=1` turns on speaker diarization for the `Them` channel (`[SPEAKER_n]` per turn). The API makes diarization and vocabulary mutually exclusive, so it's off by default — memory fidelity beats speaker labels within a channel; the Me/Them split is exact regardless.

### API key

Only needed for Gemini (chosen, or the fallback of `auto` when on-device isn't available) and live mode. Resolved in this order:

1. `$GOOGLE_API_KEY`
2. `$GEMINI_API_KEY`
3. `~/.config/google/key` (mode 600)

`meeting-capture doctor` reports whether a key is found — one the recorder will see. It runs under launchd, so `$GOOGLE_API_KEY` exported in your shell never reaches it; the key file is the place for it.

### Memory guardrail

The daemon self-exits (and launchd respawns it) if its `phys_footprint` exceeds `MEETING_CAPTURE_MAX_FOOTPRINT_MB` (default 2048) — a backstop against any runaway-memory regression. The check uses `phys_footprint`, not RSS, because leaked memory is often compressed/swapped and invisible to RSS.

### Stopping, and a removed app

The recorder stops cleanly on SIGTERM (Quit, an update, `meeting-capture stop`, launchd): capture stops, the chunk being recorded is flushed and handed to transcription, and whatever can't be transcribed within a few seconds is kept with its meeting and transcribed at the next start. A second SIGTERM exits at once. Inside Contorch.app, the recorder also watches its own program: if the app is moved to the Trash (gone for three checks in a row, ~6 s), it keeps the chunk in hand, exits and unloads its launchd job, idle or mid-call. A Homebrew recorder doesn't do this (its venv is rebuilt on every `brew upgrade`).

**`meeting-capture status --json`** (schema `meeting-capture.status/1`) is the one answer to "is a meeting being recorded right now?" — the menu bar's ● REC, the update gate (never install while recording, nor while it can't tell), adopt and heal read it; nobody parses the log to decide. The daemon writes `~/.meeting-capture/state.json` (`meeting-capture.state/1`: `pid`, `state` idle|recording|paused, `since`, `meeting_id`, `source`, `updated_at`) at every transition and every 30 s while recording. One document on stdout, exit 0 always: `{schema, ok, recording: true|false|null, state, since, meeting_id, pid, stale, reason?}`. `recording` is `null` (can't tell) with `reason` `no_state` (no daemon has run since 0.8), `daemon_not_running`, or `stale_heartbeat` (says recording, but its heartbeat is over 90 s old). Line-in listens all the time; it counts as recording while an input carried sound in the last minute.

Where the audio comes from is in the same document (and in `state.json`): `source` is the **configured** source (`sck` | `linein`); `input` = `{device, me_channel, them_channel}` (0-based; line-in only, else `null`); `effective_source` = `linein` | `sck` (this Mac's call audio — configured, or the line-in fallback) | `null` (nothing can record); `linein_fallback` (the setting); `problem` = `null` or `{code: linein_device_missing | linein_unavailable, device, message, since, fallback: active | armed | off}` — the configured interface can't be used since `since`, and this Mac's call audio is being recorded instead (`active`), will be when a call starts (`armed`), or isn't (`off`). A daemon older than these fields writes none of them. `meeting-capture status` and `doctor` show the same as a `source:` line.

**Line-in fallback.** When the line-in interface is missing (unplugged, renamed) the daemon logs `line-in capture unavailable: … — retrying in 30s` and looks again every 30 s. Meanwhile, if this Mac is in a call (the same mic-in-use gate as source `sck`), it records this Mac's call audio instead of nothing — same chunks, same meeting rows — and logs `line-in: '<device>' not connected — this Mac is in a call: recording this Mac's call audio instead` before the usual `mic active — starting recording session`. It stays on this Mac's audio until that call ends (a call is never split), then goes back to the interface if it's there (`line-in: '<device>' is back`). Turn it off with `meeting-capture config set linein_fallback 0` (`MEETING_CAPTURE_LINEIN_FALLBACK`, default on); a call during an outage is then logged once as `NOT recording`.

## Contract

**`meeting-capture config --json`** (schema `meeting-capture.config/1`): `{schema, ok, file, watch_paths[], restart_pending, agent{backend, installed, sysaudio, plist}, settings{name: {value, source: agent|file|default, shell?}}, overridden[]}`. `name` is the key without `MEETING_CAPTURE_`, lower-case (`mode`, `source`, `stt`, …). A caller caches it until one of `watch_paths` changes (a `stat`, never a read). `overridden` lists file settings that the agent's own environment still overrides (an old plist that hasn't been migrated).

**`meeting-capture start|stop|restart|install|uninstall --json`** (schema `meeting-capture.agent/1`) is the one way other programs drive the recorder agent (`stop` also takes `--reason user|quit|update`, echoed back): `{schema, ok, action, backend: launchctl|none, label, program, loaded, performed, why?, reason?, error?{code, message}}`. Exit 0 done or nothing to do (`performed: false` + `why`), 1 failed, **3 refused**: `channel_conflict` (another install owns Contorch on this Mac — the marker `~/.contorch/channel.json`, or an SMAppService job that holds the label) or `agent_elsewhere` (`install` over an agent that runs another install's existing Python). `restart` never starts a stopped recorder.

**Contorch.app.** Inside the app the recorder is a bundled SMAppService agent (`Contents/Library/LaunchAgents/com.contorch.meeting-capture.plist`, BundleProgram `Contents/MacOS/Contorch Recorder`), registered in-process by `meeting_capture.registrar` (PyObjC; the `app` extra) — the same verbs, `backend: app`: `start`/`install` register, `stop`/`uninstall` unregister, `restart` is `kickstart -k` (the sealed plist has no settings). It is used when `$CONTORCH_CHANNEL` is `app` and the process runs from a bundle that has that plist. The plist is generated by the app's build, never hand-written: `meeting-capture plist --bundled --bundle-id ID [--program "Contents/MacOS/Contorch Recorder"] [--label L]` (no settings, no `~`, no log path: the daemon opens its own log; `ExitTimeOut 15`; `AssociatedBundleIdentifiers [ID]`; `CONTORCH_CHANNEL=app`). `install --adopt [--no-load] [--backup-dir D]` is pipeline-monitor's adopt (run with its operation token; it never overrides the guard): settings move into the env file, the other install's legacy plist is backed up into `D` and moved aside, `agent.json` is written, and with `--no-load` nothing starts until `meeting-capture start`. `uninstall --adopt` is the app's side of a rollback. `heal --json` re-registers the app's agent when its registration is stale (`reasons`: `moved`, `version`, `spawn_failed`, `codesigning`, `exit_78`). sysaudio is resolved in this order everywhere: `agent.json`'s pin, a legacy plist's pin, `$MEETING_CAPTURE_SYSAUDIO`, the running app's `Contents/Helpers/sysaudio`, `bin/` beside the package, `PATH`.

**`meeting-capture where --json`** (schema `meeting-capture.where/1`): where this install's files are — `{channel, version, bin{meeting_capture, python}, bundle, sysaudio, agent{backend, label, plist, app, record}, plist, settings, log, state, skills{meeting{source, installed}}}` (absolute paths). **`meeting-capture skill install|uninstall|status --json`** (schema `meeting-capture.skill/1`): `{ok, path, action: linked|kept_user_copy|removed|none}`.

**Channel.** Every process learns its install channel from `$CONTORCH_CHANNEL` only (`app`, `brew`; unset or anything else = `dev`). The legacy plist carries `brew` or `dev`. Before it changes the recorder agent, meeting-capture asks the marker that pipeline-monitor alone writes (`~/.contorch/channel.json`, `contorch.channel/1`): no marker, or this channel in its `writers` (and the operation token `$CONTORCH_OP` when it names one) — otherwise it prints the marker's `blocked_message` and exits 3. The fixtures are pipeline-monitor's `contract/channel_guard/`, vendored in `tests/fixtures/channel_guard` with their sha256.

**`meeting-capture stt --json`** is how other programs learn how meetings get transcribed. pipeline-monitor (the Contorch menu bar, `contorch setup`, `status`, `doctor`) runs it instead of re-implementing the rules in `transcriber.py` — this repo is the only implementation; change the fields here and in pipeline-monitor's `transcription.py` together (its README has the same section). It prints one JSON object on stdout (diagnostics go to stderr) describing what the recorder's configuration — `~/.meeting-capture/env` under what its agent injects, or under this shell's environment when no agent is installed; a key only in the caller's shell doesn't count for an installed agent — resolves to, and exits 0 whenever it could answer (1: it couldn't, nothing on stdout; 2: usage — a meeting-capture without `stt --json` also exits 2, which callers read as "too old"). It runs the helper's read-only probe once per language (≈ 0.1–0.2 s in all). `"schema": 1`; adding a field keeps the schema, removing or redefining one bumps it.

| Field | Meaning |
|---|---|
| `schema`, `version`, `agent_installed` | contract version (1), meeting-capture's version, whether the launchd agent exists |
| `choice`, `choice_label` | the setting: `auto` \| `apple` \| `gemini` |
| `engine`, `engine_label`, `ready`, `reason` | what transcribes batch chunks now: `apple` (on this Mac) \| `gemini` \| `none` (audio kept until one can), whether it can run, and why |
| `uploads` | batch chunks go to Google (engine `gemini`; when not ready, once a key exists) |
| `live.requested`, `live.active`, `live.blocker` | live mode asked for; actually streaming every call to Gemini (**also uploads**); why it runs batch instead |
| `gemini_fallback` | `auto` with a key: if on-device transcription stops working (a macOS update, a removed model, one failed helper run), chunks fall back to Gemini — with no user action |
| `may_upload` | **the privacy answer**: meeting audio can reach Google without anyone changing a setting — `uploads` or `live.active` or `gemini_fallback`. `false` only when audio stays on this Mac whatever happens |
| `locale`, `locale_source`, `locale_why`, `locale_guessed`, `mac_language` | on-device language, where it comes from (`setting` \| `mac` \| `default`), whether en-US is only a guess because the Mac's language can't be done on-device |
| `apple` | the helper's probe: `available`, `usable`, `installable`, `installed`, `reason`, `supported`, `installed_locales`, `exit_code`, … |
| `needs_model`, `install_hint` | on-device would run but its language's model isn't installed/reserved yet; the exact command that fixes it (else `null`) |
| `on_device_hint` | the exact command that makes batch transcription run on this Mac under `auto` (installing the model if needed) — with a key, Gemini stays its backup (`gemini_fallback`); `null` when it already does or can't |
| `on_device_only_hint` | the exact command that makes it run on this Mac **and never upload** (`meeting-capture stt apple`, which installs the model if needed); `null` when it already does or can't |
| `gemini_key`, `notice` | a key the recorder will see; the upgrade note (`null` when none) |
| `on_device_line`, `on_device_for_mac_language` | one sentence about what this Mac can transcribe itself, to show as it is (it never says "this Mac can transcribe meetings" when on-device can't do the Mac's own language and `auto` keeps Gemini); whether on-device covers the Mac's language |
| `key_check` | only with `--check-key`: `{key: accepted \| rejected \| missing \| unreachable, message}` — one tiny read-only Gemini call (`models.get`, no audio, no retries) with the key the recorder would use |

Privacy wording belongs to these fields only: audio leaves the Mac now when `uploads` or `live.active` is true, and may leave it when `may_upload` is true; say "never leaves this Mac" only when `may_upload` is false. A caller that asks in the background (a menu bar timer) should run the venv's own `~/.meeting-capture/venv/bin/meeting-capture` — the code the recorder runs — not the Homebrew wrapper: after a `brew upgrade` the wrapper deletes and rebuilds that venv, under the running recorder. `meeting-capture stt auto|apple|gemini [--language L]` and `meeting-capture language L` are safe to run from another program: no prompts, progress as lines on stdout (model download percentages included), errors on stderr, exit 0 when applied (then ask `stt --json` for the result), 1 when refused with the configuration untouched, 2 for usage errors.

**`meeting-capture check --json`** (schema `meeting-capture.permissions/1`) is the recorder's permission state: pipeline-monitor's menu, doctor and setup show its rows as they are instead of writing their own hints. It runs `sysaudio check --json` (schema `sysaudio.check/1`, read-only: `CGPreflightScreenCaptureAccess`, `AVCaptureDevice.authorizationStatus` and TCC's preflight for `kTCCServiceAudioCapture`; it also lists the `backends` this sysaudio runs here) as its own responsible process, the identity it captures as. `--request screen_audio|system_audio|microphone` asks macOS first (`CGRequestScreenCaptureAccess` / TCC's request, or a briefly started tap whose samples are discarded / `requestAccess`; macOS shows its prompt once). One document on stdout, exit 0:

| Field | Meaning |
|---|---|
| `schema`, `ok`, `error{code,message}` | `ok: false` when the state couldn't be read: `no_helper`, `helper_too_old` (a sysaudio without `check`), `helper_failed`, `helper_timeout` |
| `channel` | `$CONTORCH_CHANNEL`: `app` \| `brew` \| `dev` (unset) |
| `identity.helper`, `identity.subject` | the sysaudio the recorder runs, and who macOS asks about: the outermost app bundle's id (Contorch.app), else the binary's real path |
| `permissions[]` | one row each for `screen_audio`, `system_audio` (System Audio Recording Only) and `microphone` (line-in included): `status` (`granted` \| `not_granted` \| `denied` \| `not_determined` \| `restricted` \| `unknown` \| `unsupported`), `required` (with the current source, mic setting and backend: exactly one of `screen_audio`/`system_audio` with source `sck`), `can_request`, `hint` (what to do, worded for the channel; `null` when granted), `settings_url` (the Privacy pane) |
| `backend` | `selected` (`taps` \| `sck`), `setting` (`MEETING_CAPTURE_BACKEND`), `reason`, `available` (sysaudio's `backends`, `null` for one that predates them). `system_audio` and `backend` were added within schema 1 |
| `requested` | the `--request` given, else `null` |

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
