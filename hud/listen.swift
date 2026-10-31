// Wake-word listener and orb for the Claude HUD. On-device speech recognition (Apple's Speech
// framework); nothing leaves the Mac and nothing is kept until "Hey Claude" is heard. Then: an orb in
// the middle of the screen, the HUD docked beside the terminal, and what you say next goes to Jarvis.
//
//   listen                         listen until killed
//   listen --orb                   show the orb once and exit
//   listen --text "hey claude ..." push a transcript through the wake handler (no microphone)
//   --dry-run                      print what would be sent instead of calling the server
//
// Built by dock.py: swiftc -swift-version 5 with an embedded Info.plist (usage descriptions).
import AppKit
import AVFoundation
import Speech

let args = CommandLine.arguments
let port = ProcessInfo.processInfo.environment["CLAUDE_MODS_HUD_PORT"] ?? "7431"
let hudDir = ProcessInfo.processInfo.environment["CLAUDE_HUD_DIR"] ?? FileManager.default.currentDirectoryPath
let dryRun = args.contains("--dry-run")
let wakeRegex = try! NSRegularExpression(pattern: "\\bhey,?\\s+(claude|cloud|clawed|claud|clod)\\b[,.!?]*", options: .caseInsensitive)

func log(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

func post(_ path: String, _ body: [String: Any]) {
    if dryRun { log("POST \(path) \(body)"); return }
    var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!)
    req.httpMethod = "POST"
    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    req.httpBody = try? JSONSerialization.data(withJSONObject: body)
    URLSession.shared.dataTask(with: req) { _, r, e in
        if let e = e { log("\(path): \(e.localizedDescription)") }
        else if let r = r as? HTTPURLResponse, r.statusCode >= 400 { log("\(path): HTTP \(r.statusCode)") }
    }.resume()
}

func openHud() {
    if dryRun { log("open hud"); return }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    p.arguments = [hudDir + "/dock.py", "open"]
    p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
    try? p.run()
}

// MARK: - Orb: a transparent, click-through window over everything, pulsing rings for ~1.6 s
final class OrbView: NSView {
    var t: Double = 0
    override func draw(_ rect: NSRect) {
        let c = NSPoint(x: bounds.midX, y: bounds.midY)
        for i in 0..<3 {
            let phase = (t * 1.6 + Double(i) / 3).truncatingRemainder(dividingBy: 1)
            let r = 18 + phase * 90
            let p = NSBezierPath(ovalIn: NSRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
            NSColor(red: 0.54, green: 0.71, blue: 0.98, alpha: (1 - phase) * 0.7).setStroke()
            p.lineWidth = 2.5; p.stroke()
        }
        let glow = 22 + 4 * sin(t * 6)
        let core = NSBezierPath(ovalIn: NSRect(x: c.x - glow, y: c.y - glow, width: 2 * glow, height: 2 * glow))
        NSColor(red: 0.71, green: 0.75, blue: 1.0, alpha: 0.9).setFill(); core.fill()
        NSColor(red: 0.12, green: 0.12, blue: 0.18, alpha: 1).setFill()
        NSBezierPath(ovalIn: NSRect(x: c.x - glow + 5, y: c.y - glow + 5, width: 2 * glow - 10, height: 2 * glow - 10)).fill()
    }
}

func showOrb(seconds: Double = 1.6, then: (() -> Void)? = nil) {
    guard let screen = NSScreen.main else { then?(); return }
    let size: CGFloat = 260
    let frame = NSRect(x: screen.frame.midX - size / 2, y: screen.frame.midY - size / 2, width: size, height: size)
    let w = NSWindow(contentRect: frame, styleMask: .borderless, backing: .buffered, defer: false)
    w.isOpaque = false; w.backgroundColor = .clear; w.hasShadow = false
    w.level = .screenSaver; w.ignoresMouseEvents = true
    w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
    let v = OrbView(frame: NSRect(origin: .zero, size: frame.size)); w.contentView = v
    w.alphaValue = 0; w.orderFrontRegardless()
    NSAnimationContext.runAnimationGroup { ctx in ctx.duration = 0.25; w.animator().alphaValue = 1 }
    let start = Date()
    let timer = Timer(timeInterval: 1.0 / 60, repeats: true) { timer in
        v.t = Date().timeIntervalSince(start); v.needsDisplay = true
        if v.t > seconds {
            timer.invalidate()
            NSAnimationContext.runAnimationGroup({ ctx in ctx.duration = 0.4; w.animator().alphaValue = 0 }, completionHandler: { w.orderOut(nil); then?() })
        }
    }
    RunLoop.main.add(timer, forMode: .common)
}

// MARK: - Wake handling
final class Jarvis {
    var capturing = false
    var wakeAt = Date.distantPast
    var lastChange = Date.distantPast
    var rest = ""
    var cooldownUntil = Date.distantPast
    var onRestart: (() -> Void)?

