// `swift test` (from swift/). Pure logic only: nothing here calls Speech,
// reserves a locale, or touches capture, the mic or any TCC-gated API.
import Foundation
import Testing
@testable import sysaudio

// What SpeechTranscriber.installedLocales returns on a Mac where macOS itself
// holds the English models (no app has reserved them).
private let systemHeldEnglish = ["en-AU", "en-CA", "en-GB", "en-IE", "en-IN", "en-NZ", "en-SG", "en-US", "en-ZA"]

@Suite("ModelReadiness: installed means on disk AND reserved by this app")
struct ModelReadinessTests {
    @Test("a system-held model this app has not reserved is not ready (probe 75)")
    func onDiskButUnreserved() {
        let r = ModelReadiness.of("en-US", installed: systemHeldEnglish, reserved: [])
        #expect(r == .unreserved)
        #expect(r.probeExitCode == TRANSCRIBE_EX_NO_MODEL)
        #expect(r.reason("en-US").contains("not reserved"))
        #expect(r.reason("en-US").contains("meeting-capture language en-US"))
    }

    @Test("reserved by another locale only: still not ready")
    func reservedSomethingElse() {
        let r = ModelReadiness.of("en-US", installed: systemHeldEnglish, reserved: ["en-GB", "fr-FR"])
        #expect(r == .unreserved)
    }

    @Test("on disk and reserved: ready (probe 0)")
    func ready() {
        let r = ModelReadiness.of("en-US", installed: systemHeldEnglish, reserved: ["en-US"])
        #expect(r == .ready)
        #expect(r.probeExitCode == TRANSCRIBE_EX_OK)
    }

    @Test("reserved but not downloaded (an interrupted --install): missing")
    func reservedNotOnDisk() {
        let r = ModelReadiness.of("fr-FR", installed: systemHeldEnglish, reserved: ["fr-FR"])
        #expect(r == .missing)
        #expect(r.probeExitCode == TRANSCRIBE_EX_NO_MODEL)
        #expect(r.reason("fr-FR").contains("not installed yet"))
    }

    @Test("neither on disk nor reserved: missing")
    func neither() {
        #expect(ModelReadiness.of("hi-IN", installed: systemHeldEnglish, reserved: []) == .missing)
        #expect(ModelReadiness.of("en-US", installed: [], reserved: []) == .missing)
    }

    @Test("FILE pre-flight: reserve an unreserved on-disk model, refuse a missing one")
    func fileActions() {
        #expect(ModelReadiness.ready.fileAction(skipCheck: false) == .proceed)
        #expect(ModelReadiness.unreserved.fileAction(skipCheck: false) == .reserveThenProceed)
        #expect(ModelReadiness.missing.fileAction(skipCheck: false) == .noModel)
    }

    @Test("SYSAUDIO_TRANSCRIBE_SKIP_INSTALLED_CHECK skips the whole pre-flight, reservation included",
          arguments: [ModelReadiness.ready, .unreserved, .missing])
    func skipCheck(_ r: ModelReadiness) {
        #expect(r.fileAction(skipCheck: true) == .proceed)
    }
}

@Suite("probe JSON")
struct ProbeJSONTests {
    @Test("carries reserved_locales next to installed_locales")
    func reservedLocalesKey() throws {
        let d = TranscribeCommand.probeJSON(
            available: true, reason: "r", locale: "en-US", installed: false,
            supported: ["en-US"], installedLocales: ["en-US"], reservedLocales: [])
        let keys = Set(d.keys)
        #expect(keys == ["available", "reason", "os", "arch", "locale", "installed",
                         "supported", "installed_locales", "reserved_locales"])
        #expect(d["installed"] as? Bool == false)
        #expect(d["installed_locales"] as? [String] == ["en-US"])
        #expect(d["reserved_locales"] as? [String] == [])
    }
}

#if compiler(>=6.2) && canImport(Speech)
@Suite("exit codes once macOS refuses an unallocated locale")
struct UnallocatedLocaleExitCodeTests {
    @Test("SFSpeechErrorDomain 10 (assetLocaleNotAllocated) and 4 (noModel) exit 75, also when wrapped",
          arguments: [4, 10])
    func modelErrorsAreTempFail(_ code: Int) throws {
        guard #available(macOS 26.0, *) else { return }
        let direct = NSError(domain: "SFSpeechErrorDomain", code: code)
        #expect(OnDeviceTranscriber.exitCode(for: direct) == TRANSCRIBE_EX_NO_MODEL)
        let wrapped = NSError(domain: "SomeOuterDomain", code: 1,
                              userInfo: [NSUnderlyingErrorKey: direct])
        #expect(OnDeviceTranscriber.exitCode(for: wrapped) == TRANSCRIBE_EX_NO_MODEL)
    }
}
#endif
