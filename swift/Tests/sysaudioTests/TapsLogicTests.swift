// `swift test` (from swift/). The taps backend's pure parts: the capture
// command line, the permission words, buffer → mono → 16 kHz int16 → payload,
// and the device-change state machine. Nothing here opens a device, creates
// a tap or asks for a permission.
import AVFoundation
import CoreAudio
import Foundation
import Testing
@testable import sysaudio

// MARK: - helpers

/// An AudioBufferList with `buffers` buffers of the given raw bytes.
final class ABL {
    let list: UnsafeMutableAudioBufferListPointer
    private var storage: [UnsafeMutableRawPointer] = []

    init(_ buffers: [(channels: Int, bytes: [UInt8])]) {
        list = AudioBufferList.allocate(maximumBuffers: buffers.count)
        for (i, b) in buffers.enumerated() {
            let p = UnsafeMutableRawPointer.allocate(byteCount: max(1, b.bytes.count), alignment: 16)
            b.bytes.withUnsafeBytes { p.copyMemory(from: $0.baseAddress!, byteCount: b.bytes.count) }
            storage.append(p)
            list[i] = AudioBuffer(mNumberChannels: UInt32(b.channels), mDataByteSize: UInt32(b.bytes.count), mData: p)
        }
    }

    var pointer: UnsafePointer<AudioBufferList> { UnsafePointer(list.unsafePointer) }

    deinit {
        storage.forEach { $0.deallocate() }
        free(list.unsafeMutablePointer)
    }
}

func bytes<T>(_ values: [T]) -> [UInt8] {
    values.withUnsafeBytes { Array($0) }
}

func asbd(rate: Double, channels: UInt32, float: Bool = true, bits: UInt32 = 32,
          nonInterleaved: Bool = false) -> AudioStreamBasicDescription {
    var flags: AudioFormatFlags = kAudioFormatFlagIsPacked
    flags |= float ? kAudioFormatFlagIsFloat : kAudioFormatFlagIsSignedInteger
    if nonInterleaved { flags |= kAudioFormatFlagIsNonInterleaved }
    let bpf = (bits / 8) * (nonInterleaved ? 1 : channels)
    return AudioStreamBasicDescription(mSampleRate: rate, mFormatID: kAudioFormatLinearPCM, mFormatFlags: flags,
                                       mBytesPerPacket: bpf, mFramesPerPacket: 1, mBytesPerFrame: bpf,
                                       mChannelsPerFrame: channels, mBitsPerChannel: bits, mReserved: 0)
}

func sine(_ freq: Double, rate: Double, seconds: Double, amp: Float) -> [Float] {
    let n = Int(rate * seconds)
    return (0..<n).map { amp * Float(sin(2 * Double.pi * freq * Double($0) / rate)) }
}

func rms(_ s: [Int16]) -> Double {
    guard !s.isEmpty else { return 0 }
    return sqrt(s.reduce(0.0) { $0 + Double($1) * Double($1) } / Double(s.count))
}

func zeroCrossings(_ s: [Int16]) -> Int {
    var n = 0
    for i in 1..<s.count where (s[i - 1] < 0) != (s[i] < 0) { n += 1 }
    return n
}

// MARK: - command line

@Suite("capture command line")
struct CaptureArgsTests {
    @Test("no flags: the old defaults, sck backend")
    func defaults() {
        #expect(CaptureCommand.parse([]) == .run(CaptureArgs(sampleRate: 16000, mic: false, backend: .sck)))
    }

