# "סקיל ה-PDF" של טימי: הופך סיכום שיחה (Markdown) למסמך HTML ממותג ונקי ללקוח. טימי מדפיס אותו ל-PDF.
#
# שימוש:
#   python pdf.py "<סיכום.md>" --out "<קובץ.html>" [--local] [--preview]   בונה מסמך. --preview: תצוגה מקדימה מהירה (תוכן שמור/מקומי)
#   python pdf.py --design "<בקשה>" [--logo "<קובץ>"]                      צ'אט "עיצוב ה-PDF": Claude משנה את התבנית / brand.json
#   python pdf.py --undo | --reset                                         בטל שינוי אחרון / חזור לעיצוב המקורי
#   python pdf.py --snapshot                                               שמור גרסה לפני עריכה ידנית (כדי שאפשר לבטל)
# פלט: שורת JSON אחת.
#
# הקבצים של המשתמש (נוצרים בשינוי הראשון, עד אז ברירת המחדל):
#   <תיקיית טימי>/pdf/template.html   התבנית (HTML + CSS). חובה להשאיר בה {{TITLE}} ו-{{CONTENT}}.
#   <תיקיית טימי>/pdf/brand.json      {"name", "tagline", "logo", "color", "contact", "notes": [הוראות לתוכן]}
#   <תיקיית טימי>/pdf/history/N/       גרסאות קודמות (לביטול)
# שורת "נשלח על ידי טימי" לא כאן ולא בתבנית: האפליקציה מוסיפה אותה בקוד, אחרי כל שלב שמשתמש או Claude יכולים לשנות.
# ברירת המחדל: pdf_template.html שליד הקובץ הזה.
#
# התוכן: Claude (summary_model) כותב את גוף המסמך מתוך הסיכום (+ התמלול להקשר) ולפי notes.
# אם Claude לא זמין / נכשל / חרג מ-120 שניות: המרה מקומית של ה-Markdown, כך שתמיד יוצא PDF.
import sys, os, re, json, html, base64, subprocess, shutil, glob, time, hashlib
from html.parser import HTMLParser

APP_DIR = os.environ.get("TIMMY_APP_DIR") or os.path.expanduser("~/Library/Application Support/Timmy")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(HERE, "pdf_template.html")
PDF_DIR = os.path.join(APP_DIR, "pdf")
USER_TEMPLATE = os.path.join(PDF_DIR, "template.html")
BRAND_JSON = os.path.join(PDF_DIR, "brand.json")
HISTORY = os.path.join(PDF_DIR, "history")
BRAND_FILES = os.path.join(APP_DIR, "brand")
PREVIEW_CACHE = os.path.join(PDF_DIR, "preview_cache.json")
DEFAULT_ACCENT = "#FFA046"


def load_config():
    try:
        with open(os.path.join(APP_DIR, "config.json"), encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


CFG = load_config()
MODELS = ("haiku", "sonnet", "opus")


def pick_model(key, default):
    v = str(CFG.get(key) or "").strip().lower()
    return v if v in MODELS else default


def claude_path():
    cands = [CFG.get("claude_path") or "", shutil.which("claude") or "",
             os.path.expanduser("~/.claude/local/claude"), os.path.expanduser("~/.local/bin/claude"),
             "/opt/homebrew/bin/claude", "/usr/local/bin/claude"] + sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/claude")), reverse=True)
    return next((c for c in cands if c and os.path.exists(c)), "")


# ---------- צבעים ----------
def norm_hex(h, default):
    h = str(h or "").strip().lstrip("#")
    if len(h) == 3: h = "".join(c * 2 for c in h)
    return "#" + h.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", h) else default


def rgb(h): return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
def to_hex(c): return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(x)))) for x in c)


def lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = c
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_on_white(c): return 1.05 / (lum(c) + 0.05)


