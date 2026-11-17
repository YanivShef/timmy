// בועת הקלטה צפה בסגנון Liquid Glass, עם תמלול בלייב של האודיו של Google Chrome בלבד.
// לחיצה = התחלה/עצירה, גרירה = הזזה, קליק ימני = תפריט.
import Cocoa
import SwiftUI
import Combine
import ScreenCaptureKit
import AVFoundation
import CoreMedia
import ServiceManagement

enum Phase: Equatable { case idle, recording, finishing, message(String) }

struct Line: Identifiable { let id = UUID(); let t: String; let text: String; var speaker: String = "other" }  // speaker: "me" (אתה) / "other"

let home = FileManager.default.homeDirectoryForCurrentUser

// כיוון ימין-לשמאל קבוע לטקסט בעברית: סימן RLM בתחילת כל שורה.
// בלעדיו שורה שמתחילה באנגלית או במספר ("Claude Code זה…") מוצגת משמאל לימין והסדר מתבלגן.
func he(_ s: String) -> String { "\u{200F}" + s.replacingOccurrences(of: "\n", with: "\n\u{200F}") }
// הכל מותקן ב-~/Library/Application Support/Timmy (ע"י install.sh)
// TIMMY_APP_DIR: תיקיית הגדרות חלופית (לבדיקות, בלי לגעת בהתקנה האמיתית)
let appDir: URL = {
    if let d = ProcessInfo.processInfo.environment["TIMMY_APP_DIR"], !d.isEmpty { return URL(fileURLWithPath: d) }
    return home.appendingPathComponent("Library/Application Support/Timmy")
}()
let configURL = appDir.appendingPathComponent("config.json")
func loadConfig() -> [String: Any] {
    guard let d = try? Data(contentsOf: configURL),
          let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return [:] }
    return o
}
func saveConfig(_ updates: [String: Any]) {
    var c = loadConfig()
    for (k, v) in updates { c[k] = v }
    try? FileManager.default.createDirectory(at: appDir, withIntermediateDirectories: true)
    if let d = try? JSONSerialization.data(withJSONObject: c, options: [.prettyPrinted, .withoutEscapingSlashes]) { try? d.write(to: configURL) }
}
// תיקיית ההקלטות נקראת מחדש בכל פעם (אפשר לשנות אותה באונבורדינג)
var recDir: URL {
    if let p = loadConfig()["output_dir"] as? String, !p.isEmpty { return URL(fileURLWithPath: (p as NSString).expandingTildeInPath) }
    return home.appendingPathComponent("Documents/טימי - הקלטות")
}

// מקום פנוי בדיסק של התיקייה (או של התיקייה הקיימת הקרובה אליה)
func freeBytes(at url: URL) -> Int64 {
    // לבדיקות: TIMMY_FAKE_FREE_MB מדמה דיסק כמעט מלא
    if let f = ProcessInfo.processInfo.environment["TIMMY_FAKE_FREE_MB"], let v = Int64(f) { return v * mb }
    var u = url
    while !FileManager.default.fileExists(atPath: u.path) && u.path != "/" { u = u.deletingLastPathComponent() }
    let v = try? u.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
    return v?.volumeAvailableCapacityForImportantUsage ?? Int64.max
}
let mb: Int64 = 1_000_000
let minStartBytes: Int64 = 500 * mb   // פחות מזה לא מתחילים הקלטה (שעה של שיחה ≈ 250MB בזמן ההקלטה)
let minKeepBytes: Int64 = 200 * mb    // פחות מזה באמצע הקלטה: עוצרים ושומרים מה שיש
let worker = appDir.appendingPathComponent("live.py").path
let python = appDir.appendingPathComponent("venv/bin/python").path

final class Recorder: NSObject, ObservableObject, SCStreamOutput, SCStreamDelegate {
    @Published var phase: Phase = .idle
    @Published var elapsed = 0
    @Published var lines: [Line] = []
    @Published var partial = ""
    @Published var partialSpeaker = "other"
    @Published var thinking = false   // מזוהה דיבור אבל עוד אין טקסט אמין: מציגים "מתמלל…"
    @Published var loading = false
    @Published var level: Float = 0   // עוצמת קול נוכחית 0...1, לוויזואליזציה
    @Published var bullets: [String] = []

    private var stream: SCStream?
    private var proc: Process?
    private var stdinHandle: FileHandle?
    private var base = ""
    private var doneFile: String?
    private var timer: Timer?
    private var startDate = Date()
    private var outBuf = Data()
    private var stoppedForDisk = false
    private let q = DispatchQueue(label: "audio")

    var isCard: Bool { phase == .recording || phase == .finishing }

    func toggle() {
        switch phase {
        case .idle, .message: Task { @MainActor in await self.start() }
        case .recording: Task { @MainActor in await self.stop() }
        case .finishing: break
        }
    }

    @MainActor func flash(_ text: String) {
        phase = .message(text)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
            if case .message = self?.phase { self?.phase = .idle }
        }
    }

    @MainActor func start() async {
        // בדיקת מקום לפני הקלטה
        if freeBytes(at: recDir) < minStartBytes {
            flash("אין מקום")
            NotificationCenter.default.post(name: Notification.Name("timmy.diskFull"), object: nil)
            return
        }
        stoppedForDisk = false
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let chrome = content.applications.first(where: { $0.bundleIdentifier == "com.google.Chrome" }) else {
                flash("כרום סגור"); return
            }
            guard let display = content.displays.first else { flash("שגיאה"); return }
            let filter = SCContentFilter(display: display, including: [chrome], exceptingWindows: [])
            let cfg = SCStreamConfiguration()
            cfg.capturesAudio = true
            cfg.excludesCurrentProcessAudio = true
            cfg.sampleRate = 16000
            cfg.channelCount = 1
            cfg.width = 2; cfg.height = 2
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            // גם המיקרופון שלך, כערוץ נפרד (macOS 15+)
            if #available(macOS 15.0, *) { cfg.captureMicrophone = true }

            try FileManager.default.createDirectory(at: recDir, withIntermediateDirectories: true)
            let df = DateFormatter(); df.dateFormat = "yyyy-MM-dd HH-mm-ss"
            base = recDir.appendingPathComponent("\(df.string(from: Date())) כרום").path
            lines = []; partial = ""; loading = true; bullets = []; doneFile = nil
            try launchWorker()

            var s = SCStream(filter: filter, configuration: cfg, delegate: self)
            try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: q)
            if #available(macOS 15.0, *) { try? s.addStreamOutput(self, type: .microphone, sampleHandlerQueue: q) }
            do {
                try await s.startCapture()
            } catch {
                // אין הרשאת מיקרופון? ממשיכים עם כרום בלבד
                NSLog("start with mic failed: \(error), retrying chrome only")
                if #available(macOS 15.0, *) { cfg.captureMicrophone = false }
                s = SCStream(filter: filter, configuration: cfg, delegate: self)
                try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: q)
                try await s.startCapture()
            }
            stream = s
            startDate = Date(); elapsed = 0; phase = .recording
            timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
                guard let self else { return }
                self.elapsed = Int(Date().timeIntervalSince(self.startDate))
                // כל 20 שניות: אם המקום נגמר, עוצרים ושומרים
                if self.elapsed > 0 && self.elapsed % 20 == 0 && self.phase == .recording && freeBytes(at: recDir) < minKeepBytes {
                    self.stoppedForDisk = true
                    Task { @MainActor in await self.stop() }
                }
            }
        } catch {
            NSLog("start error: \(error)")
            stdinHandle?.closeFile(); stdinHandle = nil
            flash("צריך הרשאה")
            NotificationCenter.default.post(name: Notification.Name("timmy.needPermissions"), object: nil)
        }
    }

    private func launchWorker() throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [worker, base]
        let inPipe = Pipe(), outPipe = Pipe()
        p.standardInput = inPipe; p.standardOutput = outPipe
        p.standardError = FileHandle(forWritingAtPath: "/dev/null")
        outBuf = Data()
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty else { return }
            DispatchQueue.main.async { self?.consume(d) }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.workerDone() }
        }
        try p.run()
        proc = p; stdinHandle = inPipe.fileHandleForWriting
    }

    private func consume(_ d: Data) {
        outBuf.append(d)
        while let nl = outBuf.firstIndex(of: 0x0A) {
            let lineData = outBuf[outBuf.startIndex..<nl]
            outBuf.removeSubrange(outBuf.startIndex...nl)
            guard let o = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any],
                  let type = o["type"] as? String else { continue }
            switch type {
            case "ready": loading = false
            case "partial":
                let sp = o["speaker"] as? String ?? "other"
                let t = o["text"] as? String ?? ""
                if !t.isEmpty { partial = t; partialSpeaker = sp; thinking = false }
                else if (o["thinking"] as? Bool) == true {
                    // לא דורסים טקסט חי של הדובר השני (רעש במיק לא מחליף את מה שהצד השני אומר)
                    if partial.isEmpty || partialSpeaker == sp { partial = ""; partialSpeaker = sp; thinking = true }
                }
                else if sp == partialSpeaker { partial = ""; thinking = false }
            case "final":
                let sp = o["speaker"] as? String ?? "other"
                lines.append(Line(t: o["t"] as? String ?? "", text: o["text"] as? String ?? "", speaker: sp))
                if sp == partialSpeaker { partial = ""; thinking = false }
            case "bullets": bullets = o["items"] as? [String] ?? bullets
            case "done": doneFile = o["txt"] as? String
            case "diskfull":
                if phase == .recording { stoppedForDisk = true; Task { @MainActor in await self.stop() } }
            default: break
            }
        }
    }

    @MainActor func stop() async {
        timer?.invalidate(); timer = nil
        try? await stream?.stopCapture(); stream = nil
        phase = .finishing
        q.sync { stdinHandle?.closeFile(); stdinHandle = nil }
    }

    private func workerDone() {
        proc = nil
        let txt = URL(fileURLWithPath: doneFile ?? (base + ".txt"))
        if lines.isEmpty {
            try? FileManager.default.removeItem(at: txt)
            Task { @MainActor in self.flash("לא נקלט דיבור") }
        } else {
            NSWorkspace.shared.activateFileViewerSelecting([txt])
            let full = stoppedForDisk
            Task { @MainActor in self.flash(full ? "נשמר, אין מקום" : "נשמר ✓") }
        }
    }

    // דמו לעיצוב: מציג כרטיס עם טקסט לדוגמה, בלי הקלטה
    func startDemo(finishing: Bool) {
        lines = [Line(t: "00:00:04", text: "אז מי שהיה בסדנה תרשמו לי כן."),
                 Line(t: "00:00:08", text: "שזה לא אתה אלא דמותי, ניקח את זה כמחמאה, כן כן מהסדנה, איזה כיף."),
                 Line(t: "00:00:14", text: "יש כאן מישהו שלא היה בסדנה? זה מעניין אותי, מישהו שלא היה ובכל זאת הגיע."),
                 Line(t: "00:00:20", text: "אני לא הייתי, אבל ראיתי את ההקלטה.", speaker: "me")]
        partial = args.contains("--demo-thinking") ? "" : "אני לא הייתי, אוקיי, אז בואו נתחיל"
        thinking = args.contains("--demo-thinking")
        bullets = ["הסדנה הקודמת עברה טוב, רוב המשתתפים חזרו", "יש כמה משתתפים חדשים שלא היו בסדנה", "המטרה היום: לבנות סוכן ראשון ב-Claude Code"]
        loading = false; elapsed = 83; phase = finishing ? .finishing : .recording
        Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            self?.level = Float.random(in: 0.05...0.9)
        }
    }

    private let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!
    private var converters: [Int: (AVAudioFormat, AVAudioConverter)] = [:]

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        var chan = -1
        if type == .audio { chan = 0 }
        if #available(macOS 15.0, *), type == .microphone { chan = 1 }
        guard chan >= 0, sb.isValid, let h = stdinHandle,
              let asbd = sb.formatDescription?.audioStreamBasicDescription else { return }
        var a = asbd
        guard let fmt = AVAudioFormat(streamDescription: &a) else { return }
        let n = AVAudioFrameCount(sb.numSamples)
        guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: n) else { return }
        buf.frameLength = n
        guard CMSampleBufferCopyPCMDataIntoAudioBufferList(sb, at: 0, frameCount: Int32(n), into: buf.mutableAudioBufferList) == noErr else { return }

        // המרה ל-16kHz מונו float32 אם צריך (המיקרופון מגיע בפורמט המקורי שלו)
        var out = buf
        if fmt.sampleRate != 16000 || fmt.channelCount != 1 || fmt.commonFormat != .pcmFormatFloat32 {
            if converters[chan]?.0 != fmt { converters[chan] = (fmt, AVAudioConverter(from: fmt, to: target)!) }
            let conv = converters[chan]!.1
            let cap = AVAudioFrameCount(Double(n) * 16000 / fmt.sampleRate) + 32
            guard let ob = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { return }
            var fed = false
            var err: NSError?
            conv.convert(to: ob, error: &err) { _, status in
                if fed { status.pointee = .noDataNow; return nil }
                fed = true; status.pointee = .haveData; return buf
            }
            if err != nil { return }
            out = ob
        }
        guard let ch = out.floatChannelData, out.frameLength > 0 else { return }
        let m = Int(out.frameLength)
        if chan == 0 {
            var sum: Float = 0
            for i in 0..<m { sum += ch[0][i] * ch[0][i] }
            let rms = (sum / Float(m)).squareRoot()
            let lv = min(1, max(0, (20 * log10(max(rms, 1e-6)) + 50) / 45))
            DispatchQueue.main.async { self.level = max(lv, self.level * 0.6) }
        }
        var header = Data([UInt8(chan)])
        var count = UInt32(m).littleEndian
        header.append(Data(bytes: &count, count: 4))
        do { try h.write(contentsOf: header + Data(bytes: ch[0], count: m * MemoryLayout<Float>.size)) } catch { NSLog("pipe write: \(error)") }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        NSLog("stream stopped: \(error)")
        Task { @MainActor in if self.phase == .recording { await self.stop() } }
    }
}

