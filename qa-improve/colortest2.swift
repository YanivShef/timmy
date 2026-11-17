import Foundation
let defaultAccent = "#FFA046"
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
let tests = [
 "תחליף את הצבע לכחול","#1E90FF","תחזיר לכתום","תחזיר את הצבע המקורי","ברירת מחדל","תאפס את המראה",
 "תשנה לכתום","תעשה אותו סגול","אני רוצה כחול","כחול","make it blue","change color to blue","בצבע של פייסבוק",
 "עיניים ירוקות","רקע כחול כהה","רקע לבן","תוריד אנימציות","תחזיר אנימציות",
 "תסמן משימות דחופות באדום","תכתוב את ההחלטות בכחול","תסמן משימות דחופות בצבע אדום","תוסיף אימוג׳י צבעוני לכל נקודה",
 "תעשה את המשימות הדחופות באדום","תשנה את הסיכום כך שהחלטות יהיו בירוק","תעשה סיכום מיוחד לשיחות עם לילך",
 "תעשה ordered list למשימות","תכתוב את מספר ההזמנה #1234","תוסיף בסוף האשטגים כמו #deadline","תוסיף האשטג #fab",
 "תחליף צבע לאדום, לא כתום","תחליף את הצבע ל-#GG0000","תשנה את הצבע ל-#12345","תעשה טבלה עם עמודת סטטוס ירוק/אדום",
 "תכתוב בסיכום רקע קצר על הלקוח","תוסיף סעיף רקע על החברה","תציין אם יצרתי קשר עין","תוסיף מה הלקוח ראה בעיניים שלו",
 "תוסיף סעיף מראה עיניים: איך הלקוח הגיב","תחזיר את הסעיף של המשימות","תשנה את הסדר: קודם משימות ואז סיכום, ותחזיר שחור על לבן",
 "תעשה כל נקודה זהב","תחליף לצבע turquoise","תחזיר הכל כמו שהיה","תחזיר את המשימות לסוף, כמו שהיה"]
for t in tests {
  let a = isAppearanceInstruction(t)
  let p = parseAppearance(t)
  let r = !a ? "→instruction(saved)" : (p.map { "\($0)" } ?? "→ASK-CLAUDE")
  print(r.padding(toLength: 44, withPad: " ", startingAt: 0), "|", t) }