def brand_colors(hexv):
    """צבע המותג + גרסה כהה לטקסט על לבן (ניגודיות 4.5 לפחות) + גוון רקע בהיר + צבע טקסט על המותג."""
    c = rgb(hexv)
    ink = c
    while contrast_on_white(ink) < 4.5:
        ink = tuple(x * 0.9 for x in ink)
    soft = tuple(255 - (255 - x) * 0.14 for x in c)
    on = "#111111" if lum(c) > 0.35 else "#FFFFFF"
    return hexv, to_hex(ink), to_hex(soft), on


# ---------- קריאת הסיכום ----------
def read_call(md_path):
    md = open(md_path, encoding="utf-8").read()
    tr_path = md_path[:-3] + " - תמלול.txt"
    transcript = open(tr_path, encoding="utf-8").read() if os.path.exists(tr_path) else ""
    lines = md.splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), os.path.basename(md_path)[:-3])
    date_line = next((l for l in lines if "**תאריך:**" in l), "")
    def field(name):
        m = re.search(r"\*\*" + name + r":\*\*\s*(.+?)\s*(?:·|$)", date_line)
        return m.group(1).strip() if m else ""
    person = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]#|]+)(?:#[^\]]*)?\]\]", r"\1", field("עם"))
    length = field("אורך").replace("דק׳", "").strip()
    body = [l for l in lines if not l.startswith("# ") and "**תאריך:**" not in l and not l.startswith("תמלול מלא:")]
    return {"md": md, "body": "\n".join(body).strip(), "transcript": transcript, "title": title,
            "date": hebrew_date(field("תאריך")), "length": length, "person": person}


MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]
def hebrew_date(s):
    """2026-11-17 10:30 -> 17 בנובמבר 2026, בשעה 10:30 (תאריך ISO מתהפך בטקסט מימין לשמאל)"""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:\s+(\d{1,2}:\d{2}))?", s or "")
    if not m: return s
    out = f"{int(m.group(3))} ב{MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    return out + (f", בשעה {m.group(4)}" if m.group(4) else "")


# ---------- הפרומפט ----------
PDF_PROMPT = """אתה מעצב מסמכים. הפוך את סיכום השיחה שלמטה למסמך מסודר ונקי שאפשר לשלוח ללקוח.

{brand}
פרטי השיחה:
כותרת: {title}
תאריך: {date}
משך: {length}
עם: {person}

החזר רק HTML של תוכן העמוד (מה שבתוך <body>), בלי <html>, <head>, <body>, <script> ובלי ```. העיצוב כבר קיים, אל תוסיף style inline. השתמש רק במבנה הזה:
<header class="doc-head"><div class="kicker">סיכום שיחה</div><h1>כותרת</h1><div class="meta"><span><b>תאריך:</b> ...</span><span><b>משך:</b> ... דק׳</span><span><b>עם:</b> ...</span></div></header> (התאריך בדיוק כמו בפרטי השיחה)
<section><h2>כותרת סעיף</h2> ואז <p>, <ul><li>, <ol><li> או <table><thead><tr><th>..</th></tr></thead><tbody><tr><td>..</td></tr></tbody></table> </section>
משימות: <section><h2>משימות</h2><ul class="tasks"><li><span class="task">מה צריך לעשות</span><span class="owner">אחראי</span></li></ul></section> (אם לא ידוע מי אחראי, בלי span של owner).
מותר <strong> ו-<mark> להדגשה, במידה.

כללים:
1. אל תמציא שום עובדה, מספר, תאריך, שם או הבטחה שלא מופיעים בסיכום או בתמלול. אל תשמיט עובדות, החלטות או משימות מהסיכום.
2. עברית תקינה, ניסוח נקי ומקצועי שמתאים לעיני הלקוח. בלי מקף ארוך.
3. סדר מומלץ: סיכום, עיקרי הדברים או החלטות, משימות. סעיף בלי תוכן לא מופיע.
4. אל תכלול הערות פנימיות שלא מתאימות ללקוח (למשל הערכות על הלקוח עצמו).
5. שם האדם בשורת "עם" כמו שהוא, בלי סוגריים מרובעים.{instructions}

הסיכום (Markdown):
{md}

התמלול (להקשר בלבד, לא להעתיק):
{transcript}"""