// MARK: - תצוגה

// המסגרת של כפתור העצירה בקואורדינטות החלון (מלמעלה-שמאל). כל עיצוב חייב לסמן את כפתור העצירה ב-.stopButtonArea()
var stopRect: CGRect = .zero
extension View {
    func stopButtonArea() -> some View {
        background(GeometryReader { g in
            Color.clear
                .onAppear { stopRect = g.frame(in: .global) }
                .onChange(of: g.frame(in: .global)) { _, f in stopRect = f }
        })
    }
}

// מצב דמו לעיצוב: --demo-idle / --demo-card / --demo-finishing, ו---offset-y N להזזה אנכית
let args = CommandLine.arguments
func argValue(_ k: String) -> Double? {
    guard let i = args.firstIndex(of: k), i + 1 < args.count else { return nil }
    return Double(args[i + 1])
}

// MARK: עיצוב A: "Dynamic Island" מחובר לנוטש — קפסולה שחורה שיוצאת מהנוטש ונפתחת לכרטיס כלפי מטה

let recRed = Color(red: 1.0, green: 0.27, blue: 0.27)
// צבע ההדגשה של טימי. ברירת מחדל אמבר; אפשר לשנות דרך "שפר את טימי" ("תחליף לצבע כחול"), נשמר ב-config: accent_color
let defaultAccent = "#FFA046"
final class Theme {
    static var accentHex: String = (loadConfig()["accent_color"] as? String) ?? defaultAccent
    static var eyeHex: String = (loadConfig()["eye_color"] as? String) ?? ""          // ריק = כמו צבע ההדגשה
    static var bgHex: String = (loadConfig()["background_color"] as? String) ?? "#000000"
    static var animations: Bool = (loadConfig()["animations"] as? Bool) ?? true
    static var version = 0
    static var isDefault: Bool { accentHex == defaultAccent && eyeHex.isEmpty && bgHex == "#000000" && animations }
    static func set(_ hex: String?) { apply(["accent_color": hex ?? defaultAccent]) }
    // מחיל שינויי מראה: accent_color / eye_color / background_color / animations / reset
    static func apply(_ c: [String: Any]) {
        if c["reset"] as? Bool == true {
            accentHex = defaultAccent; eyeHex = ""; bgHex = "#000000"; animations = true
        }
        if let v = c["accent_color"] as? String { accentHex = v }
        if let v = c["eye_color"] as? String { eyeHex = v }
        if let v = c["background_color"] as? String { bgHex = readableBackground(v) }
        if let v = c["animations"] as? Bool { animations = v }
        version += 1
        saveConfig(["accent_color": accentHex, "eye_color": eyeHex, "background_color": bgHex, "animations": animations])
        NotificationCenter.default.post(name: Notification.Name("timmy.themeChanged"), object: nil)
    }
    // רקע בהיר מדי -> מכהים אותו, כדי שהטקסט הלבן יישאר קריא
    static func readableBackground(_ hex: String) -> String {
        var h = hex.replacingOccurrences(of: "#", with: "")
        if h.count == 3 { h = h.map { "\($0)\($0)" }.joined() }
        guard h.count == 6, let v = UInt32(h, radix: 16) else { return "#000000" }
        var r = Double((v >> 16) & 0xFF), g = Double((v >> 8) & 0xFF), b = Double(v & 0xFF)
        let lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
        if lum > 0.22 { let f = 0.22 / lum; r *= f; g *= f; b *= f }
        return String(format: "#%02X%02X%02X", Int(r), Int(g), Int(b))
    }
}
func colorFromHex(_ hex: String) -> Color {
    var h = hex.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "#", with: "")
    if h.count == 3 { h = h.map { "\($0)\($0)" }.joined() }
    guard h.count == 6, let v = UInt32(h, radix: 16) else { return Color(red: 1.0, green: 0.627, blue: 0.275) }
    return Color(red: Double((v >> 16) & 0xFF) / 255, green: Double((v >> 8) & 0xFF) / 255, blue: Double(v & 0xFF) / 255)
}
var amber: Color { colorFromHex(Theme.accentHex) }
var eyeColor: Color { colorFromHex(Theme.eyeHex.isEmpty ? Theme.accentHex : Theme.eyeHex) }
var bgColor: Color { colorFromHex(Theme.bgHex) }
var animOn: Bool { Theme.animations }

// זיהוי הוראת צבע ("תחליף לצבע כחול", "#1E90FF", "תחזיר לכתום"). מחזיר hex, או "reset", או nil אם זו לא הוראת צבע
let colorNames: [(String, String)] = [
    ("תכלת", "#38BDF8"), ("טורקיז", "#14B8A6"), ("כחול", "#3B82F6"), ("נייבי", "#1E3A8A"),
    ("ירוק", "#22C55E"), ("מנטה", "#34D399"), ("ליים", "#A3E635"), ("צהוב", "#FACC15"),
    ("זהב", "#D4AF37"), ("זהוב", "#D4AF37"), ("אדום", "#EF4444"), ("בורדו", "#9F1239"),
    ("ורוד", "#EC4899"), ("סגול", "#A855F7"), ("לילך", "#C4B5FD"), ("לבן", "#F5F5F5"), ("אפור", "#9CA3AF"),
    ("blue", "#3B82F6"), ("green", "#22C55E"), ("purple", "#A855F7"), ("pink", "#EC4899"), ("red", "#EF4444"),
    ("yellow", "#FACC15"), ("teal", "#14B8A6"), ("gold", "#D4AF37")]
