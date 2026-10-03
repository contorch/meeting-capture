// `sysaudio transcribe`: on-device speech-to-text with SpeechAnalyzer +
// SpeechTranscriber (macOS 26+, Apple silicon). It only reads an audio FILE.
// It never touches ScreenCaptureKit, the microphone or any other TCC-gated API,
// and it never asks for Speech Recognition authorization (SpeechAnalyzer does
// not use that gate).
//
// meeting_capture (Python) depends on this contract. Change both sides together.
//
//   sysaudio transcribe --probe [--locale L]
//     stdout: {"available","reason","os","arch","locale","installed",
//              "supported":[...],"installed_locales":[...]}
//     exit 0 usable now | 69 unusable (macOS < 26, Intel, locale unsupported)
//          | 75 supported, but the model is not installed
//
//   sysaudio transcribe --install [--locale L]
//     stdout: {"installed","locale","seconds"} (plus "reason" when it fails).
//     Download progress goes to stderr.
//     exit 0 ok | 69 unsupported | 1 other error
//
//   sysaudio transcribe [--locale L] FILE
//     stdout: {"text","segments":[{"start","end","text","confidence"}],"locale","ms"}
//     exit 0 ok (no speech gives text "") | 69 unavailable (OS/arch/locale)
//          | 75 model missing or released | 70 FILE unreadable or undecodable
//          | 1 any other error. Nothing goes to stdout on failure.
//
// The default locale is en-US. Diagnostics go to stderr only. Usage errors
// exit 1 and never print "unknown arg": Python reads that phrase as "this
// sysaudio predates `transcribe`".

import Foundation
import AVFoundation
import CoreMedia
#if compiler(>=6.2) && canImport(Speech)
// SpeechAnalyzer/SpeechTranscriber/AssetInventory arrived in the macOS 26 SDK
// (Swift 6.2, Xcode 26). An older toolchain still builds a working capture
// binary; its `transcribe` reports "unavailable" (exit 69). Speech.framework
// itself exists on macOS 13, and every macOS 26 symbol is weak-linked through
// @available, so the binary still launches on the macOS 13 deployment target.
import Speech
#endif

let TRANSCRIBE_EX_OK: Int32 = 0
let TRANSCRIBE_EX_FAILURE: Int32 = 1
let TRANSCRIBE_EX_UNAVAILABLE: Int32 = 69 // EX_UNAVAILABLE
let TRANSCRIBE_EX_BAD_AUDIO: Int32 = 70   // EX_SOFTWARE, used here for "this file"
let TRANSCRIBE_EX_NO_MODEL: Int32 = 75    // EX_TEMPFAIL: install the model, then retry

func transcribeLog(_ s: String) {
    logErr("sysaudio transcribe: \(s)")
}

/// Writes exactly one JSON line to stdout.
func transcribeEmit(_ obj: [String: Any]) {
    guard var data = try? JSONSerialization.data(
        withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes]
    ) else {
        transcribeLog("could not encode the JSON result")
        return
    }
    data.append(0x0A)
    FileHandle.standardOutput.write(data)
}

/// A JSON number rounded to `places` decimals. JSONSerialization writes a raw
/// Double with 17 significant digits (0.808 comes out as 0.80800000000000005);
/// a decimal number prints as written.
func jsonNumber(_ x: Double, places: Int = 3) -> NSDecimalNumber {
    guard x.isFinite else { return NSDecimalNumber.zero }
    return NSDecimalNumber(string: String(format: "%.\(places)f", locale: Locale(identifier: "en_US_POSIX"), x))
}

enum TranscribeMode {
    case probe
    case install
    case file(String)
}

enum TranscribeCommand {
    static let usage = """
    Usage: sysaudio transcribe [--locale L] FILE       transcribe an audio file on this Mac
           sysaudio transcribe --probe [--locale L]    can this Mac transcribe locale L now?
           sysaudio transcribe --install [--locale L]  download and reserve the model for L
      --locale L   BCP-47 locale such as en-US, en-IN, hi-IN or de-DE (default en-US)
    On-device speech-to-text (SpeechAnalyzer, macOS 26+ on Apple silicon); no network
    is used except to download a language model. Prints one JSON line on stdout.
    Exit codes: 0 ok, 69 unavailable on this Mac or locale, 75 model not installed,
                70 audio file unreadable, 1 other error.
    """