def instructions_block(b):
    items = [x.strip() for x in b.get("notes") or [] if isinstance(x, str) and x.strip()]
    if not items: return ""
    return ("\n6. העדפות אישיות של בעל העסק לתוכן המסמך. הן גוברות על ברירות המחדל של המבנה (אבל לא על כלל 1):\n"
            + "\n".join("   - " + x for x in items))


def brand_block(b):
    parts = []
    if b["name"]: parts.append(f"שם העסק: {b['name']}")
    if b["tagline"]: parts.append(f"תיאור העסק: {b['tagline']}")
    if not parts: return ""
    return "המסמך נשלח בשם העסק (הלוגו, השם ופרטי הקשר כבר מופיעים בראש ובתחתית העמוד, אל תוסיף אותם):\n" + "\n".join(parts) + "\n"


# ---------- ניקוי הפלט של Claude ----------
def sanitize(out):
    out = out.strip()
    if out.startswith("```"):
        out = out.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    m = re.search(r"<body[^>]*>(.*)</body>", out, re.S | re.I)
    if m: out = m.group(1)
    out = re.sub(r"<(script|iframe|object|embed)\b.*?</\1\s*>", "", out, flags=re.S | re.I)
    out = re.sub(r"</?(html|head|body|meta|link|base|script|iframe)\b[^>]*>", "", out, flags=re.I)
    out = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", out, flags=re.I)
    out = re.sub(r"javascript:", "", out, flags=re.I)
    # בלוק style אחד לכל היותר, בלי משאבים חיצוניים
    def clean_style(mm):
        css = re.sub(r"@import[^;]*;?", "", mm.group(1), flags=re.I)
        css = re.sub(r"url\([^)]*\)", "none", css, flags=re.I)
        return "<style>" + css + "</style>"
    out = re.sub(r"<style[^>]*>(.*?)</style>", clean_style, out, flags=re.S | re.I)
    return out.replace("—", "-").strip()


def looks_valid(frag):
    return len(frag) > 200 and re.search(r"<h1|<h2", frag) and re.search(r"<p|<li|<td", frag)


def ask_claude(call, brand):
    """גוף המסמך מ-Claude. מחזיר (html, שגיאה)."""
    claude = claude_path()
    if not claude: return None, "Claude לא נמצא"
    prompt = PDF_PROMPT.format(brand=brand_block(brand), title=call["title"], date=call["date"] or "(לא ידוע)",
                               length=call["length"] or "(לא ידוע)", person=call["person"] or "(לא ידוע)",
                               instructions=instructions_block(brand), md=call["body"], transcript=call["transcript"][-40000:] or "(אין)")
    try:
        r = subprocess.run([claude, "-p", "--model", pick_model("summary_model", "sonnet"), "--tools", "", "--strict-mcp-config",
                            "--no-session-persistence"], input=prompt, capture_output=True, text=True, timeout=120, cwd="/tmp")
    except subprocess.TimeoutExpired:
        return None, "Claude לא ענה תוך 120 שניות"
    except Exception as e:
        return None, f"שגיאה בהפעלת Claude: {e}"
    frag = sanitize(r.stdout)
    if r.returncode != 0 or not looks_valid(frag):
        return None, "Claude לא החזיר מסמך תקין"
    return frag, None


# ---------- המרה מקומית (גיבוי) ----------
def inline(s):
    s = re.sub(r"\[\[(?:[^\]|]*\\?\|)?([^\]#|]+)(?:#[^\]]*)?\]\]", r"\1", s)   # [[שם]] / [[פתק|שם]] -> שם
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s.replace("—", "-")


