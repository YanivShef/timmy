// בועת הקלטה צפה בסגנון Liquid Glass, עם תמלול בלייב של האודיו של Google Chrome בלבד.
// לחיצה = התחלה/עצירה, גרירה = הזזה, קליק ימני = תפריט.
import Cocoa
import SwiftUI
import Combine
import ScreenCaptureKit
import AVFoundation
import CoreMedia
import ServiceManagement
import WebKit
import PDFKit
import UniformTypeIdentifiers

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
    if FileManager.default.fileExists(atPath: configURL.path) && loadConfig().isEmpty,
       let raw = try? Data(contentsOf: configURL), raw.count > 2 {
        NSLog("config.json לא תקין, לא שומר כדי לא למחוק הגדרות"); return
    }
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
// מילה שלמה בעברית, עם תחיליות אפשריות (ה/ל/ב/ו/ש), לא חלק ממילה אחרת ("קרקע" ≠ "רקע", "ordered" ≠ "red")
func hasWord(_ l: String, _ w: String, prefixes: Bool = true) -> Bool {
    let pre = prefixes ? "[הלבוש]?" : ""
    return l.range(of: "(^|[\\s,.!?:\"'()])" + pre + NSRegularExpression.escapedPattern(for: w) + "($|[\\s,.!?:\"'()])", options: .regularExpression) != nil
}
func colorIn(_ l: String) -> String? {
    if let r = l.range(of: "(^|\\s)#[0-9a-f]{6}($|\\s)", options: .regularExpression) {
        return String(l[r]).trimmingCharacters(in: .whitespaces).uppercased()
    }
    if hasWord(l, "כתום") || hasWord(l, "orange", prefixes: false) { return defaultAccent }
    if hasWord(l, "שחור") || hasWord(l, "black", prefixes: false) { return "#000000" }
    for (name, hex) in colorNames {
        let english = name.range(of: "^[a-z]+$", options: .regularExpression) != nil
        if hasWord(l, name, prefixes: !english) || (!english && (hasWord(l, name + "ה") || hasWord(l, name + "ים") || hasWord(l, name + "ות"))) { return hex }
    }
    return nil
}
// הוראת מראה רק כשהיא באמת על האפליקציה. הוראה שמדברת על הסיכום/משימות/נקודות היא הוראה לסיכום, גם אם יש בה צבע
func isAppearanceInstruction(_ t: String) -> Bool {
    let l = t.lowercased()
    let summaryWords = ["סיכום", "הסיכום", "סעיף", "נקודות", "הנקודות", "משימות", "המשימות", "טבלה", "החלטות", "הודעה", "וואטסאפ", "תמלול", "שיחה", "שיחות", "לקוח", "list", "table", "summary"]
    let explicitApp = ["טימי", "האפליקציה", "אפליקציה"].contains { l.contains($0) }
    if !explicitApp && summaryWords.contains(where: { hasWord(l, $0, prefixes: false) || l.contains(" " + $0) }) { return false }
    let looks = ["צבע", "רקע", "עיניים", "אנימציה", "אנימציות", "מצמוץ", "מראה"].contains { hasWord(l, $0) }
        || ["color", "background", "eyes", "animation", "animations"].contains { hasWord(l, $0, prefixes: false) }
    if looks || explicitApp && colorIn(l) != nil { return true }
    // פקודה קצרה עם צבע ("כחול", "תשנה לירוק", "תחזיר לכתום", "make it blue")
    return l.split(separator: " ").count <= 4 && colorIn(l) != nil
}
func isColorInstruction(_ t: String) -> Bool { isAppearanceInstruction(t) }
// מפענח הוראת מראה בלי Claude. מחזיר שינויים, או nil אם צריך את Claude (ניסוח יצירתי)
func parseAppearance(_ t: String) -> [String: Any]? {
    let l = t.lowercased()
    let has = { (ws: [String]) in ws.contains { hasWord(l, $0) } }
    if (has(["אפס", "תאפס", "reset"]) || l.contains("ברירת מחדל") || l.contains("כמו שהיה") || l.contains("תחזיר הכל"))
        && (has(["מראה", "הכל", "צבעים", "עיצוב"]) || l.contains("ברירת מחדל") || l.contains("כמו שהיה")) { return ["reset": true] }
    var out: [String: Any] = [:]
    if has(["אנימציה", "אנימציות", "מצמוץ", "animation", "animations"]) {
        if has(["תוריד", "בלי", "תכבה", "תבטל", "תעצור", "off"]) { out["animations"] = false }
        else if has(["תחזיר", "תדליק", "תפעיל", "on"]) { out["animations"] = true }
    }
    let target: String = has(["רקע", "background"]) ? "background_color" : (has(["עיניים", "eyes"]) ? "eye_color" : "accent_color")
    if has(["מקורי", "מקוריים", "מקוריות"]) {
        out[target] = target == "background_color" ? "#000000" : (target == "eye_color" ? "" : defaultAccent)
    } else if let hex = colorIn(l) {
        out[target] = hex
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
        m.addItem(withTitle: "השיחות שלי…", action: #selector(AppDelegate.openCalls), keyEquivalent: "")
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
        // בדיקות: יצירת PDF משורת הפקודה, בלי בועה ובלי חלונות
        if args.contains("--make-pdf") { _ = onboarding; runPDFCLI(); return }
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
            self.calls.objectWillChange.send(); self.pdfDesign.objectWillChange.send()
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
        if args.contains("--demo-calls") { DispatchQueue.main.async { self.runCallsDemo() } }
        if args.contains("--demo-design") { DispatchQueue.main.async { self.runDesignDemo() } }
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
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 820),
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
    // "השיחות שלי"
    var callsWindow: NSWindow?
    let calls = CallsModel()
    @objc func openCalls() {
        calls.start()
        if callsWindow == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1140, height: 800),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
            w.titlebarAppearsTransparent = true; w.titleVisibility = .hidden
            w.isMovableByWindowBackground = true; w.isReleasedWhenClosed = false
            w.backgroundColor = NSColor(bgColor); w.appearance = NSAppearance(named: .darkAqua)
            w.minSize = NSSize(width: 940, height: 640)
            let host = NSHostingView(rootView: CallsView(m: calls))
            host.sizingOptions = [.minSize]
            w.contentView = host
            w.center(); callsWindow = w
            NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: w, queue: .main) { [weak self] _ in self?.calls.stop() }
        }
        NSApp.activate(ignoringOtherApps: true)
        callsWindow?.makeKeyAndOrderFront(nil)
    }
    // "עיצוב ה-PDF" (צ'אט). send: הוראה שהגיעה מ"שפר את טימי"
    var pdfDesignWindow: NSWindow?
    let pdfDesign = PDFDesignModel()
    func openPDFDesign(send text: String?) {
        if pdfDesignWindow == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 680, height: 860),
                             styleMask: [.titled, .closable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
            w.titlebarAppearsTransparent = true; w.titleVisibility = .hidden
            w.isMovableByWindowBackground = true; w.isReleasedWhenClosed = false
            w.backgroundColor = NSColor(bgColor); w.appearance = NSAppearance(named: .darkAqua)
            let host = NSHostingView(rootView: PDFDesignView(m: pdfDesign))
            host.sizingOptions = [.minSize]
            w.contentView = host
            w.center(); pdfDesignWindow = w
        }
        NSApp.activate(ignoringOtherApps: true)
        pdfDesignWindow?.makeKeyAndOrderFront(nil)
        pdfDesign.load()
        if let text, !text.isEmpty { pdfDesign.send(text) }
    }
    // דמו לבדיקות: --demo-calls [--demo-select N] [--demo-query X] [--demo-send kind] [--demo-autosend] [--demo-connect] [--demo-empty-wa]
    func runCallsDemo() {
        openCalls()
        if let i = args.firstIndex(of: "--demo-select"), i + 1 < args.count, let n = Int(args[i + 1]), calls.calls.indices.contains(n) { calls.selectedID = calls.calls[n].id }
        if let i = args.firstIndex(of: "--demo-query"), i + 1 < args.count { calls.query = args[i + 1] }
        if args.contains("--demo-connect") { calls.overlay = .connect }
        if args.contains("--demo-connect-run") {   // בדיקה: מריץ את החיבור (עם claude מזויף) ובודק שוב
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { self.calls.connectToClaude() }
            DispatchQueue.main.asyncAfter(deadline: .now() + 4.5) { self.calls.checkWhatsApp() }
        }
        if let i = args.firstIndex(of: "--demo-send"), i + 1 < args.count, let c = calls.selected {
            calls.openSend(c)
            let s = calls.session!
            s.kind = SendKind(rawValue: args[i + 1]) ?? .summary
            if s.kind == .free { s.request = "תשלח לה תזכורת למשימות ביום חמישי" }
            if args.contains("--demo-choose") { return }
            func go() {
                if case .connected = calls.wa { s.makeDraft() } else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { go() } }
            }
            go()
            if args.contains("--demo-autosend") {
                var cancel: AnyCancellable?
                cancel = s.$stage.sink { st in
                    if st == .review { DispatchQueue.main.async { s.send() } }
                    if case .pick(let c) = st, args.contains("--demo-pickfirst"), let f = c.first {
                        DispatchQueue.main.async { s.recipient = f.name; s.send(jid: f.jid); cancel?.cancel() }
                    }
                    if case .sent = st { cancel?.cancel() }
                }
                if let c = cancel { bag.insert(c) }
            }
        }
        if args.contains("--demo-pdf"), let c = calls.selected { calls.makePDF(c) }
    }
    // --demo-design [--demo-design-send "הוראה"]... [--demo-logo קובץ]
    func runDesignDemo() {
        openPDFDesign(send: nil)
        var logo: URL?
        if let i = args.firstIndex(of: "--demo-logo"), i + 1 < args.count { logo = pdfDesign.copyLogo(URL(fileURLWithPath: args[i + 1])) }
        var first = true
        for (i, a) in args.enumerated() where a == "--demo-design-send" && i + 1 < args.count {
            pdfDesign.send(args[i + 1], logo: first ? logo : nil); first = false
        }
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
        // כבר עבר את ההגדרה: "השיחות שלי". אחרת: מסך ההגדרה
        if UserDefaults.standard.bool(forKey: "onboarded") { openCalls() } else { showOnboarding(step: nil) }
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
    let examples = ["תחליף את הצבע לכחול", "עיניים ירוקות", "רקע כחול כהה", "נקודות קצרות יותר, עד 8 מילים", "תוסיף סעיף \"החלטות\" בטבלה: נושא | מה סוכם | אחראי", "תוסיף את הלוגו שלי ל-PDF", "תוריד אנימציות",
                    "תוסיף סעיף \"צעדים הבאים ללקוח\"", "תכתוב את הסיכום בגוף ראשון, כאילו אני כתבתי",
                    "בסוף תכתוב הודעת וואטסאפ קצרה ללקוח עם סיכום השיחה", "בלי אימוג'י ובלי הדגשות"]
    func reload() { items = (loadConfig()["custom_instructions"] as? [String]) ?? [] }
    private func save() { saveConfig(["custom_instructions": items]) }
    @Published var accent = Theme.accentHex
    func add(_ text: String? = nil) {
        let t = (text ?? draft).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        // הוראה על ה-PDF (עיצוב, לוגו, שם העסק במסמך...): עוברת לצ'אט "עיצוב ה-PDF"
        if isPDFInstruction(t) {
            if text == nil { draft = "" }
            (NSApp.delegate as? AppDelegate)?.openPDFDesign(send: t)
            status = .done("העברתי לעיצוב ה-PDF")
            return
        }
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
        guard let claude = Onboarding.shared?.claudePath() else {
            if !items.contains(t) { items.append(t); save() }
            status = .done("נשמר כהוראה לנקודות ולסיכומים"); return
        }
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
                else if !self.items.contains(t) {
                    self.items.append(t); self.save()
                    self.status = .done("נשמר כהוראה לנקודות ולסיכומים")
                } else { self.status = .failed("לא הבנתי מה לשנות במראה. נסה למשל: רקע כחול כהה") }
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
        guard status != .working else { return }
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
                    self.status = .done("שופר: " + callTitle(md))
                    NSWorkspace.shared.activateFileViewerSelecting([md])
                } else if text.contains("\"changed\": false") {
                    self.status = .failed("הסיכום לא השתנה: Claude לא הצליח לכתוב אותו לפי ההוראות בלי לשבור את המבנה. נסה לנסח אחרת")
                } else { self.status = .failed("לא הצלחתי להריץ את השיפור. בדוק שה-Claude מחובר") }
            }
        }
        do { try p.run() } catch { status = .failed("שגיאה בהפעלה") }
    }
}

struct ImproveView: View {
    @ObservedObject var m: ImproveModel
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 14) {
                if let img = resImage("timmy") {
                    Image(nsImage: img).resizable().interpolation(.high).frame(width: 58, height: 58)
                        .shadow(color: amber.opacity(0.35), radius: 12)
                }
                VStack(alignment: .leading, spacing: 5) {
                    Text(he("שפר את טימי")).font(.system(size: 24, weight: .bold)).foregroundColor(.white)
                    Text(he("תגיד לי מה לשנות בנקודות, בסיכומים או במראה.\nאזכור את זה לכל השיחות הבאות."))
                        .font(.system(size: 13)).foregroundColor(.white.opacity(0.6)).lineSpacing(3)
                }
            }
            .padding(.top, 48)

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
            FlowChips(items: Array(m.examples.filter { !m.items.contains($0) }.prefix(6))) { m.add($0) }

            AppearanceRow(m: m)

            InstructionsList(m: m).frame(maxHeight: .infinity)

            ImproveFooter(m: m)
            HStack { Spacer(); CreatorBadge(); Spacer() }
        }
        .padding(.horizontal, 36).padding(.bottom, 26)
        .frame(width: 620, height: 820)
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

// שם השיחה בלי התאריך ("2026-11-17 21-40 דנה כהן - הצעת מחיר" -> "דנה כהן - הצעת מחיר")
func callTitle(_ url: URL) -> String {
    let n = url.deletingPathExtension().lastPathComponent
    return n.replacingOccurrences(of: "^\\d{4}-\\d{2}-\\d{2} \\d{2}-\\d{2}(-\\d{2})? ", with: "", options: .regularExpression)
}

