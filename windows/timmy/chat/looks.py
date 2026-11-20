"""Looks instructions without Claude: port of Recorder.swift colorNames / hasWord / colorIn / isAppearanceInstruction /
parseAppearance / looksTargets / shade / isSimpleLooks / isFollowUp / mentionsMemory / validLooks / colorWord /
applyLooks / contrastNote / isPDFInstruction. Same words, same rules (tests/test_chat.py pins them with tables).
"""
from __future__ import annotations

import re

from timmy.ui import theme as T

DEFAULT_ACCENT = T.DEFAULT_ACCENT

COLOR_NAMES = [
    ("תכלת", "#38BDF8"), ("טורקיז", "#14B8A6"), ("כחול", "#3B82F6"), ("נייבי", "#1E3A8A"),
    ("ירוק", "#22C55E"), ("מנטה", "#34D399"), ("ליים", "#A3E635"), ("צהוב", "#FACC15"),
    ("זהב", "#D4AF37"), ("זהוב", "#D4AF37"), ("אדום", "#EF4444"), ("בורדו", "#9F1239"),
    ("ורוד", "#EC4899"), ("סגול", "#A855F7"), ("לילך", "#C4B5FD"), ("לבן", "#F5F5F5"), ("אפור", "#9CA3AF"),
    ("blue", "#3B82F6"), ("green", "#22C55E"), ("purple", "#A855F7"), ("pink", "#EC4899"), ("red", "#EF4444"),
    ("yellow", "#FACC15"), ("teal", "#14B8A6"), ("gold", "#D4AF37")]
_EN = re.compile(r"^[a-z]+$")


def has_word(l: str, w: str, prefixes: bool = True) -> bool:
    """A whole word, with an optional Hebrew prefix (ה/ל/ב/ו/ש), never part of another word."""
    pre = "[הלבוש]?" if prefixes else ""
    return re.search(r"(^|[\s,.!?:\"'()])" + pre + re.escape(w) + r"($|[\s,.!?:\"'()])", l) is not None


def color_in(l: str) -> str | None:
    m = re.search(r"(^|\s)#[0-9a-f]{6}($|\s)", l)
    if m:
        return m.group(0).strip().upper()
    if has_word(l, "כתום") or has_word(l, "orange", False):
        return DEFAULT_ACCENT
    if has_word(l, "שחור") or has_word(l, "black", False):
        return "#000000"
    for name, hx in COLOR_NAMES:
        en = _EN.match(name) is not None
        if has_word(l, name, not en) or (not en and (has_word(l, name + "ה") or has_word(l, name + "ים") or has_word(l, name + "ות"))):
            return hx
    return None


def is_appearance_instruction(t: str) -> bool:
    l = t.lower()
    summary_words = ["סיכום", "הסיכום", "סעיף", "נקודות", "הנקודות", "משימות", "המשימות", "טבלה", "החלטות", "הודעה", "וואטסאפ", "תמלול",
                     "שיחה", "שיחות", "לקוח", "list", "table", "summary"]
    explicit_app = any(x in l for x in ("טימי", "האפליקציה", "אפליקציה"))
    if not explicit_app and any(has_word(l, w, False) or (" " + w) in l for w in summary_words):
        return False
    looks = any(has_word(l, w) for w in ("צבע", "רקע", "עיניים", "הדגשה", "אנימציה", "אנימציות", "מצמוץ", "מראה")) \
        or any(has_word(l, w, False) for w in ("color", "background", "eyes", "animation", "animations"))
    if looks or (explicit_app and color_in(l) is not None):
        return True
    return len(l.split(" ")) <= 4 and color_in(l) is not None


def looks_targets(l: str) -> list[str]:
    l = l.lower()
    out = []
    if has_word(l, "רקע") or has_word(l, "background", False):
        out.append("background_color")
    if any(has_word(l, w) for w in ("עיניים", "עין", "עינים")) or has_word(l, "eyes", False):
        out.append("eye_color")
    if any(has_word(l, w) for w in ("הדגשה", "הדגשות", "כפתורים", "כפתור", "הכפתורים")) or "צבע הכפתורים" in l or has_word(l, "accent", False):
        out.append("accent_color")
    if any(has_word(l, w) for w in ("אנימציה", "אנימציות", "מצמוץ")) or any(has_word(l, w, False) for w in ("animation", "animations")):
        out.append("animations")
    return out