    @Test("--backend in both spellings, with the old flags")
    func backend() {
        #expect(CaptureCommand.parse(["--sample-rate", "16000", "--mic", "--backend", "taps"])
            == .run(CaptureArgs(sampleRate: 16000, mic: true, backend: .taps)))
        #expect(CaptureCommand.parse(["--backend=sck", "--mic"]) == .run(CaptureArgs(sampleRate: 16000, mic: true, backend: .sck)))
        #expect(CaptureCommand.parse(["--sample-rate", "48000"]) == .run(CaptureArgs(sampleRate: 48000, mic: false, backend: .sck)))
    }

    @Test("bad values are usage errors; unknown flags keep 'unknown arg: X'")
    func errors() {
        #expect(CaptureCommand.parse(["--no-such-flag"]) == .usage("unknown arg: --no-such-flag"))
        // As before: a non-numeric rate isn't consumed and then fails as an unknown arg.
        #expect(CaptureCommand.parse(["--sample-rate", "abc"]) == .usage("unknown arg: abc"))
        for argv in [["--backend"], ["--backend", "coreaudio"], ["--backend=x"], ["--sample-rate", "0"]] {
            guard case .usage(let m) = CaptureCommand.parse(argv) else {
                Issue.record("\(argv) should be a usage error")
                continue
            }
            #expect(!m.contains("unknown arg"))
        }
        #expect(CaptureCommand.parse(["-h"]) == .help)
        #expect(CaptureCommand.parse(["--mic", "--help"]) == .help)
    }

    @Test("the usage line still starts the way callers grep for it")
    func usageLine() {
        #expect(CaptureCommand.usage.hasPrefix("Usage: sysaudio [--sample-rate N] [--mic]"))
    }

    @Test("sck is always available; taps from macOS 14.2")
    func backends() {
        let b = CaptureCommand.availableBackends()
        #expect(b.first == "sck")
        if #available(macOS 14.2, *) { #expect(b.contains("taps")) } else { #expect(!b.contains("taps")) }
    }

    @Test("the aggregate layout knob")
    func aggregateMode() {
        #expect(TapAggregateMode.fromEnvironment([:]) == .tapOnly)
        #expect(TapAggregateMode.fromEnvironment(["SYSAUDIO_TAP_AGGREGATE": "with-output"]) == .withOutput)
        #expect(TapAggregateMode.fromEnvironment(["SYSAUDIO_TAP_AGGREGATE": "WITH-OUTPUT"]) == .withOutput)
        #expect(TapAggregateMode.fromEnvironment(["SYSAUDIO_TAP_AGGREGATE": "nonsense"]) == .tapOnly)
    }
}

@Suite("check: System Audio Recording Only")
struct CheckSystemAudioTests {
    @Test("--request system_audio parses; the old requests are unchanged")
    func request() {
        #expect(CheckCommand.parse(["--json", "--request", "system_audio"])
            == .run(CheckOptions(json: true, request: .systemAudio)))
        #expect(CheckCommand.parse(["--request=system_audio"]) == .run(CheckOptions(json: false, request: .systemAudio)))
        #expect(CheckCommand.parse(["--request", "screen"]) == .run(CheckOptions(json: false, request: .screen)))
        guard case .usage(let m) = CheckCommand.parse(["--request", "audio"]) else {
            Issue.record("--request audio should be a usage error")
            return
        }
        #expect(m.contains("system_audio"))
    }

    @Test("TCC preflight answers map to the contract's words")
    func words() {
        #expect(AudioCaptureTCC.word(0) == "granted")
        #expect(AudioCaptureTCC.word(1) == "denied")
        #expect(AudioCaptureTCC.word(2) == "not_determined")
        #expect(AudioCaptureTCC.word(nil) == "unknown")
        #expect(AudioCaptureTCC.word(7) == "unknown")
    }

    @Test("the document carries system_audio and backends next to the old keys")
    func document() throws {
        let doc = CheckCommand.document(screen: "granted", mic: "denied", systemAudio: "not_determined",
                                        backends: ["sck", "taps"], requested: .systemAudio)
        #expect(doc["schema"] as? String == "sysaudio.check/1")
        #expect(doc["screen_capture"] as? String == "granted")
        #expect(doc["microphone"] as? String == "denied")
        #expect(doc["system_audio"] as? String == "not_determined")
        #expect(doc["backends"] as? [String] == ["sck", "taps"])
        #expect(doc["requested"] as? String == "system_audio")
        let data = try JSONSerialization.data(withJSONObject: doc, options: [.sortedKeys])
        #expect(String(data: data, encoding: .utf8)!.contains("\"backends\":[\"sck\",\"taps\"]"))
    }
}

// MARK: - samples

