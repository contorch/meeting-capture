// When a taps-backend capture (the tap's aggregate device, or the microphone)
// must be torn down and built again. Pure: the caller feeds it events and a
// clock (seconds that stop while the Mac sleeps, e.g. systemUptime) and acts
// on `decide(now:)` from one serial queue. Unit-tested with a fake clock.
//
// Inputs:
//   change()       a device event: default output/input switched, device list
//                  changed, the tap's format or the device's rate changed, a
//                  device died. Bursts are coalesced (debounce) so AirPods
//                  connecting or a TV output flapping on HDMI give one rebuild,
//                  not twenty; a burst that never settles is still acted on
//                  after `maxDefer`.
//   buffer()       an IO callback delivered audio. No buffers for `stallAfter`
//                  while running = the IO died silently (sleep/wake, a device
//                  vanished without notice): rebuild.
//   built(ok:)     the result of a (re)build. Failures retry with backoff; if
//                  nothing has worked for `giveUpAfter` the process exits so
//                  meeting_capture respawns it (it would bail on the missing
//                  frames anyway; exiting is the explicit, non-hanging way).
//
// `requiresRebuild` false means a change only asks for a check (the caller
// compares the format it built with to the current one and rebuilds only if
// they differ): a tap-only aggregate survives a default-output switch.

import Foundation

enum RebuildAction: Equatable {
    case none
    /// Re-read the format; rebuild only if it differs.
    case verify
    case rebuild(String)
    /// Builds keep failing; exit so the parent respawns us.
    case giveUp(String)
}

struct RebuildPolicy {
    var debounce = 0.5
    var maxDefer = 3.0
    var stallAfter = 3.0
    var minInterval = 1.0
    var retryBackoff = [0.5, 1.0, 2.0, 4.0]
    var giveUpAfter = 20.0
    /// Stall rebuilds in a row with no audio in between before giving up.
    var maxStallRebuilds = 5
    /// A change rebuilds outright (true) or only asks for a format check.
    var requiresRebuild = true

    private(set) var running = false
    private(set) var lastBuffer = 0.0
    private(set) var lastBuild = -Double.infinity
    private(set) var changeFirst: Double? = nil
    private(set) var changeLast: Double? = nil
    private(set) var changeHard = false
    private(set) var failingSince: Double? = nil
    private(set) var failures = 0
    private(set) var rebuilds = 0
    private(set) var stallRebuilds = 0

    init(requiresRebuild: Bool = true) { self.requiresRebuild = requiresRebuild }

    /// `hard`: the object we read from is gone or no longer valid (device
    /// died, aggregate not alive); rebuild even when changes normally only
    /// ask for a format check.
    mutating func change(now: Double, hard: Bool = false) {
        if changeFirst == nil { changeFirst = now }
        changeLast = now
        if hard { changeHard = true }
    }

    mutating func buffer(now: Double) {
        lastBuffer = now
        stallRebuilds = 0
    }

    mutating func built(ok: Bool, now: Double) {
        lastBuild = now
        if ok {
            running = true
            failures = 0
            failingSince = nil
            lastBuffer = now   // the stall clock starts at the build
        } else {
            running = false
            failures += 1
            if failingSince == nil { failingSince = now }
        }
    }

    /// The capture was torn down on purpose (e.g. before a rebuild).
    mutating func stopped() { running = false }

    mutating func decide(now: Double) -> RebuildAction {
        if let since = failingSince, now - since >= giveUpAfter {
            return .giveUp(String(format: "no working capture for %.0fs after %d attempts", now - since, failures))
        }
        // A failed build: retry on the backoff schedule.
        if !running, failures > 0 {
            let wait = retryBackoff[min(failures - 1, retryBackoff.count - 1)]
            if now - lastBuild >= wait {
                consumeChange()
                rebuilds += 1
                return .rebuild("retry \(failures)")
            }
            return .none
        }
        if let first = changeFirst, let last = changeLast {
            let settled = now - last >= debounce
            let overdue = now - first >= maxDefer
            if (settled || overdue) && now - lastBuild >= minInterval {
                let hard = changeHard
                consumeChange()
                if requiresRebuild || hard {
                    rebuilds += 1
                    return .rebuild(settled ? "device change" : "device change (still changing)")
                }
                return .verify
            }
            return .none
        }
        if running, now - lastBuffer >= stallAfter, now - lastBuild >= minInterval {
            if stallRebuilds >= maxStallRebuilds {
                return .giveUp("no audio buffers after \(stallRebuilds) rebuilds")
            }
            stallRebuilds += 1
            rebuilds += 1
            return .rebuild(String(format: "no audio buffers for %.1fs", now - lastBuffer))
        }
        return .none
    }

    private mutating func consumeChange() {
        changeFirst = nil
        changeLast = nil
        changeHard = false
    }
}