func isAppearanceInstruction(_ t: String) -> Bool {
    let l = t.lowercased()
    let keys = ["צבע", "רקע", "עיניים", "עין", "אנימצי", "הנפש", "מצמ", "תזוז", "מראה", "color", "background", "eyes", "animation"]
    if keys.contains(where: { l.contains($0) }) || l.range(of: "#[0-9a-f]{3,6}", options: .regularExpression) != nil { return true }
    // פועל + שם צבע ("תחזיר לכתום", "תשנה לירוק"); בלי פועל ("תסמן באדום") זו הוראה לסיכום
    let verbs = ["תחליף", "תשנה", "תעשה", "שנה", "החלף", "תחזיר", "החזר"]
    let colors = colorNames.map { $0.0 } + ["כתום", "שחור", "orange", "black"]
    return verbs.contains(where: { l.contains($0) }) && colors.contains(where: { l.contains($0) })
}
func isColorInstruction(_ t: String) -> Bool { isAppearanceInstruction(t) }
// מפענח הוראת מראה בלי Claude. מחזיר שינויים, או nil אם צריך את Claude (ניסוח יצירתי)
func parseAppearance(_ t: String) -> [String: Any]? {
    let l = t.lowercased()
    let has = { (ws: [String]) in ws.contains { l.contains($0) } }
    if has(["אפס", "תאפס", "ברירת מחדל", "כמו שהיה", "תחזיר הכל", "reset"]) && has(["מראה", "הכל", "צבעים", "עיצוב", "reset"]) { return ["reset": true] }
    if has(["תחזיר", "החזר"]) && has(["כתום", "orange", "מקורי"]) && !has(["רקע", "עיניים", "עין"]) { return ["accent_color": defaultAccent] }
    var out: [String: Any] = [:]
    if has(["אנימצי", "הנפש", "מצמ", "תזוז", "animation"]) {
        if has(["תוריד", "בלי", "תכבה", "תבטל", "תעצור", "off"]) { out["animations"] = false }
        else if has(["תחזיר", "תדליק", "תפעיל"]) { out["animations"] = true }
    }
    var hex: String?
    if let r = l.range(of: "#[0-9a-f]{6}|#[0-9a-f]{3}", options: .regularExpression) { hex = String(l[r]).uppercased() }
    else if has(["כתום", "orange"]) { hex = defaultAccent }
    else if has(["שחור", "black"]) { hex = "#000000" }
    else { hex = colorNames.first(where: { l.contains($0.0) })?.1 }
    if let hex {
        if has(["רקע", "background"]) { out["background_color"] = hex }
        else if has(["עיניים", "עין", "eyes", "eye"]) { out["eye_color"] = hex }
        else { out["accent_color"] = hex }
    }
    return out.isEmpty ? nil : out
}
func parseColor(_ t: String) -> String? {
    guard let c = parseAppearance(t) else { return nil }
    if c["reset"] != nil { return "reset" }
    return c["accent_color"] as? String
}
let okGreen = Color(red: 0.188, green: 0.82, blue: 0.345)   // #30D158

// מידות הנוטש מהמסך בזמן ריצה; במסך בלי נוטש: אותה צורה במרכז העליון בגובה 32
struct NotchGeo {
    let width: CGFloat, height: CGFloat, centerX: CGFloat, top: CGFloat
    static let current: NotchGeo = {
        guard let s = NSScreen.main ?? NSScreen.screens.first else { return NotchGeo(width: 160, height: 32, centerX: 0, top: 0) }
        if s.safeAreaInsets.top > 0, let l = s.auxiliaryTopLeftArea, let r = s.auxiliaryTopRightArea {
            return NotchGeo(width: r.minX - l.maxX, height: s.safeAreaInsets.top, centerX: (l.maxX + r.minX) / 2, top: s.frame.maxY)
        }
        return NotchGeo(width: 160, height: 32, centerX: s.frame.midX, top: s.frame.maxY)
    }()
}
let notch = NotchGeo.current

let shoulder: CGFloat = 8            // הקימור הקעור במקום שהצורה פוגשת את שורת התפריטים
let sideMargin: CGFloat = 26         // מרווח שקוף לצל בצדדים
let bottomMargin: CGFloat = 40       // מרווח שקוף לצל למטה (אין מרווח למעלה)
let wingW: CGFloat = 46, msgWingW: CGFloat = 84, hoverGrow: CGFloat = 6
let cardBodyW: CGFloat = 600
let cardBodyH: CGFloat = notch.height + 188
let island = Animation.spring(response: 0.38, dampingFraction: 0.86)

// גודל החלון: הצורה + שוליים לצל
let mouthH: CGFloat = 16            // הפה של טימי יורד מתחת לנוטש בריחוף
let dotFrame = NSSize(width: notch.width + 2 * (msgWingW + hoverGrow) + 2 * shoulder + 2 * sideMargin,
                      height: notch.height + mouthH + bottomMargin)
let cardSize = NSSize(width: cardBodyW + 2 * shoulder + 2 * sideMargin, height: cardBodyH + bottomMargin)

// מצב ריחוף, מוזן מ-BubbleHost (tracking area שעובד גם כשהאפליקציה לא פעילה)
final class HoverState: ObservableObject {
    @Published var over = false
    @Published var overStop = false
}
let hover = HoverState()

// צורת הנוטש: למעלה ישר וצמוד למסך עם "כתפיים" קעורות, למטה פינות רציפות
struct NotchShape: Shape {
    var bottomRadius: CGFloat
    var animatableData: CGFloat { get { bottomRadius } set { bottomRadius = newValue } }
    func path(in r: CGRect) -> Path {
        let s = shoulder, w = r.width, h = r.height
        let br = max(0, min(bottomRadius, (w - 2 * s) / 2, h - s))
        let k: CGFloat = 0.32   // נקודות בקרה קרובות לפינה = פינה רציפה (squircle)
        var p = Path()
        p.move(to: CGPoint(x: r.minX, y: r.minY))
        p.addQuadCurve(to: CGPoint(x: r.minX + s, y: r.minY + s), control: CGPoint(x: r.minX + s, y: r.minY))
        p.addLine(to: CGPoint(x: r.minX + s, y: r.minY + h - br))
        p.addCurve(to: CGPoint(x: r.minX + s + br, y: r.minY + h),
                   control1: CGPoint(x: r.minX + s, y: r.minY + h - br * k),
                   control2: CGPoint(x: r.minX + s + br * k, y: r.minY + h))
        p.addLine(to: CGPoint(x: r.minX + w - s - br, y: r.minY + h))
        p.addCurve(to: CGPoint(x: r.minX + w - s, y: r.minY + h - br),
                   control1: CGPoint(x: r.minX + w - s - br * k, y: r.minY + h),
                   control2: CGPoint(x: r.minX + w - s, y: r.minY + h - br * k))
        p.addLine(to: CGPoint(x: r.minX + w - s, y: r.minY + s))
        p.addQuadCurve(to: CGPoint(x: r.minX + w, y: r.minY), control: CGPoint(x: r.minX + w - s, y: r.minY))
        p.closeSubpath()
        return p
    }
}

extension View {
    func islandSurface(radius: CGFloat, fill: Color = .black) -> some View {
        background(NotchShape(bottomRadius: radius).fill(fill)
            .shadow(color: .black.opacity(0.35), radius: 18, y: 10))
    }
}

// הכרטיס "יורד" מהנוטש: מתיחה אנכית מלמעלה
struct DropDown: ViewModifier {
    let y: CGFloat, alpha: Double
    func body(content: Content) -> some View {
        content.scaleEffect(x: 1, y: y, anchor: .top).opacity(alpha)
    }
}

// העין של טימי: עיגול אמבר זוהר שממצמץ מדי פעם
struct TimmyEye: View {
    var big = false
    @State private var blink = false
    var body: some View {
        Capsule()
            .fill(eyeColor)
            .frame(width: big ? 11 : 9, height: blink ? 1.5 : (big ? 12 : 10))
            .shadow(color: eyeColor.opacity(0.75), radius: big ? 5 : 3.5)
            .animation(.easeInOut(duration: 0.09), value: blink)
            .animation(.spring(response: 0.3, dampingFraction: 0.7), value: big)
            .onAppear { scheduleBlink() }
    }
    func scheduleBlink() {
        DispatchQueue.main.asyncAfter(deadline: .now() + Double.random(in: 2.8...5.5)) {
            guard animOn else { scheduleBlink(); return }   // אנימציות כבויות: בלי מצמוץ
            blink = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.13) { blink = false; scheduleBlink() }
        }
    }
}

// הפה של טימי: גל קול קטן שנושם
struct TimmyMouth: View {
    @State private var phase = false
    let heights: [CGFloat] = [3, 6, 9, 5, 10, 6, 3]
    var body: some View {
        HStack(spacing: 2.5) {
            ForEach(0..<heights.count, id: \.self) { i in
                Capsule().fill(eyeColor.opacity(0.9))
                    .frame(width: 2.5, height: !animOn ? heights[i] * 0.8 : (phase ? heights[i] : max(2.5, heights[i] * 0.45)))
                    .animation(.easeInOut(duration: 0.5).repeatForever().delay(Double(i) * 0.06), value: phase)
            }
        }
        .shadow(color: eyeColor.opacity(0.5), radius: 3)
        .onAppear { if animOn { phase = true } }
    }
}

// נקודת הקלטה
struct RecDot: View {
    var size: CGFloat = 8
    var pulsing = false
    @State private var on = false
    var body: some View {
        Circle().fill(recRed)
            .frame(width: size, height: size)
            .shadow(color: recRed.opacity(0.5), radius: 2)
            .opacity(pulsing ? (on ? 0.3 : 1) : 1)
            .onAppear { if pulsing && animOn { withAnimation(.easeInOut(duration: 0.9).repeatForever()) { on = true } } }
    }
}

// גל קול חי, מונוכרומטי, היסטוריה מתגלגלת
struct Waveform: View {
    var level: Float
    var active: Bool
    static let bars = 18
    @State private var hist: [CGFloat] = Array(repeating: 0, count: Waveform.bars)
    var body: some View {
        HStack(alignment: .center, spacing: 2.5) {
            ForEach(0..<Waveform.bars, id: \.self) { i in
                let v = active ? hist[i] : 0
                Capsule()
                    .fill(Color.white.opacity(active ? 0.35 + 0.55 * Double(v) : 0.25))
                    .frame(width: 2.5, height: 3 + v * 11)
            }
        }
        .frame(height: 14)
        .animation(.easeOut(duration: 0.12), value: hist)
        .onChange(of: level) { _, lv in
            var h = hist
            h.removeFirst()
            h.append(min(1, (h.last ?? 0) * 0.25 + CGFloat(lv) * 0.75))
            hist = h
        }
    }
}

struct BubbleView: View {
    @ObservedObject var r: Recorder
    @ObservedObject var hv = hover

