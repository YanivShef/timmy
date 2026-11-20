"""Sending a call's summary on WhatsApp, always in two steps: draft -> the owner reads / edits it -> an explicit click
on a confirmed recipient -> send. Port of Recorder.swift SendSession (same kinds, prompts, cleanup, stages, texts).

Two ways to send (CallsModel.wa on the Mac):
* "linked": Timmy has its own Greenolink token (integrations/greenolink.py), contacts are searched and messages sent
  directly, no Claude involved in the sending itself;
* ("connected", server): the quiet fallback, Greenolink is connected to Claude Code as an MCP server; Claude Code
  searches (search permission only) and later sends with exactly the text shown, under a prompt that forbids anything else.

The rule "never send without an explicit click" is structural: the only code that sends is _send_direct /
_send_via_claude, reachable only from confirm_send(), which only the confirm button of the UI calls
(tests/test_whatsapp.py checks both, plus the mock server's log).

The session never blocks: slow work goes through `spawn(fn, done)` (fn in a worker thread, done(result) on the UI
thread). Tests pass a synchronous spawn. Changes are announced through `notify()`.
"""
from __future__ import annotations

import os
import re

from timmy import config as tconfig
from timmy.chat import memory, skills
from timmy.engine import procrun
from timmy.integrations.greenolink import Candidate, GLError, Greenolink, MCPServer, pretty_phone

LONG_DASH = chr(0x2014)
LRI, PDI = chr(0x2066), chr(0x2069)

KINDS = ("free", "summary", "tasks", "pdf")       # "בקשה משלי" first: the big card
TITLES = {"summary": "הודעת סיכום ללקוח", "tasks": "רשימת המשימות", "pdf": "ה-PDF", "free": "בקשה משלי"}
SUBS = {"summary": "כמה שורות על מה דיברנו ומה סוכם", "tasks": "מי עושה מה, שורה לכל משימה",
        "pdf": "קובץ הסיכום המעוצב + שורה קצרה", "free": "תגיד לטימי מה לנסח, מה לעשות ומה לשלוח"}


def mentions_file(t: str) -> bool:
    """The request talks about a file (PDF / קובץ / מסמך): "attach the PDF" switches on by itself, unless a negation
    is up to 3 words before it ("בלי ה-PDF", "לא לצרף קובץ")."""
    l = (t or "").lower().replace("פי די אף", "pdf").replace("סיכום המעוצב", "pdf")
    keys = ("pdf", "פידיאף", "קובץ", "קבצ", "מסמך", "צרף", "מצורף")
    neg = {"בלי", "ובלי", "לא", "ולא", "אל", "ואל", "ללא", "without", "no", "not", "don't", "dont"}
    words = [w for w in re.split(r"[\s,.!?:;\"'()]+", l) if w]
    for i, w in enumerate(words):
        if any(k in w for k in keys) and not any(x in neg for x in words[max(0, i - 3):i]):
            return True
    return False


def is_number(s: str) -> bool:
    s = (s or "").strip()
    return sum(ch.isdigit() for ch in s) >= 9 and re.fullmatch(r"[+0-9 ()-]+", s) is not None


def recipient_label(c: Candidate) -> str:
    """"name · 050-123-4567" (only the number when the name is the number)."""
    ph = pretty_phone(c.phone or c.jid)
    digits = lambda x: "".join(ch for ch in x if ch.isdigit())   # noqa: E731
    return ph if digits(c.name) == digits(c.phone) else c.name + " · " + ph


def _cands(o) -> list[Candidate]:
    out = []
    for c in (o or {}).get("candidates") or []:
        if not isinstance(c, dict):
            continue
        j = c.get("jid")
        if not isinstance(j, str) or not j:
            continue
        ph = c.get("phone") if isinstance(c.get("phone"), str) and c.get("phone") else j.split("@")[0]
        out.append(Candidate(c.get("name") if isinstance(c.get("name"), str) else j, j, ph))
    return out


def summary_body(text: str) -> str:
    from timmy.ui.calls import summary_body as sb
    return sb(text)


def _sync_spawn(fn, done):
    done(fn())


