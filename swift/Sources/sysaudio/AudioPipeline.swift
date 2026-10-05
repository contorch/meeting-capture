// The taps backend's sample path, kept free of devices so `swift test` covers
// it: HAL buffers (any channel count, interleaved or not, Float32/Int16/Int32)
// → mono Float32 → resampled int16 LE mono at the output rate → batched
// payloads for PCMWriter. The output is exactly what the sck backend writes:
// int16 little-endian mono PCM at --sample-rate.

import AVFoundation
import CoreAudio
import Foundation

/// How several channels become one.
enum MonoMix: Equatable {
    /// Mean of all channels (system audio: a stereo mix must keep both sides).
    case average
    /// Channel 0 only (a microphone: averaging in an unused, silent second
    /// channel would halve the level).
    case first
}

enum SampleFormat: Equatable {
    case float32
    case int16
    case int32
    case unsupported

    init(_ asbd: AudioStreamBasicDescription) {
        guard asbd.mFormatID == kAudioFormatLinearPCM else { self = .unsupported; return }
        let isFloat = asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0
        let bigEndian = asbd.mFormatFlags & kAudioFormatFlagIsBigEndian != 0
        if bigEndian { self = .unsupported; return }
        switch (isFloat, asbd.mBitsPerChannel) {
        case (true, 32): self = .float32
        case (false, 16): self = .int16
        case (false, 32): self = .int32
        default: self = .unsupported
        }
    }

    var bytes: Int {
        switch self {
        case .float32, .int32: return 4
        case .int16: return 2
        case .unsupported: return 0
        }
    }
}

enum Downmix {
    /// Mono Float32 samples from the last buffers of `abl` that hold one
    /// stream in format `asbd`. `takeLast` is how many AudioBuffers that
    /// stream occupies (1 when interleaved, the channel count otherwise; 0 =
    /// every buffer). An aggregate device lists its sub-devices' input streams
    /// before its taps, so the tap is always the trailing buffers.
    /// Returns nil when the format is unsupported or the buffers don't match it.
    static func mono(_ abl: UnsafePointer<AudioBufferList>, format asbd: AudioStreamBasicDescription,
                     mix: MonoMix, takeLast: Int = 0) -> [Float]? {
        let list = audioBuffers(abl)
        let fmt = SampleFormat(asbd)
        guard fmt != .unsupported, list.count > 0 else { return nil }
        let nonInterleaved = asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
        let channels = max(1, Int(asbd.mChannelsPerFrame))
        let want = takeLast > 0 ? min(takeLast, list.count) : list.count
        let buffers = Array(list[(list.count - want)...])

        if nonInterleaved {
            // One channel per buffer.
            let used = mix == .first ? Array(buffers.prefix(1)) : buffers
            guard let n = used.map({ Int($0.mDataByteSize) / fmt.bytes }).min(), n > 0 else { return [] }
            var out = [Float](repeating: 0, count: n)
            for b in used {
                guard let p = b.mData else { continue }
                accumulate(p, fmt: fmt, stride: 1, offset: 0, count: n, into: &out)
            }
            if used.count > 1 { let k = Float(used.count); for i in 0..<n { out[i] /= k } }
            return out
        }

        // Interleaved: one buffer carrying `channels` channels per frame.
        guard let b = buffers.first, let p = b.mData else { return [] }
        let chans = max(1, Int(b.mNumberChannels) > 0 ? Int(b.mNumberChannels) : channels)
        let frames = Int(b.mDataByteSize) / (fmt.bytes * chans)
        guard frames > 0 else { return [] }
        var out = [Float](repeating: 0, count: frames)
        let useChans = mix == .first ? 1 : chans
        for c in 0..<useChans {
            accumulate(p, fmt: fmt, stride: chans, offset: c, count: frames, into: &out)
        }
        if useChans > 1 { let k = Float(useChans); for i in 0..<frames { out[i] /= k } }
        return out
    }

    /// The buffers of an AudioBufferList, read by hand rather than through
    /// the CoreAudio Swift overlay's UnsafeMutableAudioBufferListPointer, so
    /// libswiftCoreAudio stays a weak (optional) dependency as before.
    static func audioBuffers(_ abl: UnsafePointer<AudioBufferList>) -> [AudioBuffer] {
        let n = Int(abl.pointee.mNumberBuffers)
        guard n > 0, let offset = MemoryLayout<AudioBufferList>.offset(of: \AudioBufferList.mBuffers) else { return [] }
        let first = UnsafeRawPointer(abl).advanced(by: offset).assumingMemoryBound(to: AudioBuffer.self)
        return Array(UnsafeBufferPointer(start: first, count: n))
    }

    private static func accumulate(_ p: UnsafeMutableRawPointer, fmt: SampleFormat, stride: Int, offset: Int,
                                   count: Int, into out: inout [Float]) {
        switch fmt {
        case .float32:
            let s = p.assumingMemoryBound(to: Float.self)
            for i in 0..<count { out[i] += s[i * stride + offset] }
        case .int16:
            let s = p.assumingMemoryBound(to: Int16.self)
            for i in 0..<count { out[i] += Float(s[i * stride + offset]) / 32768.0 }
        case .int32:
            let s = p.assumingMemoryBound(to: Int32.self)
            for i in 0..<count { out[i] += Float(s[i * stride + offset]) / 2_147_483_648.0 }
        case .unsupported:
            break
        }
    }
}