    var body: some View {
        ZStack(alignment: .top) {
            if r.isCard {
                card.transition(.modifier(active: DropDown(y: 0.6, alpha: 0), identity: DropDown(y: 1, alpha: 1)))
            } else {
                pill.transition(.opacity)
            }
        }
        .animation(animOn ? island : nil, value: r.isCard)
        .environment(\.colorScheme, .dark)
        .id(Theme.version)
        .frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity, alignment: .top)
    }

    // MARK: מנוחה: הנוטש + שתי כנפיים
    var message: String? { if case .message(let t) = r.phase { return t } else { return nil } }

    // MARK: מנוחה: טימי. הנוטש הוא הפנים שלו, עין בכל צד, ופה של גל קול שיורד בריחוף
    @ViewBuilder var pill: some View {
        if message == nil { timmyFace } else { statusPill }
    }

    var timmyFace: some View {
        let w = wingW + (hv.over ? hoverGrow : 0)
        return VStack(spacing: 0) {
            HStack(spacing: 0) {
                TimmyEye(big: hv.over).frame(width: w)
                Color.clear.frame(width: notch.width)
                TimmyEye(big: hv.over).frame(width: w)
            }
            .frame(height: notch.height)
            if hv.over {
                TimmyMouth()
                    .frame(height: mouthH - 4)
                    .padding(.bottom, 4)
                    .transition(.opacity.combined(with: .scale(scale: 0.6, anchor: .top)))
            }
        }
        .padding(.horizontal, shoulder)
        .islandSurface(radius: hv.over ? 14 : 12)
        .animation(.spring(response: 0.3, dampingFraction: 0.8), value: hv.over)
    }

    var statusPill: some View {
        let msg = message
        let w = (msg != nil ? msgWingW : wingW) + (hv.over ? hoverGrow : 0)
        let saved = msg?.contains("נשמר") ?? false
        return HStack(spacing: 0) {
            // כנף שמאל: נקודה אדומה / סימן סטטוס
            ZStack {
                if msg == nil {
                    RecDot(size: 8)
                } else if saved {
                    Image(systemName: "checkmark").font(.system(size: 11, weight: .semibold)).foregroundStyle(okGreen)
                } else {
                    Image(systemName: "exclamationmark").font(.system(size: 11, weight: .bold)).foregroundStyle(amber)
                }
            }
            .frame(width: w)
            Color.clear.frame(width: notch.width)
            // כנף ימין: טקסט
            Text(he(msg.map { $0.replacingOccurrences(of: " ✓", with: "") } ?? "הקלטה"))
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.white.opacity(0.9))
                .lineLimit(1).minimumScaleFactor(0.8)
                .frame(width: w - 12, alignment: .trailing)
                .padding(.trailing, 12)
                .id(msg ?? "idle")
                .transition(.opacity)
        }
        .frame(height: notch.height)
        .padding(.horizontal, shoulder)
        .islandSurface(radius: 12)
        .animation(.easeOut(duration: 0.2), value: hv.over)
        .animation(island, value: msg)
    }

    // MARK: כרטיס הקלטה
    var card: some View {
        let finishing = r.phase == .finishing
        return VStack(spacing: 0) {
            // כותרת בתוך פס הנוטש
            HStack(spacing: 0) {
                HStack(spacing: 12) {
                    stopControl(finishing)
                    Waveform(level: r.level, active: !finishing)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Color.clear.frame(width: notch.width)
                HStack(spacing: 7) {
                    Text(String(format: "%d:%02d", r.elapsed / 60, r.elapsed % 60))
                        .font(.system(size: 15, weight: .semibold).monospacedDigit())
                        .foregroundStyle(.white.opacity(finishing ? 0.5 : 1))
                    RecDot(size: 7, pulsing: true).opacity(finishing ? 0 : 1)
                }
                .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .frame(height: notch.height)

            HStack(alignment: .top, spacing: 16) {
                notes.frame(width: 220)
                transcript
            }
            .padding(.top, 12)
            .padding(.bottom, 16)
        }
        .padding(.horizontal, 18)
        .frame(width: cardBodyW, height: cardBodyH)
        .padding(.horizontal, shoulder)
        .islandSurface(radius: 24, fill: bgColor)
    }

    func stopControl(_ finishing: Bool) -> some View {
        HStack(spacing: 6) {
            if finishing {
                ProgressView().controlSize(.mini).tint(.white)
                Text("שומר…").font(.system(size: 12, weight: .semibold)).foregroundStyle(.white.opacity(0.7))
            } else {
                RoundedRectangle(cornerRadius: 2.5, style: .continuous).fill(recRed).frame(width: 9, height: 9)
                Text("עצור").font(.system(size: 12, weight: .semibold)).foregroundStyle(.white.opacity(0.9))
            }
        }
        .padding(.horizontal, 10)
        .frame(height: min(28, notch.height - 4))
        .background(Capsule().fill(Color.white.opacity(hv.overStop && !finishing ? 0.2 : 0.12)))
        .animation(.easeOut(duration: 0.15), value: hv.overStop)
        .stopButtonArea()
    }

    var notes: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 5) {
                Image(systemName: "sparkles").font(.system(size: 9.5, weight: .semibold)).foregroundStyle(amber)
                Text("נקודות עיקריות").font(.system(size: 11, weight: .semibold)).foregroundStyle(.white.opacity(0.5))
            }
            if r.bullets.isEmpty {
                bulletRow("הנקודות יופיעו תוך דקה…", dot: false)
            } else {
                ScrollView(showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(r.bullets.enumerated()), id: \.offset) { _, b in
                            bulletRow(b, dot: true).transition(.opacity)
                        }
                    }
                    .animation(.easeOut(duration: 0.3), value: r.bullets)
                    .padding(.top, 2)
                }
                .defaultScrollAnchor(.top, for: .alignment)
                .defaultScrollAnchor(.bottom, for: .initialOffset)
                .defaultScrollAnchor(.bottom, for: .sizeChanges)
                .mask(VStack(spacing: 0) {
                    LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: 8)
                    Rectangle()
                })
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.white.opacity(0.07)))
        .environment(\.layoutDirection, .rightToLeft)
    }

    // שורת נקודה; במצב ריק הנקודה שקופה כך שהטקסט מתיישר בדיוק על קצה טקסט הבולטים
    func bulletRow(_ s: String, dot: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Circle().fill(dot ? amber.opacity(0.85) : .clear).frame(width: 4, height: 4)
                .alignmentGuide(.firstTextBaseline) { d in d[.bottom] + 3.5 }
            Text(he(s)).font(.system(size: 12.5))
                .foregroundStyle(.white.opacity(dot ? 0.8 : 0.3))
                .lineSpacing(1.5)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // אנימציית "מתמלל": שלוש נקודות שעולות ויורדות
    struct ThinkingDots: View {
        @State private var on = false
        var body: some View {
            HStack(spacing: 5) {
                Text("מתמלל").font(.system(size: 13, weight: .medium)).foregroundColor(.white.opacity(0.4))
                ForEach(0..<3) { i in
                    Circle().fill(Color.white.opacity(0.55)).frame(width: 5, height: 5)
                        .offset(y: on ? -3 : 2)
                        .animation(.easeInOut(duration: 0.45).repeatForever().delay(Double(i) * 0.15), value: on)
                }
            }
            .padding(.vertical, 4)
            .onAppear { on = animOn }
        }
    }

    // תווית דובר קטנה: "אני" בכתום, "צד שני" באפור
    func speakerTag(_ sp: String, show: Bool) -> Text {
        guard show else { return Text("") }
        return Text(sp == "me" ? "אני  " : "צד שני  ")
            .font(.system(size: 12, weight: .semibold))
            .foregroundColor(sp == "me" ? amber : .white.opacity(0.5))
    }

    var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 8) {
                    Color.clear.frame(height: 6)
                    ForEach(Array(r.lines.enumerated()), id: \.element.id) { i, l in
                        (speakerTag(l.speaker, show: i == 0 || r.lines[i - 1].speaker != l.speaker)
                         + Text(he(l.text)).font(.system(size: 16)).foregroundColor(.white.opacity(l.speaker == "me" ? 0.78 : 0.95)))
                            .lineSpacing(3)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if !r.partial.isEmpty {
                        (speakerTag(r.partialSpeaker, show: r.lines.last?.speaker != r.partialSpeaker)
                         + Text(he(r.partial)).font(.system(size: 16)).foregroundColor(.white.opacity(0.45)))
                            .lineSpacing(3)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if r.partial.isEmpty && r.thinking {
                        HStack(spacing: 6) {
                            if r.lines.last?.speaker != r.partialSpeaker { speakerTag(r.partialSpeaker, show: true) }
                            ThinkingDots()
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if r.lines.isEmpty && r.partial.isEmpty && !r.thinking {
                        Text(r.loading ? "טוען מודל…" : "מקשיב…")
                            .font(.system(size: 14)).foregroundStyle(.white.opacity(0.35))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Color.clear.frame(height: 1).id("end")
                }
                .environment(\.layoutDirection, .rightToLeft)
            }
            .mask(VStack(spacing: 0) {
                LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: 14)
                Rectangle()
            })
            .onAppear { proxy.scrollTo("end", anchor: .bottom) }
            .onChange(of: r.lines.count) { _, _ in withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
            .onChange(of: r.partial) { _, _ in proxy.scrollTo("end", anchor: .bottom) }
        }
    }
}

// בלי גרירה (הבועה מעוגנת לנוטש). לחיצה: בבועה = התחלה, בכרטיס = עצירה רק על הכפתור. ריחוף מזין את HoverState.
final class BubbleHost: NSHostingView<BubbleView> {
    var onClick: (NSPoint) -> Void = { _ in }
    private var tracking: NSTrackingArea?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .mouseMoved, .activeAlways, .inVisibleRect],
                               owner: self, userInfo: nil)
        addTrackingArea(t); tracking = t
    }
    private func updateHover(_ e: NSEvent?) {
        guard let e else { hover.over = false; hover.overStop = false; return }
        let p = e.locationInWindow
        let flipped = CGPoint(x: p.x, y: bounds.height - p.y)
        // ריחוף רק מעל הצורה עצמה (לא מעל שולי הצל)
        let inShape = flipped.y <= bounds.height - bottomMargin && abs(flipped.x - bounds.midX) <= bounds.width / 2 - sideMargin
        if hover.over != inShape { hover.over = inShape }
        let s = stopRect.insetBy(dx: -6, dy: -6).contains(flipped)
        if hover.overStop != s { hover.overStop = s }
    }
    override func mouseEntered(with e: NSEvent) { updateHover(e) }
    override func mouseMoved(with e: NSEvent) { updateHover(e) }
    override func mouseExited(with e: NSEvent) { updateHover(nil) }
    override func mouseDown(with e: NSEvent) {}
    override func mouseDragged(with e: NSEvent) {}
    override func mouseUp(with e: NSEvent) { onClick(e.locationInWindow) }
    override func rightMouseDown(with e: NSEvent) {
        let m = NSMenu()
        m.addItem(withTitle: "פתח תיקיית הקלטות", action: #selector(AppDelegate.openFolder), keyEquivalent: "")
        m.addItem(withTitle: "שפר את טימי…", action: #selector(AppDelegate.openImprove), keyEquivalent: "")
        m.addItem(withTitle: "הרשאות והגדרה (טימי)", action: #selector(AppDelegate.openOnboarding), keyEquivalent: "")
        m.addItem(.separator())
        m.addItem(withTitle: "יציאה", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        NSMenu.popUpContextMenu(m, with: e, for: self)
    }
}

// פאנל שמותר לו לשבת מעל שורת התפריטים
final class NotchPanel: NSPanel {
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }
    override var canBecomeKey: Bool { false }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var panel: NSPanel!
    let recorder = Recorder()
    var bag = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ n: Notification) {
        panel = NotchPanel(contentRect: NSRect(origin: .zero, size: dotFrame),
                           styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.mainMenuWindow)) + 2)
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.isMovable = false
        let host = BubbleHost(rootView: BubbleView(r: recorder))
        host.onClick = { [weak self] p in self?.clicked(p) }
        host.sizingOptions = []   // החלון לא גדל לבד מהפינה, רק לפי setFrame
        panel.contentView = host

        // מיקום: צמוד לראש המסך, ממורכז על הנוטש
        place(size: dotFrame)
        panel.orderFrontRegardless()
        // הפעלה/עצירה מבחוץ (קיצור מקלדת או בדיקות): התראת מערכת com.timmy.toggle
        DistributedNotificationCenter.default().addObserver(forName: Notification.Name("com.timmy.toggle"), object: nil, queue: .main) { [weak self] _ in
            self?.recorder.toggle()
        }

        NotificationCenter.default.addObserver(forName: Notification.Name("timmy.needPermissions"), object: nil, queue: .main) { [weak self] _ in
            self?.showOnboarding(step: 1)
        }
        // שינוי צבע: מרעננים את כל המסכים
        NotificationCenter.default.addObserver(forName: Notification.Name("timmy.themeChanged"), object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            self.recorder.objectWillChange.send(); self.onboarding.objectWillChange.send(); self.improve.objectWillChange.send()
            hover.objectWillChange.send()
        }
        NotificationCenter.default.addObserver(forName: Notification.Name("timmy.diskFull"), object: nil, queue: .main) { [weak self] _ in
            self?.showOnboarding(step: 5)
        }
        if !UserDefaults.standard.bool(forKey: "onboarded") && !args.contains(where: { $0.hasPrefix("--demo") }) {
            showOnboarding(step: nil)
        }
        if args.contains("--demo-hover") { hover.over = true }
        if args.contains("--demo-improve") { DispatchQueue.main.async { self.openImprove() } }
        if args.contains("--demo-onboarding") {
            let st = args.firstIndex(of: "--demo-onboarding").flatMap { $0 + 1 < args.count ? Int(args[$0 + 1]) : nil } ?? 0
            showOnboarding(step: st)
        }

        recorder.$phase.sink { [weak self] ph in
            guard let self else { return }
            let card = (ph == .recording || ph == .finishing)
            self.place(size: card ? cardSize : dotFrame)
        }.store(in: &bag)
        if args.contains("--demo-card") || args.contains("--demo-finishing") || args.contains("--demo-thinking") { recorder.startDemo(finishing: args.contains("--demo-finishing")) }
        if let i = args.firstIndex(of: "--demo-message"), i + 1 < args.count {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self.recorder.phase = .message(args[i + 1]) }
        }
    }

    // מעוגן למרכז העליון: הקצה העליון = ראש המסך
    func place(size: NSSize) {
        panel.setFrame(NSRect(x: (notch.centerX - size.width / 2).rounded(), y: notch.top - size.height,
                              width: size.width, height: size.height), display: true)
    }

    func clicked(_ p: NSPoint) {
        if recorder.isCard {
            let flipped = CGPoint(x: p.x, y: panel.frame.height - p.y)
            if stopRect.insetBy(dx: -6, dy: -6).contains(flipped) { recorder.toggle() }
        } else {
            recorder.toggle()
        }
    }

    var onboardingWindow: NSWindow?
    let onboarding = Onboarding()
    @objc func openOnboarding() { showOnboarding(step: 1) }

    var improveWindow: NSWindow?
    let improve = ImproveModel()
    @objc func openImprove() {
        improve.reload()
        if improveWindow == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 720),
                             styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
            w.titlebarAppearsTransparent = true; w.titleVisibility = .hidden
            w.isMovableByWindowBackground = true; w.isReleasedWhenClosed = false
            w.backgroundColor = NSColor(bgColor); w.appearance = NSAppearance(named: .darkAqua)
            w.contentView = NSHostingView(rootView: ImproveView(m: improve))
            w.center(); improveWindow = w
        }
        NSApp.activate(ignoringOtherApps: true)
        improveWindow?.makeKeyAndOrderFront(nil)
    }
    func showOnboarding(step: Int?) {
        if let step { onboarding.go(step) }
        if onboardingWindow == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 500, height: 600),
                             styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
            w.titlebarAppearsTransparent = true; w.titleVisibility = .hidden
            w.isMovableByWindowBackground = true; w.isReleasedWhenClosed = false
            w.backgroundColor = NSColor(bgColor); w.appearance = NSAppearance(named: .darkAqua)
            w.contentView = NSHostingView(rootView: OnboardingView(o: onboarding, close: { [weak w] in w?.close() }))
            w.center(); onboardingWindow = w
        }
        NSApp.activate(ignoringOtherApps: true)
        onboardingWindow?.makeKeyAndOrderFront(nil)
    }

    @objc func openFolder() {
        try? FileManager.default.createDirectory(at: recDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(recDir)
    }

    // פתיחה של האפליקציה כשהיא כבר רצה (מ-Spotlight / Launchpad / תיקיית היישומים): מראים את מסך ההגדרות
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showOnboarding(step: UserDefaults.standard.bool(forKey: "onboarded") ? 6 : nil)
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if recorder.phase == .recording {
            Task { @MainActor in
                await recorder.stop()
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { NSApp.reply(toApplicationShouldTerminate: true) }
            }
            return .terminateLater
        }
        return .terminateNow
    }
}


