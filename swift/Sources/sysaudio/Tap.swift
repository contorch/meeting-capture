// `sysaudio --backend taps`: the other side of the call through a Core Audio
// process tap (macOS 14.2+), the microphone through the HAL. Same stdout
// protocol as the sck backend (CaptureArgs.swift, Sysaudio.swift).
//
//   them ('S')  A private, global, mono tap of every process's output except
//               sysaudio's own, read through a private aggregate device whose
//               only member is the tap (TapAggregateMode). The tap's format
//               follows the output hardware (48 kHz speakers, 24/16 kHz AirPods
//               in a call): every buffer is mixed to mono and resampled to
//               --sample-rate.
//   me ('M')    An IOProc on the default input device (--mic). The same
//               Microphone grant as the sck backend's SCK mic; the request
//               flow before starting is the same code.
//
// Device changes (AirPods in/out, a TV output appearing and vanishing, a new
// default input), rate changes, a device dying and sleep/wake are handled by
// one control queue: HAL property listeners and a 250 ms tick feed a
// RebuildPolicy per channel, which coalesces bursts, rebuilds, detects IO
// that silently stopped, and gives up (exit 2, meeting_capture respawns)
// rather than hang. Nothing here blocks the IO queue.
//
// Permission: "System Audio Recording Only" (kTCCServiceAudioCapture,
// AudioCaptureTCC). A refusal makes the tap deliver silence, so a known
// refusal is reported up front with the "declined TCCs" wording the daemon
// log readers already know, and the first seconds' peak level is logged.

import AVFoundation
import CoreAudio
import Foundation

// MARK: - HAL property helpers

enum HAL {
    static let system = AudioObjectID(kAudioObjectSystemObject)

    static func address(_ sel: AudioObjectPropertySelector,
                        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    static func get<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
                       scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, initial: T) -> T? {
        var addr = address(sel, scope: scope)
        var value = initial
        var size = UInt32(MemoryLayout<T>.size)
        let st = withUnsafeMutablePointer(to: &value) { AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, $0) }
        return st == noErr ? value : nil
    }

    static func string(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
        var addr = address(sel)
        var ref: Unmanaged<CFString>? = nil
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let st = withUnsafeMutablePointer(to: &ref) { AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, $0) }
        guard st == noErr, let r = ref else { return nil }
        return r.takeRetainedValue() as String
    }

    static func defaultDevice(input: Bool) -> AudioObjectID? {
        let sel = input ? kAudioHardwarePropertyDefaultInputDevice : kAudioHardwarePropertyDefaultOutputDevice
        guard let id = get(system, sel, initial: AudioObjectID(kAudioObjectUnknown)),
              id != kAudioObjectUnknown else { return nil }
        return id
    }

    static func uid(_ dev: AudioObjectID) -> String? { string(dev, kAudioDevicePropertyDeviceUID) }
    static func name(_ dev: AudioObjectID) -> String? { string(dev, kAudioObjectPropertyName) }

    static func isAlive(_ dev: AudioObjectID) -> Bool {
        (get(dev, kAudioDevicePropertyDeviceIsAlive, initial: UInt32(0)) ?? 0) != 0
    }

    /// The HAL process object for a pid (nil when that process has none yet).
    static func processObject(pid: pid_t) -> AudioObjectID? {
        var addr = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
        var qualifier = pid
        var obj = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let st = AudioObjectGetPropertyData(system, &addr, UInt32(MemoryLayout<pid_t>.size), &qualifier, &size, &obj)
        return st == noErr && obj != kAudioObjectUnknown ? obj : nil
    }

    static func inputStreams(_ dev: AudioObjectID) -> [AudioStreamID] {
        var addr = address(kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeInput)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioStreamID](repeating: 0, count: Int(size) / MemoryLayout<AudioStreamID>.size)
        guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &ids) == noErr else { return [] }
        return ids
    }

    /// The format an IOProc sees for the device's first input stream.
    static func firstInputFormat(_ dev: AudioObjectID) -> AudioStreamBasicDescription? {
        guard let s = inputStreams(dev).first else { return nil }
        return get(s, kAudioStreamPropertyVirtualFormat, initial: AudioStreamBasicDescription())
    }

    static func describe(_ f: AudioStreamBasicDescription) -> String {
        let kind = SampleFormat(f)
        let inter = f.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0 ? "non-interleaved" : "interleaved"
        return "\(Int(f.mSampleRate)) Hz, \(f.mChannelsPerFrame) ch, \(kind), \(inter)"
    }

    static func sameFormat(_ a: AudioStreamBasicDescription, _ b: AudioStreamBasicDescription) -> Bool {
        a.mSampleRate == b.mSampleRate && a.mChannelsPerFrame == b.mChannelsPerFrame
            && a.mFormatID == b.mFormatID && a.mFormatFlags == b.mFormatFlags
            && a.mBitsPerChannel == b.mBitsPerChannel
    }
}