class SendSession:
    def __init__(self, call, wa, gl: Greenolink | None = None, notify=None, spawn=None, build_pdf=None, cfg: dict | None = None):
        self.call = call
        self.wa = wa                                   # "linked" | ("connected", MCPServer) | "missing"
        self.gl = gl
        self.notify = notify or (lambda: None)
        self.spawn = spawn or _sync_spawn
        self.build_pdf = build_pdf                     # (md, dest, done(ok)) -> None, the UI's PdfBuild
        self.cfg = cfg if cfg is not None else tconfig.load()
        self.kind = "summary"
        self.recipient = (call.person or "").split(" - ")[0].strip()
        self.request = ""
        self.attach_pdf = False
        self.pdf_touched = False
        self.guidance = ""
        self.draft = ""
        self.stage = ("choose",)       # choose | drafting(msg) | review | confirm(c) | sending | pick([c]) | sent(label) | failed(why)
        self.revision = ""
        self.revisions: list[str] = []
        self.revising = False
        self.revise_error = None
        self.prev_draft = None
        self.revised_draft = None
        self.gen = 0
        self.pdf_path = None
        self.resolving = False
        self.resolved = None
        self.chosen: Candidate | None = None
        self.resolve_error = None
        self.resolved_query = ""
        self.sent_log: list[tuple] = []     # (jid, kind) of what was actually sent (tests, UI)
        self.auth_failed = None              # callback(err) when the token is gone

    # ---- simple state ---------------------------------------------------------------------------------------------
    @property
    def direct(self) -> bool:
        return self.wa == "linked"

    @property
    def server(self) -> MCPServer | None:
        return self.wa[1] if isinstance(self.wa, tuple) and self.wa and self.wa[0] == "connected" else None

    @property
    def needs_pdf(self) -> bool:
        return self.kind == "pdf" or (self.kind == "free" and self.attach_pdf)

    @property
    def pending_revision(self) -> bool:
        return bool(self.revision.strip())

    @property
    def can_undo_revision(self) -> bool:
        return self.prev_draft is not None and not self.revising and self.draft == self.revised_draft

    @property
    def recipient_is_number(self) -> bool:
        return is_number(self.recipient)

    def set_stage(self, *st):
        self.stage = st
        self.notify()

    def set_request(self, text: str):
        self.request = text
        if not self.pdf_touched:
            self.attach_pdf = mentions_file(text)

    def touch_pdf(self, on: bool):
        self.pdf_touched = True
        self.attach_pdf = bool(on)

    @property
    def owner(self) -> str:
        return str(self.cfg.get("owner_name") or "").strip()

    def model_id(self) -> str:
        return str(self.cfg.get("summary_model") or "sonnet")

    # ---- prompts --------------------------------------------------------------------------------------------------
    def memory_for_draft(self, me: str) -> str:
        n = memory.load()
        if not n:
            return ""
        return (f"\nמה טימי יודע על {me} מצ'אטים קודמים. אלה העדפות קבועות: כבד בניסוח את מה שנוגע לכתיבה (טון, צורת פנייה, אימוג'ים, "
                "שם העסק), והתעלם ממה שנוגע למראה של האפליקציה. אלה לא עובדות על השיחה הזאת, אל תכתוב אותן בהודעה כאילו נאמרו בה. "
                "אם הנחיה אחרת כאן סותרת אותן, ההנחיה גוברת:\n" + "\n".join("- " + x for x in n))

    def _read(self, p) -> str:
        try:
            with open(p, encoding="utf-8-sig") as f:
                return f.read()
        except OSError:
            return ""

    def draft_prompt(self, revise: list[str] | None = None, current: str = "") -> str:
        md = summary_body(self._read(self.call.path))
        tr = self._read(self.call.transcript_path)
        me = self.owner or "בעל העסק"
        who = self.recipient or "הלקוח"
        ask = self.request.strip()
        guide = self.guidance.strip()
        if self.kind == "summary":
            what = f"הודעת וואטסאפ ל{who} עם סיכום השיחה: 2 עד 5 שורות קצרות, מה דיברנו, מה סוכם ומה הצעד הבא."
        elif self.kind == "tasks":
            what = f"הודעת וואטסאפ ל{who} עם רשימת המשימות שסוכמו בשיחה: שורת פתיחה קצרה, ואז כל משימה בשורה משלה עם מי אחראי ומתי (רק אם נאמר)."
        elif self.kind == "pdf":
            what = f"הודעה קצרה (שורה או שתיים) ל{who} שתישלח יחד עם קובץ PDF של סיכום השיחה."
        else:
            what = (f"הודעת וואטסאפ ל{who} בדיוק לפי מה ש{me} ביקש. זו הבקשה שלו, במילים שלו (מה לכתוב, באיזה טון, מה להדגיש, מה לבקש):\n"
                    f"<<<בקשה\n{ask}\nבקשה>>>\n"
                    f"בצע את הבקשה בתוך ההודעה עצמה: אם הוא מבקש להזכיר משהו, להדגיש נקודה, לבקש משהו מ{who}, לכתוב בטון מסוים או באורך מסוים, "
                    f"תעשה את זה. מה שהוא כתב בבקשה הוא עובדה שמותר להשתמש בה. ההודעה נשלחת מיד כש{me} לוחץ שלח, אז אם הבקשה מזכירה זמן "
                    "(למשל יום חמישי), שלב אותו בתוכן ההודעה. אם הבקשה מבקשת פעולה שאי אפשר לעשות בהודעה (למשל לקבוע ביומן), נסח רק את ההודעה."
                    + ("\nיחד עם ההודעה יישלח קובץ PDF של סיכום השיחה. אפשר להזכיר בקצרה שהוא מצורף, בלי קישור." if self.attach_pdf
                       else "\nלא מצורף שום קובץ להודעה, אז אל תכתוב שמשהו מצורף."))
        guide_line = (f"\nהנחיות של {me} לניסוח (חובה לכבד אותן, ומה שהוא כתב בהן הוא עובדה שמותר להשתמש בה): {guide}"
                      if self.kind != "free" and guide else "")
        rules = (f"כללים: עברית (אלא אם {me} ביקש שפה אחרת), גוף ראשון, בקול של {me}, כאילו הוא כותב בעצמו. קצר וטבעי לוואטסאפ. "
                 "אל תפתח ב\"תודה\" ואל תודה על השיחה. בלי מקף ארוך. אל תמציא עובדות, מספרים, תאריכים, מחירים או הבטחות שלא מופיעים "
                 f"בסיכום, בתמלול או במה ש{me} כתב כאן. בלי כותרות Markdown; מותר *הדגשה* בסגנון וואטסאפ במידה.\n"
                 f"{skills.skill_guide('writing')}{self.memory_for_draft(me)}\n"
                 "החזר רק את טקסט ההודעה הסופית, בלי שום הקדמה, הסבר, מחשבות, תיקונים עצמיים או מרכאות.")
        ctx = f"סיכום השיחה:\n{md}\n\nהתמלול (להקשר):\n{tr[-20000:]}"
        if not revise:
            return f"נסח {what}{guide_line}\n{rules}\n\n{ctx}"
        lst = "\n".join(f"{i + 1}. {a}" + ("   <- הבקשה החדשה" if i == len(revise) - 1 else "") for i, a in enumerate(revise))
        return (f"{me} ביקש ממך לנסח {what}{guide_line}\n\n"
                f"זו הטיוטה הנוכחית, כולל שינויים ש{me} אולי עשה בה בעצמו:\n<<<טיוטה\n{current}\nטיוטה>>>\n\n"
                f"בקשות השינוי ש{me} נתן עד עכשיו על הטיוטה, לפי הסדר. כולן עדיין בתוקף, האחרונה היא החדשה:\n{lst}\n\n"
                "עדכן את הטיוטה הנוכחית לפי הבקשה החדשה, בהקשר של כל השיחה: בקשה כמו \"עוד יותר קצר\", \"עוד קצת\", \"תחזיר את מה שהיה\" או "
                "\"גם בסוף\" מתייחסת לבקשות הקודמות ולטיוטה הנוכחית. שמור על מה שהבקשות הקודמות קבעו ועל שינויים שהוא עשה בעצמו, אלא אם "
                f"הבקשה החדשה אומרת אחרת. אל תשנה דברים שלא התבקשו.\n{rules}\n\n{ctx}")

    def clean_draft(self, out: str) -> str:
        text = (out or "").strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:]).replace("```", "")
        lines = text.split("\n")
        idx = None
        for i, ln in enumerate(lines[:3]):
            t = ln.strip()
            if t.endswith(":") and re.search(r"(הנה|להלן|זו|זאת).{0,25}(ההודעה|הטיוטה|הנוסח)", t):
                idx = i
        if idx is not None and idx + 1 < len(lines):
            text = "\n".join(lines[idx + 1:])
        first = (self.owner.split(" ") or [""])[0]
        if len(first) >= 2:
            m = re.match(r"^\s*" + re.escape(first) + r"\s*,\s*(הנה|להלן|זו|זאת|הכנתי|ניסחתי)[^.:!\n]{0,40}[.:!]\s*", text)
            if m and text[m.end():].strip():
                text = text[m.end():]
        return text.replace(LONG_DASH, "-").strip().strip("\"״").strip()

    # ---- draft ----------------------------------------------------------------------------------------------------
    def _claude(self, prompt: str, timeout: float = 120) -> procrun.ProcResult:
        return procrun.run_claude(["-p", "--model", self.model_id(), "--tools", "", "--strict-mcp-config", "--no-session-persistence"],
                                  prompt, timeout=timeout, cfg=self.cfg)

    def make_draft(self):
        self.gen += 1
        self.revisions, self.revision, self.revise_error, self.prev_draft, self.revising = [], "", None, None, False
        if self.resolved_query != self.recipient.strip() or self.resolved is None:
            self.start_resolve()
        if self.kind == "free" and not self.request.strip():
            return self.set_stage("failed", "תכתוב לטימי מה לנסח ומה לשלוח")
        self.pdf_path = None
        pdf = self.call.pdf_path
        if self.needs_pdf and not os.path.exists(pdf):
            if self.build_pdf is None:
                return self.set_stage("failed", "לא הצלחתי להכין את ה-PDF")
            self.set_stage("drafting", "מכין PDF…")
            g = self.gen

            def built(ok):
                if g != self.gen:
                    return
                if ok:
                    self.pdf_path = pdf
                    self._run_draft()
                else:
                    self.set_stage("failed", "לא הצלחתי להכין את ה-PDF")
            self.build_pdf(self.call.path, pdf, built)
            return
        if self.needs_pdf:
            self.pdf_path = pdf
        self._run_draft()

    def _run_draft(self):
        self.set_stage("drafting", "Claude מנסח…")
        prompt, g = self.draft_prompt(), self.gen

        def work():
            r = self._claude(prompt)
            return r, self.clean_draft(r.out)

        def done(res):
            r, text = res
            if g != self.gen:
                return
            if r.ok and text:
                self.draft = text
                self.set_stage("review")
            else:
                self.set_stage("failed", "Claude לא ענה בזמן. נסה שוב" if r.timed_out else "לא הצלחתי לנסח הודעה. בדוק שה-Claude מחובר")
        self.spawn(work, done)

    def revise(self):
        ask = self.revision.strip()
        if not ask or self.revising or self.stage[0] != "review":
            return
        cur, asks, g = self.draft, self.revisions + [ask], self.gen
        self.revising, self.revise_error = True, None
        self.notify()
        prompt = self.draft_prompt(revise=asks, current=cur)

        def work():
            r = self._claude(prompt)
            return r, self.clean_draft(r.out)

        def done(res):
            r, text = res
            if g != self.gen:
                return
            self.revising = False
            if self.stage[0] == "review":
                if r.ok and text:
                    self.prev_draft, self.draft, self.revised_draft, self.revisions = cur, text, text, asks
                    if self.revision.strip() == ask:
                        self.revision = ""
                else:
                    self.revise_error = ("Claude לא ענה בזמן. הטיוטה לא השתנתה, נסה שוב" if r.timed_out
                                         else "לא הצלחתי לעדכן את הנוסח. הטיוטה לא השתנתה")
            self.notify()
        self.spawn(work, done)

    def undo_revision(self):
        if self.prev_draft is None or not self.can_undo_revision:
            return
        self.draft, self.prev_draft, self.revised_draft = self.prev_draft, None, None
        if self.revisions:
            self.revisions.pop()
        self.notify()

    # ---- recipient (search only, never sends) ---------------------------------------------------------------------
    def _apply_resolved(self, q, cands, error=None):
        if self.resolved_query != q:
            return
        self.resolving, self.resolve_error = False, error
        self.resolved = cands if error is None else None
        if len(cands) == 1:
            self.chosen = cands[0]
        self.notify()

    def _search_prompt(self, q: str) -> tuple[str, list[str]]:
        s = self.server
        prompt = (f"קרא ל-{s.tool_prefix}search_contacts עם query=\"{q}\" (אם הכלי עוד לא טעון, טען אותו עם ToolSearch). אסור לשלוח שום דבר.\n"
                  "החזר שורה אחת של JSON בלבד: {\"candidates\": [{\"name\": \"...\", \"jid\": \"...\", \"phone\": \"...\"}]} עם כל אנשי הקשר "
                  "שיכולים להתאים (עד 8), או רשימה ריקה אם אין.")
        args = ["-p", "--model", "haiku", "--permission-mode", "dontAsk", "--tools", "ToolSearch",
                "--allowedTools", s.tool_prefix + "search_contacts", "--no-session-persistence"]
        return prompt, args

    def start_resolve(self):
        q = self.recipient.strip()
        self.resolved_query, self.chosen, self.resolved, self.resolve_error = q, None, None, None
        if not q:
            return
        if self.recipient_is_number:
            d = "".join(ch for ch in q if ch.isdigit())
            self.chosen = Candidate(q, d, d)
            self.resolved = [self.chosen]
            return
        if self.direct and self.gl is not None:
            self.resolving = True

            def work():
                try:
                    return self.gl.search_contacts(q), None
                except GLError as e:
                    return [], e

            def done(res):
                c, e = res
                self._apply_resolved(q, c, error=(e.msg if e else None))
                if e is not None and e.auth and self.auth_failed:
                    self.auth_failed(e)
            self.spawn(work, done)
            return
        if self.server is None:
            return
        self.resolving = True
        prompt, args = self._search_prompt(q)

        def work2():
            r = procrun.run_claude(args, prompt, timeout=90, cfg=self.cfg)
            return _cands(procrun.last_json_object(r.out, "candidates"))
        self.spawn(work2, lambda c: self._apply_resolved(q, c))

    def resolve_recipient(self):
        """"שלח" in the review: shows the confirm step (name + number) or the pick list. Never sends."""
        if not self.draft.strip() and self.pdf_path is None:
            return self.set_stage("failed", "ההודעה ריקה")
        if self.recipient_is_number:
            d = "".join(ch for ch in self.recipient if ch.isdigit())
            return self.set_stage("confirm", Candidate(self.recipient.strip(), d, d))
        rec = self.recipient.strip()
        if self.chosen is not None and self.resolved_query == rec:
            return self.set_stage("confirm", self.chosen)
        if self.resolved is not None and self.resolved_query == rec and len(self.resolved) > 1:
            return self.set_stage("pick", list(self.resolved))
        if not self.direct and self.server is None:
            return self.set_stage("failed", "וואטסאפ לא מחובר")
        self.set_stage("drafting", f"מחפש את {rec} באנשי הקשר…")
        if self.direct and self.gl is not None:
            def work():
                try:
                    return self.gl.search_contacts(rec), None
                except GLError as e:
                    return [], e
        else:
            prompt, args = self._search_prompt(rec)

            def work():
                r = procrun.run_claude(args, prompt, timeout=90, cfg=self.cfg)
                o = procrun.last_json_object(r.out, "candidates")
                return _cands(o), (None if o is not None else GLError("לא הצלחתי לחפש באנשי הקשר. בדוק שוואטסאפ מחובר"))

        def done(res):
            c, e = res
            if e is not None:
                if e.auth and self.auth_failed:
                    self.auth_failed(e)
                return self.set_stage("failed", e.msg)
            if not c:
                return self.set_stage("failed", f"לא מצאתי איש קשר בשם {rec}. אפשר לכתוב מספר טלפון")
            self.resolved_query, self.resolved = rec, c
            if len(c) == 1:
                self.chosen = c[0]
                return self.set_stage("confirm", c[0])
            self.set_stage("pick", c)
        self.spawn(work, done)

    def pick(self, c: Candidate):
        """A contact picked from the list: back to the confirm step (still no sending)."""
        self.chosen = c
        self.resolved_query = self.recipient.strip()
        self.set_stage("confirm", c)

    # ---- sending: only from the confirm click ---------------------------------------------------------------------
    def confirm_send(self, c: Candidate):
        """The ONLY way to send. Called by the "שלח עכשיו" button of the confirm step, with the candidate it shows."""
        if self.stage[0] != "confirm" or self.stage[1] != c:
            return
        if not self.draft.strip() and self.pdf_path is None:
            return self.set_stage("failed", "ההודעה ריקה")
        self.set_stage("sending")
        if self.direct and self.gl is not None:
            self.spawn(lambda: self._send_direct(c), self._sent)
        elif self.server is not None:
            self.spawn(lambda: self._send_via_claude(c), self._sent)
        else:
            self.set_stage("failed", "וואטסאפ לא מחובר")

    def _sent(self, res):
        ok, msg, err = res
        if ok:
            self.set_stage("sent", msg)
        else:
            if err is not None and getattr(err, "auth", False) and self.auth_failed:
                self.auth_failed(err)
            self.set_stage("failed", msg)

    def _send_direct(self, c: Candidate):
        text, pdf = self.draft.strip(), self.pdf_path
        text_sent = False
        try:
            if text:
                self.gl.send_text(c.jid, text)
                text_sent = True
                self.sent_log.append((c.jid, "text"))
            if pdf:
                self.gl.send_file(c.jid, pdf)
                self.sent_log.append((c.jid, "file"))
            return True, recipient_label(c), None
        except GLError as e:
            return False, ("ההודעה נשלחה, אבל הקובץ לא: " + e.msg if text_sent else e.msg), e

    def send_prompt(self, prefix: str, jid: str) -> str:
        text = self.draft.strip()
        steps = [f"הנמען הוא {jid}. השתמש בו בדיוק כמו שהוא בשדה recipient, בלי לחפש אנשי קשר."]
        if text:
            steps.append(f"שלח עם {prefix}send_message(recipient=<ה-jid>, message=...) את הטקסט שבין <<<הודעה לבין הודעה>>> בדיוק כמו שהוא, "
                         "תו בתו, בלי לשנות, להוסיף או לתרגם.")
        if self.pdf_path:
            steps.append(f"שלח את קובץ ה-PDF: קרא ל-{prefix}request_file_upload(filename=\"{os.path.basename(self.pdf_path)}\"), העלה את הקובץ "
                         f"עם Bash בפקודה הזאת בדיוק (עם ה-upload_url שקיבלת): curl -sS -X POST --data-binary @\"{self.pdf_path}\" \"<upload_url>\" "
                         f"ואז קרא ל-{prefix}send_file(recipient=<ה-jid>, upload_id=<ה-upload_id>).")
        numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
        return (f"אתה סוכן השליחה של טימי. המשימה: לשלוח הודעת וואטסאפ אחת דרך הכלים של Greenolink (שמות הכלים מתחילים ב-{prefix}; "
                "אם הם עוד לא טעונים, טען אותם עם ToolSearch).\n"
                f"נמען: \"{self.recipient}\"\n{numbered}\n"
                "אסור: לשלוח למישהו אחר, לשלוח יותר מפעם אחת, לשלוח הודעות נוספות, לקרוא או לשנות הודעות.\n"
                "בסוף החזר שורה אחת של JSON בלבד, בלי שום טקסט נוסף:\n"
                "{\"sent\": true או false, \"to\": \"<שם> (<jid>)\", \"error\": \"<אם נכשל: הסבר קצר בעברית>\", \"candidates\": []}\n\n"
                f"<<<הודעה\n{text}\nהודעה>>>")

    def send_args(self, prefix: str) -> list[str]:
        a = ["-p", "--model", self.model_id(), "--permission-mode", "dontAsk", "--tools", "ToolSearch" if self.pdf_path is None else "ToolSearch,Bash",
             "--allowedTools", prefix + "*"]
        if self.pdf_path is not None:
            a.append("Bash(curl:*)")
        a.append("--no-session-persistence")
        return a

    def _send_via_claude(self, c: Candidate):
        s = self.server
        r = procrun.run_claude(self.send_args(s.tool_prefix), self.send_prompt(s.tool_prefix, c.jid), timeout=180, cfg=self.cfg)
        o = procrun.last_json_object(r.out, "sent")
        if o is None:
            return False, ("השליחה לא הסתיימה בזמן. בדוק בוואטסאפ אם ההודעה יצאה לפני שתנסה שוב" if r.timed_out
                           else "לא קיבלתי תשובה ברורה מ-Claude. בדוק בוואטסאפ אם ההודעה יצאה"), None
        if o.get("sent") is True:
            self.sent_log.append((c.jid, "claude"))
            return True, recipient_label(c), None
        e = o.get("error")
        return False, (e if isinstance(e, str) and e else "ההודעה לא נשלחה"), None


def check_wa(cfg: dict | None = None):
    """Mac checkWhatsApp: our own token -> "linked"; else (unless greenolink_fallback_off) Greenolink connected to
    Claude Code -> ("connected", server); else "missing". Blocks (runs `claude mcp list`): call from a worker thread."""
    from timmy.integrations import greenolink
    cfg = cfg if cfg is not None else tconfig.load()
    if greenolink.has_token():
        return "linked"
    if cfg.get("greenolink_fallback_off") is True:
        return "missing"
    r = procrun.run_claude(["mcp", "list"], timeout=60, cfg=cfg)
    if not r.launched:
        return "missing"
    s = greenolink.find_greenolink(greenolink.parse_mcp_list(r.out))
    return ("connected", s) if s is not None and s.connected else "missing"