def shade(hx: str, l: str) -> str:
    """"כחול כהה" / "ירוק בהיר": the listed color darkened (62% to black) or lightened (55% to white)."""
    c = T.rgb(hx)
    if c is None:
        return hx
    dark = has_word(l, "כהה", False) or has_word(l, "dark", False)
    light = has_word(l, "בהיר", False) or has_word(l, "light", False)
    if dark == light:
        return hx
    to, t = (0.0, 0.62) if dark else (1.0, 0.55)
    m = tuple(v + (to - v) * t for v in c)
    return "#%02X%02X%02X" % tuple(int(v * 255 + 0.5) for v in m)     # Swift .rounded(): half away from zero


def parse_appearance(t: str) -> dict | None:
    l = t.lower()
    has = lambda ws: any(has_word(l, w) for w in ws)   # noqa: E731
    if ((has(["אפס", "תאפס", "reset"]) or "ברירת מחדל" in l or "כמו שהיה" in l or "תחזיר הכל" in l)
            and (has(["מראה", "הכל", "צבעים", "עיצוב"]) or "ברירת מחדל" in l or "כמו שהיה" in l)):
        return {"reset": True}
    out = {}
    if has(["אנימציה", "אנימציות", "מצמוץ", "animation", "animations"]):
        if has(["תוריד", "בלי", "תכבה", "תבטל", "תעצור", "off"]):
            out["animations"] = False
        elif has(["תחזיר", "תדליק", "תפעיל", "on"]):
            out["animations"] = True
    targets = looks_targets(l)
    color_targets = [x for x in targets if x != "animations"]
    target = color_targets[0] if color_targets else "accent_color"
    mentions_anim = "animations" in targets
    if has(["מקורי", "מקוריים", "מקוריות"]):
        if mentions_anim and not color_targets:
            out.setdefault("animations", True)
        else:
            out[target] = "#000000" if target == "background_color" else ("" if target == "eye_color" else DEFAULT_ACCENT)
    else:
        hx = color_in(l)
        if hx is not None and not (mentions_anim and not color_targets):
            out[target] = shade(hx, l)
    return out or None


def _color_count(l: str) -> int:
    hits = {hx for name, hx in COLOR_NAMES if any(has_word(l, w) for w in (name, name + "ה", name + "ות", name + "ים"))}
    return len(hits) + (1 if has_word(l, "כתום") else 0) + (1 if has_word(l, "שחור") else 0) + (1 if re.search(r"#[0-9a-f]{6}", l) else 0)


def is_simple_looks(t: str, recent: list[str] | None = None) -> bool:
    """One explicit target, one color, nothing else: applied right away without Claude. A color without a target goes to
    Claude, except "תחליף את הצבע לסגול" (= the accent) when the last messages did not talk about the eyes / background."""
    l = t.lower()
    targets = looks_targets(l)
    colors = _color_count(l)
    other = any(w in l for w in ("נקודות", "סיכום", "סעיף", "משימות", "pdf", "לוגו", "הוראה", "שיחה"))
    if len(targets) > 1 or colors > 1 or other or " וגם " in l:
        return False
    negated = any(has_word(l, w) for w in ("לא", "אל", "בלי", "מדי", "ללא", "not", "don't", "without", "too"))
    if negated and (colors > 0 or any(x != "animations" for x in targets)):
        return False
    if targets == ["animations"] and (colors > 0 or "צבע" in l or has_word(l, "color", False)):
        return False
    if len(targets) == 1:
        return True
    if not (colors == 1 and (has_word(l, "צבע") or has_word(l, "color", False))):
        return False
    return not any(x in ("background_color", "eye_color") for m in (recent or []) for x in looks_targets(m))


def is_follow_up(t: str) -> bool:
    l = t.lower()
    return any(has_word(l, w) for w in ("בעצם", "גם", "עוד", "תחזיר", "החזר", "תחזור", "כמו", "אותו", "אותה", "אותם", "שיהיו", "שיהיה",
                                         "זה", "זאת", "במקום", "פחות", "יותר", "שלך", "שם", "לא", "כן"))


def mentions_memory(t: str) -> bool:
    l = t.lower()
    return any(w in l for w in ("זכור", "תזכר", "זכרי", "שכח", "אוהב", "אוהבת", "שונא", "שונאת", "מעדיף", "מעדיפה", "remember", "forget"))