struct ImproveFooter: View {
    @ObservedObject var m: ImproveModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // פעולה משנית: להחיל את ההוראות גם על השיחה האחרונה (מופיע רק כשיש הוראות לסיכום ויש שיחה שמורה)
            if !m.items.isEmpty, let last = m.lastSummary() {
                HStack(spacing: 10) {
                    Text(he("להחיל את ההוראות גם על השיחה האחרונה?"))
                        .font(.system(size: 12)).foregroundColor(.white.opacity(0.6))
                    Spacer(minLength: 8)
                    Button(action: { m.improveLast() }) {
                        HStack(spacing: 6) {
                            if m.status == .working { ProgressView().controlSize(.mini) }
                            Text(he(m.status == .working ? "מחיל…" : "החל על: " + callTitle(last)))
                                .font(.system(size: 12, weight: .medium)).foregroundColor(amber).lineLimit(1).truncationMode(.tail)
                        }
                        .padding(.vertical, 6).padding(.horizontal, 12)
                        .background(Capsule().stroke(amber.opacity(0.5), lineWidth: 1))
                    }
                    .buttonStyle(.plain).disabled(m.status == .working).frame(maxWidth: 300)
                }
                .padding(.vertical, 8).padding(.horizontal, 12)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.04)))
            }
            switch m.status {
            case .done(let name): Text(he("✓ " + name)).font(.system(size: 12)).foregroundColor(Color(red: 0.19, green: 0.82, blue: 0.35)).lineLimit(1)
            case .failed(let why): Text(he(why)).font(.system(size: 12)).foregroundColor(Color(red: 1, green: 0.4, blue: 0.4))
            default: Text(he("שינויים במראה חלים מיד. הוראות לנקודות ולסיכומים חלות מהשיחה הבאה. הוראות על ה-PDF עוברות לעיצוב ה-PDF.")).font(.system(size: 11)).foregroundColor(.white.opacity(0.4))
            }
        }
    }
}