// תמונה מתוך Resources של האפליקציה (או מ-TIMMY_RES_DIR בבדיקות)
func resImage(_ name: String) -> NSImage? {
    if let p = Bundle.main.path(forResource: name, ofType: "png"), let i = NSImage(contentsOfFile: p) { return i }
    if let d = ProcessInfo.processInfo.environment["TIMMY_RES_DIR"] { return NSImage(contentsOfFile: d + "/" + name + ".png") }
    return nil
}

// MARK: - "שפר את טימי": הוראות אישיות לנקודות ולסיכומים
final class ImproveModel: ObservableObject {
    enum Status: Equatable { case idle, working, done(String), failed(String) }
    @Published var items: [String] = []
    @Published var draft = ""
    @Published var status: Status = .idle
    let examples = ["תחליף את הצבע לכחול", "עיניים ירוקות", "רקע כחול כהה", "תוריד אנימציות", "נקודות קצרות יותר, עד 8 מילים", "תוסיף סעיף \"החלטות\" בטבלה: נושא | מה סוכם | אחראי",
                    "תוסיף סעיף \"צעדים הבאים ללקוח\"", "תכתוב את הסיכום בגוף ראשון, כאילו אני כתבתי",
                    "בסוף תכתוב הודעת וואטסאפ קצרה ללקוח עם סיכום השיחה", "בלי אימוג'י ובלי הדגשות"]
    func reload() { items = (loadConfig()["custom_instructions"] as? [String]) ?? [] }
    private func save() { saveConfig(["custom_instructions": items]) }
    @Published var accent = Theme.accentHex
    func add(_ text: String? = nil) {
        let t = (text ?? draft).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        // הוראת מראה (צבע, רקע, עיניים, אנימציה): משנה את האפליקציה מיד, לא נשמרת כהוראה לסיכום
        if isAppearanceInstruction(t) {
            if text == nil { draft = "" }
            if let c = parseAppearance(t) { applyAppearance(c) } else { askClaudeForAppearance(t) }
            return
        }
        guard !items.contains(t) else { return }
        items.append(t); save(); if text == nil { draft = "" }
    }
    func applyAppearance(_ c: [String: Any]) {
        let wantedBg = c["background_color"] as? String
        Theme.apply(c); accent = Theme.accentHex
        if c["reset"] != nil { status = .done("המראה חזר לברירת המחדל"); return }
        var parts: [String] = []
        if c["accent_color"] != nil { parts.append("צבע") }
        if c["eye_color"] != nil { parts.append("עיניים") }
        if let w = wantedBg { parts.append(Theme.bgHex.uppercased() == w.uppercased() ? "רקע" : "רקע (הוכהה כדי שהטקסט יהיה קריא)") }
        if let a = c["animations"] as? Bool { parts.append(a ? "אנימציות פעילות" : "אנימציות כבויות") }
        status = .done("עודכן: " + parts.joined(separator: ", "))
    }
    func remove(_ i: Int) { guard items.indices.contains(i) else { return }; items.remove(at: i); save() }
    func applyColor(_ hex: String?) { applyAppearance(hex == nil ? ["reset": true] : ["accent_color": hex!]) }
    func askClaudeForAppearance(_ t: String) {
        guard let claude = Onboarding.shared?.claudePath() else { status = .failed("לא הבנתי מה לשנות במראה. נסה למשל: רקע כחול כהה"); return }
        status = .working
        let prompt = "המשתמש כתב הוראה לשינוי המראה של אפליקציה כהה (רקע שחור, טקסט לבן, צבע הדגשה כתום): \"" + t + "\". החזר JSON בלבד, עם רק המפתחות שהמשתמש ביקש לשנות מתוך: accent_color (#RRGGBB, כפתורים והדגשות), eye_color (#RRGGBB, העיניים והפה של הדמות), background_color (#RRGGBB, כהה מספיק לטקסט לבן), animations (true/false), reset (true, לחזרה לברירת המחדל). בלי שום טקסט נוסף."
        DispatchQueue.global().async {
            let p = Process(); p.executableURL = URL(fileURLWithPath: claude)
            p.arguments = ["-p", "--model", "haiku", prompt]
            p.currentDirectoryURL = URL(fileURLWithPath: NSTemporaryDirectory())
            let out = Pipe(); p.standardOutput = out; p.standardError = Pipe(); p.standardInput = FileHandle.nullDevice
            var changes: [String: Any]?
            if (try? p.run()) != nil {
                let deadline = Date().addingTimeInterval(45)
                while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.2) }
                if p.isRunning { p.terminate() }
                let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                if let a = text.firstIndex(of: "{"), let b = text.lastIndex(of: "}"),
                   let o = try? JSONSerialization.jsonObject(with: Data(text[a...b].utf8)) as? [String: Any] {
                    var c: [String: Any] = [:]
                    for k in ["accent_color", "eye_color", "background_color"] {
                        if let v = o[k] as? String, v.range(of: "^#[0-9A-Fa-f]{6}$", options: .regularExpression) != nil { c[k] = v.uppercased() }
                    }
                    if let v = o["animations"] as? Bool { c["animations"] = v }
                    if o["reset"] as? Bool == true { c["reset"] = true }
                    if !c.isEmpty { changes = c }
                }
            }
            DispatchQueue.main.async {
                if let changes { self.applyAppearance(changes) }
                else { self.status = .failed("לא הבנתי מה לשנות במראה. נסה למשל: רקע כחול כהה") }
            }
        }
    }
    // הסיכום האחרון בתיקיית ההקלטות
    func lastSummary() -> URL? {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: recDir, includingPropertiesForKeys: [.contentModificationDateKey]) else { return nil }
        return files.filter { $0.pathExtension == "md" && !$0.lastPathComponent.hasSuffix(" נקודות.md") }
            .max { (a, b) in
                let da = (try? a.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                let db = (try? b.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                return da < db }
    }
    // מחיל את ההוראות על הסיכום האחרון (live.py --restyle)
    func improveLast() {
        if !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { add() }
        guard !items.isEmpty else { status = .failed("קודם תכתוב מה לשפר"); return }
        guard let md = lastSummary() else { status = .failed("עוד אין שיחות שמורות"); return }
        status = .working
        let p = Process(); p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [worker, "--restyle", md.path]
        let out = Pipe(); p.standardOutput = out; p.standardError = FileHandle(forWritingAtPath: "/dev/null")
        p.terminationHandler = { _ in
            let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                if text.contains("\"changed\": true") {
                    self.status = .done("שופר: " + md.deletingPathExtension().lastPathComponent)
                    NSWorkspace.shared.activateFileViewerSelecting([md])
                } else { self.status = .failed("לא הצלחתי לשפר, בדוק שה-Claude מחובר") }
            }
        }
        do { try p.run() } catch { status = .failed("שגיאה בהפעלה") }
    }
}