@Suite("buffers to mono")
struct DownmixTests {
    @Test("interleaved stereo Float32 averages the channels")
    func interleavedAverage() throws {
        let abl = ABL([(2, bytes([Float(1.0), Float(0.0), Float(0.5), Float(-0.5)]))])
        let m = try #require(Downmix.mono(abl.pointer, format: asbd(rate: 48000, channels: 2), mix: .average))
        #expect(m == [0.5, 0.0])
    }

    @Test("interleaved stereo, first channel only (the mic rule)")
    func interleavedFirst() throws {
        let abl = ABL([(2, bytes([Float(0.25), Float(1.0), Float(-0.25), Float(1.0)]))])
        let m = try #require(Downmix.mono(abl.pointer, format: asbd(rate: 48000, channels: 2), mix: .first))
        #expect(m == [0.25, -0.25])
    }

    @Test("non-interleaved stereo: one buffer per channel")
    func nonInterleaved() throws {
        let abl = ABL([(1, bytes([Float(1.0), Float(1.0)])), (1, bytes([Float(0.0), Float(-1.0)]))])
        let f = asbd(rate: 48000, channels: 2, nonInterleaved: true)
        #expect(try #require(Downmix.mono(abl.pointer, format: f, mix: .average)) == [0.5, 0.0])
        #expect(try #require(Downmix.mono(abl.pointer, format: f, mix: .first)) == [1.0, 1.0])
    }

    @Test("an aggregate's trailing buffers are the tap (takeLast)")
    func takeLast() throws {
        // A sub-device's input stream first (loud), then the mono tap (quiet).
        let abl = ABL([(1, bytes([Float(0.9), Float(0.9)])), (1, bytes([Float(0.1), Float(0.2)]))])
        let m = try #require(Downmix.mono(abl.pointer, format: asbd(rate: 48000, channels: 1), mix: .average, takeLast: 1))
        #expect(m == [0.1, 0.2])
    }

    @Test("Int16 and Int32 input scale to ±1")
    func integers() throws {
        let i16 = ABL([(1, bytes([Int16(16384), Int16(-32768)]))])
        #expect(try #require(Downmix.mono(i16.pointer, format: asbd(rate: 16000, channels: 1, float: false, bits: 16),
                                          mix: .average)) == [0.5, -1.0])
        let i32 = ABL([(1, bytes([Int32(1 << 30)]))])
        #expect(try #require(Downmix.mono(i32.pointer, format: asbd(rate: 16000, channels: 1, float: false, bits: 32),
                                          mix: .average)) == [0.5])
    }

    @Test("unsupported formats are refused, empty buffers give no samples")
    func refused() {
        let abl = ABL([(1, bytes([UInt8(0), 0, 0]))])
        var f = asbd(rate: 48000, channels: 1, float: false, bits: 24)
        #expect(Downmix.mono(abl.pointer, format: f, mix: .average) == nil)
        f = asbd(rate: 48000, channels: 1)
        f.mFormatID = kAudioFormatMPEG4AAC
        #expect(Downmix.mono(abl.pointer, format: f, mix: .average) == nil)
        let empty = ABL([(1, [])])
        #expect(Downmix.mono(empty.pointer, format: asbd(rate: 48000, channels: 1), mix: .average) == [])
    }
}

@Suite("resample and quantise")
struct ResamplerTests {
    @Test("float → int16 is the sck backend's rule")
    func quantise() {
        #expect(int16Sample(1.0) == 32767)
        #expect(int16Sample(-1.0) == -32767)
        #expect(int16Sample(2.0) == 32767)
        #expect(int16Sample(-3.0) == -32767)
        #expect(int16Sample(0.5) == 16383)
        #expect(int16Sample(0) == 0)
    }

    @Test("same rate: a plain quantise, sample for sample")
    func sameRate() throws {
        let r = try #require(PCM16Resampler(inputRate: 16000, outputRate: 16000))
        let x: [Float] = [0, 0.5, -0.5, 1.2]
        #expect(r.process(x) == x.map(int16Sample))
    }