struct HALError: Error, CustomStringConvertible {
    let what: String
    let status: OSStatus
    var description: String {
        let be = UInt32(bitPattern: status).bigEndian
        let four = withUnsafeBytes(of: be) { Array($0) }
        let printable = four.allSatisfy { $0 >= 32 && $0 < 127 }
        let code = printable ? "'\(String(bytes: four, encoding: .ascii) ?? "")'" : "\(status)"
        return "\(what) failed (OSStatus \(code))"
    }
}

@inline(__always)
func check(_ status: OSStatus, _ what: String) throws {
    if status != noErr { throw HALError(what: what, status: status) }
}

/// Listener blocks registered on one object; removed together.
final class ListenerSet {
    private var entries: [(AudioObjectID, AudioObjectPropertyAddress, AudioObjectPropertyListenerBlock)] = []
    private let queue: DispatchQueue

    init(queue: DispatchQueue) { self.queue = queue }

    func add(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
             scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
             _ block: @escaping () -> Void) {
        var addr = HAL.address(sel, scope: scope)
        let b: AudioObjectPropertyListenerBlock = { _, _ in block() }
        if AudioObjectAddPropertyListenerBlock(obj, &addr, queue, b) == noErr {
            entries.append((obj, addr, b))
        }
    }

    func removeAll() {
        for (obj, addr, b) in entries {
            var a = addr
            AudioObjectRemovePropertyListenerBlock(obj, &a, queue, b)
        }
        entries.removeAll()
    }
}

/// Thread-safe "last time a buffer arrived" (IO queue writes, control reads).
final class Stamp {
    private let lock = NSLock()
    private var value = 0.0
    func set(_ t: Double) { lock.lock(); value = t; lock.unlock() }
    func get() -> Double { lock.lock(); defer { lock.unlock() }; return value }
}

func uptime() -> Double { ProcessInfo.processInfo.systemUptime }

/// Which HAL call a (re)build is in. Read by the slow-start notice: with
/// System Audio Recording undecided, macOS can hold a tap's start until the
/// user answers its prompt, and the log should say where it waits.
enum BuildStage {
    private static let lock = NSLock()
    private static var value = "idle"
    static func set(_ s: String) { lock.lock(); value = s; lock.unlock() }
    static func get() -> String { lock.lock(); defer { lock.unlock() }; return value }
}

// MARK: - One output channel: buffers → mono → resample → batch → writer

/// Lives on the IO queue only. Survives rebuilds (the batcher keeps its
/// partial payload; the resampler is replaced when the input rate changes).
final class ChannelProcessor {
    let tag: UInt8
    let label: String
    let mix: MonoMix
    let outputRate: Int
    private let writer: PCMWriter
    private var resampler: PCM16Resampler?
    private var batcher: FrameBatcher
    private var probe = LevelProbe(window: 5.0)
    private var loggedFormat = false
    let stamp = Stamp()
    private let zeroHint: String

    init(tag: UInt8, label: String, mix: MonoMix, outputRate: Int, writer: PCMWriter, zeroHint: String) {
        self.tag = tag
        self.label = label
        self.mix = mix
        self.outputRate = outputRate
        self.writer = writer
        self.zeroHint = zeroHint
        batcher = FrameBatcher(flushSamples: max(1, outputRate / 10)) // 100 ms payloads
    }