// שבבי דוגמאות שנשברים לשורות
struct FlowChips: View {
    let items: [String]; let tap: (String) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(stride(from: 0, to: items.count, by: 2)), id: \.self) { i in
                HStack(spacing: 10) {
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

// תג היוצר: תמונה + "@yaniv_shef", לינק לאינסטגרם
struct CreatorBadge: View {
    var label = "נבנה על ידי"
    var body: some View {
        Button(action: { NSWorkspace.shared.open(URL(string: "https://www.instagram.com/yaniv_shef/")!) }) {
            HStack(spacing: 8) {
                if let img = resImage("creator") {
                    Image(nsImage: img).resizable().interpolation(.high).scaledToFill()
                        .frame(width: 26, height: 26).clipShape(Circle())
                        .overlay(Circle().stroke(amber.opacity(0.7), lineWidth: 1.2))
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
    func claudePath() -> String? { findClaudePath() }
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
            HStack(spacing: 10) {
                secondary("השיחות שלי") { (NSApp.delegate as? AppDelegate)?.openCalls() }
                secondary("✨ שפר את טימי") { (NSApp.delegate as? AppDelegate)?.openImprove() }
            }
        }
    }
}

// MARK: - כלים משותפים: מודלים, הרצת תהליכים, פתיחות חיצוניות

// בחירת מודל (config: summary_model / bullets_model). מותר רק haiku / sonnet / opus
let modelOptions: [(id: String, label: String)] = [("haiku", "Haiku (מהיר)"), ("sonnet", "Sonnet (מאוזן)"), ("opus", "Opus (הכי חכם)")]
func configModel(_ key: String, _ def: String) -> String {
    let v = ((loadConfig()[key] as? String) ?? "").trimmingCharacters(in: .whitespaces).lowercased()
    return modelOptions.contains { $0.id == v } ? v : def
}
var summaryModel: String { configModel("summary_model", "sonnet") }

// TIMMY_DRY_OPEN=1: פתיחת Finder / דפדפן / טרמינל רק נרשמת ל-dry-open.log (בדיקות בלי חלונות קופצים)
let dryOpen = ProcessInfo.processInfo.environment["TIMMY_DRY_OPEN"] != nil
func dryLog(_ s: String) {
    let u = appDir.appendingPathComponent("dry-open.log")
    let line = Data((s + "\n").utf8)
    if let h = try? FileHandle(forWritingTo: u) { h.seekToEndOfFile(); h.write(line); try? h.close() } else { try? line.write(to: u) }
}
func revealInFinder(_ u: URL) { if dryOpen { dryLog("reveal: " + u.path) } else { NSWorkspace.shared.activateFileViewerSelecting([u]) } }
func openExternal(_ u: URL) { if dryOpen { dryLog("open: " + (u.isFileURL ? u.path : u.absoluteString)) } else { NSWorkspace.shared.open(u) } }
func runInTerminal(_ command: String) {
    if dryOpen { dryLog("terminal: " + command); return }
    let esc = command.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
    NSAppleScript(source: "tell application \"Terminal\"\nactivate\ndo script \"\(esc)\"\nend tell")?.executeAndReturnError(nil)
}

// Claude Code: איפה הוא מותקן (קודם claude_path מההגדרות)
func findClaudePath() -> String? {
    var cands: [String] = []
    if let p = loadConfig()["claude_path"] as? String, !p.isEmpty { cands.append(p) }
    cands += [home.path + "/.claude/local/claude", home.path + "/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    if let vers = try? FileManager.default.contentsOfDirectory(atPath: home.path + "/.nvm/versions/node") {
        cands += vers.sorted(by: >).map { home.path + "/.nvm/versions/node/\($0)/bin/claude" }
    }
    return cands.first { FileManager.default.isExecutableFile(atPath: $0) }
}

struct ProcResult {
    var status: Int32 = -1, out = "", timedOut = false, launched = false
    var ok: Bool { launched && !timedOut && status == 0 && !out.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
}
private final class DataBox { var data = Data() }
// מריץ תהליך ומחכה (לא על ה-main thread). חורג מהזמן: נהרג
func runProcess(_ exe: String, _ args: [String], input: String? = nil, timeout: TimeInterval) -> ProcResult {
    var r = ProcResult()
    let p = Process(); p.executableURL = URL(fileURLWithPath: exe); p.arguments = args
    p.currentDirectoryURL = URL(fileURLWithPath: NSTemporaryDirectory())
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = (exe as NSString).deletingLastPathComponent + ":/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
    p.environment = env
    let out = Pipe(), inp = Pipe()
    p.standardOutput = out; p.standardError = FileHandle.nullDevice
    p.standardInput = input == nil ? FileHandle.nullDevice : inp
    let box = DataBox(), g = DispatchGroup()
    do { try p.run() } catch { return r }
    r.launched = true
    g.enter()
    DispatchQueue.global().async { box.data = out.fileHandleForReading.readDataToEndOfFile(); g.leave() }
    if let input {
        DispatchQueue.global().async { try? inp.fileHandleForWriting.write(contentsOf: Data(input.utf8)); try? inp.fileHandleForWriting.close() }
    }
    let deadline = Date().addingTimeInterval(timeout)
    while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
    if p.isRunning {
        r.timedOut = true; p.terminate()
        Thread.sleep(forTimeInterval: 1)
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }
    p.waitUntilExit()
    // תהליכי-בן שמחזיקים את הצינור פתוח לא יתקעו אותנו
    if g.wait(timeout: .now() + 3) == .success { r.out = String(data: box.data, encoding: .utf8) ?? "" }
    r.status = p.terminationStatus
    return r
}
func runClaude(_ args: [String], input: String? = nil, timeout: TimeInterval = 120) -> ProcResult {
    guard let path = Onboarding.shared?.claudePath() ?? findClaudePath() else { return ProcResult() }
    return runProcess(path, args, input: input, timeout: timeout)
}
// אובייקט ה-JSON האחרון בטקסט שיש בו את המפתח key
func lastJSONObject(_ s: String, key: String) -> [String: Any]? {
    var end = s.lastIndex(of: "}")
    while let e = end {
        var start = s[..<e].lastIndex(of: "{")
        while let st = start {
            if let o = try? JSONSerialization.jsonObject(with: Data(s[st...e].utf8)) as? [String: Any], o[key] != nil { return o }
            start = s[..<st].lastIndex(of: "{")
        }
        end = s[..<e].lastIndex(of: "}")
    }
    return nil
}

// MARK: - Markdown קטן -> SwiftUI (כותרות, פסקאות, רשימות, משימות, טבלאות, הדגשה)

func rxGroups(_ s: String, _ pattern: String) -> [String]? {
    guard let re = try? NSRegularExpression(pattern: pattern),
          let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) else { return nil }
    return (0..<m.numberOfRanges).map { i in Range(m.range(at: i), in: s).map { String(s[$0]) } ?? "" }
}
// [[פתק|שם]] / [[שם#סעיף]] -> שם
func plainWikilinks(_ s: String) -> String {
    s.replacingOccurrences(of: "\\[\\[[^\\]|]*\\\\?\\|([^\\]]+)\\]\\]", with: "$1", options: .regularExpression)
     .replacingOccurrences(of: "\\[\\[([^\\]#|]+)(#[^\\]]*)?\\]\\]", with: "$1", options: .regularExpression)
}

enum MDBlock: Hashable {
    case heading(Int, String), para(String), bullet(String, Int), task(Bool, String), numbered(String, String), table([[String]]), quote(String), rule
}
func mdTableRow(_ t: String) -> [String] {
    var r = t.replacingOccurrences(of: "\\|", with: "\u{0}")
    if r.hasPrefix("|") { r.removeFirst() }
    if r.hasSuffix("|") { r.removeLast() }
    return r.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "\u{0}", with: "|") }
}
func parseMarkdown(_ text: String) -> [MDBlock] {
    var blocks: [MDBlock] = [], para: [String] = [], table: [[String]] = []
    func flushPara() { if !para.isEmpty { blocks.append(.para(para.joined(separator: " "))); para = [] } }
    func flushTable() { if !table.isEmpty { blocks.append(.table(table)); table = [] } }
    for raw in text.components(separatedBy: "\n") {
        let line = raw.replacingOccurrences(of: "\r", with: "")
        let t = line.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("|") {
            flushPara()
            let cells = mdTableRow(t)
            if cells.allSatisfy({ $0.isEmpty || $0.range(of: "^:?-{2,}:?$", options: .regularExpression) != nil }) { continue }
            table.append(cells); continue
        }
        flushTable()
        if t.isEmpty { flushPara(); continue }
        let indent = line.prefix { $0 == " " || $0 == "\t" }.count / 2
        if let g = rxGroups(t, "^(#{1,6})\\s+(.*)$") { flushPara(); blocks.append(.heading(g[1].count, g[2])) }
        else if let g = rxGroups(t, "^[-*+]\\s+\\[([ xX])\\]\\s+(.*)$") { flushPara(); blocks.append(.task(g[1] != " ", g[2])) }
        else if t.range(of: "^(-{3,}|\\*{3,}|_{3,})$", options: .regularExpression) != nil { flushPara(); blocks.append(.rule) }
        else if let g = rxGroups(t, "^[-*+•]\\s+(.*)$") { flushPara(); blocks.append(.bullet(g[1], indent)) }
        else if let g = rxGroups(t, "^(\\d+)[.)]\\s+(.*)$") { flushPara(); blocks.append(.numbered(g[1], g[2])) }
        else if let g = rxGroups(t, "^>\\s?(.*)$") { flushPara(); blocks.append(.quote(g[1])) }
        else { para.append(t) }
    }
    flushPara(); flushTable()
    return blocks
}
// טקסט עם הדגשות (bold / italic / קישורים), RTL קבוע
func mdInline(_ s: String) -> AttributedString {
    let t = plainWikilinks(s)
    let a = (try? AttributedString(markdown: t, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(t)
    return AttributedString("\u{200F}") + a
}

struct MarkdownView: View {
    let blocks: [MDBlock]
    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, b in MDBlockView(block: b) }
        }
    }
}
struct MDBlockView: View {
    let block: MDBlock
    var body: some View {
        switch block {
        case .heading(let l, let t): MDHeading(level: l, text: t)
        case .para(let t): MDText(text: t, size: 14.5, opacity: 0.86)
        case .bullet(let t, let ind): MDBullet(text: t, indent: ind)
        case .task(let done, let t): MDTask(done: done, text: t)
        case .numbered(let n, let t): MDNumbered(n: n, text: t)
        case .table(let rows): MDTable(rows: rows)
        case .quote(let t): MDQuote(text: t)
        case .rule: Rectangle().fill(Color.white.opacity(0.1)).frame(height: 1).padding(.vertical, 6)
        }
    }
}
struct MDText: View {
    let text: String; var size: CGFloat = 14.5; var opacity: Double = 0.86
    var body: some View {
        Text(mdInline(text)).font(.system(size: size)).foregroundColor(.white.opacity(opacity))
            .lineSpacing(5).tint(amber)
            .frame(maxWidth: .infinity, alignment: .leading).fixedSize(horizontal: false, vertical: true)
            .textSelection(.enabled)
    }
}
struct MDHeading: View {
    let level: Int, text: String
    var body: some View {
        HStack(spacing: 8) {
            if level <= 2 { Capsule().fill(amber).frame(width: 3, height: 15) }
            Text(mdInline(text)).font(.system(size: level <= 1 ? 20 : (level == 2 ? 16.5 : 14.5), weight: .bold))
                .foregroundColor(.white)
            Spacer(minLength: 0)
        }
        .padding(.top, level <= 2 ? 14 : 8)
    }
}
struct MDBullet: View {
    let text: String, indent: Int
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            Circle().fill(amber.opacity(0.85)).frame(width: 5, height: 5).alignmentGuide(.firstTextBaseline) { d in d[.bottom] + 4 }
            MDText(text: text)
        }
        .padding(.leading, CGFloat(indent) * 18 + 2)
    }
}
struct MDNumbered: View {
    let n: String, text: String
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(n + ".").font(.system(size: 13.5, weight: .semibold).monospacedDigit()).foregroundColor(amber)
            MDText(text: text)
        }
    }
}
struct MDTask: View {
    let done: Bool, text: String
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: done ? "checkmark.square.fill" : "square").font(.system(size: 14, weight: .medium))
                .foregroundColor(done ? amber : .white.opacity(0.55))
            MDText(text: text, opacity: done ? 0.5 : 0.9).strikethrough(done, color: .white.opacity(0.4))
        }
        .padding(.vertical, 7).padding(.horizontal, 12)
        .background(RoundedRectangle(cornerRadius: 9).fill(Color.white.opacity(0.045)))
    }
}
struct MDQuote: View {
    let text: String
    var body: some View {
        HStack(spacing: 10) {
            Rectangle().fill(amber.opacity(0.7)).frame(width: 3)
            MDText(text: text, opacity: 0.7)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}
struct MDTable: View {
    let rows: [[String]]
    var cols: Int { rows.map(\.count).max() ?? 0 }
    var body: some View {
        Grid(alignment: .topLeading, horizontalSpacing: 0, verticalSpacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.offset) { i, row in
                GridRow {
                    ForEach(0..<cols, id: \.self) { c in
                        MDCell(text: c < row.count ? row[c] : "", header: i == 0, striped: i > 0 && i % 2 == 0)
                    }
                }
                if i < rows.count - 1 { Rectangle().fill(Color.white.opacity(i == 0 ? 0.16 : 0.07)).frame(height: 1) }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(Color.white.opacity(0.1), lineWidth: 1))
        .padding(.vertical, 2)
    }
}
struct MDCell: View {
    let text: String, header: Bool, striped: Bool
    var bg: Color { header ? amber.opacity(0.14) : (striped ? Color.white.opacity(0.03) : Color.clear) }
    var body: some View {
        Text(mdInline(text)).font(.system(size: 13, weight: header ? .semibold : .regular))
            .foregroundColor(.white.opacity(header ? 0.95 : 0.85)).lineSpacing(3)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.vertical, 8).padding(.horizontal, 12)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(bg)
    }
}

// שדה טקסט מימין לשמאל. TextField של SwiftUI במק מיישר טקסט מוקלד לשמאל גם בסביבת RTL
struct RTLField: NSViewRepresentable {
    let placeholder: String
    @Binding var text: String
    var size: CGFloat = 14
    var onSubmit: () -> Void = {}
    func makeCoordinator() -> Coord { Coord(self) }
    func makeNSView(context: Context) -> NSTextField {
        let f = NSTextField()
        f.isBordered = false; f.drawsBackground = false; f.focusRingType = .none; f.isBezeled = false
        f.font = .systemFont(ofSize: size); f.textColor = .white
        f.alignment = .right; f.baseWritingDirection = .rightToLeft
        f.cell?.usesSingleLineMode = true; f.cell?.lineBreakMode = .byTruncatingTail
        let ps = NSMutableParagraphStyle(); ps.alignment = .right; ps.baseWritingDirection = .rightToLeft
        f.placeholderAttributedString = NSAttributedString(string: placeholder, attributes: [.foregroundColor: NSColor(white: 1, alpha: 0.35), .font: NSFont.systemFont(ofSize: size), .paragraphStyle: ps])
        f.delegate = context.coordinator
        f.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return f
    }
    func updateNSView(_ f: NSTextField, context: Context) {
        context.coordinator.parent = self
        if f.stringValue != text { f.stringValue = text }
    }
    final class Coord: NSObject, NSTextFieldDelegate {
        var parent: RTLField
        init(_ p: RTLField) { parent = p }
        func controlTextDidChange(_ n: Notification) { if let f = n.object as? NSTextField { parent.text = f.stringValue } }
        func control(_ c: NSControl, textView: NSTextView, doCommandBy sel: Selector) -> Bool {
            if sel == #selector(NSResponder.insertNewline(_:)) { parent.onSubmit(); return true }
            return false
        }
    }
}

// עורך טקסט רב-שורתי בעברית (ימין לשמאל), לעריכת טיוטת ההודעה
struct RTLTextView: NSViewRepresentable {
    @Binding var text: String
    @Binding var focused: Bool
    var size: CGFloat = 14.5
    func makeCoordinator() -> Coord { Coord(self) }
    func makeNSView(context: Context) -> NSScrollView {
        let sv = NSTextView.scrollableTextView()
        sv.drawsBackground = false; sv.hasVerticalScroller = true; sv.autohidesScrollers = true
        let tv = sv.documentView as! NSTextView
        tv.drawsBackground = false; tv.isRichText = false; tv.allowsUndo = true
        tv.font = .systemFont(ofSize: size); tv.textColor = .white; tv.insertionPointColor = .white
        tv.alignment = .right; tv.baseWritingDirection = .rightToLeft
        let ps = NSMutableParagraphStyle(); ps.alignment = .right; ps.baseWritingDirection = .rightToLeft; ps.lineSpacing = 4
        tv.defaultParagraphStyle = ps
        tv.typingAttributes = [.font: NSFont.systemFont(ofSize: size), .foregroundColor: NSColor.white, .paragraphStyle: ps]
        tv.textContainerInset = NSSize(width: 4, height: 6)
        tv.delegate = context.coordinator
        tv.string = text
        tv.textStorage?.setAttributes(tv.typingAttributes, range: NSRange(location: 0, length: (tv.string as NSString).length))
        return sv
    }
    func updateNSView(_ sv: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let tv = sv.documentView as? NSTextView else { return }
        if tv.string != text {
            tv.string = text
            tv.textStorage?.setAttributes(tv.typingAttributes, range: NSRange(location: 0, length: (tv.string as NSString).length))
        }
    }
    final class Coord: NSObject, NSTextViewDelegate {
        var parent: RTLTextView
        init(_ p: RTLTextView) { parent = p }
        func textDidChange(_ n: Notification) { if let tv = n.object as? NSTextView { parent.text = tv.string } }
        func textDidBeginEditing(_ n: Notification) { parent.focused = true }
        func textDidEndEditing(_ n: Notification) { parent.focused = false }
    }
}

// MARK: - שיחה שמורה

struct CallItem: Identifiable, Equatable {
    let id: String
    let url: URL
    let title: String
    let person: String
    let date: Date
    let length: String
    let modified: Date
    var transcriptURL: URL { URL(fileURLWithPath: String(url.path.dropLast(3)) + " - תמלול.txt") }
    var pdfURL: URL { url.deletingPathExtension().appendingPathExtension("pdf") }
    var hasTranscript: Bool { FileManager.default.fileExists(atPath: transcriptURL.path) }
}

func loadCall(_ u: URL, modified: Date) -> CallItem {
    let text = (try? String(contentsOf: u, encoding: .utf8)) ?? ""
    let meta = text.components(separatedBy: "\n").prefix(15).first { $0.contains("**תאריך:**") || $0.contains("**עם:**") } ?? ""
    var person = rxGroups(meta, "\\*\\*עם:\\*\\*\\s*(.+?)\\s*(·|$)")?[1] ?? ""
    person = plainWikilinks(person).trimmingCharacters(in: .whitespaces)
    let length = rxGroups(meta, "\\*\\*אורך:\\*\\*\\s*(.+?)\\s*(·|$)")?[1] ?? ""
    var date = modified
    let df = DateFormatter(); df.locale = Locale(identifier: "en_US_POSIX"); df.dateFormat = "yyyy-MM-dd HH-mm"
    if let g = rxGroups(u.lastPathComponent, "^(\\d{4}-\\d{2}-\\d{2} \\d{2}-\\d{2})"), let d = df.date(from: g[1]) { date = d }
    // הכותרת מהשם בלי האדם (הוא מופיע בשורה נפרדת)
    var title = callTitle(u)
    if !person.isEmpty, let r = title.range(of: " - ") {
        let pre = String(title[..<r.lowerBound])
        if person.hasPrefix(pre) || pre.hasPrefix(person.components(separatedBy: " ").first ?? person) { title = String(title[r.upperBound...]) }
    }
    return CallItem(id: u.path, url: u, title: title, person: person, date: date, length: length, modified: modified)
}
// כל השיחות (סיכומי md) בתיקיית ההקלטות, החדשה ראשונה
func listCalls() -> [CallItem] {
    let fm = FileManager.default
    guard let files = try? fm.contentsOfDirectory(at: recDir, includingPropertiesForKeys: [.contentModificationDateKey], options: [.skipsHiddenFiles]) else { return [] }
    return files.filter { $0.pathExtension == "md" && !$0.lastPathComponent.hasSuffix(" נקודות.md") }
        .map { u in loadCall(u, modified: (try? u.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast) }
        .sorted { $0.date > $1.date }
}
func callDateText(_ d: Date) -> String {
    let cal = Calendar.current
    let tf = DateFormatter(); tf.dateFormat = "HH:mm"
    let time = tf.string(from: d)
    if cal.isDateInToday(d) { return "היום, " + time }
    if cal.isDateInYesterday(d) { return "אתמול, " + time }
    let df = DateFormatter(); df.locale = Locale(identifier: "he_IL")
    df.dateFormat = cal.isDate(d, equalTo: Date(), toGranularity: .year) ? "d בMMMM" : "d בMMMM yyyy"
    return df.string(from: d) + ", " + time
}
// הסיכום להצגה: בלי הכותרת, שורת התאריך ושורת "תמלול מלא" (הם מוצגים בכותרת החלון)
func summaryBody(_ text: String) -> String {
    text.components(separatedBy: "\n").filter { l in
        !(l.hasPrefix("# ") || l.contains("**תאריך:**") || l.hasPrefix("תמלול מלא:"))
    }.joined(separator: "\n")
}

// MARK: - PDF: HTML -> PDF בעמודי A4 (WKWebView מחוץ למסך), ושורת תחתית בכל עמוד

final class HTMLToPDF: NSObject, WKNavigationDelegate {
    static var live: [HTMLToPDF] = []
    let html: String, dest: URL, done: (Bool) -> Void
    private var wv: WKWebView!
    private var win: NSWindow!
    private var printed = false, finished = false
    init(html: String, dest: URL, done: @escaping (Bool) -> Void) { self.html = html; self.dest = dest; self.done = done }
    func start() {
        HTMLToPDF.live.append(self)
        wv = WKWebView(frame: NSRect(x: 0, y: 0, width: 794, height: 1123))
        win = NSWindow(contentRect: NSRect(x: -8000, y: -8000, width: 794, height: 1123), styleMask: [.borderless], backing: .buffered, defer: false)
        win.isReleasedWhenClosed = false
        win.contentView = wv
        wv.navigationDelegate = self
        wv.loadHTMLString(html, baseURL: URL(string: "https://localhost/"))
        DispatchQueue.main.asyncAfter(deadline: .now() + 25) { [weak self] in self?.printNow() }
    }
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        // מחכים לפונט (Heebo), אבל לא יותר מכמה שניות
        w.callAsyncJavaScript("await document.fonts.ready; return true", arguments: [:], in: nil, in: .page) { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self?.printNow() }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [weak self] in self?.printNow() }
    }
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { finish(false) }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { finish(false) }
    private func printNow() {
        guard !printed, !finished else { return }
        printed = true
        try? FileManager.default.removeItem(at: dest)
        let pi = NSPrintInfo()
        pi.paperSize = NSSize(width: 595.2, height: 841.8)
        pi.topMargin = 0; pi.bottomMargin = 0; pi.leftMargin = 0; pi.rightMargin = 0
        pi.horizontalPagination = .fit; pi.verticalPagination = .automatic
        pi.jobDisposition = .save
        pi.dictionary()[NSPrintInfo.AttributeKey.jobSavingURL] = dest
        let op = wv.printOperation(with: pi)
        op.showsPrintPanel = false; op.showsProgressPanel = false
        op.view?.frame = wv.bounds
        op.runModal(for: win, delegate: self, didRun: #selector(didRun(_:success:ctx:)), contextInfo: nil)
    }
    // הדפסה רצה בת'רד משלה: חוזרים ל-main לפני סגירת החלון והמשך העבודה
    @objc private func didRun(_ op: NSPrintOperation, success: Bool, ctx: UnsafeMutableRawPointer?) {
        let ok = success && FileManager.default.fileExists(atPath: dest.path)
        DispatchQueue.main.async { self.finish(ok) }
    }
    private func finish(_ ok: Bool) {
        guard Thread.isMainThread else { DispatchQueue.main.async { self.finish(ok) }; return }
        guard !finished else { return }
        finished = true
        done(ok)
        win.close()
        HTMLToPDF.live.removeAll { $0 === self }
    }
}

// MARK: שורת "נשלח על ידי טימי" בסוף כל PDF
// קבועה בקוד: מוזרקת ל-HTML אחרי התבנית של המשתמש ואחרי Claude, רגע לפני הרינדור, כך שאי אפשר להסיר או להסתיר אותה מהצ'אט.
let timmyFooterURL = "https://yanivshef.com/timmy"
let timmyFooterLead = "נשלח על ידי טימי · רוצה לנסות? "
let timmyFooterLink = "לחץ כאן"
let timmyLogoURI: String = {
    guard let img = resImage("timmy"),
          let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 64, pixelsHigh: 64, bitsPerSample: 8, samplesPerPixel: 4,
                                     hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return "" }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    img.draw(in: NSRect(x: 0, y: 0, width: 64, height: 64))
    NSGraphicsContext.restoreGraphicsState()
    guard let d = rep.representation(using: .png, properties: [:]) else { return "" }
    return "data:image/png;base64," + d.base64EncodedString()
}()
func injectTimmyFooter(_ html: String) -> String {
    var h = html
    // 1. כל מה שמגיע מהתבנית / מ-Claude ומכוון לשורה (אלמנט בשם הזה, או CSS שמסתיר אותה) נמחק
    h = h.replacingOccurrences(of: "<([a-zA-Z][a-zA-Z0-9]*)\\b[^>]*timmy-footer[^>]*>[\\s\\S]*?</\\1\\s*>", with: "", options: [.regularExpression, .caseInsensitive])
    h = h.replacingOccurrences(of: "<[^>]*timmy-footer[^>]*>", with: "", options: [.regularExpression, .caseInsensitive])
    h = h.replacingOccurrences(of: "[^{}<>]*timmy-footer[^{}<>]*\\{[^}]*\\}", with: "", options: [.regularExpression, .caseInsensitive])
    // 2. השורה עצמה, עם CSS משלה (id גובר על סלקטורים של התבנית, inline + !important גובר על הכל)
    let force = "display:block !important;visibility:visible !important;opacity:1 !important;position:static !important;transform:none !important;filter:none !important;clip-path:none !important;height:auto !important;max-height:none !important;overflow:visible !important"
    let css = "<style>#timmy-footer,#timmy-footer *{all:revert !important}"
        + "#timmy-footer{\(force);margin:34px 0 0 !important;padding:9px 0 0 !important;border:0 !important;border-top:1px solid #ececf0 !important;background:transparent !important;font:7.5pt/16px Heebo,-apple-system,'Arial Hebrew',sans-serif !important;color:#a1a1a6 !important;text-align:center !important;direction:rtl !important;break-inside:avoid !important;page-break-inside:avoid !important}"
        + "#timmy-footer img{display:inline-block !important;visibility:visible !important;opacity:1 !important;width:14px !important;height:14px !important;vertical-align:middle !important;margin:0 0 0 6px !important;border-radius:3px !important}"
        + "#timmy-footer span{display:inline !important;visibility:visible !important;vertical-align:middle !important;color:#a1a1a6 !important;font:inherit !important}"
        + "#timmy-footer a{display:inline !important;visibility:visible !important;color:#7c7c82 !important;text-decoration:underline !important;font:inherit !important}</style>"
    let img = timmyLogoURI.isEmpty ? "" : "<img src=\"\(timmyLogoURI)\" alt=\"\" style=\"display:inline-block !important;width:14px !important;height:14px !important\">"
    let footer = css + "<div id=\"timmy-footer\" style=\"\(force)\">\(img)<span>\(timmyFooterLead)<a href=\"\(timmyFooterURL)\">\(timmyFooterLink)</a></span></div>"
    if let r = h.range(of: "</body>", options: [.caseInsensitive, .backwards]) { h.replaceSubrange(r, with: footer + "\n</body>") }
    else { h += footer }
    return h
}

