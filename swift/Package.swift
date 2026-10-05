// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "sysaudio",
    platforms: [.macOS(.v13)],
    targets: [
        // The embedded __info_plist section (bundle id + NSMicrophoneUsageDescription
        // + NSAudioCaptureUsageDescription) is what lets an unbundled CLI binary
        // present the Microphone and System Audio Recording TCC prompts.
        // Without it, a launchd-spawned sysaudio (no app ancestor) is auto-denied
        // silently and never appears in System Settings → Privacy → Microphone.
        .executableTarget(
            name: "sysaudio",
            path: "Sources/sysaudio",
            exclude: ["Info.plist"],
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Sources/sysaudio/Info.plist",
                ])
            ]
        ),
        // `swift test`: the pure parts of `sysaudio transcribe` (no Speech
        // calls, no capture). `swift build` does not build it.
        .testTarget(
            name: "sysaudioTests",
            dependencies: ["sysaudio"],
            path: "Tests/sysaudioTests"
        ),
    ]
)
