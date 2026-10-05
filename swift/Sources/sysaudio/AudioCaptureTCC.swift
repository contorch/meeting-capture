// "System Audio Recording Only" (TCC service kTCCServiceAudioCapture): the one
// permission the taps backend needs for the other side of the call.
//
// macOS has no public preflight for it. Its documented prompt appears "the
// first time you start recording from an aggregate device that contains a
// tap" and a refusal is answered with silence, not an error. So:
//   - reading the state uses TCC's TCCAccessPreflight (private, looked up at
//     run time; absent → "unknown", never a crash). It never prompts.
//   - asking uses TCCAccessRequest when present (one prompt, with an answer),
//     else the documented route: start a tap briefly (TapCapture.requestByTap),
//     discarding every sample, until the state is decided.
// Both are charged to the RESPONSIBLE process, like every check sysaudio makes:
// meeting_capture starts it through tccspawn, so the answer is sysaudio's own
// (Contorch.app's bundle id inside the app; the binary path for Homebrew).
//
// Not usable in a Mac App Store build (private symbols), as tccspawn already.

import Foundation

enum AudioCaptureTCC {
    static let service = "kTCCServiceAudioCapture" as CFString

    private typealias PreflightFn = @convention(c) (CFString, CFDictionary?) -> Int32
    private typealias RequestFn = @convention(c) (CFString, CFDictionary?, @escaping @convention(block) (Bool) -> Void) -> Void

    private static let handle: UnsafeMutableRawPointer? =
        dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW)

    private static let preflightFn: PreflightFn? = {
        guard let h = handle, let sym = dlsym(h, "TCCAccessPreflight") else { return nil }
        return unsafeBitCast(sym, to: PreflightFn.self)
    }()

    private static let requestFn: RequestFn? = {
        guard let h = handle, let sym = dlsym(h, "TCCAccessRequest") else { return nil }
        return unsafeBitCast(sym, to: RequestFn.self)
    }()

    /// TCCAccessPreflight's answer → the check contract's word. Unit-tested.
    /// 0 granted, 1 denied, 2 not yet asked; nil (no SPI) or anything else unknown.
    static func word(_ preflight: Int32?) -> String {
        switch preflight {
        case 0: return "granted"
        case 1: return "denied"
        case 2: return "not_determined"
        default: return "unknown"
        }
    }

    /// The current state, without asking. Taps unavailable (macOS < 14.2) →
    /// "unsupported".
    static func status() -> String {
        guard #available(macOS 14.2, *) else { return "unsupported" }
        return word(preflightFn?(service, nil))
    }

    static var canRequestDirectly: Bool { requestFn != nil }

    /// Ask with TCCAccessRequest. nil when the SPI is missing; otherwise the
    /// answer (false also for a timeout).
    static func requestDirectly(timeout: TimeInterval = 300) async -> Bool? {
        guard let fn = requestFn else { return nil }
        return await withCheckedContinuation { (cont: CheckedContinuation<Bool?, Never>) in
            let lock = NSLock()
            var resumed = false
            func finish(_ v: Bool?) {
                lock.lock()
                defer { lock.unlock() }
                if resumed { return }
                resumed = true
                cont.resume(returning: v)
            }
            fn(service, nil) { granted in finish(granted) }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { finish(false) }
        }
    }
}