def task_html(t):
    """משימה + אחראי. מזהה "לשלוח הצעה (דנה)", "לשלוח הצעה - אחראי: דנה", "דנה: לשלוח הצעה"."""
    owner = ""
    m = re.match(r"^(.*?)\s*\((?:אחראי:?\s*)?([^()]{1,30})\)\s*\.?$", t)
    if m:
        t, owner = m.group(1), m.group(2)
    else:
        m = re.match(r"^(.*?)\s*[-–]\s*אחראי:?\s*(.{1,30})$", t)
        if m:
            t, owner = m.group(1), m.group(2)
        else:
            m = re.match(r"^\**([^:*]{1,25})\**:\s*(.+)$", t)
            if m: owner, t = m.group(1), m.group(2)
    o = f'<span class="owner">{inline(owner.strip())}</span>' if owner.strip() else ""
    return f'<li><span class="task">{inline(t.strip())}</span>{o}</li>'


def split_row(r):
    r = r.strip().replace("\\|", "\x00")
    if r.startswith("|"): r = r[1:]
    if r.endswith("|"): r = r[:-1]
    return [c.strip().replace("\x00", "|") for c in r.split("|")]


def md_to_html(body):
    out, para, lst, rows = [], [], None, []
    def close():
        nonlocal para, lst, rows
        if para: out.append("<p>" + inline(" ".join(para)) + "</p>"); para = []
        if lst: out.append("</ol>" if lst == "ol" else "</ul>"); lst = None
        if rows:
            head, rest = rows[0], rows[1:]
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rest) + "</tbody></table>")
            rows = []
    def open_list(kind):
        nonlocal lst
        if lst != kind:
            close(); out.append({"ul": "<ul>", "ol": "<ol>", "tasks": '<ul class="tasks">'}[kind]); lst = kind
    for raw in body.splitlines():
        t = raw.strip()
        if t.startswith("|"):
            cells = split_row(t)
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c): continue
            if not rows: close()
            rows.append(cells); continue
        if rows: close()
        if not t: close(); continue
        m = re.match(r"^(#{1,6})\s+(.*)$", t)
        if m:
            close(); lvl = max(2, min(3, len(m.group(1))))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>"); continue
        m = re.match(r"^[-*+]\s+\[([ xX])\]\s+(.*)$", t)
        if m:
            open_list("tasks"); li = task_html(m.group(2))
            if m.group(1) != " ": li = li.replace('<span class="task">', '<span class="task" style="text-decoration:line-through;opacity:.55">✓ ')
            out.append(li); continue
        m = re.match(r"^[-*+•]\s+(.*)$", t)
        if m: open_list("ul"); out.append(f"<li>{inline(m.group(1))}</li>"); continue
        m = re.match(r"^\d+[.)]\s+(.*)$", t)
        if m: open_list("ol"); out.append(f"<li>{inline(m.group(1))}</li>"); continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", t): close(); out.append("<hr>"); continue
        m = re.match(r"^>\s?(.*)$", t)
        if m: close(); out.append(f"<blockquote>{inline(m.group(1))}</blockquote>"); continue
        if lst: close()
        para.append(t)
    close()
    return "\n".join(out)


def local_html(call):
    meta = []
    if call["date"]: meta.append(f"<span><b>תאריך:</b> {html.escape(call['date'])}</span>")
    if call["length"]: meta.append(f"<span><b>משך:</b> {html.escape(call['length'])} דק׳</span>")
    if call["person"]: meta.append(f"<span><b>עם:</b> {html.escape(call['person'])}</span>")
    head = (f'<header class="doc-head"><div class="kicker">סיכום שיחה</div><h1>{inline(call["title"])}</h1>'
            f'<div class="meta">{"".join(meta)}</div></header>')
    return head + "\n" + md_to_html(call["body"])


# ---------- מיתוג ותבנית (העותק של המשתמש) ----------
def default_brand():
    return {"name": "", "tagline": "", "logo": "", "color": norm_hex(CFG.get("accent_color"), DEFAULT_ACCENT), "contact": "", "notes": []}