    /// Called when a new source starts (IO queue): log its format once more
    /// and re-arm the level probe.
    func sourceChanged() {
        loggedFormat = false
        probe = LevelProbe(window: 5.0)
    }

    func process(_ abl: UnsafePointer<AudioBufferList>, format: AudioStreamBasicDescription, takeLast: Int) {
        guard let mono = Downmix.mono(abl, format: format, mix: mix, takeLast: takeLast) else { return }
        stamp.set(uptime())
        guard !mono.isEmpty else { return }
        if !loggedFormat {
            loggedFormat = true
            logErr("sysaudio: \(label) frames flowing (\(HAL.describe(format)))")
        }
        if let peak = probe.feed(mono, rate: format.mSampleRate) {
            logErr(String(format: "sysaudio: %@ peak over first %.0fs: %.5f%@", label, probe.window, peak,
                          peak == 0 ? " — ALL ZERO (\(zeroHint))" : ""))
        }
        if resampler == nil || resampler!.inputRate != format.mSampleRate {
            resampler = PCM16Resampler(inputRate: format.mSampleRate, outputRate: outputRate)
        }
        guard let resampler else { return }
        for payload in batcher.append(resampler.process(mono)) {
            writer.write(tag: tag, payload: payload)
        }
    }
}

// MARK: - The tap

@available(macOS 14.2, *)
final class TapSource {
    let mode: TapAggregateMode
    private(set) var tapID = AudioObjectID(kAudioObjectUnknown)
    private(set) var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private(set) var format = AudioStreamBasicDescription()
    private(set) var outputUID: String?
    private(set) var tapBuffers = 1

    private init(mode: TapAggregateMode) { self.mode = mode }

    /// Creates the tap and its aggregate and starts IO. `handler` runs on
    /// `ioQueue` with the tap's buffers. Throws (after cleaning up) on any
    /// HAL failure.
    static func start(mode: TapAggregateMode, ioQueue: DispatchQueue,
                      handler: @escaping (UnsafePointer<AudioBufferList>, AudioStreamBasicDescription, Int) -> Void)
        throws -> TapSource
    {
        let src = TapSource(mode: mode)
        do {
            try src.build(ioQueue: ioQueue, handler: handler)
        } catch {
            src.teardown()
            throw error
        }
        return src
    }

