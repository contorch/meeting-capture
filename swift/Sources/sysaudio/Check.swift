// `sysaudio check`: the permissions sysaudio's capture needs, as macOS sees
// them for THIS process, and the capture backends this binary can run here.
// It captures nothing and opens no device (except --request system_audio's
// fallback, below).
//
// meeting_capture (Python, `meeting-capture check --json`) depends on this
// contract. Change both sides together.
//
//   sysaudio check [--json] [--request screen|mic|system_audio]
//     stdout (--json): one line
//       {"schema":"sysaudio.check/1","screen_capture":S,"microphone":M,
//        "system_audio":A,"backends":["sck","taps"],
//        "os":"26.0","arch":"arm64","requested":null|"screen"|"mic"|"system_audio"}
//     S: "granted" | "not_granted"   (CGPreflightScreenCaptureAccess; macOS
//        does not say whether "not granted" was denied or never asked)
//     M: "granted" | "denied" | "not_determined" | "restricted"
//        (AVCaptureDevice.authorizationStatus(for: .audio))
//     A: System Audio Recording Only, what `--backend taps` needs:
//        "granted" | "denied" | "not_determined" | "unknown" (TCC's preflight
//        is private and was not found) | "unsupported" (macOS < 14.2)
//     backends: what `sysaudio --backend` accepts on this macOS ("taps" from
//        14.2). Added in schema 1 without a bump (new keys only); a reader
//        that sees no "backends" has a sysaudio that only knows sck.
//     exit 0 whatever the answers are | 1 usage error
//
//   Without --request nothing is shown to the user: every call only reads the
//   state. --request screen calls CGRequestScreenCaptureAccess (macOS shows its
//   prompt the first time, then only reports); --request mic calls
//   AVCaptureDevice.requestAccess when the state is not determined;
//   --request system_audio (not determined only) asks through TCC
//   (AudioCaptureTCC), or, where that is unavailable, starts a tap so macOS
//   shows its documented prompt, discarding every sample. The statuses
//   printed are read again after the request.
//
// Who is asked: macOS charges the check to the RESPONSIBLE process. Python
// starts sysaudio through tccspawn (responsibility disclaimed), so the answer
// is sysaudio's own: the outer app's bundle id inside Contorch.app, the
// binary's path for a bare (Homebrew) sysaudio.

import Foundation
import AVFoundation
import CoreGraphics

let CHECK_SCHEMA = "sysaudio.check/1"

enum CheckRequest: String, Equatable {
    case screen
    case mic
    case systemAudio = "system_audio"
}

struct CheckOptions: Equatable {
    var json = false
    var request: CheckRequest? = nil
}

enum CheckParse: Equatable {
    case run(CheckOptions)
    case help
    case usage(String)
}

enum CheckCommand {
    static let usage = """
    Usage: sysaudio check [--json] [--request screen|mic|system_audio]
      Reports whether this process may capture the screen's audio (Screen & System
      Audio Recording), system audio alone (System Audio Recording Only, for
      --backend taps) and the microphone, and which capture backends run here.
      Captures nothing.
      --json                  one JSON line on stdout (schema sysaudio.check/1)
      --request screen        ask macOS for Screen & System Audio Recording first
      --request mic           ask macOS for the microphone first
      --request system_audio  ask macOS for System Audio Recording Only first
    Exit codes: 0 reported, 1 usage error.
    """

    /// Pure: argv (after "check") to what to do. Unit-tested.
    static func parse(_ argv: [String]) -> CheckParse {
        var opts = CheckOptions()
        var args = argv[...]
        while let a = args.popFirst() {
            switch a {
            case "--json":
                opts.json = true
            case "--request":
                guard let v = args.popFirst() else { return .usage("--request needs screen, mic or system_audio") }
                guard let r = CheckRequest(rawValue: v) else { return .usage("--request takes screen, mic or system_audio, not \(v)") }
                if opts.request != nil { return .usage("--request can be given once") }
                opts.request = r
            case _ where a.hasPrefix("--request="):
                let v = String(a.dropFirst("--request=".count))
                guard let r = CheckRequest(rawValue: v) else { return .usage("--request takes screen, mic or system_audio, not \(v)") }
                if opts.request != nil { return .usage("--request can be given once") }
                opts.request = r
            case "-h", "--help":
                return .help
            default:
                return .usage("unrecognised option \(a)")
            }
        }
        return .run(opts)
    }

    /// Pure: AVAuthorizationStatus to the contract's word. Unit-tested.
    static func micWord(_ s: AVAuthorizationStatus) -> String {
        switch s {
        case .authorized: return "granted"
        case .denied: return "denied"
        case .restricted: return "restricted"
        case .notDetermined: return "not_determined"
        @unknown default: return "not_determined"
        }
    }

    static func screenWord(_ granted: Bool) -> String { granted ? "granted" : "not_granted" }

    static func document(screen: String, mic: String, systemAudio: String, backends: [String],
                         requested: CheckRequest?) -> [String: Any] {
        [
            "schema": CHECK_SCHEMA,
            "screen_capture": screen,
            "microphone": mic,
            "system_audio": systemAudio,
            "backends": backends,
            "os": TranscribeCommand.osVersion(),
            "arch": TranscribeCommand.processArch(),
            "requested": requested?.rawValue ?? NSNull(),
        ]
    }

    static func run(_ argv: [String]) async -> Int32 {
        let opts: CheckOptions
        switch parse(argv) {
        case .help:
            print(usage)
            return 0
        case .usage(let msg):
            // Never "unknown arg": Python reads that phrase as "this sysaudio
            // predates the subcommand".
            logErr("sysaudio check: \(msg) (see: sysaudio check --help)")
            return 1
        case .run(let o):
            opts = o
        }

        switch opts.request {
        case .screen:
            _ = CGRequestScreenCaptureAccess()
        case .mic:
            if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
                _ = await AVCaptureDevice.requestAccess(for: .audio)
            }
        case .systemAudio:
            await requestSystemAudio()
        case nil:
            break
        }

        let screen = screenWord(CGPreflightScreenCaptureAccess())
        let mic = micWord(AVCaptureDevice.authorizationStatus(for: .audio))
        let audio = AudioCaptureTCC.status()
        let backends = CaptureCommand.availableBackends()
        if opts.json {
            transcribeEmit(document(screen: screen, mic: mic, systemAudio: audio, backends: backends,
                                    requested: opts.request))
        } else {
            print("screen_capture: \(screen)")
            print("microphone:     \(mic)")
            print("system_audio:   \(audio)")
            print("backends:       \(backends.joined(separator: " "))")
        }
        return 0
    }

    /// --request system_audio: only when not yet decided (a decided state
    /// can't be asked again; System Settings is the way back).
    static func requestSystemAudio() async {
        guard #available(macOS 14.2, *) else {
            logErr("sysaudio check: System Audio Recording Only needs macOS 14.2 or later")
            return
        }
        let before = AudioCaptureTCC.status()
        guard before == "not_determined" || before == "unknown" else { return }
        let via = ProcessInfo.processInfo.environment["SYSAUDIO_REQUEST_VIA"] ?? ""
        if via != "tap", AudioCaptureTCC.canRequestDirectly {
            logErr("sysaudio check: asking for System Audio Recording (TCC request)")
            _ = await AudioCaptureTCC.requestDirectly()
            return
        }
        logErr("sysaudio check: asking for System Audio Recording (starting a tap; no audio is kept)")
        await requestAudioCaptureByTap(timeout: 300)
    }
}