def load_brand():
    b = default_brand()
    try:
        with open(BRAND_JSON, encoding="utf-8") as f: u = json.load(f)
        for k in b:
            if k in u: b[k] = u[k]
    except Exception: pass
    b["color"] = norm_hex(b.get("color"), default_brand()["color"])
    for k in ("name", "tagline", "logo", "contact"): b[k] = str(b.get(k) or "").strip()
    b["logo"] = os.path.expanduser(b["logo"])
    b["notes"] = [x for x in (b.get("notes") or []) if isinstance(x, str) and x.strip()]
    return b


def load_template():
    p = USER_TEMPLATE if os.path.exists(USER_TEMPLATE) else DEFAULT_TEMPLATE
    return open(p, encoding="utf-8").read()


def strip_comments(t): return re.sub(r"<!--.*?-->", "", t, flags=re.S)


def logo_data_uri(path):
    if not path or not os.path.isfile(path): return ""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp", "svg": "svg+xml"}.get(ext)
    if not mime or os.path.getsize(path) > 5_000_000: return ""
    with open(path, "rb") as f:
        return f"data:image/{mime};base64," + base64.b64encode(f.read()).decode()


def brand_header(b, logo):
    if not logo and not b["name"]: return ""
    img = f'<img class="logo" src="{logo}" alt="">' if logo else ""
    text = ""
    if b["name"] or b["tagline"]:
        text = ('<div class="brand-text">' + (f'<div class="brand-name">{html.escape(b["name"])}</div>' if b["name"] else "")
                + (f'<div class="tagline">{html.escape(b["tagline"])}</div>' if b["tagline"] else "") + "</div>")
    return f'<div class="brand-bar">{img}{text}</div>'


def fill(tpl, title, content, b):
    brand, ink, soft, on = brand_colors(b["color"])
    logo = logo_data_uri(b["logo"])
    vals = {"TITLE": html.escape(title), "BRAND_INK": ink, "BRAND_SOFT": soft, "ON_BRAND": on, "BRAND": brand,
            "BRAND_HEADER": brand_header(b, logo), "LOGO_SRC": logo, "BRAND_NAME": html.escape(b["name"]),
            "BRAND_TAGLINE": html.escape(b["tagline"]), "BRAND_CONTACT": html.escape(b["contact"])}
    out = strip_comments(tpl)
    for k, v in vals.items(): out = out.replace("{{" + k + "}}", v)
    return out.replace("{{CONTENT}}", keep_headings(content))   # אחרון: שהתוכן עצמו לא יעבור החלפות


def keep_headings(html_text):
    """כותרת סעיף לא נשארת לבד בתחתית עמוד: עוטפים אותה יחד עם הבלוק שאחריה (WebKit מתעלם מ-break-after: avoid)."""
    return re.sub(r"(<h[23]\b[^>]*>.*?</h[23]>\s*<(ul|ol|table|p|blockquote)\b.*?</\2>)",
                  r'<div class="keep" style="break-inside:avoid;page-break-inside:avoid">\1</div>', html_text, flags=re.S)