struct ImproveView: View {
    @ObservedObject var m: ImproveModel
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                if let img = resImage("timmy") { Image(nsImage: img).resizable().frame(width: 44, height: 44) }
                VStack(alignment: .leading, spacing: 3) {
                    Text("שפר את טימי").font(.system(size: 22, weight: .bold)).foregroundColor(.white)
                    Text("תגיד לי מה לשנות בנקודות ובסיכומים. אזכור את זה לכל השיחות הבאות.")
                        .font(.system(size: 13)).foregroundColor(.white.opacity(0.6))
                }
            }
            .padding(.top, 26)

            // שדה כתיבה
            HStack(spacing: 8) {
                TextField("למשל: תוסיף סעיף של שאלות פתוחות", text: $m.draft)
                    .textFieldStyle(.plain).font(.system(size: 14)).foregroundColor(.white)
                    .padding(.vertical, 10).padding(.horizontal, 12)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.08)))
                    .onSubmit { m.add() }
                Button(action: { m.add() }) {
                    Text("הוסף").font(.system(size: 13, weight: .semibold)).foregroundColor(.black)
                        .padding(.vertical, 9).padding(.horizontal, 16).background(Capsule().fill(amber))
                }.buttonStyle(.plain)
            }

            // דוגמאות
            Text("רעיונות").font(.system(size: 11, weight: .semibold)).foregroundColor(.white.opacity(0.45))
            FlowChips(items: m.examples.filter { !m.items.contains($0) }) { m.add($0) }

            AppearanceRow(m: m)

            InstructionsList(m: m).frame(maxHeight: .infinity)

            ImproveFooter(m: m)
        .padding(.horizontal, 28).padding(.bottom, 24)
        .frame(width: 560, height: 720)
        .background(bgColor)
        .environment(\.layoutDirection, .rightToLeft)
    }
}

// שורת "מראה" בחלון השיפור: צבעים, אנימציות, איפוס
struct AppearanceRow: View {
    @ObservedObject var m: ImproveModel
    var swatches: [(String, String)] {
        let eye: String = Theme.eyeHex.isEmpty ? Theme.accentHex : Theme.eyeHex
        return [("אפליקציה", Theme.accentHex), ("עיניים", eye), ("רקע", Theme.bgHex)]
    }
    var body: some View {
            HStack(spacing: 12) {
                Text(he("מראה")).font(.system(size: 13, weight: .medium)).foregroundColor(.white.opacity(0.85))
                ForEach(swatches, id: \.0) { item in
                    HStack(spacing: 5) {
                        Circle().fill(colorFromHex(item.1)).frame(width: 14, height: 14)
                            .overlay(Circle().stroke(Color.white.opacity(0.35), lineWidth: 1))
                        Text(he(item.0)).font(.system(size: 11)).foregroundColor(.white.opacity(0.6))
                    }
                }
                Text(he(Theme.animations ? "אנימציות פעילות" : "אנימציות כבויות")).font(.system(size: 11)).foregroundColor(.white.opacity(0.6))
                Spacer()
                if !Theme.isDefault {
                    Button(action: { m.applyAppearance(["reset": true]) }) {
                        Text(he("אפס מראה")).font(.system(size: 12)).foregroundColor(.white.opacity(0.7))
                            .padding(.vertical, 5).padding(.horizontal, 10).background(Capsule().fill(Color.white.opacity(0.08)))
                    }.buttonStyle(.plain)
                }
            }
            .padding(.vertical, 8).padding(.horizontal, 12)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.05)))
    }
}

struct InstructionsList: View {
    @ObservedObject var m: ImproveModel
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // ההוראות השמורות
            Text("ההוראות שלך (\(m.items.count))").font(.system(size: 11, weight: .semibold)).foregroundColor(.white.opacity(0.45))
            ScrollView {
                VStack(spacing: 6) {
                    if m.items.isEmpty {
                        Text("עוד אין הוראות. טימי כותב בסגנון ברירת המחדל.").font(.system(size: 13))
                            .foregroundColor(.white.opacity(0.4)).frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6)
                    }
                    ForEach(Array(m.items.enumerated()), id: \.offset) { i, t in
                        HStack(spacing: 8) {
                            Circle().fill(amber).frame(width: 5, height: 5)
                            Text(he(t)).font(.system(size: 13)).foregroundColor(.white.opacity(0.9))
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Button(action: { m.remove(i) }) {
                                Image(systemName: "xmark").font(.system(size: 10, weight: .bold)).foregroundColor(.white.opacity(0.5))
                                    .padding(6).background(Circle().fill(Color.white.opacity(0.08)))
                            }.buttonStyle(.plain)
                        }
                        .padding(.vertical, 8).padding(.horizontal, 12)
                        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.05)))
                    }
                }
            }
            .frame(minHeight: 70, maxHeight: .infinity)
        }
    }
}

struct ImproveFooter: View {
    @ObservedObject var m: ImproveModel
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // שיפור הסיכום האחרון
            VStack(spacing: 8) {
                Button(action: { m.improveLast() }) {
                    HStack(spacing: 8) {
                        if m.status == .working { ProgressView().controlSize(.small) }
                        Text(m.status == .working ? "משפר את הסיכום האחרון…" : "✨ שפר גם את הסיכום של השיחה האחרונה")
                            .font(.system(size: 14, weight: .semibold)).foregroundColor(.black)
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 11).background(Capsule().fill(amber))
                }
                .buttonStyle(.plain).disabled(m.status == .working)
                switch m.status {
                case .done(let name): Text(he("✓ " + name)).font(.system(size: 12)).foregroundColor(Color(red: 0.19, green: 0.82, blue: 0.35)).lineLimit(1)
                case .failed(let why): Text(he(why)).font(.system(size: 12)).foregroundColor(Color(red: 1, green: 0.4, blue: 0.4))
                default: Text("הנקודות החיות והסיכומים של השיחות הבאות כבר ייכתבו לפי ההוראות.").font(.system(size: 11)).foregroundColor(.white.opacity(0.4))
                }
            }
        }
        }
    }
}

// שבבי דוגמאות שנשברים לשורות
struct FlowChips: View {
    let items: [String]; let tap: (String) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(stride(from: 0, to: items.count, by: 2)), id: \.self) { i in
                HStack(spacing: 6) {
                    ForEach(items[i..<min(i + 2, items.count)], id: \.self) { t in
                        Button(action: { tap(t) }) {
                            Text("+ " + t).font(.system(size: 12)).foregroundColor(.white.opacity(0.8)).lineLimit(1)
                                .padding(.vertical, 6).padding(.horizontal, 10)
                                .background(Capsule().stroke(Color.white.opacity(0.18), lineWidth: 1))
                        }.buttonStyle(.plain)
                    }
                }
            }
        }
    }
}