// שורת תחתית בכל עמוד: פרטי הקשר (ימין) ומספר עמוד (שמאל)
func stampFooter(_ url: URL, contact: String) {
    guard let doc = PDFDocument(url: url), doc.pageCount > 0, let first = doc.page(at: 0) else { return }
    // עמוד אחרון ריק (התוכן גלש בכמה פיקסלים של ריווח): מוחקים
    while doc.pageCount > 1, let last = doc.page(at: doc.pageCount - 1),
          (last.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { doc.removePage(at: doc.pageCount - 1) }
    // שורת טימי: עמוד אחרון שיש בו רק אותה -> נמחק והשורה נכנסת לתחתית העמוד הקודם.
    // ואם משהו בכל זאת הסתיר אותה (CSS של תבנית), היא מצוירת בתחתית העמוד האחרון. בכל מקרה יש שורה + קישור.
    func squash(_ s: String?) -> String { (s ?? "").replacingOccurrences(of: "\\s", with: "", options: .regularExpression) }
    var poweredInFooter = false
    if doc.pageCount > 1, let last = doc.page(at: doc.pageCount - 1) {
        let t = squash(last.string)
        if t.hasPrefix("נשלחעלידיטימי") && t.count < 45 { doc.removePage(at: doc.pageCount - 1); poweredInFooter = true }
    }
    var timmyLinkPage = -1
    if !poweredInFooter {
        let pagesWithText = (0..<doc.pageCount).filter { squash(doc.page(at: $0)?.string).contains("נשלחעלידיטימי") }
        if let p = pagesWithText.last {
            let hasLink = doc.page(at: p)?.annotations.contains { ($0.url ?? ($0.action as? PDFActionURL)?.url)?.absoluteString == timmyFooterURL } ?? false
            if !hasLink { timmyLinkPage = p }
        } else { poweredInFooter = true }
    }
    let data = NSMutableData()
    var box = first.bounds(for: .mediaBox)
    guard let consumer = CGDataConsumer(data: data as CFMutableData),
          let ctx = CGContext(consumer: consumer, mediaBox: &box, nil) else { return }
    let n = doc.pageCount
    let gray = NSColor(white: 0.5, alpha: 1)
    for i in 0..<n {
        guard let page = doc.page(at: i), let ref = page.pageRef else { continue }
        var mb = page.bounds(for: .mediaBox)
        let info = [kCGPDFContextMediaBox as String: Data(bytes: &mb, count: MemoryLayout<CGRect>.size)] as CFDictionary
        ctx.beginPDFPage(info)
        ctx.drawPDFPage(ref)
        // הציור מחדש לא מעתיק קישורים: מוסיפים אותם שוב (למשל "לנסות בחינם")
        for a in page.annotations {
            if let u = a.url ?? (a.action as? PDFActionURL)?.url { ctx.setURL(u as CFURL, for: a.bounds) }
        }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
        let side: CGFloat = 54, y: CGFloat = 30
        NSColor(white: 0.9, alpha: 1).setFill()
        NSRect(x: side, y: y + 17, width: mb.width - 2 * side, height: 0.6).fill()
        let rtl = NSMutableParagraphStyle(); rtl.baseWritingDirection = .rightToLeft; rtl.alignment = .right
        let ltr = NSMutableParagraphStyle(); ltr.baseWritingDirection = .rightToLeft; ltr.alignment = .left
        let font = NSFont.systemFont(ofSize: 8)
        if !contact.isEmpty {
            NSAttributedString(string: "\u{200F}" + contact, attributes: [.font: font, .foregroundColor: gray, .paragraphStyle: rtl])
                .draw(in: NSRect(x: mb.width / 2 + 75, y: y, width: mb.width / 2 - 75 - side, height: 13))   // חצי ימין, לא נוגע במרכז
        }
        NSAttributedString(string: "\u{200F}עמוד \(i + 1) מתוך \(n)", attributes: [.font: font, .foregroundColor: gray, .paragraphStyle: ltr])
            .draw(in: NSRect(x: side, y: y, width: 100, height: 13))
        if poweredInFooter && i == n - 1 {
            let line = NSAttributedString(string: "\u{200F}" + timmyFooterLead + timmyFooterLink, attributes: [.font: NSFont.systemFont(ofSize: 7), .foregroundColor: NSColor(white: 0.6, alpha: 1)])
            let w = line.size().width, x0 = (mb.width - w) / 2 + 6
            line.draw(at: NSPoint(x: x0, y: y + 1))
            resImage("timmy")?.draw(in: NSRect(x: x0 - 13, y: y + 0.5, width: 10, height: 10))
            ctx.setURL(URL(string: timmyFooterURL)! as CFURL, for: CGRect(x: x0 - 13, y: y - 1, width: w + 14, height: 12))
        }
        // השורה קיימת אבל הקישור אבד: מוסיפים קישור על "לחץ כאן"
        if i == timmyLinkPage, let sel = doc.findString(timmyFooterLink, withOptions: .backwards).last(where: { $0.pages.first === page }) {
            ctx.setURL(URL(string: timmyFooterURL)! as CFURL, for: sel.bounds(for: page).insetBy(dx: -2, dy: -2))
        }
        NSGraphicsContext.restoreGraphicsState()
        ctx.endPDFPage()
    }
    ctx.closePDF()
    try? (data as Data).write(to: url)
}

// גיבוי אחרון אם pdf.py לא רץ בכלל: HTML פשוט מה-Markdown
func swiftFallbackHTML(_ md: String) -> String {
    func esc(_ s: String) -> String {
        plainWikilinks(s).replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\\*\\*(.+?)\\*\\*", with: "<b>$1</b>", options: .regularExpression)
    }
    var out = ""
    for b in parseMarkdown(md.components(separatedBy: "\n").filter { !$0.hasPrefix("תמלול מלא:") }.joined(separator: "\n")) {
        switch b {
        case .heading(let l, let t): out += "<h\(min(l, 3))>\(esc(t))</h\(min(l, 3))>"
        case .para(let t), .quote(let t): out += "<p>\(esc(t))</p>"
        case .bullet(let t, _), .numbered(_, let t): out += "<p>• \(esc(t))</p>"
        case .task(_, let t): out += "<p>☐ \(esc(t))</p>"
        case .table(let rows): out += "<table>" + rows.map { "<tr>" + $0.map { "<td>\(esc($0))</td>" }.joined() + "</tr>" }.joined() + "</table>"
        case .rule: out += "<hr>"
        }
    }
    let a = Theme.accentHex
    return "<!doctype html><html dir=\"rtl\" lang=\"he\"><head><meta charset=\"utf-8\"><style>@page{size:A4;margin:20mm 19mm 24mm}body{font-family:-apple-system,'Arial Hebrew',sans-serif;font-size:11pt;line-height:1.7;color:#1c1c1e}h1,h2,h3{color:\(a)}td{border-bottom:1px solid #eee;padding:6px 8px}table{border-collapse:collapse;width:100%}</style></head><body>\(out)</body></html>"
}

let pdfScript = appDir.appendingPathComponent("pdf.py").path
// מסמך PDF מסיכום: pdf.py (Claude או המרה מקומית) -> HTML -> PDF בעמודי A4 + שורת תחתית
func buildPDF(md: URL, dest: URL, local: Bool = false, preview: Bool = false, done: @escaping (_ ok: Bool, _ usedClaude: Bool, _ note: String?) -> Void) {
    DispatchQueue.global().async {
        let tmp = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("timmy-pdf-\(UUID().uuidString).html")
        var a = [pdfScript, md.path, "--out", tmp.path]
        if local { a.append("--local") }
        if preview { a.append("--preview") }
        let r = runProcess(python, a, timeout: 160)
        let o = lastJSONObject(r.out, key: "html") ?? [:]
        var html = (try? String(contentsOf: tmp, encoding: .utf8)) ?? ""
        try? FileManager.default.removeItem(at: tmp)
        let usedClaude = o["claude"] as? Bool ?? false
        var note = o["error"] as? String
        let contact = o["footer"] as? String ?? ""
        if html.isEmpty {
            html = swiftFallbackHTML((try? String(contentsOf: md, encoding: .utf8)) ?? "")
            note = note ?? "pdf.py לא רץ"
        }
        html = injectTimmyFooter(html)   // תמיד, בכל מסלול (Claude / מקומי / גיבוי / תצוגה מקדימה)
        DispatchQueue.main.async {
            HTMLToPDF(html: html, dest: dest) { ok in
                if ok { stampFooter(dest, contact: contact) }
                done(ok, usedClaude, note)
            }.start()
        }
    }
}
// עמוד ראשון של PDF כתמונה (לתצוגה מקדימה בצ'אט)
func pdfFirstPagePNG(_ pdf: URL, to png: URL, width: CGFloat = 900) -> Bool {
    guard let doc = PDFDocument(url: pdf), let page = doc.page(at: 0) else { return false }
    let b = page.bounds(for: .mediaBox)
    let img = page.thumbnail(of: NSSize(width: width, height: width * b.height / b.width), for: .mediaBox)
    guard let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
          let data = rep.representation(using: .png, properties: [:]) else { return false }
    return (try? data.write(to: png)) != nil
}

// MARK: - וואטסאפ דרך Greenolink (מחבר MCP של Claude). טימי לא מדבר עם וואטסאפ בעצמו: הוא מבקש מ-Claude Code

let greenolinkURL = "https://mcp.greenolink.com/mcp"
let greenolinkRegister = URL(string: "https://www.greenolink.com/register")!

// שורה מ-`claude mcp list`, למשל:
//   claude.ai whatsapp: https://mcp.greenolink.com/mcp - ✔ Connected
//   greenolink: https://mcp.greenolink.com/mcp (HTTP) - ! Needs authentication
struct MCPServer: Equatable {
    let name: String, target: String, status: String
    var connected: Bool { status.contains("Connected") && !status.contains("Failed") }
    var needsAuth: Bool { status.localizedCaseInsensitiveContains("auth") }
    // שם הכלים: mcp__<שם השרת, כל תו שאינו אות/ספרה/_/- הופך ל-_>__
    var toolPrefix: String {
        "mcp__" + name.replacingOccurrences(of: "[^A-Za-z0-9_-]", with: "_", options: .regularExpression) + "__"
    }
}
func parseMCPList(_ out: String) -> [MCPServer] {
    out.components(separatedBy: "\n").compactMap { line in
        let l = line.trimmingCharacters(in: .whitespaces)
        guard let dash = l.range(of: " - ", options: .backwards), let colon = l.range(of: ": ") , colon.upperBound <= dash.lowerBound else { return nil }
        let name = String(l[..<colon.lowerBound]), target = String(l[colon.upperBound..<dash.lowerBound])
        let status = String(l[dash.upperBound...]).trimmingCharacters(in: CharacterSet(charactersIn: "✔✓!✗✘× ").union(.whitespaces))
        return name.isEmpty ? nil : MCPServer(name: name, target: target, status: status)
    }
}
// השרת של Greenolink: לפי הכתובת או השם (greenolink / whatsapp). מחובר קודם
func findGreenolink(_ list: [MCPServer]) -> MCPServer? {
    let c = list.filter { s in
        let n = s.name.lowercased()
        return s.target.lowercased().contains("greenolink") || n.contains("greenolink") || n.contains("whatsapp")
    }
    return c.first(where: \.connected) ?? c.first
}

enum WAState: Equatable { case unknown, checking, connected(MCPServer), needsAuth(MCPServer), missing, noClaude }

// MARK: שליחה בוואטסאפ: תמיד בשני שלבים (טיוטה -> אישור -> שליחה)
enum SendKind: String, CaseIterable, Identifiable {
    case summary, tasks, pdf, free
    var id: String { rawValue }
    var title: String {
        switch self { case .summary: return "הודעת סיכום ללקוח"; case .tasks: return "רשימת המשימות"; case .pdf: return "ה-PDF"; case .free: return "בקשה חופשית" }
    }
    var sub: String {
        switch self {
        case .summary: return "כמה שורות על מה דיברנו ומה סוכם"
        case .tasks: return "מי עושה מה, שורה לכל משימה"
        case .pdf: return "קובץ הסיכום המעוצב + שורה קצרה"
        case .free: return "תכתוב ל-Claude מה לשלוח"
        }
    }
    var icon: String {
        switch self { case .summary: return "text.bubble"; case .tasks: return "checklist"; case .pdf: return "doc.richtext"; case .free: return "sparkles" }
    }
}
struct Candidate: Equatable, Identifiable { let name: String, jid: String, phone: String; var id: String { jid } }

final class SendSession: ObservableObject {
    enum Stage: Equatable { case choose, drafting(String), review, confirm(Candidate), sending, pick([Candidate]), sent(String), failed(String) }
    let call: CallItem
    @Published var kind: SendKind = .summary
    @Published var recipient: String
    @Published var request = ""
    @Published var draft = ""
    @Published var stage: Stage = .choose
    var pdfPath: URL?
    weak var model: CallsModel?
    // חיפוש איש הקשר רץ במקביל לניסוח הטיוטה
    @Published var resolving = false
    @Published var resolved: [Candidate]? = nil
    @Published var chosen: Candidate? = nil
    var resolvedQuery = ""
    var recipientIsNumber: Bool {
        recipient.filter(\.isNumber).count >= 9 && recipient.range(of: "^[+0-9 ()-]+$", options: .regularExpression) != nil
    }
    func startResolve() {
        let q = recipient.trimmingCharacters(in: .whitespaces)
        resolvedQuery = q; chosen = nil; resolved = nil
        guard !q.isEmpty else { return }
        if recipientIsNumber { let d = q.filter(\.isNumber); chosen = Candidate(name: q, jid: d, phone: d); resolved = [chosen!]; return }
        guard case .connected(let server)? = model?.wa else { return }
        resolving = true
        let prompt = """
        קרא ל-\(server.toolPrefix)search_contacts עם query="\(q)" (אם הכלי עוד לא טעון, טען אותו עם ToolSearch). אסור לשלוח שום דבר.
        החזר שורה אחת של JSON בלבד: {"candidates": [{"name": "...", "jid": "...", "phone": "..."}]} עם כל אנשי הקשר שיכולים להתאים (עד 8), או רשימה ריקה אם אין.
        """
        let args = ["-p", "--model", "haiku", "--permission-mode", "dontAsk",
                    "--tools", "ToolSearch", "--allowedTools", server.toolPrefix + "search_contacts", "--no-session-persistence"]
        DispatchQueue.global().async {
            let r = runClaude(args, input: prompt, timeout: 90)
            let o = lastJSONObject(r.out, key: "candidates")
            DispatchQueue.main.async {
                guard self.resolvedQuery == q else { return }   // הנמען השתנה בינתיים
                let cands = (o?["candidates"] as? [[String: Any]] ?? []).compactMap { c -> Candidate? in
                    guard let j = c["jid"] as? String, !j.isEmpty else { return nil }
                    let ph = (c["phone"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? j.components(separatedBy: "@").first ?? ""
                    return Candidate(name: c["name"] as? String ?? j, jid: j, phone: ph)
                }
                self.resolving = false
                self.resolved = cands
                if cands.count == 1 { self.chosen = cands[0] }
            }
        }
    }
    // תיאור נקי של הנמען: "שם · 058-123-4567"
    func recipientLabel(_ c: Candidate) -> String { c.name + " · " + prettyPhone(c.phone.isEmpty ? c.jid : c.phone) }
    init(call: CallItem, model: CallsModel) {
        self.call = call; self.model = model
        recipient = call.person.components(separatedBy: " - ").first ?? call.person
    }
    var owner: String { ((loadConfig()["owner_name"] as? String) ?? "").trimmingCharacters(in: .whitespaces) }

    func draftPrompt() -> String {
        let md = summaryBody((try? String(contentsOf: call.url, encoding: .utf8)) ?? "")
        let tr = (try? String(contentsOf: call.transcriptURL, encoding: .utf8)) ?? ""
        let me = owner.isEmpty ? "בעל העסק" : owner
        let who = recipient.isEmpty ? "הלקוח" : recipient
        let what: String
        switch kind {
        case .summary: what = "הודעת וואטסאפ ל\(who) עם סיכום השיחה: 2 עד 5 שורות קצרות, מה דיברנו, מה סוכם ומה הצעד הבא."
        case .tasks: what = "הודעת וואטסאפ ל\(who) עם רשימת המשימות שסוכמו בשיחה: שורת פתיחה קצרה, ואז כל משימה בשורה משלה עם מי אחראי ומתי (רק אם נאמר)."
        case .pdf: what = "הודעה קצרה (שורה או שתיים) ל\(who) שתישלח יחד עם קובץ PDF של סיכום השיחה."
        case .free: what = "הודעת וואטסאפ ל\(who) לפי הבקשה של \(me): \"\(request)\". ההודעה נשלחת מיד כש\(me) לוחץ שלח, אז אם הבקשה מזכירה זמן (למשל יום חמישי), שלב אותו בתוכן ההודעה."
        }
        return """
        נסח \(what)
        כללים: עברית, גוף ראשון, בקול של \(me), כאילו הוא כותב בעצמו. קצר וטבעי לוואטסאפ. אל תפתח ב"תודה" ואל תודה על השיחה. בלי מקף ארוך. אל תמציא עובדות, מספרים, תאריכים או הבטחות שלא מופיעים בסיכום או בתמלול. בלי כותרות Markdown; מותר *הדגשה* בסגנון וואטסאפ במידה.
        החזר רק את טקסט ההודעה, בלי שום הקדמה, הסבר או מרכאות.

        סיכום השיחה:
        \(md)

        התמלול (להקשר):
        \(String(tr.suffix(20000)))
        """
    }

    func makeDraft() {
        if resolvedQuery != recipient.trimmingCharacters(in: .whitespaces) || resolved == nil { startResolve() }
        if kind == .free && request.trimmingCharacters(in: .whitespaces).isEmpty { stage = .failed("תכתוב מה לשלוח"); return }
        if kind == .pdf && !FileManager.default.fileExists(atPath: call.pdfURL.path) {
            stage = .drafting("מכין PDF…")
            buildPDF(md: call.url, dest: call.pdfURL) { [weak self] ok, _, _ in
                guard let self else { return }
                if ok { self.pdfPath = self.call.pdfURL; self.model?.objectWillChange.send(); self.runDraft() }
                else { self.stage = .failed("לא הצלחתי להכין את ה-PDF") }
            }
            return
        }
        if kind == .pdf { pdfPath = call.pdfURL }
        runDraft()
    }
    private func runDraft() {
        stage = .drafting("Claude מנסח…")
        let prompt = draftPrompt(), m = model?.summaryModelID ?? summaryModel
        DispatchQueue.global().async {
            let r = runClaude(["-p", "--model", m, "--tools", "", "--strict-mcp-config", "--no-session-persistence"], input: prompt, timeout: 120)
            var text = r.out.trimmingCharacters(in: .whitespacesAndNewlines)
            if text.hasPrefix("```") { text = text.components(separatedBy: "\n").dropFirst().joined(separator: "\n").replacingOccurrences(of: "```", with: "") }
            text = text.replacingOccurrences(of: "—", with: "-").trimmingCharacters(in: CharacterSet(charactersIn: "\"״").union(.whitespacesAndNewlines))
            DispatchQueue.main.async {
                if r.ok && !text.isEmpty { self.draft = text; self.stage = .review }
                else { self.stage = .failed(r.timedOut ? "Claude לא ענה בזמן. נסה שוב" : "לא הצלחתי לנסח הודעה. בדוק שה-Claude מחובר") }
            }
        }
    }

    // ההוראה לשליחה. jid: כשהמשתמש כבר בחר איש קשר מתוך כמה
    func sendPrompt(prefix: String, jid: String?) -> String {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let direct: String? = jid ?? (recipient.filter(\.isNumber).count >= 9 && recipient.range(of: "^[+0-9 ()-]+$", options: .regularExpression) != nil
                                     ? recipient.filter(\.isNumber) : nil)
        var steps: [String] = []
        if let direct {
            steps.append("הנמען הוא \(direct). השתמש בו בדיוק כמו שהוא בשדה recipient, בלי לחפש אנשי קשר.")
        } else {
            steps.append("""
            קרא ל-\(prefix)search_contacts עם query="\(recipient)".
               - אם אין אף תוצאה: אל תשלח. החזר {"sent": false, "error": "לא מצאתי איש קשר בשם \(recipient)"}.
               - אם יש בדיוק איש קשר אחד שמתאים: השתמש ב-jid שלו.
               - אם יש יותר מאחד שיכול להתאים: אל תשלח ואל תנחש. החזר {"sent": false, "candidates": [{"name": "...", "jid": "...", "phone": "..."}]} עם כולם (עד 8).
            """)
        }
        if !text.isEmpty {
            steps.append("שלח עם \(prefix)send_message(recipient=<ה-jid>, message=...) את הטקסט שבין <<<הודעה לבין הודעה>>> בדיוק כמו שהוא, תו בתו, בלי לשנות, להוסיף או לתרגם.")
        }
        if let pdf = pdfPath {
            steps.append("שלח את קובץ ה-PDF: קרא ל-\(prefix)request_file_upload(filename=\"\(pdf.lastPathComponent)\"), העלה את הקובץ עם Bash בפקודה הזאת בדיוק (עם ה-upload_url שקיבלת): curl -sS -X POST --data-binary @\"\(pdf.path)\" \"<upload_url>\" ואז קרא ל-\(prefix)send_file(recipient=<ה-jid>, upload_id=<ה-upload_id>).")
        }
        let numbered = steps.enumerated().map { "\($0.offset + 1). \($0.element)" }.joined(separator: "\n")
        return """
        אתה סוכן השליחה של טימי. המשימה: לשלוח הודעת וואטסאפ אחת דרך הכלים של Greenolink (שמות הכלים מתחילים ב-\(prefix); אם הם עוד לא טעונים, טען אותם עם ToolSearch).
        נמען: "\(recipient)"
        \(numbered)
        אסור: לשלוח למישהו אחר, לשלוח יותר מפעם אחת, לשלוח הודעות נוספות, לקרוא או לשנות הודעות.
        בסוף החזר שורה אחת של JSON בלבד, בלי שום טקסט נוסף:
        {"sent": true או false, "to": "<שם> (<jid>)", "error": "<אם נכשל: הסבר קצר בעברית>", "candidates": []}

        <<<הודעה
        \(text)
        הודעה>>>
        """
    }
    func sendArgs(prefix: String) -> [String] {
        var a = ["-p", "--model", model?.summaryModelID ?? summaryModel, "--permission-mode", "dontAsk",
                 "--tools", pdfPath == nil ? "ToolSearch" : "ToolSearch,Bash",
                 "--allowedTools", prefix + "*"]
        if pdfPath != nil { a.append("Bash(curl:*)") }
        a.append("--no-session-persistence")
        return a
    }
    // לפני שליחה: מאתרים את איש הקשר (חיפוש בלבד, בלי הרשאת שליחה) ומראים שם + מספר לאישור. כמה התאמות = בחירה
    func resolveRecipient() {
        guard case .connected(let server)? = model?.wa else { stage = .failed("וואטסאפ לא מחובר"); return }
        if draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && pdfPath == nil { stage = .failed("ההודעה ריקה"); return }
        let digits = recipient.filter(\.isNumber)
        if digits.count >= 9 && recipient.range(of: "^[+0-9 ()-]+$", options: .regularExpression) != nil {
            stage = .confirm(Candidate(name: recipient, jid: digits, phone: digits)); return
        }
        stage = .drafting("מחפש את \(recipient) באנשי הקשר…")
        let prompt = """
        קרא ל-\(server.toolPrefix)search_contacts עם query="\(recipient)" (אם הכלי עוד לא טעון, טען אותו עם ToolSearch). אסור לשלוח שום דבר.
        החזר שורה אחת של JSON בלבד: {"candidates": [{"name": "...", "jid": "...", "phone": "..."}]} עם כל אנשי הקשר שיכולים להתאים (עד 8), או רשימה ריקה אם אין.
        """
        let args = ["-p", "--model", model?.summaryModelID ?? summaryModel, "--permission-mode", "dontAsk",
                    "--tools", "ToolSearch", "--allowedTools", server.toolPrefix + "search_contacts", "--no-session-persistence"]
        DispatchQueue.global().async {
            let r = runClaude(args, input: prompt, timeout: 90)
            let o = lastJSONObject(r.out, key: "candidates")
            DispatchQueue.main.async {
                let cands = (o?["candidates"] as? [[String: Any]] ?? []).compactMap { c -> Candidate? in
                    guard let j = c["jid"] as? String, !j.isEmpty else { return nil }
                    let ph = (c["phone"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? j.components(separatedBy: "@").first ?? ""
                    return Candidate(name: c["name"] as? String ?? j, jid: j, phone: ph)
                }
                switch cands.count {
                case 0: self.stage = .failed(o == nil ? "לא הצלחתי לחפש באנשי הקשר. בדוק שוואטסאפ מחובר" : "לא מצאתי איש קשר בשם \(self.recipient)")
                case 1: self.stage = .confirm(cands[0])
                default: self.stage = .pick(cands)
                }
            }
        }
    }

    func send(jid: String? = nil) {
        guard case .connected(let server)? = model?.wa else { stage = .failed("וואטסאפ לא מחובר"); return }
        if draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && pdfPath == nil { stage = .failed("ההודעה ריקה"); return }
        stage = .sending
        let prompt = sendPrompt(prefix: server.toolPrefix, jid: jid), args = sendArgs(prefix: server.toolPrefix)
        DispatchQueue.global().async {
            let r = runClaude(args, input: prompt, timeout: 180)
            let o = lastJSONObject(r.out, key: "sent")
            DispatchQueue.main.async {
                guard let o else {
                    self.stage = .failed(r.timedOut ? "השליחה לא הסתיימה בזמן. בדוק בוואטסאפ אם ההודעה יצאה לפני שתנסה שוב" : "לא קיבלתי תשובה ברורה מ-Claude. בדוק בוואטסאפ אם ההודעה יצאה")
                    return
                }
                let cands = (o["candidates"] as? [[String: Any]] ?? []).compactMap { c -> Candidate? in
                    guard let j = c["jid"] as? String, !j.isEmpty else { return nil }
                    return Candidate(name: c["name"] as? String ?? j, jid: j, phone: c["phone"] as? String ?? "")
                }
                if o["sent"] as? Bool == true {
                    let raw = (o["to"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? self.recipient
                    let cleaned = raw.replacingOccurrences(of: "\\s*\\(?[0-9]+@[a-z.]+\\)?", with: "", options: .regularExpression)
                    self.stage = .sent(self.chosen.map { self.recipientLabel($0) } ?? cleaned)
                } else if !cands.isEmpty {
                    self.stage = .pick(cands)
                } else {
                    self.stage = .failed((o["error"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "ההודעה לא נשלחה")
                }
            }
        }
    }
}

// MARK: - המודל של חלון "השיחות שלי"

final class CallsModel: ObservableObject {
    enum PDFState: Equatable { case working, done(URL, Bool), failed(String) }
    enum Overlay: Equatable { case none, send, connect }
    @Published var calls: [CallItem] = []
    @Published var selectedID: String?
    @Published var query = ""
    @Published var summaryModelID = summaryModel
    @Published var bulletsModelID = configModel("bullets_model", "haiku")
    @Published var pdf: [String: PDFState] = [:]
    @Published var wa: WAState = .unknown
    @Published var connectNote = ""
    @Published var overlay: Overlay = .none
    @Published var session: SendSession?
    private var signature = ""
    private var timer: Timer?
    private var textCache: [String: (Date, [MDBlock])] = [:]

    func start() {
        reload()
        timer?.invalidate()
        // התיקייה משתנה (שיחה חדשה נשמרה / קובץ נמחק): בדיקה קלה כל 2 שניות
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.reloadIfChanged() }
        if wa == .unknown { checkWhatsApp() }
    }
    func stop() { timer?.invalidate(); timer = nil }

    private func currentSignature() -> String {
        let fm = FileManager.default
        let files = (try? fm.contentsOfDirectory(at: recDir, includingPropertiesForKeys: [.contentModificationDateKey], options: [.skipsHiddenFiles])) ?? []
        return recDir.path + "|" + files.filter { $0.pathExtension == "md" }.map { u in
            let d = (try? u.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate)?.timeIntervalSince1970 ?? 0
            return u.lastPathComponent + "@\(d)"
        }.sorted().joined(separator: "|")
    }
    func reloadIfChanged() { if currentSignature() != signature { reload() } }
    func reload() {
        signature = currentSignature()
        calls = listCalls()
        if selectedID == nil || !calls.contains(where: { $0.id == selectedID }) { selectedID = calls.first?.id }
    }
    var filtered: [CallItem] {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return calls }
        return calls.filter { $0.title.localizedCaseInsensitiveContains(q) || $0.person.localizedCaseInsensitiveContains(q) }
    }
    var selected: CallItem? { calls.first { $0.id == selectedID } }
    func blocks(_ c: CallItem) -> [MDBlock] {
        if let (d, b) = textCache[c.id], d == c.modified { return b }
        let b = parseMarkdown(summaryBody((try? String(contentsOf: c.url, encoding: .utf8)) ?? ""))
        textCache[c.id] = (c.modified, b)
        return b
    }

    // מודלים
    func setSummaryModel(_ m: String) { summaryModelID = m; saveConfig(["summary_model": m]) }
    func setBulletsModel(_ m: String) { bulletsModelID = m; saveConfig(["bullets_model": m]) }

    // פעולות על שיחה
    func reveal(_ c: CallItem) { revealInFinder(c.url) }
    func openTranscript(_ c: CallItem) { if c.hasTranscript { openExternal(c.transcriptURL) } }
    func makePDF(_ c: CallItem) {
        if pdf[c.id] == .working { return }
        pdf[c.id] = .working
        buildPDF(md: c.url, dest: c.pdfURL) { [weak self] ok, claude, _ in
            guard let self else { return }
            if ok { self.pdf[c.id] = .done(c.pdfURL, claude); revealInFinder(c.pdfURL) }
            else { self.pdf[c.id] = .failed("לא הצלחתי ליצור PDF") }
        }
    }
    func openSend(_ c: CallItem) {
        session = SendSession(call: c, model: self)
        overlay = .send
        if wa == .unknown || wa == .missing || wa == .noClaude { checkWhatsApp() }
    }

    // Greenolink: בדיקת חיבור דרך `claude mcp list` (קריאה בלבד)
    func checkWhatsApp() {
        if wa == .checking { return }
        wa = .checking
        DispatchQueue.global().async {
            guard Onboarding.shared?.claudePath() ?? findClaudePath() != nil else { DispatchQueue.main.async { self.wa = .noClaude }; return }
            let r = runClaude(["mcp", "list"], timeout: 60)
            let server = findGreenolink(parseMCPList(r.out))
            DispatchQueue.main.async {
                if let s = server { self.wa = s.connected ? .connected(s) : .needsAuth(s) } else { self.wa = .missing }
            }
        }
    }
    // חיבור: (1) חשבון + QR באתר, (2) הוספת השרת ל-Claude Code, (3) טרמינל עם claude לאישור ב-/mcp
    func openRegister() { openExternal(greenolinkRegister) }
    func connectToClaude() {
        connectNote = "מחבר את Greenolink ל-Claude…"
        // כבר רשום (למשל מחבר claude.ai בשם "claude.ai whatsapp" שמחכה לאישור): רק מאשרים, לא מוסיפים שוב
        var needAdd = true, serverName = "greenolink"
        switch wa { case .needsAuth(let s), .connected(let s): needAdd = false; serverName = s.name; default: break }
        DispatchQueue.global().async {
            var ok = true
            if needAdd {
                let r = runClaude(["mcp", "add", "--scope", "user", "--transport", "http", "greenolink", greenolinkURL], timeout: 30)
                ok = r.launched && (r.status == 0 || r.out.localizedCaseInsensitiveContains("already exists"))
            }
            DispatchQueue.main.async {
                guard ok, let path = Onboarding.shared?.claudePath() ?? findClaudePath() else {
                    self.connectNote = "לא הצלחתי להוסיף את Greenolink ל-Claude. בדוק ש-Claude Code מותקן"; return
                }
                self.connectNote = "נפתח טרמינל עם Claude. בחר שם את \(serverName), אשר בדפדפן, ואז לחץ \"בדוק שוב\"."
                runInTerminal("clear; echo 'טימי: כדי לחבר את הוואטסאפ, הקלד /mcp, בחר \(serverName) ואשר בדפדפן שנפתח. אחר כך חזור לטימי ולחץ בדוק שוב.'; echo; '\(path)'")
            }
        }
    }
}

// MARK: - חלון "השיחות שלי"

struct CallsView: View {
    @ObservedObject var m: CallsModel
    var body: some View {
        ZStack {
            VStack(alignment: .leading, spacing: 0) {
                CallsHeader(m: m)
                CallsSettingsBar(m: m).padding(.top, 18)
                if m.calls.isEmpty {
                    CallsEmptyState().frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    HStack(alignment: .top, spacing: 22) {
                        CallsSidebar(m: m).frame(width: 300)
                        CallDetailPane(m: m).frame(maxWidth: .infinity)
                    }
                    .padding(.top, 22)
                }
                HStack { Spacer(); CreatorBadge(); Spacer() }.padding(.top, 16)
            }
            .padding(.horizontal, 36).padding(.bottom, 20)
            CallsOverlayHost(m: m)
        }
        .frame(minWidth: 940, maxWidth: .infinity, minHeight: 640, maxHeight: .infinity)
        .background(bgColor)
        .environment(\.layoutDirection, .rightToLeft)
        .environment(\.colorScheme, .dark)
    }
}

struct CallsHeader: View {
    @ObservedObject var m: CallsModel
    var body: some View {
        HStack(spacing: 14) {
            if let img = resImage("timmy") {
                Image(nsImage: img).resizable().interpolation(.high).frame(width: 52, height: 52)
                    .shadow(color: amber.opacity(0.35), radius: 12)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(he("השיחות שלי")).font(.system(size: 24, weight: .bold)).foregroundColor(.white)
                Text(he(subtitle)).font(.system(size: 13)).foregroundColor(.white.opacity(0.55)).lineLimit(1)
            }
            Spacer()
            WhatsAppChip(m: m)
        }
        .padding(.top, 44)
    }
    var subtitle: String {
        let n = m.calls.count
        let count = n == 1 ? "שיחה אחת" : "\(n) שיחות"
        return count + " · נשמרות ב: " + FileManager.default.displayName(atPath: recDir.path)
    }
}

// סטטוס וואטסאפ (Greenolink) בכותרת
struct WhatsAppChip: View {
    @ObservedObject var m: CallsModel
    var connectButton = true
    var body: some View {
        HStack(spacing: 8) {
            icon
            Text(he("וואטסאפ (Greenolink): " + label)).font(.system(size: 12.5, weight: .medium))
                .foregroundColor(.white.opacity(0.85)).lineLimit(1)
            if showConnect && connectButton {
                Button(action: { m.overlay = .connect }) {
                    Text(he("חבר את Greenolink")).font(.system(size: 12, weight: .semibold)).foregroundColor(.black)
                        .padding(.vertical, 4).padding(.horizontal, 10).background(Capsule().fill(amber))
                }.buttonStyle(.plain)
            } else if m.wa != .checking {
                Button(action: { m.checkWhatsApp() }) {
                    Image(systemName: "arrow.clockwise").font(.system(size: 11, weight: .semibold)).foregroundColor(.white.opacity(0.55))
                }.buttonStyle(.plain).help("בדוק שוב")
            }
        }
        .padding(.vertical, 7).padding(.horizontal, 12)
        .background(Capsule().fill(Color.white.opacity(0.06)))
    }
    var label: String {
        switch m.wa {
        case .unknown, .checking: return "בודק…"
        case .connected: return "מחובר ✓"
        case .needsAuth: return "ממתין לאישור"
        case .missing: return "לא מחובר"
        case .noClaude: return "צריך Claude Code"
        }
    }
    var showConnect: Bool {
        switch m.wa { case .needsAuth, .missing: return true; default: return false }
    }
    @ViewBuilder var icon: some View {
        switch m.wa {
        case .checking, .unknown: ProgressView().controlSize(.mini)
        case .connected: Circle().fill(okGreen).frame(width: 8, height: 8)
        default: Circle().fill(Color.white.opacity(0.3)).frame(width: 8, height: 8)
        }
    }
}

// הגדרות: מודל לסיכומים ול-PDF, מודל לנקודות החיות, עיצוב ה-PDF
struct CallsSettingsBar: View {
    @ObservedObject var m: CallsModel
    var body: some View {
        HStack(spacing: 18) {
            ModelPicker(label: "מודל לסיכומים ול-PDF:", value: m.summaryModelID) { m.setSummaryModel($0) }
            ModelPicker(label: "נקודות בזמן השיחה:", value: m.bulletsModelID) { m.setBulletsModel($0) }
            Spacer(minLength: 8)
            Button(action: { (NSApp.delegate as? AppDelegate)?.openImprove() }) {
                Text(he("✨ שפר את טימי")).font(.system(size: 12.5, weight: .semibold)).foregroundColor(.black)
                    .padding(.vertical, 6).padding(.horizontal, 12)
                    .background(Capsule().fill(amber))
            }.buttonStyle(.plain)
                .help("הוראות לנקודות ולסיכומים, צבעים, עיניים, רקע ואנימציות")
            Button(action: { (NSApp.delegate as? AppDelegate)?.openPDFDesign(send: nil) }) {
                Text(he("עצב את ה-PDF")).font(.system(size: 12.5, weight: .medium)).foregroundColor(amber)
                    .padding(.vertical, 6).padding(.horizontal, 12)
                    .background(Capsule().stroke(amber.opacity(0.5), lineWidth: 1))
            }.buttonStyle(.plain)
        }
        .padding(.vertical, 9).padding(.horizontal, 14)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.white.opacity(0.05)))
    }
}
struct ModelPicker: View {
    let label: String, value: String, set: (String) -> Void
    var current: String { modelOptions.first { $0.id == value }?.label ?? value }
    var body: some View {
        HStack(spacing: 7) {
            Text(he(label)).font(.system(size: 12.5)).foregroundColor(.white.opacity(0.6))
            Menu {
                ForEach(modelOptions, id: \.id) { o in
                    Button(action: { set(o.id) }) { Text((o.id == value ? "✓ " : "") + o.label) }
                }
            } label: {
                Text(current).font(.system(size: 12.5, weight: .semibold)).foregroundColor(.white)
            }
            .menuStyle(.borderlessButton).fixedSize()
            .padding(.vertical, 4).padding(.horizontal, 10)
            .background(Capsule().fill(Color.white.opacity(0.08)))
        }
    }
}

struct CallsEmptyState: View {
    var body: some View {
        VStack(spacing: 18) {
            if let img = resImage("timmy") {
                Image(nsImage: img).resizable().interpolation(.high).frame(width: 110, height: 110)
                    .shadow(color: amber.opacity(0.35), radius: 18)
            }
            Text(he("עוד אין שיחות.")).font(.system(size: 20, weight: .bold)).foregroundColor(.white)
            Text(he("לחץ על העיניים ליד הנוטש כדי להקליט.")).font(.system(size: 14)).foregroundColor(.white.opacity(0.6))
        }
    }
}

// MARK: רשימת השיחות (בצד ימין)
struct CallsSidebar: View {
    @ObservedObject var m: CallsModel
    var body: some View {
        VStack(spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").font(.system(size: 12)).foregroundColor(.white.opacity(0.45))
                RTLField(placeholder: "חיפוש לפי שם או נושא", text: $m.query, size: 13)
                if !m.query.isEmpty {
                    Button(action: { m.query = "" }) { Image(systemName: "xmark.circle.fill").foregroundColor(.white.opacity(0.4)) }.buttonStyle(.plain)
                }
            }
            .padding(.vertical, 9).padding(.horizontal, 12)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.07)))
            ScrollView(showsIndicators: false) {
                LazyVStack(spacing: 4) {
                    ForEach(m.filtered) { c in
                        CallRow(c: c, selected: c.id == m.selectedID).onTapGesture { m.selectedID = c.id }
                    }
                    if m.filtered.isEmpty {
                        Text(he("לא נמצאו שיחות")).font(.system(size: 13)).foregroundColor(.white.opacity(0.4)).padding(.top, 20)
                    }
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.white.opacity(0.035)))
    }
}
struct CallRow: View {
    let c: CallItem, selected: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(he(c.title)).font(.system(size: 14, weight: .semibold)).foregroundColor(.white.opacity(selected ? 1 : 0.88))
                .lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
            if !c.person.isEmpty {
                Text(he("עם: " + c.person)).font(.system(size: 12, weight: .medium)).foregroundColor(amber.opacity(0.9)).lineLimit(1)
            }
            Text(he(callDateText(c.date) + (c.length.isEmpty ? "" : " · " + c.length)))
                .font(.system(size: 11.5)).foregroundColor(.white.opacity(0.45)).lineLimit(1)
        }
        .padding(.vertical, 10).padding(.horizontal, 12)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.white.opacity(selected ? 0.1 : 0.0001)))
        .overlay(alignment: .leading) {
            if selected { Capsule().fill(amber).frame(width: 3).padding(.vertical, 10) }
        }
        .contentShape(Rectangle())
    }
}