    static func run(_ argv: [String]) async -> Int32 {
        var locale = "en-US"
        var probe = false
        var install = false
        var files: [String] = []
        var args = argv[...]
        while let a = args.popFirst() {
            switch a {
            case "--locale":
                guard let v = args.popFirst() else { return usageError("--locale needs a value") }
                locale = v
            case _ where a.hasPrefix("--locale="):
                locale = String(a.dropFirst("--locale=".count))
            case "--probe":
                probe = true
            case "--install":
                install = true
            case "-h", "--help":
                print(usage)
                return TRANSCRIBE_EX_OK
            case "--":
                files.append(contentsOf: args)
                args = []
            default:
                if a.hasPrefix("-") && a != "-" { return usageError("unrecognised option \(a)") }
                files.append(a)
            }
        }

        let mode: TranscribeMode
        switch (probe, install, files.count) {
        case (true, false, 0): mode = .probe
        case (false, true, 0): mode = .install
        case (false, false, 1): mode = .file(files[0])
        case (true, true, _): return usageError("--probe and --install are exclusive")
        case (false, false, 0): return usageError("give an audio FILE, --probe or --install")
        case (false, false, _): return usageError("give exactly one audio FILE")
        default: return usageError("--probe and --install take no FILE")
        }

        if let reason = platformBlocker() {
            return unavailable(mode, locale: locale, reason: reason)
        }
        #if compiler(>=6.2) && canImport(Speech)
        if #available(macOS 26.0, *) {
            return await OnDeviceTranscriber.run(mode, requested: locale)
        }
        return unavailable(
            mode, locale: locale,
            reason: "on-device transcription needs macOS 26 or later (this Mac runs macOS \(osVersion()))"
        )
        #else
        return unavailable(
            mode, locale: locale,
            reason: "this sysaudio was built without the macOS 26 SDK, so it cannot transcribe "
                + "on-device (rebuild it with Xcode 26 or later)"
        )
        #endif
    }

    static func usageError(_ msg: String) -> Int32 {
        transcribeLog("\(msg) (see: sysaudio transcribe --help)")
        return TRANSCRIBE_EX_FAILURE
    }

    /// Reports "cannot run here" in the shape each mode promises, exit 69.
    static func unavailable(_ mode: TranscribeMode, locale: String, reason: String) -> Int32 {
        switch mode {
        case .probe:
            transcribeEmit(probeJSON(available: false, reason: reason, locale: locale,
                                     installed: false, supported: [], installedLocales: []))
        case .install:
            transcribeEmit(["installed": false, "locale": locale, "seconds": jsonNumber(0, places: 2), "reason": reason])
        case .file:
            transcribeLog(reason)
        }
        return TRANSCRIBE_EX_UNAVAILABLE
    }

    static func probeJSON(available: Bool, reason: String, locale: String, installed: Bool,
                          supported: [String], installedLocales: [String]) -> [String: Any] {
        [
            "available": available,
            "reason": reason,
            "os": osVersion(),
            "arch": processArch(),
            "locale": locale,
            "installed": installed,
            "supported": supported,
            "installed_locales": installedLocales,
        ]
    }

    /// Hardware or OS reasons that rule transcription out before any Speech call.
    static func platformBlocker() -> String? {
        if !sysctlFlag("hw.optional.arm64") {
            return "on-device transcription needs an Apple silicon Mac (this one is Intel)"
        }
        return nil
    }

    static func osVersion() -> String {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        return v.patchVersion > 0
            ? "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
            : "\(v.majorVersion).\(v.minorVersion)"
    }

    static func processArch() -> String {
        #if arch(arm64)
        return "arm64"
        #elseif arch(x86_64)
        return "x86_64"
        #else
        return "unknown"
        #endif
    }

    static func runningUnderRosetta() -> Bool { sysctlFlag("sysctl.proc_translated") }

    static func sysctlFlag(_ name: String) -> Bool {
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        return sysctlbyname(name, &value, &size, nil, 0) == 0 && value == 1
    }
}

#if compiler(>=6.2) && canImport(Speech)