def asks_last_call(t: str) -> bool:
    return any(w in (t or "").lower() for w in ("אחרונ", "האחרון", "הקודמת", "last"))


def is_pdf_instruction(t: str) -> bool:
    l = t.lower()
    if "pdf" in l or "פי די אף" in l:
        return True
    if any(w in l for w in ("הקובץ ששולחים", "עיצוב המסמך", "כותרת המסמך")):
        return True
    return any(has_word(l, w) for w in ("מסמך", "לוגו", "פונט"))


def is_hex(v) -> bool:
    return isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v) is not None


def valid_looks(c: dict) -> dict:
    """Looks changes from Claude: only known keys with valid values."""
    out = {}
    c = c if isinstance(c, dict) else {}
    for k in ("accent_color", "background_color"):
        if is_hex(c.get(k)):
            out[k] = c[k].upper()
    v = c.get("eye_color")
    if isinstance(v, str) and (v == "" or is_hex(v)):
        out["eye_color"] = v.upper()
    if isinstance(c.get("animations"), bool):
        out["animations"] = c["animations"]
    if c.get("reset") is True:
        out["reset"] = True
    return out


def color_word(hx: str) -> str:
    """"לכחול" / "לשחור" / "לכחול כהה" / "ל-#1E3A8A"."""
    h = (hx or "").upper()
    if h == DEFAULT_ACCENT:
        return "לכתום"
    if h == "#000000":
        return "לשחור"
    hebrew = [(n, v) for n, v in COLOR_NAMES if not _EN.match(n)]
    for n, v in hebrew:
        if v == h:
            return "ל" + n
    for w, mod in (("כהה", "כהה"), ("בהיר", "בהיר")):
        for n, v in hebrew:
            if shade(v, w) == h:
                return "ל" + n + " " + mod
    return "ל-" + h


def look_state(look: T.Look | None = None) -> str:
    """The current look in words, so Claude can bring back a previous state ("תחזיר את מה שהיה")."""
    lk = look or T.current()
    eye = 'כמו צבע ההדגשה ("")' if not lk.eye_hex else lk.eye_hex
    return f"צבע הדגשה {lk.accent_hex}, עיניים {eye}, רקע {lk.bg_hex}, אנימציות {'פועלות' if lk.animations else 'כבויות'}"


def contrast_note(c: dict, look: T.Look | None = None) -> str | None:
    """A short sentence about what Timmy did to keep things readable (after the change was applied), or None."""
    lk = look or T.current()
    n = []
    bg_changed = "background_color" in c or "reset" in c
    if bg_changed and lk.bg_is_light:
        n.append("הטקסט יהיה כהה כדי שיהיה קריא")
    if "accent_color" in c or bg_changed:
        if T.contrast(lk.accent, lk.accent_on(lk.bg)) > 1.05:
            n.append("על הרקע הזה אציג את צבע ההדגשה קצת " + ("כהה" if lk.bg_is_light else "בהיר") + " יותר כדי שיראו אותו")
    if "eye_color" in c or ("accent_color" in c and not lk.eye_hex):
        e = T.rgb(lk.eye_hex) or lk.accent
        if T.contrast(e, T.BLACK) < 3:
            n.append("על הבועה השחורה העיניים יהיו קצת בהירות יותר כדי שיראו אותן")
    return ", ".join(n) + "." if n else None


def apply_looks(c: dict) -> str:
    """Applies the change (config.json + every open window) and returns Timmy's sentence about what changed."""
    wanted_bg = c.get("background_color")
    look = T.apply(c)
    parts = []
    if c.get("reset") is True:
        parts.append("חזרתי למראה המקורי")
    if isinstance(c.get("accent_color"), str):
        parts.append("החלפתי את צבע ההדגשה " + color_word(c["accent_color"]))
    if isinstance(c.get("eye_color"), str):
        parts.append("העיניים חזרו לצבע ההדגשה" if not c["eye_color"] else "צבעתי את העיניים " + color_word(c["eye_color"]))
    if isinstance(wanted_bg, str):
        parts.append("שיניתי את הרקע " + color_word(wanted_bg))
    if isinstance(c.get("animations"), bool):
        parts.append("הדלקתי את האנימציות" if c["animations"] else "כיביתי את האנימציות")
    if not parts:
        return "לא שיניתי כלום."
    note = contrast_note(c, look)
    return ", ".join(parts) + "." + (" " + note if note else "") + " זה כבר מאחורי החלון."