// MARK: הסיכום
struct CallDetailPane: View {
    @ObservedObject var m: CallsModel
    var body: some View {
        if let c = m.selected {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    CallDetailHeader(m: m, c: c)
                    Rectangle().fill(Color.white.opacity(0.08)).frame(height: 1)
                    MarkdownView(blocks: m.blocks(c))
                }
                .frame(maxWidth: 760, alignment: .leading)
                .padding(.trailing, 14).padding(.bottom, 24)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .id(c.id)
        } else {
            Text(he("בחר שיחה מהרשימה")).font(.system(size: 14)).foregroundColor(.white.opacity(0.4))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}
struct CallDetailHeader: View {
    @ObservedObject var m: CallsModel
    let c: CallItem
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(he(c.title)).font(.system(size: 24, weight: .bold)).foregroundColor(.white)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                MetaChip(icon: "calendar", text: callDateText(c.date))
                if !c.length.isEmpty { MetaChip(icon: "clock", text: c.length) }
                if !c.person.isEmpty { MetaChip(icon: "person", text: "עם: " + c.person, accent: true) }
            }
            CallActions(m: m, c: c).padding(.top, 4)
            PDFStatusLine(state: m.pdf[c.id])
        }
    }
}
struct MetaChip: View {
    let icon: String, text: String
    var accent = false
    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: icon).font(.system(size: 10.5))
            Text(he(text)).font(.system(size: 12, weight: .medium)).lineLimit(1)
        }
        .foregroundColor(accent ? amber : .white.opacity(0.65))
        .padding(.vertical, 4).padding(.horizontal, 10)
        .background(Capsule().fill(accent ? amber.opacity(0.12) : Color.white.opacity(0.06)))
    }
}
struct CallActions: View {
    @ObservedObject var m: CallsModel
    let c: CallItem
    var body: some View {
        HStack(spacing: 8) {
            ActionButton(title: "שלח בוואטסאפ", icon: "paperplane.fill", primary: true) { m.openSend(c) }
            ActionButton(title: m.pdf[c.id] == .working ? "מכין PDF…" : "הורד PDF", icon: "arrow.down.doc") { m.makePDF(c) }
                .disabled(m.pdf[c.id] == .working)
            ActionButton(title: "תמלול מלא", icon: "text.alignright") { m.openTranscript(c) }
                .disabled(!c.hasTranscript).opacity(c.hasTranscript ? 1 : 0.4)
            ActionButton(title: "פתח בתיקייה", icon: "folder") { m.reveal(c) }
        }
    }
}
struct ActionButton: View {
    let title: String, icon: String
    var primary = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 11.5, weight: .semibold))
                Text(he(title)).font(.system(size: 13, weight: .semibold))
            }
            .foregroundColor(primary ? .black : .white.opacity(0.88))
            .padding(.vertical, 8).padding(.horizontal, 14)
            .background(Capsule().fill(primary ? amber : Color.white.opacity(0.08)))
        }
        .buttonStyle(.plain)
    }
}
struct PDFStatusLine: View {
    let state: CallsModel.PDFState?
    var body: some View {
        switch state {
        case .working?:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(he("מכין PDF… (Claude מעצב את המסמך, עד שתי דקות)")).font(.system(size: 12.5)).foregroundColor(.white.opacity(0.65))
            }
        case .done(let u, let claude)?:
            HStack(spacing: 8) {
                Text(he("✓ ה-PDF נשמר ליד הסיכום" + (claude ? "" : " (בעיצוב בסיסי, בלי Claude)")))
                    .font(.system(size: 12.5)).foregroundColor(okGreen)
                Button(action: { openExternal(u) }) { Text(he("פתח")).font(.system(size: 12.5, weight: .semibold)).foregroundColor(amber) }
                    .buttonStyle(.plain)
            }
        case .failed(let why)?:
            Text(he(why)).font(.system(size: 12.5)).foregroundColor(recRed)
        case nil:
            EmptyView()
        }
    }
}