/// Audio that could not be opened, read or converted: exit 70.
struct AudioFileError: Error, CustomStringConvertible {
    let description: String
    init(_ d: String) { description = d }
}

/// Lets a value that is only touched by one task at a time cross into it.
final class UncheckedBox<T>: @unchecked Sendable {
    let value: T
    init(_ v: T) { value = v }
}

@available(macOS 26.0, *)
enum OnDeviceTranscriber {
    // SFSpeechErrorDomain codes. Numbers, not SFSpeechError.Code names, so the
    // same source builds against every macOS 26.x SDK.
    static let speechErrorDomain = "SFSpeechErrorDomain"
    static let modelErrorCodes: Set<Int> = [
        4,  // noModel: not installed, or its reservation was released
        10, // assetLocaleNotAllocated
        11, // tooManyAssetLocalesAllocated
        16, // insufficientResources
    ]
    static let unsupportedLocaleCode = 15 // cannotAllocateUnsupportedLocale
    static let audioErrorCodes: Set<Int> = [
        2, // audioReadFailed
        3, // unexpectedAudioFormat
        5, // incompatibleAudioFormats
    ]

    struct Resolution {
        var available: Bool
        var reason: String
        var locale: Locale?
        var localeID: String
        var installed: Bool
        var supported: [String]
        var installedLocales: [String]
    }

    static func run(_ mode: TranscribeMode, requested: String) async -> Int32 {
        switch mode {
        case .probe: return await probe(requested)
        case .install: return await install(requested)
        case .file(let path): return await transcribe(path: path, requested: requested)
        }
    }

    static func bcp47(_ l: Locale) -> String { l.identifier(.bcp47) }

    /// The `.transcription` preset plus word timings and confidences. The
    /// preset alone carries no audioTimeRange.
    static func makeTranscriber(_ locale: Locale) -> SpeechTranscriber {
        let preset = SpeechTranscriber.Preset.transcription
        return SpeechTranscriber(
            locale: locale,
            transcriptionOptions: preset.transcriptionOptions,
            reportingOptions: preset.reportingOptions,
            attributeOptions: preset.attributeOptions.union([.audioTimeRange, .transcriptionConfidence])
        )
    }