    private func build(ioQueue: DispatchQueue,
                       handler: @escaping (UnsafePointer<AudioBufferList>, AudioStreamBasicDescription, Int) -> Void)
        throws
    {
        // Our own output is never part of "them" (sysaudio plays nothing
        // today; this keeps it true). No process object yet = nothing to exclude.
        let excluded = [HAL.processObject(pid: getpid())].compactMap { $0 }.map { NSNumber(value: $0) }
        let desc = CATapDescription(__monoGlobalTapButExcludeProcesses: excluded)
        desc.name = "Contorch call audio"
        desc.uuid = UUID()
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        var tap = AudioObjectID(kAudioObjectUnknown)
        BuildStage.set("AudioHardwareCreateProcessTap")
        try check(AudioHardwareCreateProcessTap(desc, &tap), "AudioHardwareCreateProcessTap")
        tapID = tap

        guard let fmt = HAL.get(tapID, kAudioTapPropertyFormat, initial: AudioStreamBasicDescription()),
              fmt.mSampleRate > 0, SampleFormat(fmt) != .unsupported
        else { throw HALError(what: "reading the tap format (kAudioTapPropertyFormat)", status: -1) }
        format = fmt
        tapBuffers = fmt.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0 ? Int(max(1, fmt.mChannelsPerFrame)) : 1

        var composition: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Contorch call audio",
            kAudioAggregateDeviceUIDKey: "com.contorch.meeting-capture.sysaudio.tap.\(UUID().uuidString)",
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            // Never wait for a tapped process to play: IO must run (and
            // deliver silence) from the start, or the reader would see a stall.
            kAudioAggregateDeviceTapAutoStartKey: false,
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapUIDKey: desc.uuid.uuidString,
                kAudioSubTapDriftCompensationKey: true,
            ]],
        ]
        if mode == .withOutput {
            guard let out = HAL.defaultDevice(input: false), let uid = HAL.uid(out) else {
                throw HALError(what: "finding the default output device", status: -1)
            }
            outputUID = uid
            composition[kAudioAggregateDeviceMainSubDeviceKey] = uid
            composition[kAudioAggregateDeviceSubDeviceListKey] = [[kAudioSubDeviceUIDKey: uid]]
        }
        var agg = AudioObjectID(kAudioObjectUnknown)
        BuildStage.set("AudioHardwareCreateAggregateDevice")
        try check(AudioHardwareCreateAggregateDevice(composition as CFDictionary, &agg),
                  "AudioHardwareCreateAggregateDevice")
        aggregateID = agg

        let f = format
        let take = tapBuffers
        var proc: AudioDeviceIOProcID?
        try check(AudioDeviceCreateIOProcIDWithBlock(&proc, aggregateID, ioQueue) { _, input, _, _, _ in
            handler(input, f, take)
        }, "AudioDeviceCreateIOProcIDWithBlock")
        procID = proc
        BuildStage.set("AudioDeviceStart (tap aggregate)")
        try check(AudioDeviceStart(aggregateID, procID), "AudioDeviceStart (tap aggregate)")
        BuildStage.set("running")
    }

    /// The tap's format now (nil if unreadable).
    func currentFormat() -> AudioStreamBasicDescription? {
        HAL.get(tapID, kAudioTapPropertyFormat, initial: AudioStreamBasicDescription())
    }

    func teardown() {
        if aggregateID != kAudioObjectUnknown {
            if let p = procID {
                AudioDeviceStop(aggregateID, p)
                AudioDeviceDestroyIOProcID(aggregateID, p)
            }
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
        }
        procID = nil
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }
}

// MARK: - The microphone (HAL, default input device)

final class MicSource {
    private(set) var deviceID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private(set) var format = AudioStreamBasicDescription()

    static func start(ioQueue: DispatchQueue,
                      handler: @escaping (UnsafePointer<AudioBufferList>, AudioStreamBasicDescription) -> Void)
        throws -> MicSource
    {
        let src = MicSource()
        do {
            guard let dev = HAL.defaultDevice(input: true) else {
                throw HALError(what: "finding the default input device", status: -1)
            }
            src.deviceID = dev
            guard let fmt = HAL.firstInputFormat(dev), fmt.mSampleRate > 0, SampleFormat(fmt) != .unsupported else {
                throw HALError(what: "reading the input format of \(HAL.name(dev) ?? "the default input")", status: -1)
            }
            src.format = fmt
            var proc: AudioDeviceIOProcID?
            try check(AudioDeviceCreateIOProcIDWithBlock(&proc, dev, ioQueue) { _, input, _, _, _ in
                handler(input, fmt)
            }, "AudioDeviceCreateIOProcIDWithBlock (mic)")
            src.procID = proc
            try check(AudioDeviceStart(dev, proc), "AudioDeviceStart (mic)")
        } catch {
            src.teardown()
            throw error
        }
        return src
    }

    func currentFormat() -> AudioStreamBasicDescription? { HAL.firstInputFormat(deviceID) }

    func teardown() {
        if deviceID != kAudioObjectUnknown, let p = procID {
            AudioDeviceStop(deviceID, p)
            AudioDeviceDestroyIOProcID(deviceID, p)
        }
        procID = nil
    }
}

// MARK: - The controller

@available(macOS 14.2, *)
final class TapsCapture {
    private let control = DispatchQueue(label: "sysaudio.taps.control")
    private let io = DispatchQueue(label: "sysaudio.taps.io", qos: .userInteractive)
    private let mode: TapAggregateMode
    private let system: ChannelProcessor
    private let micProc: ChannelProcessor?

