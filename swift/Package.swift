// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "sysaudio",
    platforms: [.macOS(.v13)],
    targets: [
        // The embedded __info_plist section (bundle id + NSMicrophoneUsageDescription)
        // is what lets an unbundled CLI binary present the Microphone TCC prompt.
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
    ]
)
