// `sysaudio check`: the two permissions sysaudio's capture needs, as macOS
// sees them for THIS process. It captures nothing and opens no device.
//
// meeting_capture (Python, `meeting-capture check --json`) depends on this
// contract. Change both sides together.
//
//   sysaudio check [--json] [--request screen|mic]
//     stdout (--json): one line
//       {"schema":"sysaudio.check/1","screen_capture":S,"microphone":M,
//        "os":"26.0","arch":"arm64","requested":null|"screen"|"mic"}
//     S: "granted" | "not_granted"   (CGPreflightScreenCaptureAccess; macOS
//        does not say whether "not granted" was denied or never asked)
//     M: "granted" | "denied" | "not_determined" | "restricted"
//        (AVCaptureDevice.authorizationStatus(for: .audio))
//     exit 0 whatever the answers are | 1 usage error
//
//   Without --request nothing is shown to the user: both calls only read the
//   state. --request screen calls CGRequestScreenCaptureAccess (macOS shows its
//   prompt the first time, then only reports); --request mic calls
//   AVCaptureDevice.requestAccess when the state is not determined. The
//   statuses printed are read again after the request.
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
    Usage: sysaudio check [--json] [--request screen|mic]
      Reports whether this process may capture the screen's audio (Screen & System
      Audio Recording) and the microphone. Captures nothing.
      --json             one JSON line on stdout (schema sysaudio.check/1)
      --request screen   ask macOS for Screen & System Audio Recording first
      --request mic      ask macOS for the microphone first
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
                guard let v = args.popFirst() else { return .usage("--request needs screen or mic") }
                guard let r = CheckRequest(rawValue: v) else { return .usage("--request takes screen or mic, not \(v)") }
                if opts.request != nil { return .usage("--request can be given once") }
                opts.request = r
            case _ where a.hasPrefix("--request="):
                let v = String(a.dropFirst("--request=".count))
                guard let r = CheckRequest(rawValue: v) else { return .usage("--request takes screen or mic, not \(v)") }
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

    static func document(screen: String, mic: String, requested: CheckRequest?) -> [String: Any] {
        [
            "schema": CHECK_SCHEMA,
            "screen_capture": screen,
            "microphone": mic,
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
        case nil:
            break
        }

        let screen = screenWord(CGPreflightScreenCaptureAccess())
        let mic = micWord(AVCaptureDevice.authorizationStatus(for: .audio))
        if opts.json {
            transcribeEmit(document(screen: screen, mic: mic, requested: opts.request))
        } else {
            print("screen_capture: \(screen)")
            print("microphone:     \(mic)")
        }
        return 0
    }
}
