// The capture command line, parsed without side effects (unit-tested).
//
//   sysaudio [--sample-rate N] [--mic] [--backend sck|taps]
//
// --backend picks how the other side of the call ("them", tag 'S') is
// captured. The stdout protocol is identical for both:
//   sck   ScreenCaptureKit (the default, so a meeting_capture that predates
//         --backend gets exactly what it always got). Needs "Screen & System
//         Audio Recording"; --mic is SCK's own microphone capture (macOS 15+).
//   taps  Core Audio process taps (macOS 14.2+): a global tap of every
//         process's output except sysaudio's, read through a private aggregate
//         device. Needs only "System Audio Recording Only"
//         (NSAudioCaptureUsageDescription, kTCCServiceAudioCapture); --mic
//         reads the default input device through the HAL (same Microphone
//         grant as before). No Screen Recording, no monthly re-confirmation.
//
// meeting_capture picks the backend (recorder.choose_backend) and passes it
// explicitly; `sysaudio check --json` advertises which backends this binary
// and this macOS support ("backends").

import Foundation

enum CaptureBackend: String, Equatable, CaseIterable {
    case sck
    case taps
}

/// How the private aggregate device around the tap is composed.
/// tapOnly: the tap is the aggregate's only member (its own clock); a default
///          output switch does not invalidate it.
/// withOutput: Apple's sample layout, the default output device is the main
///          sub-device (clock); rebuilt whenever the default output changes.
/// Default tapOnly; SYSAUDIO_TAP_AGGREGATE=with-output selects the other one
/// (a lab knob, not a contract).
enum TapAggregateMode: String, Equatable {
    case tapOnly = "tap-only"
    case withOutput = "with-output"

    static func fromEnvironment(_ env: [String: String] = ProcessInfo.processInfo.environment) -> TapAggregateMode {
        TapAggregateMode(rawValue: (env["SYSAUDIO_TAP_AGGREGATE"] ?? "").lowercased()) ?? .tapOnly
    }
}

struct CaptureArgs: Equatable {
    var sampleRate = 16000
    var mic = false
    var backend: CaptureBackend = .sck
}

enum CaptureParse: Equatable {
    case run(CaptureArgs)
    case help
    /// Printed verbatim on stderr, exit 1. Unknown flags keep the historical
    /// "unknown arg: X" wording (the smoke test and old callers rely on it).
    case usage(String)
}

enum CaptureCommand {
    static let usage = """
    Usage: sysaudio [--sample-rate N] [--mic] [--backend sck|taps]
      --sample-rate  output sample rate in Hz (default 16000)
      --mic          also capture the default microphone (framed output; sck: macOS 15+)
      --backend      how system audio is captured: sck (ScreenCaptureKit, default;
                     Screen & System Audio Recording) or taps (Core Audio process
                     taps, macOS 14.2+; System Audio Recording Only)
    Subcommands: sysaudio check --help, sysaudio transcribe --help
    """

    static func parse(_ argv: [String]) -> CaptureParse {
        var out = CaptureArgs()
        var args = argv[...]
        while let a = args.popFirst() {
            switch a {
            case "--sample-rate":
                // As before: a non-numeric value is not consumed, so it then
                // fails as an unknown arg.
                if let n = args.first.flatMap(Int.init) {
                    guard n >= 8000 && n <= 192_000 else {
                        return .usage("sysaudio: --sample-rate \(n) is out of range (8000-192000)")
                    }
                    out.sampleRate = n
                    args.removeFirst()
                }
            case "--mic":
                out.mic = true
            case "--backend":
                guard let v = args.popFirst() else { return .usage("sysaudio: --backend needs sck or taps") }
                guard let b = CaptureBackend(rawValue: v) else {
                    return .usage("sysaudio: --backend takes sck or taps, not \(v)")
                }
                out.backend = b
            case _ where a.hasPrefix("--backend="):
                let v = String(a.dropFirst("--backend=".count))
                guard let b = CaptureBackend(rawValue: v) else {
                    return .usage("sysaudio: --backend takes sck or taps, not \(v)")
                }
                out.backend = b
            case "-h", "--help":
                return .help
            default:
                return .usage("unknown arg: \(a)")
            }
        }
        return .run(out)
    }

    /// Backends this binary can run on this macOS (`check --json` "backends").
    static func availableBackends() -> [String] {
        var out = [CaptureBackend.sck.rawValue]
        if #available(macOS 14.2, *) { out.append(CaptureBackend.taps.rawValue) }
        return out
    }
}