// MARK: שכבות מעל החלון: שליחה בוואטסאפ / חיבור Greenolink
struct CallsOverlayHost: View {
    @ObservedObject var m: CallsModel
    var body: some View {
        if m.overlay != .none {
            ZStack {
                Color.black.opacity(0.55).ignoresSafeArea().onTapGesture { if !busy { m.overlay = .none } }
                Group {
                    if m.overlay == .connect { ConnectCard(m: m) }
                    else if let s = m.session { SendCard(m: m, s: s) }
                }
                .frame(width: 580)
                .padding(28)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(bgColor))
                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(Color.white.opacity(0.12), lineWidth: 1))
                .shadow(color: .black.opacity(0.5), radius: 30, y: 12)
            }
            .transition(.opacity)
        }
    }
    var busy: Bool {
        guard let st = m.session?.stage else { return false }
        if case .sending = st { return true }
        return false
    }
}

struct CardTitle: View {
    let title: String, sub: String
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(he(title)).font(.system(size: 20, weight: .bold)).foregroundColor(.white)
            Text(he(sub)).font(.system(size: 13)).foregroundColor(.white.opacity(0.55)).lineLimit(2)
        }
    }
}
struct PillButton: View {
    let title: String
    var primary = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(he(title)).font(.system(size: 13.5, weight: .semibold)).foregroundColor(primary ? .black : .white.opacity(0.8))
                .padding(.vertical, 9).padding(.horizontal, 18)
                .background(Capsule().fill(primary ? amber : Color.white.opacity(0.08)))
        }.buttonStyle(.plain)
    }
}