    private var tap: TapSource?
    private var tapPolicy: RebuildPolicy
    private var tapListeners: ListenerSet
    private var mic: MicSource?
    private var micPolicy = RebuildPolicy(requiresRebuild: true)
    private var micListeners: ListenerSet
    /// The mic gave up (no usable input device): retried on the next
    /// default-input change instead of killing system capture.
    private var micDormant = false
    private let systemListeners: ListenerSet
    private var timer: DispatchSourceTimer?
    private var lastVerify = 0.0

    init(writer: PCMWriter, sampleRate: Int, wantMic: Bool, mode: TapAggregateMode) {
        self.mode = mode
        system = ChannelProcessor(tag: FRAME_TAG_SYSTEM, label: "system audio (tap)", mix: .average,
                                  outputRate: sampleRate, writer: writer,
                                  zeroHint: "nothing playing yet, or System Audio Recording not allowed for sysaudio")
        micProc = wantMic
            ? ChannelProcessor(tag: FRAME_TAG_MIC, label: "mic", mix: .first, outputRate: sampleRate, writer: writer,
                               zeroHint: "built-in mic dead in clamshell mode? or mic TCC grant missing")
            : nil
        // A tap-only aggregate is not tied to the output device: a default
        // output switch only asks for a format check. With the output device
        // as clock, it must be rebuilt.
        tapPolicy = RebuildPolicy(requiresRebuild: mode == .withOutput)
        tapListeners = ListenerSet(queue: control)
        micListeners = ListenerSet(queue: control)
        systemListeners = ListenerSet(queue: control)
    }