def build(md_path, out_path, local=False, preview=False):
    call = read_call(md_path)
    b = load_brand()
    frag, err, used = None, None, False
    key = hashlib.sha1(json.dumps([md_path, os.path.getmtime(md_path), b["notes"], pick_model("summary_model", "sonnet")],
                                  ensure_ascii=False).encode()).hexdigest()
    if preview:
        # תצוגה מקדימה בצ'אט: בלי הוראות תוכן -> המרה מקומית מהירה; עם הוראות -> Claude פעם אחת, ואז מהמטמון
        try:
            c = json.load(open(PREVIEW_CACHE, encoding="utf-8"))
            if c.get("key") == key: frag, used = c["html"], c.get("claude", False)
        except Exception: pass
        if frag is None and not b["notes"]: local = True
    if frag is None and not local:
        frag, err = ask_claude(call, b)
        used = frag is not None
        if used and preview:
            os.makedirs(PDF_DIR, exist_ok=True)
            json.dump({"key": key, "html": frag, "claude": True}, open(PREVIEW_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    if frag is None:
        frag = local_html(call)
    page = fill(load_template(), call["title"], frag, b)
    with open(out_path, "w", encoding="utf-8") as f: f.write(page)
    return {"html": out_path, "claude": used, "footer": b["contact"], "brand": norm_hex(b["color"], DEFAULT_ACCENT), "error": err}


# ---------- הצ'אט "עיצוב ה-PDF" ----------
DESIGN_PROMPT = """אתה מעצב את תבנית ה-PDF של טימי: מסמך סיכום שיחה ש{owner} שולח ללקוחות שלו.
יש שני קבצים שאתה יכול לשנות:

1. template.html: עמוד HTML מלא (A4, עברית, dir="rtl") עם CSS בתוך <style>.
   חובה להשאיר בו: {{{{TITLE}}}} (בתוך <title>) ו-{{{{CONTENT}}}} (במקום שבו נכנס תוכן המסמך).
   מקומות אופציונליים: {{{{BRAND_HEADER}}}} (לוגו + שם העסק + תיאור, נבנה מ-brand.json עם המחלקות .brand-bar .logo .brand-name .tagline),
   {{{{LOGO_SRC}}}}, {{{{BRAND_NAME}}}}, {{{{BRAND_TAGLINE}}}}, {{{{BRAND_CONTACT}}}}, ומשתני הצבע {{{{BRAND}}}}, {{{{BRAND_INK}}}} (כהה לטקסט), {{{{BRAND_SOFT}}}} (רקע בהיר), {{{{ON_BRAND}}}}.
   התוכן שנכנס ל-{{{{CONTENT}}}} בנוי כך: header.doc-head (.kicker, h1, .meta span b), section > h2, p, ul/ol/li, table/th/td, ul.tasks li (span.task, span.owner), strong, mark, blockquote.
   שורת התחתית (פרטי הקשר + מספרי עמודים) מתווספת אוטומטית לכל עמוד מ-brand.json, אל תוסיף footer ב-HTML.
2. brand.json: {{"name": שם העסק, "tagline": תיאור קצר של העסק, "logo": נתיב לקובץ הלוגו, "color": "#RRGGBB" צבע המותג, "contact": שורת פרטי קשר לתחתית, "notes": [הוראות לתוכן המסמך]}}

הבקשה של {owner}: "{request}"
{logo_line}
איך להחליט מה לשנות:
- שם העסק, תיאור, טלפון/אתר/אינסטגרם, צבע מותג, לוגו -> brand.json. צבע במילים ("כחול") -> קוד hex מתאים ומכובד.
- מה מופיע במסמך ובאיזה מבנה ("המשימות בטבלה", "בלי סעיף נקודות", "סיכום קצר יותר") -> notes ב-brand.json (משפט קצר לכל הוראה, שמור על ההוראות הקיימות אלא אם התבקשת אחרת).
- מראה (פונט, גדלים, ריווח, מיקום הלוגו, מינימליזם, קווים, כותרות) -> CSS או מבנה ב-template.html.
- שנה רק את מה שהתבקש. המסמך נשאר דף לבן, אלגנטי, מקצועי וקריא ללקוח (לא רקע כהה, לא צבעוני מדי).
- פרטים עובדתיים (שם, טלפון, אתר) רק בדיוק כמו ש{owner} כתב. אל תמציא.
- בלי <script>, בלי משאבים חיצוניים חוץ מ-fonts.googleapis.com, בלי מקף ארוך.

החזר בדיוק בפורמט הזה, בלי שום טקסט לפני או אחרי:
===SUMMARY===
משפט אחד קצר בעברית, בגוף ראשון, מה שינית (למשל: הוספתי את הלוגו בכותרת וצבעתי בכחול). אם הבקשה לא ברורה או לא קשורה ל-PDF, כתוב כאן שאלה קצרה ואל תשנה כלום.
===BRAND===
brand.json המלא (JSON תקין)
===TEMPLATE===
התבנית המלאה החדשה, או המילה UNCHANGED אם לא שינית אותה

התבנית הנוכחית:
{template}

brand.json הנוכחי:
{brand}"""


class _Check(HTMLParser):
    def __init__(self):
        super().__init__(); self.tags = set()
    def handle_starttag(self, tag, attrs): self.tags.add(tag)


def validate_template(t):
    """מחזיר שגיאה בעברית, או None אם התבנית תקינה."""
    body = strip_comments(t)
    for ph in ("{{TITLE}}", "{{CONTENT}}"):
        if ph not in body: return f"חסר בתבנית המקום {ph}"
    if body.count("{{CONTENT}}") != 1: return "המקום {{CONTENT}} חייב להופיע פעם אחת בדיוק"
    if re.search(r"<script\b", body, re.I): return "אסור סקריפט בתבנית"
    for u in re.findall(r"(?:href|src)\s*=\s*[\"']?(https?://[^\"'\s>]+)", body, re.I) + re.findall(r"@import\s+url\(['\"]?(https?://[^)'\"]+)", body, re.I):
        if not re.match(r"https://fonts\.(googleapis|gstatic)\.com/", u): return "התבנית טוענת משאב חיצוני שאסור: " + u[:60]
    c = _Check()
    try: c.feed(body); c.close()
    except Exception as e: return f"ה-HTML לא תקין: {e}"
    if not {"html", "body", "style"} <= c.tags: return "חסרים בתבנית html / body / style"
    return None


def snapshot():
    """שומר את המצב הנוכחי ל-history/N לפני שינוי. מחזיר N."""
    os.makedirs(HISTORY, exist_ok=True)
    nums = [int(x) for x in os.listdir(HISTORY) if x.isdigit()]
    n = max(nums, default=0) + 1
    d = os.path.join(HISTORY, str(n)); os.makedirs(d)
    for src, name in ((USER_TEMPLATE, "template.html"), (BRAND_JSON, "brand.json")):
        if os.path.exists(src): shutil.copy2(src, os.path.join(d, name))
    return n


def design(request, logo=None):
    claude = claude_path()
    if not claude: return {"ok": False, "error": "Claude לא נמצא במחשב"}
    b = load_brand()
    if logo:
        if not os.path.isfile(logo): return {"ok": False, "error": "קובץ הלוגו לא נמצא"}
        b["logo"] = logo
    tpl = load_template()
    owner = (CFG.get("owner_name") or "בעל העסק").strip()
    logo_line = f"צורף לוגו חדש, והוא כבר שמור ב-brand.json בשדה logo: {logo}\n" if logo else ""
    prompt = DESIGN_PROMPT.format(owner=owner, request=request, logo_line=logo_line, template=tpl,
                                  brand=json.dumps(b, ensure_ascii=False, indent=2))
    try:
        r = subprocess.run([claude, "-p", "--model", pick_model("summary_model", "sonnet"), "--tools", "", "--strict-mcp-config",
                            "--no-session-persistence"], input=prompt, capture_output=True, text=True, timeout=180, cwd="/tmp")
        out = r.stdout
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Claude לא ענה תוך 3 דקות"}
    except Exception as e:
        return {"ok": False, "error": f"שגיאה בהפעלת Claude: {e}"}
    m = re.search(r"===SUMMARY===\s*(.*?)\s*===BRAND===\s*(.*?)\s*===TEMPLATE===\s*(.*)$", out, re.S)
    if not m: return {"ok": False, "error": "Claude לא החזיר תשובה בפורמט הנכון, לא שיניתי כלום"}
    summary = m.group(1).strip().replace("—", "-")
    try:
        bj = m.group(2).strip()
        if bj.startswith("```"): bj = bj.split("\n", 1)[-1].rsplit("```", 1)[0]
        nb = json.loads(bj)
        assert isinstance(nb, dict)
    except Exception:
        return {"ok": False, "error": "brand.json שהתקבל לא תקין, לא שיניתי כלום"}
    new_t = m.group(3).strip()
    if new_t.startswith("```"): new_t = new_t.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    changed_t = new_t.upper() != "UNCHANGED" and new_t != tpl.strip()
    if changed_t:
        err = validate_template(new_t)
        if err: return {"ok": False, "error": "לא שמרתי את השינוי: " + err}
    # brand.json: רק שדות מוכרים, ערכים תקינים
    out_b = {}
    for k, v in default_brand().items():
        val = nb.get(k, b[k])
        if k == "notes":
            val = [x.strip().replace("—", "-") for x in (val if isinstance(val, list) else b[k]) if isinstance(x, str) and x.strip()][:20]
        elif k == "color":
            val = norm_hex(val, b["color"])
        else:
            val = str(val or "").strip().replace("—", "-")
        out_b[k] = val
    if logo: out_b["logo"] = logo
    elif out_b["logo"] and out_b["logo"] != b["logo"] and not os.path.isfile(os.path.expanduser(out_b["logo"])):
        out_b["logo"] = b["logo"]   # Claude לא ממציא נתיב ללוגו
    changed_b = out_b != load_brand() or not os.path.exists(BRAND_JSON) and out_b != default_brand()
    if not changed_t and not changed_b:
        return {"ok": True, "changed": False, "summary": summary or "לא שיניתי כלום"}
    n = snapshot()
    os.makedirs(PDF_DIR, exist_ok=True)
    if changed_t:
        with open(USER_TEMPLATE, "w", encoding="utf-8") as f: f.write(new_t + "\n")
    with open(BRAND_JSON, "w", encoding="utf-8") as f: json.dump(out_b, f, ensure_ascii=False, indent=2)
    return {"ok": True, "changed": True, "summary": summary or "עדכנתי את העיצוב", "version": n}


def undo():
    nums = sorted(int(x) for x in os.listdir(HISTORY) if x.isdigit()) if os.path.isdir(HISTORY) else []
    if not nums: return {"ok": False, "error": "אין שינוי לבטל"}
    d = os.path.join(HISTORY, str(nums[-1]))
    for dst, name in ((USER_TEMPLATE, "template.html"), (BRAND_JSON, "brand.json")):
        src = os.path.join(d, name)
        if os.path.exists(src): shutil.copy2(src, dst)
        elif os.path.exists(dst): os.remove(dst)
    shutil.rmtree(d)
    return {"ok": True, "changed": True, "summary": "ביטלתי את השינוי האחרון"}


def reset():
    if not os.path.exists(USER_TEMPLATE) and not os.path.exists(BRAND_JSON):
        return {"ok": True, "changed": False, "summary": "זה כבר העיצוב המקורי"}
    snapshot()   # אפשר לבטל גם את האיפוס
    for p in (USER_TEMPLATE, BRAND_JSON):
        if os.path.exists(p): os.remove(p)
    return {"ok": True, "changed": True, "summary": "חזרתי לעיצוב המקורי"}


if __name__ == "__main__":
    args = sys.argv[1:]
    def arg(k): return args[args.index(k) + 1] if k in args and args.index(k) + 1 < len(args) else None
    try:
        if "--design" in args: res = design(arg("--design") or "", arg("--logo"))
        elif "--snapshot" in args: res = {"ok": True, "version": snapshot()}
        elif "--undo" in args: res = undo()
        elif "--reset" in args: res = reset()
        elif args:
            md_path = args[0]
            res = build(md_path, arg("--out") or md_path[:-3] + ".pdf.html", local="--local" in args, preview="--preview" in args)
        else:
            print(__doc__ or "usage: pdf.py <summary.md> --out <file.html> [--local] [--preview] | --design <text> [--logo f] | --undo | --reset"); sys.exit(2)
    except Exception as e:
        res = {"ok": False, "html": "", "claude": False, "footer": "", "brand": "", "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(res, ensure_ascii=False), flush=True)
