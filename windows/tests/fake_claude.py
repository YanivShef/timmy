"""Stand-in for the Claude Code CLI (`claude -p --model m`, prompt on stdin) in tests. Never calls a model.

Answers by prompt kind (the prompts come from mac/live.py through live_shared):
  key points -> two "- " lines, summary -> the SUMMARY_PROMPT format, restyle -> the document with one line added;
  build step 4: the chat JSON ($FAKE_CLAUDE_CHAT), WhatsApp drafts ($FAKE_CLAUDE_DRAFT), contact search / send through
  the MCP fallback, a writing-voice skill, a PDF design skill (stream-json), the health question; and the CLI
  commands --version, auth status ($FAKE_CLAUDE_AUTH true|false|none), auth login ($FAKE_CLAUDE_LOGIN ok|code|outdated),
  mcp list ($FAKE_CLAUDE_MCP_LIST).
Every call is appended to $FAKE_CLAUDE_LOG as one JSON line (argv, cwd, kind, prompt) so tests can check the flags,
the working folder and that prompts went through stdin. $FAKE_CLAUDE_SLEEP delays the answer (timeout tests),
$FAKE_CLAUDE_PERSON / $FAKE_CLAUDE_TITLE change the summary answer.
"""
import json
import os
import sys
import time


def cli(argv):
    """Non-prompt commands (onboarding, WhatsApp fallback): --version, auth status / login, mcp list."""
    if argv[:1] == ["--version"]:
        print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.300 (Claude Code)"))
        return 0
    if argv[:2] == ["auth", "status"]:
        st = os.environ.get("FAKE_CLAUDE_AUTH", "true")
        if st == "none":
            print("error: unknown command 'auth'")
            return 1
        print(json.dumps({"loggedIn": st == "true", "authMethod": "claude.ai"}))
        return 0
    if argv[:2] == ["auth", "login"]:
        print("Opening browser to sign in...")
        print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=test")
        sys.stdout.flush()
        mode = os.environ.get("FAKE_CLAUDE_LOGIN", "ok")
        if mode == "code":
            code = sys.stdin.readline().strip()
            print("Login successful." if code == "GOODCODE" else "OAuth error: Invalid code")
            return 0 if code == "GOODCODE" else 1
        if mode == "outdated":
            print("error: unknown option '--claudeai'")
            return 1
        time.sleep(float(os.environ.get("FAKE_CLAUDE_LOGIN_SLEEP") or 0.3))
        print("Login successful.")
        return 0
    if argv[:2] == ["mcp", "list"]:
        print(os.environ.get("FAKE_CLAUDE_MCP_LIST", "No MCP servers configured."))
        return 0
    return None


WRITING = """===NAME===
test-writing-voice
===DESCRIPTION===
הקול והסגנון של בעל העסק. השתמש בו בכל טקסט שנכתב בשמו.
===SUMMARY===
אתה כותב קצר וישיר, בגוף ראשון.
===SKILL===
## מתי להשתמש
בכל הודעה ללקוח.
## הקול
קצר, ישיר וחם. משפטים של עד 12 מילים. פנייה בגוף שני, בלי רשמיות מיותרת.
## מילים וביטויים
- כן, ככה הוא כותב: "אהלן <שם>", "סגרנו", "יאללה"
- אף פעם לא: "בברכה", "לכבוד"
## אימוג'י, סימני פיסוק וחתימה
אימוג'י אחד לכל היותר, בלי חתימה.
## מבנה לפי סוג טקסט
### הודעת וואטסאפ ללקוח
שורת פתיחה, שתיים או שלוש שורות תוכן, שורת המשך.
## דוגמאות לפני ואחרי
לפני: שלום רב, רצינו לעדכן. אחרי: אהלן רוני, עדכון קצר.
## מה לא לעשות
לא לכתוב פסקאות ארוכות, לא להמציא מחירים.
"""

TEMPLATE = """<!doctype html><html dir="rtl" lang="he"><head><meta charset="utf-8"><title>{{TITLE}}</title>
<style>@page { size: A4; margin: 18mm 16mm; } body { font-family: Arial, sans-serif; color: #1E3A8A; } h2 { color: #EF4444; }
header.doc-head h1 { font-size: 22pt; } .kicker { color: #22C55E; } ul.tasks li { margin: 4px 0; }</style></head>
<body>{{CONTENT}}</body></html>"""