    /// Starts the tap (throws if it can't: the caller exits 1, like the sck
    /// backend's start failure) and, if wanted and allowed, the mic (a mic
    /// failure only logs: system audio is never lost to the mic).
    func start(micAllowed: Bool) throws {
        let started = Stamp()
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
            if started.get() == 0 {
                logErr("sysaudio: the tap is taking more than 5 s to start (in \(BuildStage.get())) — "
                       + "macOS may be waiting for an answer to its System Audio Recording prompt")
            }
        }
        defer { started.set(1) }
        try control.sync {
            try buildTap()
            tapPolicy.built(ok: true, now: uptime())
            if micProc != nil && micAllowed {
                do {
                    try buildMic()
                    micPolicy.built(ok: true, now: uptime())
                } catch {
                    logErr("sysaudio: mic capture failed to start (\(error)) — continuing with system audio only")
                    micDormant = true
                }
            }
            installSystemListeners(mic: micProc != nil && micAllowed)
            let t = DispatchSource.makeTimerSource(queue: control)
            t.schedule(deadline: .now() + 0.25, repeating: 0.25)
            t.setEventHandler { [weak self] in self?.tick() }
            t.resume()
            timer = t
        }
    }

    var micRunning: Bool { control.sync { mic != nil } }

    // All below run on `control`.

    private func buildTap() throws {
        let sys = system
        let withOutput = mode == .withOutput
        io.sync { sys.sourceChanged() }
        let t = try TapSource.start(mode: mode, ioQueue: io) { abl, fmt, take in
            sys.process(abl, format: fmt, takeLast: withOutput ? take : 0)
        }
        tap = t
        let out = HAL.defaultDevice(input: false).flatMap(HAL.name) ?? "?"
        logErr("sysaudio: tap started (\(HAL.describe(t.format)); aggregate \(mode.rawValue); output now: \(out))")
        tapListeners.add(t.tapID, kAudioTapPropertyFormat) { [weak self] in self?.tapEvent(hard: false) }
        tapListeners.add(t.aggregateID, kAudioDevicePropertyDeviceIsAlive) { [weak self] in self?.tapEvent(hard: true) }
        tapListeners.add(t.aggregateID, kAudioDevicePropertyNominalSampleRate) { [weak self] in self?.tapEvent(hard: false) }
    }

    private func teardownTap() {
        tapListeners.removeAll()
        tap?.teardown()
        tap = nil
        tapPolicy.stopped()
    }

    private func buildMic() throws {
        guard let mp = micProc else { return }
        io.sync { mp.sourceChanged() }
        let m = try MicSource.start(ioQueue: io) { abl, fmt in
            mp.process(abl, format: fmt, takeLast: 0)
        }
        mic = m
        logErr("sysaudio: mic started on \(HAL.name(m.deviceID) ?? "the default input") (\(HAL.describe(m.format)))")
        micListeners.add(m.deviceID, kAudioDevicePropertyDeviceIsAlive) { [weak self] in self?.micEvent(hard: true) }
        micListeners.add(m.deviceID, kAudioDevicePropertyNominalSampleRate) { [weak self] in self?.micEvent(hard: false) }
        micListeners.add(m.deviceID, kAudioDevicePropertyStreamConfiguration, scope: kAudioObjectPropertyScopeInput) {
            [weak self] in self?.micEvent(hard: false)
        }
    }

    private func teardownMic() {
        micListeners.removeAll()
        mic?.teardown()
        mic = nil
        micPolicy.stopped()
    }

    private func installSystemListeners(mic: Bool) {
        systemListeners.add(HAL.system, kAudioHardwarePropertyDefaultOutputDevice) { [weak self] in
            logErr("sysaudio: default output changed (\(HAL.defaultDevice(input: false).flatMap(HAL.name) ?? "none"))")
            self?.tapEvent(hard: false)
        }
        systemListeners.add(HAL.system, kAudioHardwarePropertyDevices) { [weak self] in self?.tapEvent(hard: false) }
        if mic {
            systemListeners.add(HAL.system, kAudioHardwarePropertyDefaultInputDevice) { [weak self] in
                logErr("sysaudio: default input changed (\(HAL.defaultDevice(input: true).flatMap(HAL.name) ?? "none"))")
                self?.micEvent(hard: true)
            }
        }
    }

    private func tapEvent(hard: Bool) { tapPolicy.change(now: uptime(), hard: hard) }

    private func micEvent(hard: Bool) {
        if micDormant {
            micDormant = false
            micPolicy = RebuildPolicy(requiresRebuild: true)
            micPolicy.built(ok: false, now: uptime() - 10) // retry at the next tick
            return
        }
        micPolicy.change(now: uptime(), hard: hard)
    }

    private func tick() {
        let now = uptime()

        // --- them
        // Only a buffer newer than the last one (or the last build) counts:
        // feeding the same stamp again would reset the stall count.
        let sysStamp = system.stamp.get()
        if sysStamp > tapPolicy.lastBuffer { tapPolicy.buffer(now: sysStamp) }
        var tapAction = tapPolicy.decide(now: now)
        if tapAction == .none, tap != nil, now - lastVerify >= 5.0 {
            lastVerify = now
            tapAction = .verify   // cheap belt and braces: a missed format notification
        }
        switch tapAction {
        case .none:
            break
        case .verify:
            if let t = tap, let cur = t.currentFormat(), !HAL.sameFormat(cur, t.format) {
                rebuildTap(reason: "tap format changed: \(HAL.describe(t.format)) → \(HAL.describe(cur))", now: now)
            }
        case .rebuild(let why):
            rebuildTap(reason: why, now: now)
        case .giveUp(let why):
            logErr("sysaudio error: system audio capture (tap) can't recover: \(why) — exiting so the recorder restarts it")
            exit(2)
        }

        // --- me
        guard let mp = micProc, !micDormant else { return }
        if mic != nil || micPolicy.failures > 0 {
            let micStamp = mp.stamp.get()
            if micStamp > micPolicy.lastBuffer { micPolicy.buffer(now: micStamp) }
            switch micPolicy.decide(now: now) {
            case .none, .verify:
                break
            case .rebuild(let why):
                teardownMic()
                do {
                    try buildMic()
                    micPolicy.built(ok: true, now: uptime())
                    logErr("sysaudio: mic rebuilt (\(why))")
                } catch {
                    micPolicy.built(ok: false, now: uptime())
                    logErr("sysaudio: mic rebuild failed (\(why)): \(error)")
                }
            case .giveUp(let why):
                teardownMic()
                micDormant = true
                logErr("sysaudio: mic unavailable (\(why)) — continuing with system audio only until the default input changes")
            }
        }
    }

    private func rebuildTap(reason: String, now: Double) {
        teardownTap()
        do {
            try buildTap()
            tapPolicy.built(ok: true, now: uptime())
            logErr("sysaudio: tap rebuilt (\(reason))")
        } catch {
            tapPolicy.built(ok: false, now: uptime())
            logErr("sysaudio: tap rebuild failed (\(reason)): \(error)")
        }
    }

    /// Ends capture (tests and the permission request path).
    func stop() {
        control.sync {
            timer?.cancel()
            timer = nil
            systemListeners.removeAll()
            teardownMic()
            teardownTap()
        }
    }
}

