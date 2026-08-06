// sysaudio: capture system audio (and optionally the microphone) via ScreenCaptureKit.
// macOS 13+ for system audio; --mic needs macOS 15+ (SCK microphone capture).
// Requires Screen Recording TCC permission; --mic additionally requires the
// Microphone TCC permission (both user-grantable, attributed to the parent
// terminal/launcher, no admin).
//
// Output contract on stdout:
//   default:  raw int16 LE mono PCM, system audio only (audiotee-compatible).
//   --mic:    framed. Each frame is 1 tag byte ('S' system | 'M' microphone),
//             a 4-byte little-endian payload length, then the payload
//             (int16 LE mono PCM at --sample-rate). Framing is in effect
//             whenever --mic was passed, even if mic setup later fell back,
//             so the reader's protocol never depends on runtime permission state.
//
// If mic capture fails to start (missing permission, older macOS), sysaudio
// logs to stderr and continues with system audio only — a meeting is never
// lost to a missing mic grant.
//
// Usage: sysaudio [--sample-rate N] [--mic]
//   --sample-rate  output sample rate in Hz (default 16000)
//   --mic          also capture the default microphone as a second channel

import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

let FRAME_TAG_SYSTEM: UInt8 = 0x53 // 'S'
let FRAME_TAG_MIC: UInt8 = 0x4D    // 'M'

func logErr(_ s: String) {
    if let d = (s + "\n").data(using: .utf8) { FileHandle.standardError.write(d) }
}

/// Serializes PCM writes from the system-audio and mic callback queues onto
/// stdout, framing each payload when two channels share the pipe.
final class PCMWriter {
    private let queue = DispatchQueue(label: "sysaudio.out")
    private let stdout = FileHandle.standardOutput
    let framed: Bool

    init(framed: Bool) { self.framed = framed }

    func write(tag: UInt8, payload: Data) {
        guard !payload.isEmpty else { return }
        queue.async { [self] in
            if framed {
                var frame = Data(capacity: payload.count + 5)
                frame.append(tag)
                var len = UInt32(payload.count).littleEndian
                withUnsafeBytes(of: &len) { frame.append(contentsOf: $0) }
                frame.append(payload)
                try? stdout.write(contentsOf: frame)
            } else {
                try? stdout.write(contentsOf: payload)
            }
        }
    }
}

@available(macOS 13.0, *)
final class AudioCaptureHandler: NSObject, SCStreamDelegate, SCStreamOutput {
    private let writer: PCMWriter

    init(writer: PCMWriter) {
        self.writer = writer
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, sampleBuffer.numSamples > 0 else { return }

        var blockBufferOut: CMBlockBuffer?
        var ablPtr = AudioBufferList()
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &ablPtr,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBufferOut
        )
        guard status == 0, let baseAddr = ablPtr.mBuffers.mData else { return }

        let byteCount = Int(ablPtr.mBuffers.mDataByteSize)
        let floatCount = byteCount / MemoryLayout<Float>.size
        let floatPtr = baseAddr.assumingMemoryBound(to: Float.self)

        // SCK delivers Float32 PCM. Convert to Int16 LE and hand to the writer.
        var int16Bytes = [UInt8]()
        int16Bytes.reserveCapacity(floatCount * 2)
        for i in 0..<floatCount {
            var f = floatPtr[i]
            if f > 1.0 { f = 1.0 } else if f < -1.0 { f = -1.0 }
            let sample = Int16(f * 32767.0)
            int16Bytes.append(UInt8(truncatingIfNeeded: Int(sample) & 0xFF))
            int16Bytes.append(UInt8(truncatingIfNeeded: (Int(sample) >> 8) & 0xFF))
        }
        writer.write(tag: FRAME_TAG_SYSTEM, payload: Data(int16Bytes))
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        logErr("stream stopped with error: \(error)")
        exit(2)
    }
}