    @Test("48/44.1/24 kHz → 16 kHz in 10 ms HAL-sized pieces: length, level and pitch survive",
          arguments: [48000.0, 44100.0, 24000.0])
    func streaming(rate: Double) throws {
        let r = try #require(PCM16Resampler(inputRate: rate, outputRate: 16000))
        let input = sine(440, rate: rate, seconds: 2.0, amp: 0.5)
        let piece = Int(rate / 100)
        var out: [Int16] = []
        var i = 0
        while i < input.count {
            out += r.process(Array(input[i..<min(i + piece, input.count)]))
            i += piece
        }
        // Converter latency holds back a little; nothing is invented.
        #expect(out.count <= 32000)
        #expect(out.count >= 32000 - 400, "got \(out.count) samples for 2 s")
        let steady = Array(out[1600..<min(out.count, 30000)])
        let expectRMS = 0.5 / 2.0.squareRoot() * 32767
        #expect(abs(rms(steady) - expectRMS) / expectRMS < 0.05, "rms \(rms(steady)) vs \(expectRMS)")
        // 440 Hz → 880 zero crossings per second.
        let perSecond = Double(zeroCrossings(steady)) / (Double(steady.count) / 16000)
        #expect(abs(perSecond - 880) < 10, "\(perSecond) crossings/s")
    }

    @Test("a 9 kHz tone (above the 8 kHz Nyquist of 16 kHz output) is filtered, not aliased")
    func antiAlias() throws {
        let r = try #require(PCM16Resampler(inputRate: 48000, outputRate: 16000))
        let out = r.process(sine(9000, rate: 48000, seconds: 1.0, amp: 0.5))
        let steady = Array(out.dropFirst(800))
        #expect(rms(steady) < 0.02 * 32767, "9 kHz leaked through at rms \(rms(steady))")
    }

    @Test("bad rates are refused")
    func refused() {
        #expect(PCM16Resampler(inputRate: 0, outputRate: 16000) == nil)
        #expect(PCM16Resampler(inputRate: 48000, outputRate: 0) == nil)
    }
}

@Suite("payload batching and the level probe")
struct BatcherTests {
    @Test("payloads come out at the threshold, little-endian, in order")
    func batching() throws {
        var b = FrameBatcher(flushSamples: 4)
        #expect(b.append([1, 2]).isEmpty)
        let out = b.append([3, -2, 5])
        #expect(out.count == 1)
        #expect(Array(out[0]) == [1, 0, 2, 0, 3, 0, 0xFE, 0xFF, 5, 0])
        #expect(b.pending.isEmpty)
        #expect(b.append([7]).isEmpty)
        let rest = b.drain()
        #expect(rest.map { Array($0) } == [7, 0])
        #expect(b.drain() == nil)
    }

    @Test("100 ms payloads at 16 kHz carry 3200 bytes")
    func size() {
        var b = FrameBatcher(flushSamples: 16000 / 10)
        var payloads: [Data] = []
        for _ in 0..<100 { payloads += b.append([Int16](repeating: 1, count: 160)) } // 1 s of 10 ms pieces
        #expect(payloads.count == 10)
        #expect(payloads.allSatisfy { $0.count == 3200 })
    }

    @Test("the probe reports one peak when its window fills")
    func probe() {
        var p = LevelProbe(window: 1.0)
        #expect(p.feed([Float](repeating: 0, count: 8000), rate: 16000) == nil)
        #expect(p.feed([0.25, -0.5] + [Float](repeating: 0, count: 7998), rate: 16000) == 0.5)
        #expect(p.feed([1.0], rate: 16000) == nil) // once only
    }
}

// MARK: - the device-change state machine

@Suite("rebuild policy")
struct RebuildPolicyTests {
    func running(requiresRebuild: Bool = true, at t: Double = 0) -> RebuildPolicy {
        var p = RebuildPolicy(requiresRebuild: requiresRebuild)
        p.built(ok: true, now: t)
        return p
    }

    @Test("a quiet, flowing capture is left alone")
    func steady() {
        var p = running()
        for i in 1...100 {
            let t = Double(i) * 0.25
            p.buffer(now: t)
            #expect(p.decide(now: t) == .none)
        }
    }

