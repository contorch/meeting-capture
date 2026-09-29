// meeting-capture-menubar — the recorder's menu bar item.
//
// Shows what the recorder is doing (recording a call / waiting / listening on
// the interface / paused / not running) and offers Pause/Resume and
// "Recording settings…" (runs `meeting-capture ui`, which opens the settings
// page). It never talks to the daemon directly: it reads the status file the
// daemon writes (~/.meeting-capture/state.json), and pausing is the same
// ~/.meeting-capture/paused file `meeting-capture pause` creates.
//
// A plain executable, not an .app: `meeting-capture install` starts it at login
// with launchd. setActivationPolicy(.accessory) keeps it out of the Dock.
//
//   meeting-capture-menubar --cli /path/to/meeting-capture

import AppKit

let stateDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".meeting-capture")
let stateFile = stateDir.appendingPathComponent("state.json")
let pauseFile = stateDir.appendingPathComponent("paused")

struct RecorderState {
    var state: String      // idle | recording | listening | paused | stopped | unknown
    var source: String
    var session: String
    var since: Double

    static func load() -> RecorderState {
        guard let data = try? Data(contentsOf: stateFile),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return RecorderState(state: "unknown", source: "", session: "", since: 0)
        }
        var s = RecorderState(
            state: obj["state"] as? String ?? "unknown",
            source: obj["source"] as? String ?? "",
            session: obj["session"] as? String ?? "",
            since: obj["since"] as? Double ?? 0)
        // A daemon that died without writing "stopped" (kill -9, crash).
        if let pid = obj["pid"] as? Int, s.state != "stopped", kill(pid_t(pid), 0) != 0 {
            s.state = "stopped"
        }
        // The pause file takes effect within a poll; show it immediately.
        if FileManager.default.fileExists(atPath: pauseFile.path), s.state != "stopped" {
            s.state = "paused"
        }
        return s
    }

    var title: String {
        switch state {
        case "recording": return "Recording a call"
        case "listening": return "Listening on the audio interface"
        case "idle": return "Waiting for a call"
        case "paused": return "Paused"
        case "stopped": return "Recorder not running"
        default: return "Recorder status unknown"
        }
    }

    var symbol: String {
        switch state {
        case "recording": return "record.circle.fill"
        case "listening": return "waveform.circle.fill"
        case "idle": return "waveform"
        case "paused": return "pause.circle"
        default: return "exclamationmark.triangle"
        }
    }
}

final class MenuBar: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    let menu = NSMenu()
    let statusLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    let sessionLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    let pauseItem = NSMenuItem(title: "Pause recording", action: #selector(togglePause), keyEquivalent: "p")
    let cli: String
    var timer: Timer?
    var last = ""

    init(cli: String) {
        self.cli = cli
        super.init()
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        statusLine.isEnabled = false
        sessionLine.isEnabled = false
        pauseItem.target = self
        let settings = NSMenuItem(title: "Recording settings…", action: #selector(openSettings), keyEquivalent: ",")
        settings.target = self
        let quit = NSMenuItem(title: "Hide menu bar item", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        for i in [statusLine, sessionLine, NSMenuItem.separator(), pauseItem, settings, NSMenuItem.separator(), quit] {
            menu.addItem(i)
        }
        menu.delegate = self
        item.menu = menu
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    func refresh() {
        let s = RecorderState.load()
        let key = "\(s.state)|\(s.session)"
        statusLine.title = s.title
        sessionLine.title = s.session.isEmpty ? "" : "Transcript: \(s.session)"
        sessionLine.isHidden = s.session.isEmpty || !(s.state == "recording" || s.state == "listening")
        pauseItem.title = s.state == "paused" ? "Resume recording" : "Pause recording"
        pauseItem.isEnabled = s.state != "stopped" && s.state != "unknown"
        if key == last { return }
        last = key
        if let button = item.button {
            let img = NSImage(systemSymbolName: s.symbol, accessibilityDescription: s.title)
            img?.isTemplate = s.state != "recording"
            if s.state == "recording", let img = img {
                // A red dot while a call is being recorded — hard to miss.
                let red = img.withSymbolConfiguration(.init(paletteColors: [.systemRed]))
                button.image = red ?? img
            } else {
                button.image = img
            }
            button.toolTip = "meeting-capture — \(s.title)"
        }
    }

    @objc func togglePause() {
        if FileManager.default.fileExists(atPath: pauseFile.path) {
            try? FileManager.default.removeItem(at: pauseFile)
        } else {
            try? FileManager.default.createDirectory(at: stateDir, withIntermediateDirectories: true)
            FileManager.default.createFile(atPath: pauseFile.path, contents: nil)
        }
        refresh()
    }

    @objc func openSettings() {
        // `meeting-capture ui` re-opens an already running page instead of
        // starting a second one, so repeated clicks are safe.
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cli)
        p.arguments = ["ui"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
        } catch {
            let alert = NSAlert()
            alert.messageText = "Couldn't open recording settings"
            alert.informativeText = "\(cli) ui — \(error.localizedDescription)"
            alert.runModal()
        }
    }

    @objc func quit() { NSApp.terminate(nil) }
}

func cliPath() -> String {
    let args = CommandLine.arguments
    if let i = args.firstIndex(of: "--cli"), i + 1 < args.count { return args[i + 1] }
    if let env = ProcessInfo.processInfo.environment["MEETING_CAPTURE_CLI"] { return env }
    for candidate in ["/opt/homebrew/bin/meeting-capture", "/usr/local/bin/meeting-capture"]
        where FileManager.default.isExecutableFile(atPath: candidate) {
        return candidate
    }
    return "meeting-capture"
}

if CommandLine.arguments.contains("--help") {
    print("Usage: meeting-capture-menubar [--cli /path/to/meeting-capture]")
    print("Menu bar item for meeting-capture: status, pause/resume, recording settings.")
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = MenuBar(cli: cliPath())
app.delegate = delegate
app.run()