// MARK: - Entry points

/// `sysaudio --backend taps [--mic]`. Never returns.
@available(macOS 14.2, *)
func runTaps(sampleRate: Int, wantMic: Bool) async -> Never {
    let tcc = AudioCaptureTCC.status()
    switch tcc {
    case "denied":
        logErr("sysaudio error: System Audio Recording was refused for this app (declined TCCs: "
               + "kTCCServiceAudioCapture). Allow it under System Settings › Privacy & Security › "
               + "Screen & System Audio Recording › System Audio Recording Only.")
        exit(1)
    case "not_determined":
        logErr("sysaudio: System Audio Recording not decided yet — macOS asks when the tap starts")
    default:
        break
    }

    var micAllowed = wantMic
    if wantMic {
        micAllowed = await micPermission()
    }

    let writer = PCMWriter(framed: wantMic)
    let capture = TapsCapture(writer: writer, sampleRate: sampleRate, wantMic: wantMic,
                              mode: TapAggregateMode.fromEnvironment())
    do {
        try capture.start(micAllowed: micAllowed)
    } catch {
        logErr("sysaudio error: system audio tap failed to start: \(error)")
        exit(1)
    }
    let mode = capture.micRunning ? "system+mic, framed"
        : (wantMic ? "system only (mic fallback), framed" : "system only, raw")
    logErr("sysaudio: stream started (backend taps, sample rate \(sampleRate), int16 LE, \(mode)), piping PCM to stdout")
    while true {
        try? await Task.sleep(nanoseconds: 1_000_000_000)
    }
}

/// `sysaudio check --request system_audio` without TCCAccessRequest: the
/// documented route. Starts a tap (the prompt appears), discards every
/// sample, and waits until the state is decided (or `timeout`).
@available(macOS 14.2, *)
func requestAudioCaptureByTap(timeout: TimeInterval) async {
    let io = DispatchQueue(label: "sysaudio.request.io")
    let src: TapSource
    do {
        src = try TapSource.start(mode: .tapOnly, ioQueue: io) { _, _, _ in }
    } catch {
        logErr("sysaudio check: could not start a tap to ask for System Audio Recording: \(error)")
        return
    }
    let deadline = Date().addingTimeInterval(timeout)
    // Without the preflight SPI there is nothing to poll: give the user a
    // fixed window to answer.
    let blind = AudioCaptureTCC.status() == "unknown"
    let blindUntil = Date().addingTimeInterval(min(timeout, 30))
    while Date() < deadline {
        try? await Task.sleep(nanoseconds: 500_000_000)
        if blind {
            if Date() >= blindUntil { break }
        } else if AudioCaptureTCC.status() != "not_determined" {
            break
        }
    }
    src.teardown()
}

/// The Microphone request flow both backends share: authorized → true; not
/// determined → ask (the prompt); denied/restricted → false, logged.
func micPermission() async -> Bool {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized:
        return true
    case .notDetermined:
        logErr("sysaudio: requesting microphone permission (watch for the macOS prompt)...")
        let granted = await AVCaptureDevice.requestAccess(for: .audio)
        if !granted {
            logErr("sysaudio: microphone permission denied — continuing with system audio only")
        }
        return granted
    default:
        logErr(
            "sysaudio: microphone permission denied/restricted — continuing with system "
            + "audio only. Grant it under System Settings → Privacy & Security → Microphone "
            + "(to the parent terminal/launcher), then restart the daemon."
        )
        return false
    }
}