/// The sck backend's float → int16 rule (clamp, ×32767, truncate), shared so
/// both backends quantise identically.
@inline(__always)
func int16Sample(_ f: Float) -> Int16 {
    var v = f
    if v > 1.0 { v = 1.0 } else if v < -1.0 { v = -1.0 }
    return Int16(v * 32767.0)
}

/// Mono Float32 at `inputRate` → mono int16 at `outputRate`. Keeps the
/// converter (and its filter state) between calls, so consecutive HAL buffers
/// resample as one continuous signal. Same rate: a plain quantise, no filter.
final class PCM16Resampler {
    let inputRate: Double
    let outputRate: Double
    private let converter: AVAudioConverter?
    private let srcFormat: AVAudioFormat?
    private let dstFormat: AVAudioFormat?

    init?(inputRate: Double, outputRate: Int) {
        guard inputRate > 0, outputRate > 0 else { return nil }
        self.inputRate = inputRate
        self.outputRate = Double(outputRate)
        if inputRate == Double(outputRate) {
            converter = nil
            srcFormat = nil
            dstFormat = nil
            return
        }
        guard let src = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: inputRate, channels: 1,
                                      interleaved: false),
              let dst = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(outputRate), channels: 1,
                                      interleaved: true),
              let conv = AVAudioConverter(from: src, to: dst)
        else { return nil }
        // Speech: favour quality over CPU; a 48 kHz → 16 kHz stream is cheap anyway.
        conv.sampleRateConverterQuality = AVAudioQuality.high.rawValue
        srcFormat = src
        dstFormat = dst
        converter = conv
    }

    func process(_ mono: [Float]) -> [Int16] {
        guard !mono.isEmpty else { return [] }
        guard let converter, let srcFormat, let dstFormat else {
            return mono.map(int16Sample)
        }
        let frames = AVAudioFrameCount(mono.count)
        guard let inBuf = AVAudioPCMBuffer(pcmFormat: srcFormat, frameCapacity: frames),
              let ch = inBuf.floatChannelData else { return [] }
        inBuf.frameLength = frames
        mono.withUnsafeBufferPointer { src in
            ch[0].update(from: src.baseAddress!, count: mono.count)
        }
        let capacity = AVAudioFrameCount((Double(frames) * outputRate / inputRate).rounded(.up)) + 64
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: dstFormat, frameCapacity: capacity) else { return [] }
        var fed = false
        var err: NSError?
        converter.convert(to: outBuf, error: &err) { _, status in
            if fed {
                status.pointee = .noDataNow
                return nil
            }
            fed = true
            status.pointee = .haveData
            return inBuf
        }
        guard err == nil, let out = outBuf.int16ChannelData else { return [] }
        return Array(UnsafeBufferPointer(start: out[0], count: Int(outBuf.frameLength)))
    }
}

/// Collects int16 samples and hands out payloads of at least `flushSamples`
/// (HAL buffers are ~10 ms; the sck backend wrote far larger payloads and the
/// reader is happiest with a few frames per second, not a hundred).
struct FrameBatcher {
    let flushSamples: Int
    private(set) var pending: [Int16] = []

    init(flushSamples: Int) {
        self.flushSamples = max(1, flushSamples)
        pending.reserveCapacity(self.flushSamples * 2)
    }

    /// Payloads ready to write (int16 LE bytes), possibly none.
    mutating func append(_ samples: [Int16]) -> [Data] {
        pending.append(contentsOf: samples)
        guard pending.count >= flushSamples else { return [] }
        let out = FrameBatcher.bytes(pending)
        pending.removeAll(keepingCapacity: true)
        return [out]
    }

    /// Whatever is left (on stop).
    mutating func drain() -> Data? {
        guard !pending.isEmpty else { return nil }
        let out = FrameBatcher.bytes(pending)
        pending.removeAll(keepingCapacity: true)
        return out
    }

    static func bytes(_ s: [Int16]) -> Data {
        var d = Data(capacity: s.count * 2)
        for v in s {
            let u = UInt16(bitPattern: v.littleEndian)
            d.append(UInt8(truncatingIfNeeded: u))
            d.append(UInt8(truncatingIfNeeded: u >> 8))
        }
        return d
    }
}

/// One-shot level diagnostic over the first `window` seconds of a channel:
/// all-zero input with buffers flowing tells the log what is wrong instead of
/// a silent channel (taps: nothing playing, or System Audio Recording not
/// allowed, which macOS answers with silence rather than an error; mic: the
/// built-in mic in clamshell mode, or no Microphone grant).
struct LevelProbe {
    let window: Double
    private(set) var seconds = 0.0
    private(set) var peak: Float = 0
    private(set) var done = false

    init(window: Double) { self.window = window }

    /// Returns the peak once, when the window completes.
    mutating func feed(_ mono: [Float], rate: Double) -> Float? {
        guard !done, rate > 0 else { return nil }
        for v in mono { peak = max(peak, abs(v)) }
        seconds += Double(mono.count) / rate
        if seconds >= window {
            done = true
            return peak
        }
        return nil
    }
}