struct ConnectCard: View {
    @ObservedObject var m: CallsModel
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            CardTitle(title: "חיבור וואטסאפ (Greenolink)", sub: "טימי שולח בוואטסאפ דרך Claude Code והמחבר Greenolink. שלושה צעדים, פעם אחת.")
            ConnectStep(n: 1, text: "פותחים חשבון ב-Greenolink וסורקים את קוד ה-QR מהוואטסאפ בטלפון.", button: "פתח את Greenolink") { m.openRegister() }
            ConnectStep(n: 2, text: "מחברים את Greenolink ל-Claude. בטרמינל שייפתח מקלידים את הפקודה הזאת, בוחרים greenolink ומאשרים בדפדפן:", command: "/mcp", button: "חבר ל-Claude") { m.connectToClaude() }
            ConnectStep(n: 3, text: "חוזרים לכאן ובודקים שהכל מחובר.", button: m.wa == .checking ? "בודק…" : "בדוק שוב") { m.checkWhatsApp() }
            if !m.connectNote.isEmpty {
                Text(he(m.connectNote)).font(.system(size: 12.5)).foregroundColor(amber.opacity(0.9)).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                WhatsAppChip(m: m, connectButton: false)
                Spacer()
                PillButton(title: "סגור") { m.overlay = m.session == nil ? .none : .send }
            }
        }
    }
}
struct ConnectStep: View {
    let n: Int, text: String
    var command: String? = nil   // פקודה להקלדה: מוצגת כשבב נפרד משמאל לימין (בתוך משפט בעברית הלוכסן מתהפך)
    let button: String, action: () -> Void
    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            Text("\(n)").font(.system(size: 13, weight: .bold)).foregroundColor(.black)
                .frame(width: 24, height: 24).background(Circle().fill(amber))
            VStack(alignment: .leading, spacing: 6) {
                Text(he(text)).font(.system(size: 13.5)).foregroundColor(.white.opacity(0.85)).lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
                if let command {
                    Text(command).font(.system(size: 13, weight: .semibold, design: .monospaced)).foregroundColor(amber)
                        .padding(.vertical, 3).padding(.horizontal, 9)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color.white.opacity(0.08)))
                        .environment(\.layoutDirection, .leftToRight)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            PillButton(title: button, action: action)
        }
    }
}

struct SendCard: View {
    @ObservedObject var m: CallsModel
    @ObservedObject var s: SendSession
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            CardTitle(title: "שליחה בוואטסאפ", sub: s.call.title + (s.call.person.isEmpty ? "" : " · " + s.call.person))
            if case .connected = m.wa { SendStageView(m: m, s: s) }
            else if m.wa == .checking || m.wa == .unknown {
                HStack(spacing: 8) { ProgressView().controlSize(.small); Text(he("בודק את החיבור לוואטסאפ…")).foregroundColor(.white.opacity(0.7)) }
            } else { SendNotConnected(m: m) }
        }
    }
}
struct SendNotConnected: View {
    @ObservedObject var m: CallsModel
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(he("כדי לשלוח בוואטסאפ צריך לחבר את Greenolink ל-Claude Code. זה לוקח דקה, פעם אחת."))
                .font(.system(size: 14)).foregroundColor(.white.opacity(0.8)).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                PillButton(title: "חבר את Greenolink", primary: true) { m.overlay = .connect }
                PillButton(title: "ביטול") { m.overlay = .none }
            }
        }
    }
}
struct SendStageView: View {
    @ObservedObject var m: CallsModel
    @ObservedObject var s: SendSession
    var body: some View {
        switch s.stage {
        case .choose: SendChoose(m: m, s: s)
        case .drafting(let what): BusyRow(text: what)
        case .review: SendReview(m: m, s: s)
        case .sending: BusyRow(text: "שולח ל-" + s.recipient + "…")
        case .pick(let cands): SendPick(m: m, s: s, cands: cands)
        case .confirm(let c): SendConfirm(m: m, s: s, c: c)
        case .sent(let to): SendDone(m: m, to: to)
        case .failed(let why): SendFailed(m: m, s: s, why: why)
        }
    }
}
struct BusyRow: View {
    let text: String
    var body: some View {
        HStack(spacing: 10) {
            ProgressView().controlSize(.small)
            Text(he(text)).font(.system(size: 14)).foregroundColor(.white.opacity(0.75))
        }
        .frame(maxWidth: .infinity, minHeight: 120)
    }
}
struct SendChoose: View {
    @ObservedObject var m: CallsModel
    @ObservedObject var s: SendSession
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                ForEach(SendKind.allCases) { k in KindCard(kind: k, on: s.kind == k).onTapGesture { s.kind = k } }
            }
            if s.kind == .free {
                FieldBox(label: "מה לשלוח?", placeholder: "למשל: תשלח לו תזכורת למשימות ביום חמישי", text: $s.request)
            }
            FieldBox(label: "אל:", placeholder: "שם איש הקשר או מספר טלפון", text: $s.recipient)
            HStack(spacing: 10) {
                PillButton(title: "נסח הודעה", primary: true) { s.makeDraft() }
                PillButton(title: "ביטול") { m.overlay = .none }
                Spacer()
                Text(he("שום דבר לא נשלח בלי שתלחץ \"שלח\".")).font(.system(size: 11.5)).foregroundColor(.white.opacity(0.4))
            }
        }
    }
}
struct KindCard: View {
    let kind: SendKind, on: Bool
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: kind.icon).font(.system(size: 15, weight: .medium)).foregroundColor(on ? amber : .white.opacity(0.6)).frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(he(kind.title)).font(.system(size: 13.5, weight: .semibold)).foregroundColor(.white)
                Text(he(kind.sub)).font(.system(size: 11.5)).foregroundColor(.white.opacity(0.5)).lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 11).padding(.horizontal, 12)
        .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(on ? amber.opacity(0.12) : Color.white.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).stroke(on ? amber.opacity(0.7) : Color.white.opacity(0.08), lineWidth: 1))
        .contentShape(Rectangle())
    }
}
struct FieldBox: View {
    let label: String, placeholder: String
    @Binding var text: String
    var body: some View {
        HStack(spacing: 10) {
            Text(he(label)).font(.system(size: 13, weight: .medium)).foregroundColor(.white.opacity(0.6))
            RTLField(placeholder: placeholder, text: $text)
        }
        .padding(.vertical, 10).padding(.horizontal, 12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.07)))
    }
}
struct SendReview: View {
    @ObservedObject var m: CallsModel
    @ObservedObject var s: SendSession
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            DraftEditor(s: s)
            if let p = s.pdfPath {
                HStack(spacing: 8) {
                    Image(systemName: "doc.fill").foregroundColor(amber)
                    Text(p.lastPathComponent).font(.system(size: 12.5)).foregroundColor(.white.opacity(0.8)).lineLimit(1).truncationMode(.middle)
                }
                .padding(.vertical, 6).padding(.horizontal, 10).background(Capsule().fill(Color.white.opacity(0.06)))
            }
            FieldBox(label: "אל:", placeholder: "שם איש הקשר או מספר טלפון", text: $s.recipient)
            RecipientStatus(s: s)
            HStack(spacing: 10) {
                if s.resolvedQuery != s.recipient.trimmingCharacters(in: .whitespaces) {
                    PillButton(title: "חפש את " + (s.recipient.isEmpty ? "?" : s.recipient), primary: true) { s.startResolve() }
                        .disabled(s.recipient.trimmingCharacters(in: .whitespaces).isEmpty)
                } else if let c = s.chosen {
                    PillButton(title: "שלח ל-" + c.name, primary: true) { s.send(jid: c.jid) }
                } else {
                    PillButton(title: "שלח", primary: true) {}.disabled(true)
                }
                PillButton(title: "נסח מחדש") { s.makeDraft() }
                PillButton(title: "חזרה") { s.stage = .choose }
            }
        }
    }
}
// אישור אחרון: למי בדיוק נשלח (שם + מספר)
func prettyPhone(_ p: String) -> String {
    let d = p.filter(\.isNumber)
    guard d.hasPrefix("972"), d.count == 12 else { return p.components(separatedBy: "@").first ?? p }
    let local = "0" + d.dropFirst(3)
    return "\(local.prefix(3))-\(local.dropFirst(3).prefix(3))-\(local.dropFirst(6))"
}
struct SendConfirm: View {
    @ObservedObject var m: CallsModel
    @ObservedObject var s: SendSession
    let c: Candidate
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(he("לשלוח את ההודעה ל:")).font(.system(size: 13, weight: .semibold)).foregroundColor(.white.opacity(0.6))
            HStack(spacing: 12) {
                Image(systemName: "person.crop.circle.fill").font(.system(size: 28)).foregroundColor(amber)
                VStack(alignment: .leading, spacing: 3) {
                    Text(he(c.name)).font(.system(size: 16, weight: .semibold)).foregroundColor(.white)
                    Text(prettyPhone(c.phone.isEmpty ? c.jid : c.phone)).font(.system(size: 14).monospacedDigit())
                        .foregroundColor(.white.opacity(0.7)).environment(\.layoutDirection, .leftToRight)
                }
                Spacer()
            }
            .padding(.vertical, 12).padding(.horizontal, 14)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.07)))
            HStack(spacing: 10) {
                PillButton(title: "שלח עכשיו", primary: true) { s.recipient = c.name; s.send(jid: c.jid) }
                PillButton(title: "לא זה, חזרה") { s.stage = .review }
            }
        }
    }
}
// מי יקבל את ההודעה: מחפש… / שם · מספר / בחירה מכמה / לא נמצא
struct RecipientStatus: View {
    @ObservedObject var s: SendSession
    var body: some View {
        Group {
            if s.resolving {
                HStack(spacing: 8) { ProgressView().controlSize(.small); Text(he("מחפש את \(s.resolvedQuery) באנשי הקשר…")).font(.system(size: 12.5)).foregroundColor(.white.opacity(0.6)) }
            } else if let c = s.chosen {
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle.fill").foregroundColor(okGreen)
                    Text(he(c.name)).font(.system(size: 13.5, weight: .semibold)).foregroundColor(.white)
                    Text(prettyPhone(c.phone.isEmpty ? c.jid : c.phone)).font(.system(size: 13).monospacedDigit()).foregroundColor(.white.opacity(0.65))
                        .environment(\.layoutDirection, .leftToRight)
                    if (s.resolved?.count ?? 0) > 1 { Button(action: { s.chosen = nil }) { Text(he("שנה")).font(.system(size: 12)).foregroundColor(amber) }.buttonStyle(.plain) }
                }
            } else if let r = s.resolved, r.count > 1 {
                VStack(alignment: .leading, spacing: 6) {
                    Text(he("מצאתי כמה אנשי קשר בשם \"\(s.resolvedQuery)\". למי לשלוח?")).font(.system(size: 12.5)).foregroundColor(.white.opacity(0.75))
                    ForEach(r) { c in
                        Button(action: { s.chosen = c }) {
                            HStack {
                                Text(he(c.name)).font(.system(size: 13, weight: .semibold)).foregroundColor(.white)
                                Spacer()
                                Text(prettyPhone(c.phone.isEmpty ? c.jid : c.phone)).font(.system(size: 12.5).monospacedDigit()).foregroundColor(.white.opacity(0.6))
                                    .environment(\.layoutDirection, .leftToRight)
                            }
                            .padding(.vertical, 7).padding(.horizontal, 10)
                            .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(0.06)))
                        }.buttonStyle(.plain)
                    }
                }
            } else if let r = s.resolved, r.isEmpty {
                Text(he("לא מצאתי איש קשר בשם \(s.resolvedQuery). אפשר לכתוב מספר טלפון.")).font(.system(size: 12.5)).foregroundColor(Color(red: 1, green: 0.45, blue: 0.45))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// טיוטת ההודעה: לחיצה עליה = עריכה (עם סימן עיפרון ומסגרת בעריכה)
struct DraftEditor: View {
    @ObservedObject var s: SendSession
    @State private var editing = false
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "pencil").font(.system(size: 11, weight: .semibold)).foregroundColor(amber)
                Text(he(editing ? "עורך… השינויים נשמרים לבד" : "לחץ על ההודעה כדי לערוך אותה")).font(.system(size: 12, weight: .semibold)).foregroundColor(.white.opacity(0.55))
            }
            RTLTextView(text: $s.draft, focused: $editing)
                .frame(height: 190)
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(editing ? 0.10 : 0.07)))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(editing ? amber.opacity(0.8) : Color.white.opacity(0.12), lineWidth: editing ? 1.5 : 1))
        }
    }
}