    /// Every partial transcript of the current recognition request passes through here.
    func handle(transcript: String, final: Bool = false) {
        let ns = transcript as NSString
        if !capturing {
            guard Date() > cooldownUntil,
                  let m = wakeRegex.firstMatch(in: transcript, range: NSRange(location: 0, length: ns.length)) else { return }
            capturing = true; wakeAt = Date(); lastChange = Date()
            rest = ns.substring(from: m.range.location + m.range.length).trimmingCharacters(in: .whitespacesAndNewlines)
            log("wake: \"\(rest)\"")
            showOrb(); openHud(); post("/state", ["name": "listening"])
            if final { finish() }
            return
        }
        // capturing: the words after the wake phrase, as they keep arriving
        if let m = wakeRegex.firstMatch(in: transcript, range: NSRange(location: 0, length: ns.length)) {
            let now = ns.substring(from: m.range.location + m.range.length).trimmingCharacters(in: .whitespacesAndNewlines)
            if now != rest { rest = now; lastChange = Date() }
        }
        if final { finish() }
    }

    /// Called a few times a second: ends the capture after 1.5 s without new words, or 8 s in all.
    func tick() {
        guard capturing else { return }
        let now = Date()
        if (!rest.isEmpty && now.timeIntervalSince(lastChange) > 1.5) || now.timeIntervalSince(wakeAt) > 8 { finish() }
    }

    func finish() {
        guard capturing else { return }
        capturing = false; cooldownUntil = Date().addingTimeInterval(3)
        if rest.isEmpty { post("/state", ["name": "idle"]); log("nothing after the wake word: HUD open, type instead") }
        else { log("ask: \(rest)"); post("/ask", ["text": rest, "speak": true]) }
        rest = ""
        onRestart?()
    }
}

let jarvis = Jarvis()

// MARK: - Microphone + on-device recognition, restarted every 50 s (Apple's per-request limit)
final class Listener {
    let engine = AVAudioEngine()
    let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    var request: SFSpeechAudioBufferRecognitionRequest?
    var task: SFSpeechRecognitionTask?
    var restartTimer: Timer?

    func start() {
        guard let recognizer = recognizer, recognizer.isAvailable else { log("speech recognizer unavailable"); exit(1) }
        SFSpeechRecognizer.requestAuthorization { status in
            guard status == .authorized else { log("speech recognition not allowed: System Settings › Privacy & Security › Speech Recognition"); exit(1) }
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                guard ok else { log("microphone not allowed: System Settings › Privacy & Security › Microphone"); exit(1) }
                DispatchQueue.main.async { self.begin() }
            }
        }
        jarvis.onRestart = { [weak self] in self?.restart() }
        let t = Timer(timeInterval: 0.25, repeats: true) { _ in jarvis.tick() }
        RunLoop.main.add(t, forMode: .common)
    }

    func begin() {
        let input = engine.inputNode
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.requiresOnDeviceRecognition = true
        req.contextualStrings = ["Claude", "Hey Claude"]
        request = req
        task = recognizer!.recognitionTask(with: req) { result, error in
            if let r = result { jarvis.handle(transcript: r.bestTranscription.formattedString, final: r.isFinal) }
            if error != nil || (result?.isFinal ?? false) { DispatchQueue.main.async { self.restart() } }
        }
        let format = input.outputFormat(forBus: 0)
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { buffer, _ in req.append(buffer) }
        engine.prepare()
        do { try engine.start() } catch { log("audio engine: \(error)"); exit(1) }
        restartTimer?.invalidate()
        restartTimer = Timer.scheduledTimer(withTimeInterval: 50, repeats: false) { _ in self.restart() }
        log("listening for \"Hey Claude\" (on-device)")
    }

    func restart() {
        task?.cancel(); task = nil
        request?.endAudio(); request = nil
        engine.inputNode.removeTap(onBus: 0); engine.stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.begin() }
    }
}

// MARK: - main
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
if args.contains("--orb") {
    showOrb { exit(0) }
} else if let i = args.firstIndex(of: "--text"), i + 1 < args.count {
    jarvis.handle(transcript: args[i + 1], final: true)
    if dryRun { exit(0) }
    DispatchQueue.main.asyncAfter(deadline: .now() + 2.6) { exit(0) }
} else {
    Listener().start()
}
app.run()