PDF_SKILL = """===NAME===
blue-test-pdf-style
===DESCRIPTION===
עיצוב PDF בסגנון כחול ונקי. השתמש בו כשמייצרים סיכום שיחה.
===SUMMARY===
למדתי עיצוב כחול ונקי עם כותרות אדומות.
===SKILL===
## מתי להשתמש
בכל PDF ללקוח.
## מערכת העיצוב
### צבעים
| תפקיד | hex | איפה |
|---|---|---|
| טקסט | #1E3A8A | גוף |
| הדגשה | #EF4444 | כותרות |
### טיפוגרפיה
Arial, 11pt, כותרות 17pt, גובה שורה 1.5.
### עמוד ושוליים
A4, שוליים 18 מ"מ למעלה ולמטה ו-16 מ"מ בצדדים, עמודה אחת.
## כללי עברית ומימין לשמאל
הכל מימין לשמאל.
### ראש המסמך
פס כחול כהה לכל רוחב העמוד, כותרת לבנה גדולה, שורת משנה ירוקה קטנה מתחתיה.
### כותרות סעיפים, טקסט ורשימות
כותרות סעיפים באדום, קו דק מתחת, רשימות עם נקודות עגולות.
## האווירה
הרבה לבן, מעט קווים, צפיפות נמוכה.
===TEMPLATE===
""" + TEMPLATE


def main():
    argv = sys.argv[1:]
    raw = b"" if argv[:2] == ["auth", "login"] else None
    if raw is None:
        r = cli(argv)
        if r is not None:
            log_call(argv, "cli", "")
            sys.exit(r)
        raw = sys.stdin.buffer.read()
    else:
        log_call(argv, "cli", "")
        sys.exit(cli(argv))
    prompt = raw.decode("utf-8")
    if "--input-format" in argv:          # stream-json with images (PDF design skill)
        kind = "pdf_skill"
    elif "אתה כותב סקיל ניסוח" in prompt:
        kind = "writing_skill"
    elif 'החזר JSON בלבד בשורה אחת: {"reply"' in prompt:
        kind = "chat"
    elif "סוכן השליחה של טימי" in prompt:
        kind = "send"
    elif "search_contacts עם query" in prompt:
        kind = "search"
    elif prompt.startswith("נסח ") or "ביקש ממך לנסח" in prompt:
        kind = "draft"
    elif "ענה במילה אחת בלבד" in prompt:
        kind = "health"
    elif "אתה רושם נקודות עיקריות" in prompt:
        kind = "bullets"
    elif "כתוב מחדש את מסמך הסיכום" in prompt:
        kind = "restyle"
    elif "עם מי השיחה" in prompt:
        kind = "summary"
    else:
        kind = "other"
    log_call(argv, kind, prompt)
    delay = float(os.environ.get("FAKE_CLAUDE_SLEEP") or 0)
    if delay:
        time.sleep(delay)
    if kind == "bullets":
        out = "- הצד השני רוצה לקבוע פגישה ביום שלישי\n- יש לדבר על הצעת המחיר לאתר"
    elif kind == "summary":
        person = os.environ.get("FAKE_CLAUDE_PERSON", "לא ידוע")
        title = os.environ.get("FAKE_CLAUDE_TITLE", "קביעת פגישה על הצעת מחיר")
        out = (f"עם: {person}\nכותרת: {title}\nסיכום:\nהצד השני ביקש לקבוע פגישה ביום שלישי בעשר בבוקר.\n"
               "הנושא הוא הצעת המחיר לאתר.\nמשימות:\n- לשלוח זימון לפגישה (אני)")
    elif kind == "restyle":
        doc = prompt.split("המסמך הנוכחי:\n", 1)[1].split("\n\nהתמלול:\n", 1)[0]
        lines = doc.split("\n")
        out = "\n".join(lines[:1] + ["", "**נכתב מחדש לפי ההוראות**"] + lines[1:]).strip()
    elif kind == "chat":
        out = os.environ.get("FAKE_CLAUDE_CHAT") or json.dumps({"reply": "בסדר, הוספתי.", "actions": []}, ensure_ascii=False)
        out = "חושב...\n" + out
    elif kind == "draft":
        out = os.environ.get("FAKE_CLAUDE_DRAFT") or ("הנה ההודעה:\nהיי, סיכמנו שנפגש ביום שלישי ב-10 ונעבור על הצעת המחיר לאתר."
                                                    if "ביקש ממך לנסח" not in prompt else "היי, נפגש ביום שלישי ב-10.")
    elif kind == "writing_skill":
        out = WRITING
    elif kind == "pdf_skill":
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": PDF_SKILL}, ensure_ascii=False)
    elif kind == "search":
        out = os.environ.get("FAKE_CLAUDE_SEARCH") or json.dumps({"candidates": [{"name": "דנה כהן", "jid": "972501234567@s.whatsapp.net",
                                                                                   "phone": "972501234567"}]}, ensure_ascii=False)
    elif kind == "send":
        out = json.dumps({"sent": True, "to": "דנה כהן (972501234567@s.whatsapp.net)", "error": "", "candidates": []}, ensure_ascii=False)
    elif kind == "health" and os.environ.get("FAKE_CLAUDE_HEALTH") == "auth":
        sys.stdout.write("Invalid API key. Please run /login\n")
        sys.exit(1)
    else:
        out = "מחובר"
    sys.stdout.buffer.write((out + "\n").encode("utf-8"))
    sys.stdout.flush()


def log_call(argv, kind, prompt):
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "kind": kind, "prompt": prompt, "pid": os.getpid()},
                               ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