struct SendPick: View {
    @ObservedObject var m: CallsModel
    @ObservedObject var s: SendSession
    let cands: [Candidate]
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(he("מצאתי כמה אנשי קשר בשם \"\(s.recipient)\". למי לשלוח?")).font(.system(size: 14)).foregroundColor(.white.opacity(0.85))
            ForEach(cands) { c in
                Button(action: { s.recipient = c.name; s.stage = .confirm(c) }) {
                    HStack {
                        Text(he(c.name)).font(.system(size: 14, weight: .semibold)).foregroundColor(.white)
                        Spacer()
                        Text(c.phone.isEmpty ? c.jid : c.phone).font(.system(size: 12).monospacedDigit()).foregroundColor(.white.opacity(0.5))
                            .environment(\.layoutDirection, .leftToRight)
                    }
                    .padding(.vertical, 10).padding(.horizontal, 12)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.06)))
                }.buttonStyle(.plain)
            }
            PillButton(title: "חזרה לטיוטה") { s.stage = .review }
        }
    }
}
struct SendDone: View {
    @ObservedObject var m: CallsModel
    let to: String
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "checkmark.circle.fill").font(.system(size: 40)).foregroundColor(okGreen)
            Text(he("נשלח ל-" + to + " ✓")).font(.system(size: 16, weight: .semibold)).foregroundColor(.white)
            PillButton(title: "סגור", primary: true) { m.overlay = .none }
        }
        .frame(maxWidth: .infinity)
    }
}
struct SendFailed: View {
    @ObservedObject var m: CallsModel
    @ObservedObject var s: SendSession
    let why: String
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(he(why)).font(.system(size: 14)).foregroundColor(recRed).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                PillButton(title: s.draft.isEmpty ? "חזרה" : "חזרה לטיוטה", primary: true) { s.stage = s.draft.isEmpty ? .choose : .review }
                PillButton(title: "סגור") { m.overlay = .none }
            }
        }
    }
}

// MARK: - "עיצוב ה-PDF": צ'אט שמשנה את התבנית של המשתמש (pdf/template.html + pdf/brand.json) דרך pdf.py --design

struct ChatMsg: Codable, Identifiable, Equatable {
    var id = UUID()
    var fromUser: Bool
    var text: String
    var image: String? = nil   // תצוגה מקדימה: עמוד 1 כ-PNG
    var pdf: String? = nil
    var isError = false
}

let pdfDesignDir = appDir.appendingPathComponent("pdf")
// כללי: האם הוראה ב"שפר את טימי" מדברת על ה-PDF (ולכן עוברת לצ'אט עיצוב ה-PDF)
func isPDFInstruction(_ t: String) -> Bool {
    let l = t.lowercased()
    if l.contains("pdf") || l.contains("פי די אף") { return true }
    if ["הקובץ ששולחים", "עיצוב המסמך", "כותרת המסמך"].contains(where: { l.contains($0) }) { return true }
    return ["מסמך", "לוגו", "פונט"].contains { hasWord(l, $0) }
}

final class PDFDesignModel: ObservableObject {
    @Published var msgs: [ChatMsg] = []
    @Published var input = ""
    @Published var pendingLogo: URL?
    @Published var busy: String?
    private var queue: [(String, URL?)] = []
    private var loaded = false
    let chatFile = pdfDesignDir.appendingPathComponent("chat.json")
    let previews = pdfDesignDir.appendingPathComponent("previews")

    func load() {
        guard !loaded else { return }
        loaded = true
        if let d = try? Data(contentsOf: chatFile), let m = try? JSONDecoder().decode([ChatMsg].self, from: d) { msgs = m }
        if msgs.isEmpty {
            msgs = [ChatMsg(fromUser: false, text: "אני מעצב את ה-PDF שאתה שולח ללקוחות. תכתוב לי מה לשנות, במילים שלך. למשל:\n• שם העסק: סטודיו דנה\n• תשים את הלוגו שלי למעלה (צרף אותו עם 📎)\n• יותר מינימליסטי, בכחול\n• בתחתית: 050-1234567 · studio.co.il\n• המשימות בטבלה")]
            preview(caption: "ככה ה-PDF נראה עכשיו:")
        }
    }
    private func save() {
        try? FileManager.default.createDirectory(at: pdfDesignDir, withIntermediateDirectories: true)
        if let d = try? JSONEncoder().encode(Array(msgs.suffix(80))) { try? d.write(to: chatFile) }
    }
    private func add(_ m: ChatMsg) { msgs.append(m); save() }

    // צירוף לוגו: הקובץ מועתק ל-<תיקיית טימי>/brand כדי שיישאר גם אם המקור יימחק
    func attachLogo() {
        let p = NSOpenPanel()
        p.allowedContentTypes = [.png, .jpeg, .gif, .webP, .svg]
        p.allowsMultipleSelection = false; p.canChooseDirectories = false
        p.message = "בחר את הלוגו של העסק"; p.prompt = "צרף"
        NSApp.activate(ignoringOtherApps: true)
        if p.runModal() == .OK, let u = p.url { pendingLogo = copyLogo(u) }
    }
    func copyLogo(_ u: URL) -> URL? {
        let dir = appDir.appendingPathComponent("brand")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent("logo-\(Int(Date().timeIntervalSince1970)).\(u.pathExtension.lowercased())")
        do { try FileManager.default.copyItem(at: u, to: dest); return dest } catch { return nil }
    }

    func send(_ text: String? = nil, logo: URL? = nil) {
        var t = (text ?? input).trimmingCharacters(in: .whitespacesAndNewlines)
        let lg = logo ?? (text == nil ? pendingLogo : nil)
        if t.isEmpty && lg == nil { return }
        if text == nil { input = ""; pendingLogo = nil }
        if busy != nil { queue.append((t, lg)); return }
        if t.isEmpty { t = "הנה הלוגו שלי, תשים אותו בראש המסמך" }
        add(ChatMsg(fromUser: true, text: t, image: lg?.path))
        var args = [pdfScript, "--design", t]
        if let lg { args += ["--logo", lg.path] }
        runPDFPy(args, busyText: "מעצב…")
    }
    func undo() { guard busy == nil else { return }; add(ChatMsg(fromUser: true, text: "בטל שינוי אחרון")); runPDFPy([pdfScript, "--undo"], busyText: "מבטל…") }
    func reset() { guard busy == nil else { return }; add(ChatMsg(fromUser: true, text: "חזור לעיצוב המקורי")); runPDFPy([pdfScript, "--reset"], busyText: "מחזיר…") }

    private func runPDFPy(_ args: [String], busyText: String) {
        busy = busyText
        DispatchQueue.global().async {
            let r = runProcess(python, args, timeout: 200)
            let o = lastJSONObject(r.out, key: "ok") ?? ["ok": false, "error": r.timedOut ? "לא קיבלתי תשובה בזמן" : "משהו השתבש"]
            DispatchQueue.main.async {
                let summary = (o["summary"] as? String) ?? ""
                if o["ok"] as? Bool != true {
                    self.add(ChatMsg(fromUser: false, text: (o["error"] as? String) ?? "לא הצלחתי", isError: true))
                    self.busy = nil; self.next()
                } else if o["changed"] as? Bool == true {
                    self.preview(caption: summary)
                } else {
                    self.add(ChatMsg(fromUser: false, text: summary.isEmpty ? "לא שיניתי כלום" : summary))
                    self.busy = nil; self.next()
                }
            }
        }
    }
    private func next() { if !queue.isEmpty { let (t, l) = queue.removeFirst(); send(t, logo: l) } }

    // תצוגה מקדימה: השיחה האחרונה (או שיחה לדוגמה), עמוד 1 כתמונה בצ'אט
    func preview(caption: String) {
        busy = "מכין תצוגה מקדימה…"
        let sample = appDir.appendingPathComponent("pdf_sample.md")
        guard let md = listCalls().first?.url ?? (FileManager.default.fileExists(atPath: sample.path) ? sample : nil) else {
            add(ChatMsg(fromUser: false, text: caption)); busy = nil; next(); return
        }
        try? FileManager.default.createDirectory(at: previews, withIntermediateDirectories: true)
        let stamp = Int(Date().timeIntervalSince1970 * 1000)
        let pdf = previews.appendingPathComponent("preview-\(stamp).pdf"), png = previews.appendingPathComponent("preview-\(stamp).png")
        buildPDF(md: md, dest: pdf, preview: true) { ok, _, _ in
            if ok && pdfFirstPagePNG(pdf, to: png) {
                self.add(ChatMsg(fromUser: false, text: caption, image: png.path, pdf: pdf.path))
            } else {
                self.add(ChatMsg(fromUser: false, text: caption + "\n(לא הצלחתי להכין תצוגה מקדימה)"))
            }
            self.busy = nil; self.next()
        }
    }
}

struct PDFDesignView: View {
    @ObservedObject var m: PDFDesignModel
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 14) {
                if let img = resImage("timmy") {
                    Image(nsImage: img).resizable().interpolation(.high).frame(width: 52, height: 52).shadow(color: amber.opacity(0.35), radius: 12)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(he("עיצוב ה-PDF")).font(.system(size: 24, weight: .bold)).foregroundColor(.white)
                    Text(he("תכתוב מה לשנות, ואני אראה לך איך זה נראה. כל ה-PDF-ים הבאים ייצאו ככה."))
                        .font(.system(size: 13)).foregroundColor(.white.opacity(0.6))
                }
            }
            .padding(.top, 44)
            ChatScroll(m: m)
            ChatControls(m: m)
            HStack { Spacer(); CreatorBadge(); Spacer() }
        }
        .padding(.horizontal, 36).padding(.bottom, 20)
        .frame(minWidth: 640, maxWidth: .infinity, minHeight: 760, maxHeight: .infinity)
        .background(bgColor)
        .environment(\.layoutDirection, .rightToLeft)
        .environment(\.colorScheme, .dark)
        .onAppear { m.load() }
    }
}
struct ChatScroll: View {
    @ObservedObject var m: PDFDesignModel
    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    ForEach(m.msgs) { msg in ChatBubble(msg: msg) }
                    if let b = m.busy { TypingBubble(text: b) }
                    Color.clear.frame(height: 1).id("end")
                }
                .padding(.vertical, 6)
            }
            .onAppear { proxy.scrollTo("end", anchor: .bottom) }
            .onChange(of: m.msgs.count) { _, _ in withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
            .onChange(of: m.busy) { _, _ in withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
        }
        .frame(maxHeight: .infinity)
    }
}
// טימי מימין (כמו צד שני בוואטסאפ בעברית), המשתמש משמאל
struct ChatBubble: View {
    let msg: ChatMsg
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            if msg.fromUser { Spacer(minLength: 90) } else { TimmyAvatar() }
            VStack(alignment: .leading, spacing: 10) {
                Text(he(msg.text)).font(.system(size: 14)).foregroundColor(msg.fromUser ? .black : (msg.isError ? recRed : .white.opacity(0.92)))
                    .lineSpacing(4).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                if let p = msg.image { ChatImage(path: p, pdf: msg.pdf, small: msg.fromUser) }
            }
            .padding(.vertical, 10).padding(.horizontal, 14)
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(msg.fromUser ? amber : Color.white.opacity(0.08)))
            if !msg.fromUser { Spacer(minLength: 90) }
        }
        .frame(maxWidth: .infinity)
    }
}
struct TimmyAvatar: View {
    var body: some View {
        Group {
            if let img = resImage("timmy") { Image(nsImage: img).resizable().interpolation(.high) }
            else { Circle().fill(amber) }
        }
        .frame(width: 28, height: 28)
    }
}
struct ChatImage: View {
    let path: String, pdf: String?, small: Bool
    var body: some View {
        if let img = NSImage(contentsOfFile: path) {
            Button(action: { if let pdf { openExternal(URL(fileURLWithPath: pdf)) } }) {
                Image(nsImage: img).resizable().interpolation(.high).aspectRatio(contentMode: .fit)
                    .frame(width: small ? 70 : 200)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .shadow(color: .black.opacity(0.35), radius: 8, y: 3)
            }
            .buttonStyle(.plain).help(pdf == nil ? "" : "פתח את ה-PDF")
        }
    }
}
struct TypingBubble: View {
    let text: String
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            TimmyAvatar()
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(he(text)).font(.system(size: 13.5)).foregroundColor(.white.opacity(0.6))
            }
            .padding(.vertical, 10).padding(.horizontal, 14)
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Color.white.opacity(0.06)))
            Spacer()
        }
    }
}
struct ChatControls: View {
    @ObservedObject var m: PDFDesignModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                SmallChip(title: "בטל שינוי אחרון", icon: "arrow.uturn.backward") { m.undo() }
                SmallChip(title: "חזור לעיצוב המקורי", icon: "arrow.counterclockwise") { m.reset() }
                Spacer()
                if let l = m.pendingLogo { LogoChip(url: l) { m.pendingLogo = nil } }
            }
            .disabled(m.busy != nil).opacity(m.busy != nil ? 0.5 : 1)
            HStack(spacing: 8) {
                Button(action: { m.attachLogo() }) {
                    Text("📎").font(.system(size: 16)).frame(width: 38, height: 38).background(Circle().fill(Color.white.opacity(0.08)))
                }.buttonStyle(.plain).help("צרף לוגו")
                RTLField(placeholder: "למשל: תעשה את זה יותר מינימליסטי בכחול", text: $m.input, onSubmit: { m.send() })
                    .padding(.vertical, 10).padding(.horizontal, 14)
                    .background(RoundedRectangle(cornerRadius: 19).fill(Color.white.opacity(0.08)))
                Button(action: { m.send() }) {
                    Image(systemName: "arrow.up").font(.system(size: 15, weight: .bold)).foregroundColor(.black)
                        .frame(width: 38, height: 38).background(Circle().fill(amber))
                }.buttonStyle(.plain)
            }
        }
    }
}
struct SmallChip: View {
    let title: String, icon: String, action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon).font(.system(size: 10.5, weight: .semibold))
                Text(he(title)).font(.system(size: 12))
            }
            .foregroundColor(.white.opacity(0.7))
            .padding(.vertical, 5).padding(.horizontal, 11).background(Capsule().fill(Color.white.opacity(0.07)))
        }.buttonStyle(.plain)
    }
}
struct LogoChip: View {
    let url: URL, remove: () -> Void
    var body: some View {
        HStack(spacing: 6) {
            if let img = NSImage(contentsOf: url) { Image(nsImage: img).resizable().aspectRatio(contentMode: .fit).frame(width: 22, height: 22) }
            Text(he("לוגו מצורף")).font(.system(size: 12)).foregroundColor(.white.opacity(0.8))
            Button(action: remove) { Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundColor(.white.opacity(0.5)) }.buttonStyle(.plain)
        }
        .padding(.vertical, 4).padding(.horizontal, 10).background(Capsule().fill(amber.opacity(0.15)))
    }
}

// MARK: - מצב שורת פקודה לבדיקות: --make-pdf <md> [--local] יוצר PDF ויוצא
func runPDFCLI() {
    guard let i = args.firstIndex(of: "--make-pdf"), i + 1 < args.count else { exit(2) }
    let md = URL(fileURLWithPath: args[i + 1])
    let dest = md.deletingPathExtension().appendingPathExtension("pdf")
    let t0 = Date()
    buildPDF(md: md, dest: dest, local: args.contains("--local")) { ok, claude, note in
        print("{\"ok\": \(ok), \"claude\": \(claude), \"pdf\": \"\(dest.path)\", \"seconds\": \(Int(Date().timeIntervalSince(t0))), \"note\": \"\(note ?? "")\"}")
        if ok { revealInFinder(dest) }
        exit(ok ? 0 : 1)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
