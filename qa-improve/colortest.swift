import Foundation
let colorNames: [(String, String)] = [
    ("תכלת", "#38BDF8"), ("טורקיז", "#14B8A6"), ("כחול", "#3B82F6"), ("נייבי", "#1E3A8A"),
    ("ירוק", "#22C55E"), ("מנטה", "#34D399"), ("ליים", "#A3E635"), ("צהוב", "#FACC15"),
    ("זהב", "#D4AF37"), ("זהוב", "#D4AF37"), ("אדום", "#EF4444"), ("בורדו", "#9F1239"),
    ("ורוד", "#EC4899"), ("סגול", "#A855F7"), ("לילך", "#C4B5FD"), ("לבן", "#F5F5F5"), ("אפור", "#9CA3AF"),
    ("blue", "#3B82F6"), ("green", "#22C55E"), ("purple", "#A855F7"), ("pink", "#EC4899"), ("red", "#EF4444"),
    ("yellow", "#FACC15"), ("teal", "#14B8A6"), ("gold", "#D4AF37")]
func isColorInstruction(_ t: String) -> Bool {
    let l = t.lowercased()
    return l.contains("צבע") || l.contains("color") || l.contains("colour") || l.range(of: "#[0-9a-f]{3,6}", options: .regularExpression) != nil
}
func parseColor(_ t: String) -> String? {
    let l = t.lowercased()
    let verb = l.contains("תחליף") || l.contains("תשנה") || l.contains("תעשה") || l.contains("שנה") || l.contains("החלף")
    guard isColorInstruction(t) || (verb && colorNames.contains(where: { l.contains($0.0) })) else { return nil }
    if l.contains("כתום") || l.contains("מקורי") || l.contains("ברירת מחדל") || l.contains("orange") { return "reset" }
    if let r = l.range(of: "#[0-9a-f]{6}|#[0-9a-f]{3}", options: .regularExpression) { return String(l[r]).uppercased() }
    return colorNames.first(where: { l.contains($0.0) })?.1
}
let tests = [
 "תחליף את הצבע לכחול","#1E90FF","תחזיר לכתום","תחזיר את הצבע המקורי","ברירת מחדל",
 "תשנה לכתום","תעשה אותו סגול","אני רוצה כחול","כחול","make it blue","change color to blue","בצבע של פייסבוק",
 "תסמן משימות דחופות באדום","תכתוב את ההחלטות בכחול","תסמן משימות דחופות בצבע אדום","תוסיף אימוג׳י צבעוני לכל נקודה",
 "תעשה את המשימות הדחופות באדום","תשנה את הסיכום כך שהחלטות יהיו בירוק","תעשה סיכום מיוחד לשיחות עם לילך",
 "תעשה ordered list למשימות","תכתוב את מספר ההזמנה #1234","תוסיף בסוף האשטגים כמו #deadline","תוסיף האשטג #fab",
 "תחליף צבע לאדום, לא כתום","תחליף את הצבע ל-#GG0000","תשנה את הצבע ל-#12345","תעשה טבלה עם עמודת סטטוס ירוק/אדום",
 "שנה שנה בתאריך לפורמט ישראלי","תעשה כל נקודה זהב","הבצבוץ","תחליף לצבע turquoise"]
for t in tests { let p = parseColor(t); print((p ?? (isColorInstruction(t) ? "→ASK-CLAUDE(color)" : "→instruction")).padding(toLength: 20, withPad: " ", startingAt: 0), "|", t) }