    @Test("a burst of device events (AirPods connecting) gives one rebuild after it settles")
    func burstCoalesced() {
        var p = running()
        var actions: [RebuildAction] = []
        var t = 10.0
        p.buffer(now: t)
        for _ in 0..<8 { // 8 events over 1.4 s
            p.change(now: t)
            p.buffer(now: t)
            actions.append(p.decide(now: t))
            t += 0.2
        }
        while t < 13 {
            p.buffer(now: t)
            actions.append(p.decide(now: t))
            t += 0.25
        }
        #expect(actions.filter { if case .rebuild = $0 { return true }; return false }.count == 1)
    }

    @Test("a TV output that keeps flapping is still acted on after maxDefer")
    func flappingOverdue() {
        var p = running()
        var t = 10.0
        var firstRebuild: Double?
        while t < 20, firstRebuild == nil {
            p.change(now: t)    // every 0.3 s, never settling
            p.buffer(now: t)
            if case .rebuild = p.decide(now: t) { firstRebuild = t }
            t += 0.3
        }
        let at = try? #require(firstRebuild)
        #expect(at != nil && at! >= 13.0 - 0.01 && at! <= 13.4)
    }

    @Test("tap-only aggregate: an output switch only asks for a format check; a dead device rebuilds")
    func verifyVersusHard() {
        var p = running(requiresRebuild: false)
        p.change(now: 5)
        p.buffer(now: 5.6)
        #expect(p.decide(now: 5.6) == .verify)
        p.change(now: 8, hard: true)
        p.buffer(now: 8.6)
        if case .rebuild = p.decide(now: 8.6) {} else { Issue.record("a hard change must rebuild") }
    }

    @Test("IO that silently stops (sleep/wake) is rebuilt; endless stalls give up")
    func stall() {
        var p = running()
        p.buffer(now: 1)
        #expect(p.decide(now: 3.9) == .none)
        guard case .rebuild(let why) = p.decide(now: 4.1) else {
            Issue.record("a 3 s stall must rebuild")
            return
        }
        #expect(why.contains("no audio buffers"))
        // Rebuilds succeed but no buffer ever arrives.
        var t = 4.1
        var outcome: RebuildAction = .none
        for _ in 0..<40 {
            p.built(ok: true, now: t)
            t += 3.1
            outcome = p.decide(now: t)
            if case .giveUp = outcome { break }
        }
        if case .giveUp = outcome {} else { Issue.record("expected give-up, got \(outcome)") }
        // Audio returning in between resets the count.
        var q = running()
        for k in 0..<20 {
            let base = Double(k) * 10
            q.buffer(now: base + 0.1)
            if case .rebuild = q.decide(now: base + 3.5) { q.built(ok: true, now: base + 3.5) }
            q.buffer(now: base + 4)
            #expect({ if case .giveUp = q.decide(now: base + 4) { return false }; return true }())
        }
    }

    @Test("failed builds retry with backoff, then give up after 20 s")
    func failures() {
        var p = running()
        p.built(ok: false, now: 0)
        #expect(p.decide(now: 0.2) == .none)
        #expect(p.decide(now: 0.5) == .rebuild("retry 1"))
        p.built(ok: false, now: 0.5)
        #expect(p.decide(now: 1.0) == .none)
        #expect(p.decide(now: 1.5) == .rebuild("retry 2"))
        var t = 1.5
        var gaveUp = false
        while t < 30 {
            p.built(ok: false, now: t)
            t += 4
            if case .giveUp = p.decide(now: t) { gaveUp = true; break }
        }
        #expect(gaveUp)
        #expect(t >= 20 && t < 26)
        // A success clears it all.
        var q = running()
        q.built(ok: false, now: 1)
        q.built(ok: true, now: 2)
        q.buffer(now: 25)
        #expect(q.decide(now: 25) == .none)
    }

    @Test("rebuilds are at least minInterval apart")
    func minInterval() {
        var p = running(at: 10)
        p.change(now: 10.0)
        p.buffer(now: 10.6)
        #expect(p.decide(now: 10.6) == .none)   // settled, but built 0.6 s ago
        p.buffer(now: 11.0)
        if case .rebuild = p.decide(now: 11.0) {} else { Issue.record("expected a rebuild at 1 s") }
    }
}
