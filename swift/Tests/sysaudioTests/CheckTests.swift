// `swift test` (from swift/). Pure logic only: nothing here reads or requests
// a permission.
import AVFoundation
import Foundation
import Testing
@testable import sysaudio

@Suite("sysaudio check: argument parsing and the contract's words")
struct CheckTests {
    @Test("no arguments: report as text, ask for nothing")
    func bare() {
        #expect(CheckCommand.parse([]) == .run(CheckOptions(json: false, request: nil)))
    }

    @Test("--json and --request in either spelling")
    func flags() {
        #expect(CheckCommand.parse(["--json"]) == .run(CheckOptions(json: true, request: nil)))
        #expect(CheckCommand.parse(["--json", "--request", "screen"]) == .run(CheckOptions(json: true, request: .screen)))
        #expect(CheckCommand.parse(["--request=mic", "--json"]) == .run(CheckOptions(json: true, request: .mic)))
    }

    @Test("usage errors never mention 'unknown arg'")
    func usageErrors() {
        for argv in [["--request"], ["--request", "camera"], ["--request=x"], ["--bogus"],
                     ["--request", "mic", "--request", "screen"], ["--sample-rate", "16000"]] {
            guard case .usage(let msg) = CheckCommand.parse(argv) else {
                Issue.record("\(argv) should be a usage error")
                continue
            }
            #expect(!msg.contains("unknown arg"))
        }
        #expect(CheckCommand.parse(["-h"]) == .help)
    }

    @Test("microphone statuses map to the contract's words")
    func micWords() {
        #expect(CheckCommand.micWord(.authorized) == "granted")
        #expect(CheckCommand.micWord(.denied) == "denied")
        #expect(CheckCommand.micWord(.restricted) == "restricted")
        #expect(CheckCommand.micWord(.notDetermined) == "not_determined")
        #expect(CheckCommand.screenWord(true) == "granted")
        #expect(CheckCommand.screenWord(false) == "not_granted")
    }

    @Test("the JSON document carries the schema and every key")
    func document() {
        let d = CheckCommand.document(screen: "granted", mic: "denied", systemAudio: "unknown", backends: ["sck"],
                                      requested: .mic)
        #expect(d["schema"] as? String == CHECK_SCHEMA)
        #expect(Set(d.keys) == ["schema", "screen_capture", "microphone", "system_audio", "backends", "os", "arch",
                                "requested"])
        #expect(d["requested"] as? String == "mic")
        #expect(CheckCommand.document(screen: "granted", mic: "granted", systemAudio: "granted", backends: ["sck"],
                                      requested: nil)["requested"] is NSNull)
    }
}