// MARK: - טימי: אונבורדינג הרשאות
final class Onboarding: ObservableObject {
    @Published var step: Int
    @Published var screenOK = false
    @Published var micOK = false
    @Published var micDenied = false
    @Published var chromeOK = false
    @Published var folder = recDir.path
    @Published var freeGB: Double = 0
    @Published var launchAtLogin = false
    enum ClaudeState { case unknown, checking, ok, notInstalled, notLoggedIn }
    @Published var claude: ClaudeState = .unknown
    private var timer: Timer?
    static weak var shared: Onboarding?
    init() {
        step = UserDefaults.standard.integer(forKey: "onboardingStep")
        defer { Onboarding.shared = self }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refresh() }
    }
    func refresh() {
        let sc = CGPreflightScreenCaptureAccess()
        let st = AVCaptureDevice.authorizationStatus(for: .audio)
        let ch = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.google.Chrome") != nil
        if sc != screenOK { screenOK = sc }
        if (st == .authorized) != micOK { micOK = st == .authorized }
        if (st == .denied || st == .restricted) != micDenied { micDenied = st == .denied || st == .restricted }
        if ch != chromeOK { chromeOK = ch }
        let f = recDir.path
        if f != folder { folder = f }
        let g = (Double(freeBytes(at: recDir)) / 1e9 * 10).rounded() / 10
        if g != freeGB { freeGB = g }
        let l = SMAppService.mainApp.status == .enabled
        if l != launchAtLogin { launchAtLogin = l }
    }
    // Claude Code: איפה הוא מותקן
    func claudePath() -> String? {
        var cands: [String] = []
        if let p = loadConfig()["claude_path"] as? String, !p.isEmpty { cands.append(p) }
        cands += [home.path + "/.claude/local/claude", home.path + "/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
        if let vers = try? FileManager.default.contentsOfDirectory(atPath: home.path + "/.nvm/versions/node") {
            cands += vers.sorted(by: >).map { home.path + "/.nvm/versions/node/\($0)/bin/claude" }
        }
        return cands.first { FileManager.default.isExecutableFile(atPath: $0) }
    }
    // בדיקה אמיתית: שאלה קטנה ל-Claude. מצליח = מחובר לחשבון
    func checkClaude() {
        guard let path = claudePath() else { claude = .notInstalled; return }
        saveConfig(["claude_path": path])
        claude = .checking
        DispatchQueue.global().async {
            let p = Process(); p.executableURL = URL(fileURLWithPath: path)
            p.arguments = ["-p", "--model", "haiku", "ענה במילה אחת בלבד: מחובר"]
            p.currentDirectoryURL = URL(fileURLWithPath: NSTemporaryDirectory())
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = (path as NSString).deletingLastPathComponent + ":/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
            p.environment = env
            let out = Pipe(); p.standardOutput = out; p.standardError = Pipe(); p.standardInput = FileHandle.nullDevice
            var ok = false
            if (try? p.run()) != nil {
                let deadline = Date().addingTimeInterval(45)
                while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.2) }
                if p.isRunning { p.terminate() }
                else {
                    let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                    ok = p.terminationStatus == 0 && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                }
            }
            DispatchQueue.main.async { self.claude = ok ? .ok : .notLoggedIn }
        }
    }
    // פותח טרמינל עם claude, כדי שהמשתמש יתחבר לחשבון
    func openClaudeLogin() {
        guard let path = claudePath() else { return }
        let script = "tell application \"Terminal\"\nactivate\ndo script \"'\(path)'\"\nend tell"
        NSAppleScript(source: script)?.executeAndReturnError(nil)
    }

    func chooseFolder() {
        let p = NSOpenPanel()
        p.canChooseDirectories = true; p.canChooseFiles = false; p.canCreateDirectories = true
        p.allowsMultipleSelection = false; p.prompt = "שמור כאן"
        p.message = "איפה לשמור את השיחות, הסיכומים וההקלטות?"
        p.directoryURL = recDir
        NSApp.activate(ignoringOtherApps: true)
        if p.runModal() == .OK, let u = p.url {
            saveConfig(["output_dir": u.path]); refresh()
        }
    }
    func setLaunchAtLogin(_ on: Bool) {
        do { if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() } }
        catch { NSLog("login item: \(error)") }
        refresh()
    }
    func go(_ s: Int) { step = s; UserDefaults.standard.set(s, forKey: "onboardingStep") }
    func requestScreen() {
        _ = CGRequestScreenCaptureAccess()
        // ניסיון גישה אמיתי: רושם את טימי ברשימת ההרשאות של המערכת
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { _, _ in }
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
    }
    func requestMic() {
        if micDenied {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!)
            return
        }
        AVCaptureDevice.requestAccess(for: .audio) { _ in DispatchQueue.main.async { self.refresh() } }
    }
    // הרשאת הקלטת מסך נכנסת לתוקף רק אחרי הפעלה מחדש של האפליקציה
    func relaunch() {
        go(step)
        let path = Bundle.main.bundlePath
        let t = Process(); t.executableURL = URL(fileURLWithPath: "/bin/sh")
        t.arguments = ["-c", "sleep 1; open \"\(path)\""]
        try? t.run()
        NSApp.terminate(nil)
    }
    func finish() {
        // בפעם הראשונה: טימי עולה עם המחשב כברירת מחדל (אפשר לכבות במסך ההגדרות)
        if !UserDefaults.standard.bool(forKey: "loginItemAsked") {
            UserDefaults.standard.set(true, forKey: "loginItemAsked")
            if !launchAtLogin { setLaunchAtLogin(true) }
        }
        UserDefaults.standard.set(true, forKey: "onboarded"); go(0)
    }
}

struct OnboardingView: View {
    @ObservedObject var o: Onboarding
    var close: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            // התקדמות
            HStack(spacing: 6) {
                ForEach(0..<7) { i in
                    Capsule().fill(i <= o.step ? amber : Color.white.opacity(0.15)).frame(width: i == o.step ? 22 : 8, height: 6)
                }
            }
            .padding(.top, 34)
            .animation(.easeInOut(duration: 0.25), value: o.step)