// Mic buffers arrive at the device's native format (typically 48 kHz Float32,
// sometimes multi-channel), NOT at SCStreamConfiguration.sampleRate — so we
// convert each buffer to 16 kHz mono int16 with AVAudioConverter, which keeps
// resampler state across calls.
@available(macOS 15.0, *)
final class MicCaptureHandler: NSObject, SCStreamOutput {
    private let writer: PCMWriter
    private let dstFormat: AVAudioFormat
    private var converter: AVAudioConverter?
    private var srcFormat: AVAudioFormat?
    private var loggedFirstBuffer = false
    // One-shot input-level diagnostic: all-zero mic input with frames flowing
    // means the mic is delivering silence — most commonly a MacBook in
    // clamshell mode (closed lid hardware-disables the built-in mic while it
    // keeps enumerating), otherwise a missing/broken TCC grant. Log it so
    // daemon.log tells the story instead of a silent "me" channel.
    private var diagBuffers = 0
    private var diagPeak: Float = 0

    init(writer: PCMWriter, outputSampleRate: Int) {
        self.writer = writer
        self.dstFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: Double(outputSampleRate),
            channels: 1,
            interleaved: true
        )!
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .microphone, sampleBuffer.isValid, sampleBuffer.numSamples > 0 else { return }
        guard let desc = CMSampleBufferGetFormatDescription(sampleBuffer) else { return }
        let inFormat = AVAudioFormat(cmAudioFormatDescription: desc)

        if !loggedFirstBuffer {
            loggedFirstBuffer = true
            logErr("sysaudio: mic frames flowing (\(Int(inFormat.sampleRate)) Hz, \(inFormat.channelCount) ch)")
        }

        let frames = AVAudioFrameCount(sampleBuffer.numSamples)
        guard let inBuf = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: frames) else { return }
        inBuf.frameLength = frames
        let copyStatus = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(frames), into: inBuf.mutableAudioBufferList
        )
        guard copyStatus == noErr else { return }

        if diagBuffers >= 0 {
            var peak: Float = 0
            let n = Int(inBuf.frameLength)
            if let f = inBuf.floatChannelData {
                for c in 0..<Int(inFormat.channelCount) {
                    for i in 0..<n { peak = max(peak, abs(f[c][i])) }
                }
            } else if let s = inBuf.int16ChannelData {
                for c in 0..<Int(inFormat.channelCount) {
                    for i in 0..<n { peak = max(peak, abs(Float(s[c][i])) / 32768.0) }
                }
            }
            diagPeak = max(diagPeak, peak)
            diagBuffers += 1
            if diagBuffers >= 300 {  // ~5s of 48kHz mic buffers
                logErr(String(format: "sysaudio: mic input peak over first %d buffers: %.5f%@",
                              diagBuffers, diagPeak,
                              diagPeak == 0
                                ? " — ALL ZERO (built-in mic dead in clamshell mode? or mic TCC grant missing)"
                                : ""))
                diagBuffers = -1
            }
        }

        if converter == nil || srcFormat != inFormat {
            converter = AVAudioConverter(from: inFormat, to: dstFormat)
            srcFormat = inFormat
        }
        guard let converter else { return }

        let capacity = AVAudioFrameCount(
            (Double(frames) * dstFormat.sampleRate / inFormat.sampleRate).rounded(.up)
        ) + 64
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: dstFormat, frameCapacity: capacity) else { return }

        var fed = false
        var convError: NSError?
        converter.convert(to: outBuf, error: &convError) { _, inputStatus in
            if fed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            fed = true
            inputStatus.pointee = .haveData
            return inBuf
        }
        guard convError == nil else { return }

        let n = Int(outBuf.frameLength)
        guard n > 0, let ch = outBuf.int16ChannelData else { return }
        writer.write(tag: FRAME_TAG_MIC, payload: Data(bytes: ch[0], count: n * 2))
    }
}