    /// Whether the requested locale can be transcribed here and now.
    /// "Installed" means SpeechTranscriber.installedLocales lists it.
    /// AssetInventory.status() is not used: it says .supported for a model
    /// that is on disk but not reserved by this process.
    static func resolve(_ requested: String) async -> Resolution {
        var r = Resolution(available: false, reason: "", locale: nil, localeID: requested,
                           installed: false, supported: [], installedLocales: [])
        guard SpeechTranscriber.isAvailable else {
            r.reason = TranscribeCommand.runningUnderRosetta()
                ? "on-device transcription is unavailable under Rosetta (run the arm64 sysaudio)"
                : "on-device transcription (SpeechTranscriber) is not available on this Mac"
            return r
        }
        r.supported = await SpeechTranscriber.supportedLocales.map(bcp47).sorted()
        r.installedLocales = await SpeechTranscriber.installedLocales.map(bcp47).sorted()
        guard !requested.isEmpty,
              let loc = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: requested))
        else {
            r.reason = "locale \(requested.isEmpty ? "\"\"" : requested) is not supported for on-device transcription"
            return r
        }
        r.available = true
        r.locale = loc
        r.localeID = bcp47(loc)
        r.installed = r.installedLocales.contains(r.localeID)
        r.reason = r.installed
            ? "the on-device model for \(r.localeID) is installed"
            : "the on-device model for \(r.localeID) is not installed yet "
                + "(download it with: meeting-capture language \(r.localeID))"
        return r
    }

    // MARK: --probe

    static func probe(_ requested: String) async -> Int32 {
        let r = await resolve(requested)
        transcribeEmit(TranscribeCommand.probeJSON(
            available: r.available, reason: r.reason, locale: r.localeID, installed: r.installed,
            supported: r.supported, installedLocales: r.installedLocales))
        if !r.available { return TRANSCRIBE_EX_UNAVAILABLE }
        return r.installed ? TRANSCRIBE_EX_OK : TRANSCRIBE_EX_NO_MODEL
    }

    // MARK: --install

    static func install(_ requested: String) async -> Int32 {
        let t0 = Date()
        let r = await resolve(requested)
        func result(_ installed: Bool, _ reason: String? = nil) -> [String: Any] {
            var d: [String: Any] = [
                "installed": installed,
                "locale": r.localeID,
                "seconds": jsonNumber(Date().timeIntervalSince(t0), places: 2),
            ]
            if let reason { d["reason"] = reason }
            return d
        }
        guard r.available, let loc = r.locale else {
            transcribeEmit(result(false, r.reason))
            return TRANSCRIBE_EX_UNAVAILABLE
        }

        // Hold a reservation for the locale. A model this process installs
        // stays usable only while a reservation holds it. Once it is released,
        // the analyzer fails with SFSpeechErrorDomain code 4 and macOS may
        // purge the files.
        await reserve(loc)

        do {
            let transcriber = makeTranscriber(loc)
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                transcribeLog("downloading the on-device speech model for \(r.localeID)…")
                let progress = request.progress
                let localeID = r.localeID
                let ticker = Task {
                    var last = -1
                    while !Task.isCancelled {
                        let pct = Int(progress.fractionCompleted * 100)
                        if pct != last {
                            transcribeLog("\(localeID) model download \(pct)%")
                            last = pct
                        }
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                }
                defer { ticker.cancel() }
                try await request.downloadAndInstall()
                transcribeLog("\(r.localeID) model download 100%, installed")
            }
        } catch {
            let code = exitCode(for: error)
            let why = "installing the \(r.localeID) model failed: \(describe(error))"
            transcribeLog(why)
            transcribeEmit(result(false, why))
            return code == TRANSCRIBE_EX_UNAVAILABLE ? TRANSCRIBE_EX_UNAVAILABLE : TRANSCRIBE_EX_FAILURE
        }

        let nowInstalled = await SpeechTranscriber.installedLocales.map(bcp47).contains(r.localeID)
        guard nowInstalled else {
            let why = "the \(r.localeID) model install finished, but macOS does not list it as installed"
            transcribeLog(why)
            transcribeEmit(result(false, why))
            return TRANSCRIBE_EX_FAILURE
        }
        transcribeEmit(result(true))
        return TRANSCRIBE_EX_OK
    }

    /// Reserves `loc` for this app (bundle id com.contorch.meeting-capture.sysaudio).
    /// macOS allows AssetInventory.maximumReservedLocales reservations per app.
    /// At the limit, older reservations are released one at a time until this
    /// one fits. meeting-capture transcribes one language at a time, so a
    /// language switch makes the old model unnecessary.
    static func reserve(_ loc: Locale) async {
        let target = bcp47(loc)
        for _ in 0...max(1, AssetInventory.maximumReservedLocales) {
            do {
                if try await AssetInventory.reserve(locale: loc) {
                    transcribeLog("reserved the \(target) model for this app")
                }
                return
            } catch {
                let ns = error as NSError
                guard ns.domain == speechErrorDomain, ns.code == 11 /* tooManyAssetLocalesAllocated */ else {
                    transcribeLog("could not reserve \(target) (\(describe(error))); installing anyway")
                    return
                }
                let others = await AssetInventory.reservedLocales.filter { bcp47($0) != target }
                guard let victim = others.first else {
                    transcribeLog("reservation limit reached and nothing to release; installing anyway")
                    return
                }
                transcribeLog("reservation limit (\(AssetInventory.maximumReservedLocales)) reached: "
                              + "releasing \(bcp47(victim)) to make room for \(target)")
                await AssetInventory.release(reservedLocale: victim)
            }
        }
    }

    // MARK: FILE

    static func transcribe(path: String, requested: String) async -> Int32 {
        let t0 = Date()
        let r = await resolve(requested)
        guard r.available, let loc = r.locale else {
            transcribeLog(r.reason)
            return TRANSCRIBE_EX_UNAVAILABLE
        }
        // Diagnostics only: skip the installedLocales pre-check so the analyzer
        // itself reports the missing model (SFSpeechErrorDomain code 4 -> 75).
        let skipCheck = ProcessInfo.processInfo.environment["SYSAUDIO_TRANSCRIBE_SKIP_INSTALLED_CHECK"] == "1"
        if !r.installed && !skipCheck {
            transcribeLog(r.reason)
            return TRANSCRIBE_EX_NO_MODEL
        }

        let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), !isDir.boolValue else {
            transcribeLog("no such audio file: \(path)")
            return TRANSCRIBE_EX_BAD_AUDIO
        }
        let file: AVAudioFile
        do {
            file = try AVAudioFile(forReading: url)
        } catch {
            transcribeLog("cannot read audio from \(path): \(describe(error))")
            return TRANSCRIBE_EX_BAD_AUDIO
        }

        let transcriber = makeTranscriber(loc)
        let modules: [any SpeechModule] = [transcriber]
        var bestFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: modules, considering: file.processingFormat
        )
        if bestFormat == nil && skipCheck {
            // No installed model means no format. For diagnostics, carry on
            // with 16 kHz mono int16 so the analyzer reports its own error.
            bestFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                       channels: 1, interleaved: true)
        }
        guard let format = bestFormat else {
            transcribeLog("the \(r.localeID) model offers no usable audio format (is the model installed?)")
            return TRANSCRIBE_EX_NO_MODEL
        }

        let collector = Task { () throws -> [Segment] in
            var segments: [Segment] = []
            for try await result in transcriber.results where result.isFinal {
                if let seg = segment(from: result) { segments.append(seg) }
            }
            return segments
        }

        let analyzer = SpeechAnalyzer(modules: modules)
        do {
            try await analyzer.prepareToAnalyze(in: format)
            let (inputs, cont) = AsyncStream<AnalyzerInput>.makeStream()
            let box = UncheckedBox(file)
            let feeder = Task { () throws in
                defer { cont.finish() }
                try feed(box.value, as: format, into: cont)
            }
            let last = try await analyzer.analyzeSequence(inputs)
            try await feeder.value
            let segments: [Segment]
            if let last {
                try await analyzer.finalizeAndFinish(through: last)
                segments = try await collector.value
            } else {
                // No audio reached the analyzer (a header-only file). That
                // means no speech, not an error. Cancelling ends the results
                // stream with CancellationError, so don't wait on it.
                await analyzer.cancelAndFinishNow()
                collector.cancel()
                segments = []
            }
            transcribeEmit([
                "text": segments.map(\.text).joined(separator: " "),
                "segments": segments.map(\.json),
                "locale": r.localeID,
                "ms": Int(Date().timeIntervalSince(t0) * 1000),
            ])
            return TRANSCRIBE_EX_OK
        } catch {
            collector.cancel()
            let code = exitCode(for: error)
            transcribeLog("transcribing \(path) as \(r.localeID) failed (exit \(code)): \(describe(error))")
            return code
        }
    }

    /// Reads `file` in ~1 s chunks, converts each to the analyzer's `format`
    /// and yields it. The buffers are contiguous, so result times count from
    /// the start of the file.
    static func feed(_ file: AVAudioFile, as format: AVAudioFormat,
                     into cont: AsyncStream<AnalyzerInput>.Continuation) throws {
        let inFmt = file.processingFormat
        guard let conv = AVAudioConverter(from: inFmt, to: format) else {
            throw AudioFileError("cannot convert \(inFmt) to \(format)")
        }
        conv.downmix = inFmt.channelCount > format.channelCount
        conv.primeMethod = .none
        let chunk = AVAudioFrameCount(max(1024, inFmt.sampleRate))
        let outCapacity = AVAudioFrameCount(
            (Double(chunk) * format.sampleRate / inFmt.sampleRate).rounded(.up)
        ) + 4096
        var eof = false
        while true {
            var inBuf: AVAudioPCMBuffer?
            if !eof {
                if file.framePosition >= file.length {
                    eof = true
                } else {
                    guard let b = AVAudioPCMBuffer(pcmFormat: inFmt, frameCapacity: chunk) else {
                        throw AudioFileError("cannot allocate a \(inFmt) buffer")
                    }
                    do {
                        try file.read(into: b, frameCount: chunk)
                    } catch {
                        throw AudioFileError("read failed at frame \(file.framePosition): \(describe(error))")
                    }
                    if b.frameLength == 0 { eof = true } else { inBuf = b }
                }
            }
            guard let outBuf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: outCapacity) else {
                throw AudioFileError("cannot allocate a \(format) buffer")
            }
            var supplied = false
            var convError: NSError?
            let atEOF = eof
            let status = conv.convert(to: outBuf, error: &convError) { _, inputStatus in
                if let b = inBuf, !supplied {
                    supplied = true
                    inputStatus.pointee = .haveData
                    return b
                }
                inputStatus.pointee = atEOF ? .endOfStream : .noDataNow
                return nil
            }
            if status == .error {
                throw AudioFileError("audio conversion failed: \(convError.map(describe) ?? "unknown error")")
            }
            if outBuf.frameLength > 0 { cont.yield(AnalyzerInput(buffer: outBuf)) }
            if status == .endOfStream || (atEOF && outBuf.frameLength == 0) { break }
        }
    }

    struct Segment: Sendable {
        let start: Double
        let end: Double
        let text: String
        let confidence: Double

        var json: [String: Any] {
            [
                "start": jsonNumber(start),
                "end": jsonNumber(end),
                "text": text,
                "confidence": jsonNumber(confidence),
            ]
        }
    }

    /// One finalized result as a segment, or nil when it has no text. The
    /// engine finalizes some empty results over noise.
    static func segment(from result: SpeechTranscriber.Result) -> Segment? {
        let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        var confidences: [Double] = []
        var wordStart: Double?
        var wordEnd: Double?
        for run in result.text.runs {
            let word = String(result.text[run.range].characters)
            if word.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { continue }
            if let c = run[AttributeScopes.SpeechAttributes.ConfidenceAttribute.self] {
                confidences.append(c)
            }
            if let tr = run[AttributeScopes.SpeechAttributes.TimeRangeAttribute.self] {
                let s = seconds(tr.start), e = seconds(CMTimeRangeGetEnd(tr))
                if let s { wordStart = min(wordStart ?? s, s) }
                if let e { wordEnd = max(wordEnd ?? e, e) }
            }
        }
        let start = seconds(result.range.start) ?? wordStart ?? 0
        let end = seconds(CMTimeRangeGetEnd(result.range)) ?? wordEnd ?? start
        // Mean word confidence. When the engine scored no word, report 1.0
        // (unknown counts as trusted) so a confidence filter downstream never
        // drops speech just because it went unscored.
        let confidence = confidences.isEmpty
            ? 1.0
            : confidences.reduce(0, +) / Double(confidences.count)
        return Segment(start: start, end: max(end, start), text: text, confidence: confidence)
    }

    static func seconds(_ t: CMTime) -> Double? {
        guard t.isValid, t.isNumeric else { return nil }
        let s = CMTimeGetSeconds(t)
        return s.isFinite ? s : nil
    }

    /// Maps an error to the contract's exit code, looking through underlying errors.
    static func exitCode(for error: Error) -> Int32 {
        if error is AudioFileError { return TRANSCRIBE_EX_BAD_AUDIO }
        var current: NSError? = error as NSError
        var depth = 0
        while let ns = current, depth < 8 {
            if ns.domain == speechErrorDomain {
                if modelErrorCodes.contains(ns.code) { return TRANSCRIBE_EX_NO_MODEL }
                if ns.code == unsupportedLocaleCode { return TRANSCRIBE_EX_UNAVAILABLE }
                if audioErrorCodes.contains(ns.code) { return TRANSCRIBE_EX_BAD_AUDIO }
            }
            current = ns.userInfo[NSUnderlyingErrorKey] as? NSError
            depth += 1
        }
        return TRANSCRIBE_EX_FAILURE
    }

    static func describe(_ error: Error) -> String {
        if let e = error as? AudioFileError { return e.description }
        let ns = error as NSError
        var code = "\(ns.code)"
        // Core Audio errors are four-char codes ('typ?' = not an audio file).
        if ns.code > 0x2020_2020, ns.code <= 0x7E7E_7E7E {
            let bytes = (0..<4).map { UInt8((ns.code >> (24 - 8 * $0)) & 0xFF) }
            if bytes.allSatisfy({ $0 >= 0x20 && $0 < 0x7F }), let s = String(bytes: bytes, encoding: .ascii) {
                code += " '\(s)'"
            }
        }
        return "\(ns.domain) code \(code): \(ns.localizedDescription)"
    }
}

#endif