            Spacer(minLength: 18)
            Group {
                switch o.step {
                case 0: welcome
                case 1: screenStep
                case 2: micStep
                case 3: chromeStep
                case 4: claudeStep
                case 5: folderStep
                default: doneStep
                }
            }
            .transition(.opacity)
            Spacer(minLength: 12)
            // התמונה והלינק של היוצר: בכל מסך באונבורדינג
            instagramLink(o.step >= 6 ? "טיפים ועדכונים באינסטגרם" : "נבנה על ידי")
        }
        .padding(.horizontal, 40).padding(.bottom, 30)
        .frame(width: 500, height: 600)
        .background(bgColor)
        .environment(\.layoutDirection, .rightToLeft)
        .animation(.easeInOut(duration: 0.2), value: o.step)
    }

    var avatar: some View {
        Group {
            if let img = resImage("timmy") {
                Image(nsImage: img).resizable().interpolation(.high).frame(width: 96, height: 96)
                    .shadow(color: amber.opacity(0.35), radius: 16)
            } else { avatarFallback }
        }
    }

    var avatarFallback: some View {
        ZStack {
            Circle().fill(LinearGradient(colors: [amber, amber.opacity(0.6)], startPoint: .top, endPoint: .bottom))
                .frame(width: 76, height: 76)
                .shadow(color: amber.opacity(0.45), radius: 18)
            Image(systemName: "waveform").font(.system(size: 32, weight: .semibold)).foregroundColor(.black)
        }
    }

    func header(_ title: String, _ sub: String) -> some View {
        VStack(spacing: 10) {
            Text(he(title)).font(.system(size: 24, weight: .bold)).foregroundColor(.white)
            Text(he(sub)).font(.system(size: 14)).foregroundColor(.white.opacity(0.65))
                .multilineTextAlignment(.center).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
        }
    }

    func primary(_ title: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(he(title)).font(.system(size: 15, weight: .semibold)).foregroundColor(.black)
                .frame(maxWidth: 260).padding(.vertical, 11)
                .background(Capsule().fill(amber))
        }.buttonStyle(.plain)
    }

    func secondary(_ title: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(he(title)).font(.system(size: 13, weight: .medium)).foregroundColor(.white.opacity(0.7))
                .padding(.vertical, 8).padding(.horizontal, 16)
                .background(Capsule().fill(Color.white.opacity(0.08)))
        }.buttonStyle(.plain)
    }

    func status(_ ok: Bool, _ okText: String, _ waitText: String, critical: Bool = false) -> some View {
        HStack(spacing: 8) {
            Image(systemName: ok ? "checkmark.circle.fill" : (critical ? "exclamationmark.triangle.fill" : "circle.dashed"))
                .foregroundColor(ok ? Color(red: 0.19, green: 0.82, blue: 0.35) : (critical ? Color(red: 1, green: 0.27, blue: 0.27) : .white.opacity(0.4)))
            Text(he(ok ? okText : waitText)).font(.system(size: 13, weight: .medium))
                .foregroundColor(ok ? .white : .white.opacity(0.55))
        }
        .padding(.vertical, 9).padding(.horizontal, 14)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.06)))
    }

    // לינק לאינסטגרם של היוצר
    func instagramLink(_ label: String) -> some View {
        Button(action: { NSWorkspace.shared.open(URL(string: "https://www.instagram.com/yaniv_shef/")!) }) {
            HStack(spacing: 8) {
                if let img = resImage("creator") {
                    Image(nsImage: img).resizable().interpolation(.high).scaledToFill()
                        .frame(width: 26, height: 26).clipShape(Circle())
                        .overlay(Circle().stroke(amber.opacity(0.7), lineWidth: 1.2))
                } else {
                    Image(systemName: "camera.circle.fill").font(.system(size: 14))
                }
                Text(he(label)).font(.system(size: 12, weight: .medium))
                Text("@yaniv_shef").font(.system(size: 12, weight: .semibold)).environment(\.layoutDirection, .leftToRight)
            }
            .foregroundColor(amber.opacity(0.9))
            .padding(.vertical, 5).padding(.leading, 6).padding(.trailing, 14)
            .background(Capsule().stroke(amber.opacity(0.35), lineWidth: 1))
        }
        .buttonStyle(.plain)
        .onHover { inside in if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() } }
    }

    var welcome: some View {
        VStack(spacing: 22) {
            avatar
            header("היי, אני טימי", "אני מקליט את השיחות שלך בכרום (מיט, זום בדפדפן, וובינרים),\nמתמלל אותן בעברית בזמן אמת,\nורושם לך נקודות, סיכום ומשימות בסוף כל שיחה.")
            Text("הכל קורה על המחשב שלך. בוא נסדר שתי הרשאות וזהו.")
                .font(.system(size: 12)).foregroundColor(.white.opacity(0.45))
            primary("בוא נתחיל") { o.go(1) }
        }
    }

    var screenStep: some View {
        VStack(spacing: 18) {
            Image(systemName: "rectangle.dashed.badge.record").font(.system(size: 40)).foregroundColor(amber)
            header("הרשאה 1: הקול של כרום", "כדי לשמוע את הצד השני בשיחה, מק דורש הרשאת\n\"הקלטת מסך ושמע מערכת\". אני לא מצלם את המסך, רק מקשיב לכרום.")
            VStack(alignment: .leading, spacing: 6) {
                Text(he("1.  לחץ \"פתח הגדרות\"")).foregroundColor(.white.opacity(0.75))
                Text(he("2.  הדלק את המתג ליד \"טימי\" (אם מבקשים, הכנס סיסמה)")).foregroundColor(.white.opacity(0.75))
                Text(he("3.  חזור לכאן ולחץ \"הפעל את טימי מחדש\"")).foregroundColor(.white.opacity(0.75))
                Text("טימי לא ברשימה? לחץ על + מתחת לרשימה ובחר את טימי מתיקיית היישומים.")
                    .font(.system(size: 12)).foregroundColor(amber.opacity(0.85))
            }.font(.system(size: 13)).frame(maxWidth: .infinity, alignment: .leading)
            status(o.screenOK, "ההרשאה פעילה", "עוד לא פעילה")
            if o.screenOK {
                primary("המשך") { o.go(2) }
            } else {
                HStack(spacing: 10) {
                    primary("פתח הגדרות") { o.requestScreen() }
                    secondary("הפעל את טימי מחדש") { o.relaunch() }
                }
            }
        }
    }

    var micStep: some View {
        VStack(spacing: 18) {
            Image(systemName: "mic.fill").font(.system(size: 40)).foregroundColor(amber)
            header("הרשאה 2: הקול שלך", "ככה אדע מה אתה אמרת ומה הצד השני אמר,\nוהסיכום והמשימות ייצאו מדויקים.")
            status(o.micOK, "המיקרופון מחובר", o.micDenied ? "המיקרופון חסום, צריך להדליק בהגדרות" : "עוד לא אושר")
            if o.micOK {
                primary("המשך") { o.go(3) }
            } else {
                primary(o.micDenied ? "פתח הגדרות מיקרופון" : "אשר מיקרופון") { o.requestMic() }
                secondary("דלג, אקליט רק את כרום") { o.go(3) }
            }
        }
    }

    var chromeStep: some View {
        VStack(spacing: 18) {
            Image(systemName: "globe").font(.system(size: 40)).foregroundColor(amber)
            header("גוגל כרום", "אני מקשיב רק לכרום. שיחות במיט, בזום מהדפדפן או בכל אתר אחר בכרום.\nטיפ: עם אוזניות התמלול יוצא הכי נקי.")
            status(o.chromeOK, "כרום מותקן", "כרום לא נמצא במחשב")
            if o.chromeOK {
                primary("המשך") { o.go(4) }
            } else {
                primary("הורד את כרום") { NSWorkspace.shared.open(URL(string: "https://www.google.com/chrome/")!) }
                secondary("המשך בכל זאת") { o.go(4) }
            }
        }
    }

    var claudeStep: some View {
        VStack(spacing: 18) {
            Group {
                if let logo = resImage("claude") {
                    Image(nsImage: logo).resizable().interpolation(.high).frame(width: 46, height: 46)
                } else {
                    Image(systemName: "sparkles").font(.system(size: 40)).foregroundColor(amber)
                }
            }
            header("החיבור ל-Claude", "את הנקודות, הסיכום והמשימות כותב Claude,\nדרך Claude Code שמותקן אצלך ועם המנוי שלך. בלי מפתחות ובלי הגדרות.\nרק הטקסט של השיחה נשלח, לא ההקלטה.")
            switch o.claude {
            case .unknown, .checking:
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(he("בודק את החיבור ל-Claude…")).font(.system(size: 13, weight: .medium)).foregroundColor(.white.opacity(0.7))
                }
                .padding(.vertical, 9).padding(.horizontal, 14)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.06)))
            case .ok:
                status(true, "מחובר ל-Claude", "")
                primary("המשך") { o.go(5) }
            case .notInstalled:
                status(false, "", "לא מצאתי את Claude Code במחשב", critical: true)
                Text(he("בלי Claude אקליט ואתמלל, אבל בלי נקודות וסיכום.")).font(.system(size: 12)).foregroundColor(.white.opacity(0.5))
                // כפתור הורדה עם הלוגו של Claude
                Button(action: { NSWorkspace.shared.open(URL(string: "https://claude.com/claude-code")!) }) {
                    HStack(spacing: 10) {
                        if let logo = resImage("claude") {
                            Image(nsImage: logo).resizable().interpolation(.high).frame(width: 22, height: 22)
                        }
                        Text(he("הורד את Claude Code")).font(.system(size: 15, weight: .semibold)).foregroundColor(.white)
                    }
                    .frame(maxWidth: 280).padding(.vertical, 11)
                    .background(Capsule().fill(Color(red: 0.85, green: 0.47, blue: 0.34).opacity(0.22)))
                    .overlay(Capsule().stroke(Color(red: 0.85, green: 0.47, blue: 0.34).opacity(0.8), lineWidth: 1.2))
                }
                .buttonStyle(.plain)
                .onHover { inside in if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() } }
                secondary("התקנתי, בדוק שוב") { o.checkClaude() }
                secondary("המשך בלי סיכומים") { o.go(5) }
            case .notLoggedIn:
                status(false, "", "יש Claude Code, אבל הוא לא מחובר לחשבון", critical: true)
                Text(he("לחץ \"התחבר\", ייפתח טרמינל עם Claude. התחבר לחשבון שלך, ואז חזור לכאן ולחץ \"בדוק שוב\"."))
                    .font(.system(size: 12)).foregroundColor(.white.opacity(0.6)).multilineTextAlignment(.center)
                HStack(spacing: 10) {
                    primary("התחבר") { o.openClaudeLogin() }
                    secondary("בדוק שוב") { o.checkClaude() }
                }
                secondary("המשך בלי סיכומים") { o.go(5) }
            }
        }
        .onAppear {
            if let st = ProcessInfo.processInfo.environment["TIMMY_DEMO_CLAUDE"] {
                o.claude = st == "missing" ? .notInstalled : .notLoggedIn; return
            }
            if o.claude == .unknown || o.claude == .notInstalled { o.checkClaude() }
        }
    }

    var shortFolder: String { FileManager.default.displayName(atPath: o.folder) }
    var parentFolder: String { FileManager.default.displayName(atPath: (o.folder as NSString).deletingLastPathComponent) }
    var spaceOK: Bool { o.freeGB >= 2 }

    var folderStep: some View {
        VStack(spacing: 18) {
            Image(systemName: "folder.fill").font(.system(size: 40)).foregroundColor(amber)
            header("איפה לשמור את השיחות?", "לכל שיחה נשמרים סיכום, תמלול מלא והקלטה.\nשעה של שיחה תופסת בערך 30 מגה אחרי השמירה.")
            HStack(spacing: 10) {
                Image(systemName: "folder").foregroundColor(.white.opacity(0.6))
                VStack(alignment: .leading, spacing: 2) {
                    Text(he(shortFolder)).font(.system(size: 14, weight: .semibold)).foregroundColor(.white).lineLimit(1)
                    Text("בתוך \(parentFolder)").font(.system(size: 11)).foregroundColor(.white.opacity(0.5)).lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 10).padding(.horizontal, 14)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.06)))
            status(spaceOK, String(format: "יש %.1f ג׳יגה פנויים, מספיק להרבה שיחות", o.freeGB),
                   o.freeGB < 0.5 ? String(format: "רק %.1f ג׳יגה פנויים: אין מספיק מקום להקלטה. פנה מקום או בחר דיסק אחר", o.freeGB)
                                  : String(format: "רק %.1f ג׳יגה פנויים: מספיק לכמה שיחות, כדאי לפנות מקום", o.freeGB),
                   critical: o.freeGB < 0.5)
            HStack(spacing: 10) {
                primary("המשך") { o.go(6) }
                secondary("בחר תיקייה אחרת") { o.chooseFolder() }
            }
        }
    }

    var doneStep: some View {
        VStack(spacing: 20) {
            avatar
            header("מוכן!", "אני מחכה לך למעלה באמצע המסך, ליד הנוטש.\nלחיצה עליי מתחילה הקלטה, ו\"עצור\" מסיים.\nבסוף אפתח לך את הסיכום בתיקייה.")
            VStack(spacing: 8) {
                status(o.screenOK, "קול של כרום: פעיל", "קול של כרום: לא פעיל")
                status(o.micOK, "מיקרופון: פעיל", "מיקרופון: כבוי (רק הצד השני יוקלט)")
                Button(action: { o.go(4) }) { status(o.claude == .ok, "חיבור ל-Claude: פעיל (נקודות וסיכומים)", "חיבור ל-Claude: כבוי (רק תמלול)") }
                    .buttonStyle(.plain)
                Button(action: { o.go(5) }) { status(spaceOK, "שמירה ב: " + shortFolder, "שמירה ב: " + shortFolder + " (מעט מקום)") }
                    .buttonStyle(.plain)
                Toggle(isOn: Binding(get: { o.launchAtLogin }, set: { o.setLaunchAtLogin($0) })) {
                    Text("הפעל את טימי כשהמחשב נדלק").font(.system(size: 13, weight: .medium)).foregroundColor(.white.opacity(0.85))
                }
                .toggleStyle(.switch).tint(amber)
            }
            primary("יאללה") { o.finish(); close() }
            secondary("✨ שפר את טימי") { (NSApp.delegate as? AppDelegate)?.openImprove() }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