@available(macOS 13.0, *)
func run(sampleRate: Int, wantMic: Bool) async throws {
    logErr("sysaudio: requesting shareable content...")
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
    guard let display = content.displays.first else {
        logErr("sysaudio: no displays found")
        exit(1)
    }
    logErr("sysaudio: using display \(display.displayID), \(content.applications.count) apps visible")

    // Filter: this display, no excluded apps, no excluded windows.
    // Audio-wise this captures ALL system audio.
    let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

    let writer = PCMWriter(framed: wantMic)
    // Keep handlers referenced for the life of the process — SCStream's
    // retention of its outputs is not documented, so don't rely on it.
    var retainedHandlers: [NSObject] = []
    let audioQueue = DispatchQueue(label: "sysaudio.audio", qos: .userInteractive)
    let videoQueue = DispatchQueue(label: "sysaudio.video", qos: .background)
    let micQueue = DispatchQueue(label: "sysaudio.mic", qos: .userInteractive)

    func buildAndStart(mic: Bool) async throws -> SCStream {
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = sampleRate
        config.channelCount = 1
        config.excludesCurrentProcessAudio = true
        // SCK requires a video config even for audio-only. Use minimum and very slow frame rate.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 5

        let handler = AudioCaptureHandler(writer: writer)
        retainedHandlers.append(handler)

        if mic {
            guard #available(macOS 15.0, *) else {
                throw NSError(domain: "sysaudio", code: 3, userInfo: [
                    NSLocalizedDescriptionKey: "--mic requires macOS 15+ (SCK microphone capture)"
                ])
            }
            config.captureMicrophone = true
        }

        let stream = SCStream(filter: filter, configuration: config, delegate: handler)
        try stream.addStreamOutput(handler, type: .audio, sampleHandlerQueue: audioQueue)
        // Some SCK versions require a video output handler too even if we ignore frames.
        try stream.addStreamOutput(handler, type: .screen, sampleHandlerQueue: videoQueue)

        if mic, #available(macOS 15.0, *) {
            let micHandler = MicCaptureHandler(writer: writer, outputSampleRate: sampleRate)
            retainedHandlers.append(micHandler)
            try stream.addStreamOutput(micHandler, type: .microphone, sampleHandlerQueue: micQueue)
        }

        try await stream.startCapture()
        return stream
    }

    var stream: SCStream
    var micActive = wantMic

    // SCK delivers NO mic buffers (and no error) when the Microphone TCC
    // permission is missing — and never triggers the prompt itself. Check
    // and request explicitly so the first --mic run pops the system dialog
    // and a denial degrades loudly to system-audio-only.
    if wantMic {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            break
        case .notDetermined:
            logErr("sysaudio: requesting microphone permission (watch for the macOS prompt)...")
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            if !granted {
                logErr("sysaudio: microphone permission denied — continuing with system audio only")
                micActive = false
            }
        default:
            logErr(
                "sysaudio: microphone permission denied/restricted — continuing with system "
                + "audio only. Grant it under System Settings → Privacy & Security → Microphone "
                + "(to the parent terminal/launcher), then restart the daemon."
            )
            micActive = false
        }
    }

    if micActive {
        do {
            stream = try await buildAndStart(mic: true)
        } catch {
            logErr("sysaudio: mic capture failed to start (\(error)) — falling back to system audio only")
            micActive = false
            retainedHandlers.removeAll()
            stream = try await buildAndStart(mic: false)
        }
    } else {
        stream = try await buildAndStart(mic: false)
    }
    _ = stream

    let mode = micActive ? "system+mic, framed" : (wantMic ? "system only (mic fallback), framed" : "system only, raw")
    logErr("sysaudio: stream started (sample rate \(sampleRate), int16 LE, \(mode)), piping PCM to stdout")

    // Run forever; SIGTERM/SIGINT will exit the process.
    while true {
        try await Task.sleep(nanoseconds: 1_000_000_000)
    }
}

@available(macOS 13.0, *)
@main
struct SysAudio {
    static func main() async {
        var sampleRate = 16000
        var wantMic = false
        var args = Array(CommandLine.arguments.dropFirst())
        while !args.isEmpty {
            let a = args.removeFirst()
            switch a {
            case "--sample-rate":
                if let n = args.first.flatMap(Int.init) { sampleRate = n; args.removeFirst() }
            case "--mic":
                wantMic = true
            case "-h", "--help":
                print("Usage: sysaudio [--sample-rate N] [--mic]")
                print("  --sample-rate  output sample rate in Hz (default 16000)")
                print("  --mic          also capture the default microphone (framed output, macOS 15+)")
                exit(0)
            default:
                FileHandle.standardError.write("unknown arg: \(a)\n".data(using: .utf8)!)
                exit(1)
            }
        }

        signal(SIGTERM, SIG_DFL)
        signal(SIGINT, SIG_DFL)

        do {
            try await run(sampleRate: sampleRate, wantMic: wantMic)
        } catch {
            FileHandle.standardError.write("sysaudio error: \(error)\n".data(using: .utf8)!)
            exit(1)
        }
    }
}
