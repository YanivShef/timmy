"""The "שפר את טימי" / "עצב את טימי" chat: you write to Timmy, he answers and changes. Port of Recorder.swift TimmyChatModel.

design=True ("עצב את טימי"): only looks (accent, eyes, background, animations), remember / forget.
design=False ("שפר את טימי"): also instructions for the key points and summaries, the client PDF (handed to the
"עיצוב ה-PDF" chat), applying to the last call, linked Claude skills, creating skills, screen-share hiding.

One `claude -p --model haiku --tools ""` call per message returns {"reply", "actions"}; a clear looks request is
applied right away without Claude (looks.is_simple_looks). The conversation (last 20 messages, hidden ctx notes),
the instructions, memory.json and the linked skills go into every prompt, exactly like on the Mac.
Not yet on Windows: images in a call's PDF (add_images / image_edit / video_frames); the prompt tells Claude so.

No Qt here: slow work goes through spawn(fn, done) and the UI is told through notify().
"""
from __future__ import annotations

import os
import re

from timmy import config as tconfig
from timmy import paths
from timmy.chat import history, looks, memory, skills
from timmy.chat.history import ChatMsg
from timmy.engine import procrun
from timmy.ui import theme as T

BUSY = "טימי כותב…"
LONG_DASH = chr(0x2014)
LRI, PDI = chr(0x2066), chr(0x2069)
DOC_EXT = (".pdf", ".txt", ".text", ".md", ".markdown", ".rtf", ".docx", ".doc", ".odt", ".html", ".htm")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp")


def is_image(p: str) -> bool:
    return p.lower().endswith(IMG_EXT)


def _sync_spawn(fn, done):
    done(fn())


def last_summary_file() -> str | None:
    from timmy.ui import calls as C
    lst = C.list_calls()
    return lst[0].path if lst else None


class TimmyChat:
    def __init__(self, design: bool, notify=None, spawn=None, open_pdf_design=None, set_share_hiding=None, reveal=None):
        self.design = design
        self.notify = notify or (lambda: None)
        self.spawn = spawn or _sync_spawn
        self.open_pdf_design = open_pdf_design or (lambda **kw: None)     # (send=, logo=, adopt=, then=, create_from=, pick=)
        self.set_share_hiding = set_share_hiding or (lambda on: tconfig.save({"hide_in_screen_share": bool(on)}))
        self.reveal = reveal or (lambda path: None)
        self.msgs: list[ChatMsg] = []
        self.busy: str | None = None
        self.items: list[str] = []
        self.memory: list[str] = []
        self.skills: dict[str, str] = {}
        self.attachments: list[str] = []
        self.cancel_action = None
        self.queue: list[str] = []
        self.loaded = False

    # ---- state ----------------------------------------------------------------------------------------------------
    @property
    def chat_file(self) -> str:
        return os.path.join(paths.app_dir(), "chat-design.json" if self.design else "chat-improve.json")

    @property
    def intro(self) -> str:
        if self.design:
            return "היי, אני טימי. תגיד לי איך אני נראה: צבע ההדגשה, העיניים, הרקע או האנימציות. אני משתנה מיד, ותראה את זה מאחורי החלון."
        return ("היי, אני טימי. תגיד לי מה לשנות: איך אני מסכם שיחות, מה אני רושם בנקודות בזמן השיחה, איך ה-PDF נראה, או איך אני נראה. "
                "אני זוכר את זה לכל השיחות הבאות.")

    @property
    def suggestions(self) -> list[str]:
        if self.design:
            return ["עיניים ירוקות", "רקע כחול כהה", "תחליף את הצבע לסגול", "תוריד אנימציות"]
        return ["נקודות קצרות יותר, עד 8 מילים", "תוסיף לסיכום סעיף \"צעדים הבאים ללקוח\"", "תכתוב את הסיכום בגוף ראשון", "עיניים ירוקות",
                "יש לי כבר סקיל PDF בקלוד קוד, תשתמש בו", "צור לי סקיל ניסוח בסגנון שלי"]

    def load_skills(self):
        self.skills = {p: lk[0] for p, _ in skills.PURPOSES if (lk := skills.linked_skill(p))}

    def load(self):
        ci = tconfig.load().get("custom_instructions")
        self.items = [x for x in ci if isinstance(x, str)] if isinstance(ci, list) else []
        self.memory = memory.load()
        self.load_skills()
        if not self.loaded:
            self.loaded = True
            self.msgs = history.load(self.chat_file)
            if not self.msgs:
                self.msgs = [ChatMsg(False, self.intro)]
        self.notify()

    def save(self):
        history.save(self.chat_file, self.msgs, 60)

    def say(self, t: str, error: bool = False, ctx: str | None = None, **kw):
        self.msgs.append(ChatMsg(False, t, is_error=error, ctx=ctx, **kw))
        self.save()
        self.notify()

    def save_items(self):
        tconfig.save({"custom_instructions": self.items})

    def remove_item(self, t: str):
        self.items = [x for x in self.items if x != t]
        self.save_items()
        self.notify()

    def remove_note(self, t: str):
        memory.forget(t)
        self.memory = memory.load()
        self.notify()

    def unlink_skill(self, purpose: str):
        skills.set_linked_skill(purpose, None)
        self.skills.pop(purpose, None)
        self.notify()

    def look_ctx(self, before: str) -> str | None:
        after = looks.look_state()
        return None if before == after else "המראה לפני: " + before + ". אחרי: " + after

    def reset_looks(self):
        before = looks.look_state()
        self.say(looks.apply_looks({"reset": True}), ctx=self.look_ctx(before))

    def set_busy(self, b):
        self.busy = b
        self.notify()

    # ---- attachments ----------------------------------------------------------------------------------------------
    def add_attachments(self, files: list[str]):
        good = [f for f in files if f.lower().endswith(DOC_EXT) or is_image(f)]
        for f in good:
            if f not in self.attachments:
                self.attachments.append(f)
        if len(good) < len(files):
            self.say("אפשר לצרף תמונה (למשל הלוגו), PDF, Word, טקסט או Markdown. את השאר דילגתי.", error=True)
        self.notify()

    @property
    def current_files(self) -> list[str]:
        m = self.msgs[-1] if self.msgs else None
        return list(m.files or []) if m is not None and m.from_user else []

    @property
    def attached_files(self) -> list[str]:
        out = []
        for m in self.msgs:
            for f in m.files or []:
                if f not in out:
                    out.append(f)
        return out

    @property
    def latest_image_batch(self) -> list[str]:
        for m in reversed(self.msgs[-8:]):
            if m.from_user:
                imgs = [f for f in (m.files or []) if is_image(f)]
                if imgs:
                    return imgs
        return []

    # ---- sending --------------------------------------------------------------------------------------------------
    def send(self, text: str | None = None, typed: str = ""):
        """text=None: what the user typed (`typed`) plus the attachments; a string: a suggestion chip / queued message."""
        t = (typed if text is None else text).strip()
        files = list(self.attachments) if text is None and not self.design else []
        if not t and not files:
            return
        if self.busy is not None and files:
            last = self.msgs[-1] if self.msgs else None
            if not (last is not None and not last.from_user and last.text.startswith("רגע, אני עוד")):
                self.say("רגע, אני עוד באמצע. הקבצים נשארים מצורפים, ותשלח אותם שוב כשאסיים.")
            return
        if text is None:
            self.attachments = []
        if self.busy is not None:
            self.queue.append(t)
            self.notify()
            return
        if files:
            names = "\n".join("📎 " + LRI + os.path.basename(f) + PDI for f in files)
            self.msgs.append(ChatMsg(True, ("צירפתי:\n" + names) if not t else t + "\n" + names, files=files))
            self.save()
            self.ask_claude(t or "צירפתי קבצים: " + ", ".join(os.path.basename(f) for f in files))
            return
        self.msgs.append(ChatMsg(True, t))
        self.save()
        follow = len(self.msgs) > 2 and looks.is_follow_up(t)
        recent = [m.text for m in self.msgs[:-1][-4:] if m.text != self.intro]
        parsed = looks.parse_appearance(t)
        reset = bool(parsed and "reset" in parsed)
        if (not follow and not looks.mentions_memory(t) and (self.design or looks.is_appearance_instruction(t))
                and (reset or looks.is_simple_looks(t, recent)) and parsed):
            before = looks.look_state()
            reply = looks.apply_looks(parsed)
            self.say(reply, ctx=self.look_ctx(before))
            self.next()
            return
        self.ask_claude(t)

    def next(self):
        if self.busy is None and self.queue:
            self.send(self.queue.pop(0))
        else:
            self.notify()

    # ---- the prompt -----------------------------------------------------------------------------------------------
    def saved_logo_exists(self) -> bool:
        import json
        try:
            with open(os.path.join(paths.app_dir(), "pdf", "brand.json"), encoding="utf-8") as f:
                lg = json.load(f).get("logo")
        except (OSError, ValueError, AttributeError):
            return False
        return isinstance(lg, str) and bool(lg) and os.path.exists(os.path.expanduser(lg))

    def skills_block(self, t: str) -> str:
        talk = any(("סקיל" in s or "skill" in s.lower()) for s in [t] + [m.text for m in self.msgs[-6:]])
        out = "סקילים מחוברים: " + skills.linked_skills_line()
        if skills.linked_skill("pdf"):
            out += " (סקיל ה-PDF מייצר את ה-PDF בעצמו)" if skills.skill_runs_pdf() else " (טימי מעצב את התבנית שלו לפי סקיל ה-PDF)"
        w = skills.linked_skill("writing")
        if w and skills.is_timmy_made(w[1]):
            out += " (את סקיל הניסוח טימי יצר)"
        allsk = skills.list_claude_skills()
        if talk and allsk:
            out += "\nהסקילים של Claude במחשב (שם: תיאור):\n" + skills.catalog(allsk)
        else:
            out += f"\n(יש במחשב {len(allsk)} סקילים של Claude. הרשימה המלאה מופיעה כאן כשמדברים על סקילים.)"
        return out

    def attachments_block(self) -> str:
        f = self.attached_files
        if not f:
            return ""
        now = self.current_files
        older = [x for x in f if x not in now]
        out = ""
        if now:
            out += (f"\nקבצים שהמשתמש צירף בהודעה החדשה ({len(now)}): " + ", ".join(os.path.basename(x) for x in now)
                    + ". \"את זה\" / \"אותן\" / \"אותם\" = כל הקבצים האלה.")
        if older:
            out += ("\nקבצים שצירף קודם בשיחה (כבר טופלו, ואולי שייכים לשיחה עם לקוח אחר. אל תשתמש בהם אלא אם ביקש אחד מהם בשם): "
                    + ", ".join(os.path.basename(x) for x in older[-10:]))
        return out

    def prompt(self, t: str) -> str:
        cfg = tconfig.load()
        owner = str(cfg.get("owner_name") or "").strip()
        mem = "\n".join(f"{i + 1}. {x}" for i, x in enumerate(self.items)) if self.items else "אין"
        hist = history.chat_history(self.msgs[:-1])
        hide = cfg.get("hide_in_screen_share")
        hide = True if hide is None else bool(hide)
        if self.design:
            scope = ("אתה במצב \"עצב את טימי\": מותר רק appearance, remember ו-forget. אם מבקשים משהו אחר (סיכומים, נקודות, PDF) "
                     "אל תחזיר פעולות כאלה, ותגיד בעדינות שזה נמצא ב\"שפר את טימי\".")
        else:
            logo_rule = ("כבר יש לוגו שמור ב-PDF: שינוי בו (גודל, מיקום) = pdf_instruction רגיל." if self.saved_logo_exists() else
                         "עוד אין לוגו שמור ב-PDF: אם ביקש לשים לוגו ולא צירף תמונה, אל תחזיר pdf_instruction, ותבקש ממנו לצרף את הלוגו עם 📎.")
            scope = f"""- {{"type":"add_instruction","text":"..."}} הוראה קבועה לנקודות שבזמן השיחה או לסיכום השיחה. ניסוח קצר וברור, כמו הוראה למסכם.
- {{"type":"remove_instruction","text":"<הטקסט המדויק מהרשימה>"}}
- {{"type":"pdf_instruction","text":"...","logo":"<שם קובץ התמונה שצירף, רק אם זה הלוגו שלו>"}} כל מה שקשור לקובץ ה-PDF ללקוח (לוגו, עיצוב המסמך, שם העסק, פרטי קשר בתחתית). הטקסט עובר לצ'אט אחר שלא רואה את השיחה הזאת, אז הוא חייב לעמוד בפני עצמו: קודם תפתור את ההקשר ואז תכתוב הוראה מלאה (לא "עוד יותר", אלא "הכותרות בכחול כהה יותר מעכשיו").
  לוגו: אם צירף תמונה ואמר שזה הלוגו, תעביר את השם שלה ב-logo, וטימי מעביר את הקובץ עצמו לחלון "עיצוב ה-PDF". {logo_rule}
  ב-reply תגיד שאתה מעביר את זה לחלון "עיצוב ה-PDF" ושם יראה את התוצאה בעוד רגע. אל תגיד שכבר שינית או הוספת: השינוי קורה שם.
- {{"type":"apply_to_last_call"}} רק אם המשתמש ביקש להחיל על השיחה האחרונה (גם "כן" לשאלה שלך אם להחיל).
- {{"type":"link_skill","purpose":"pdf" או "writing","skill":"<השם המדויק מרשימת הסקילים, או נתיב>"}} לחבר סקיל של Claude שיש במחשב.
  "pdf" = טימי משתמש בסקיל כמו שהוא: אם יש לסקיל סקריפט שמייצר PDF, כל ה-PDF-ים ללקוח ייצאו ממנו, בדיוק כמו בקלוד קוד. אם אין, טימי מעצב את התבנית שלו לפי הסקיל. זה קורה לבד בחלון "עיצוב ה-PDF" מיד אחרי החיבור, אז אל תוסיף על זה pdf_instruction. ב-reply רק תגיד איזה סקיל חיברת. אל תגיד לו לפתוח את חלון עיצוב ה-PDF: הוא נפתח לבד, וטימי מוסיף על זה שורה בעצמו.
  "יש לי כבר סקיל PDF בקלוד קוד, תשתמש בו" = link_skill עם purpose "pdf" (לפי הכללים שלמטה: סקיל אחד מתאים מחברים, כמה מתאימים שואלים איזה).
  "writing" = לנסח לפיו כל טקסט שטימי מנסח עם Claude: הודעות וואטסאפ ותוכן ה-PDF ללקוח (לא הסיכומים ולא הנקודות).
  אם יותר מסקיל אחד ברשימה מתאים למה שתיאר, אל תחבר עדיין: תשאל ב-reply איזה מהם, עם 2 עד 4 שמות מדויקים ומה כל אחד עושה בכמה מילים. בהודעה הבאה ("הראשון", "זה של המדריכים", חלק מהשם) תבין לפי השיחה למי הוא התכוון ותחבר אותו.
  אם סקיל אחד מתאים בבירור, תחבר בלי לשאול. אף פעם אל תבקש נתיב או "שם מדויק" כשיש ברשימה סקילים שקשורים למה שתיאר (לפי השם או התיאור): תציע אותם בשמם. רק אם אין ברשימה שום סקיל קשור, תגיד בכנות שלא מצאת ותבקש את השם או את הנתיב לתיקייה. נתיב שהמשתמש כתב (מתחיל ב-C:\\ או ~) מעבירים בדיוק כמו שהוא.
- {{"type":"create_pdf_skill","file":"<השם של קובץ PDF שצורף בשיחה, או ריק>"}} ליצור סקיל עיצוב PDF חדש מ-PDF שהמשתמש אוהב את העיצוב שלו (מסמך מותג, הצעת מחיר ישנה). טימי לומד ממנו צבעים, פונטים ומבנה, שומר אותו כסקיל של Claude Code, מחבר אותו, ומעכשיו ה-PDF-ים יוצאים בעיצוב הזה.
  אם צירף PDF בשיחה (ראה "קבצים שהמשתמש צירף"), תעביר את השם שלו. אם לא צירף, תשאיר ריק: טימי פותח לו מיד חלון לבחירת הקובץ.
  ב-reply: אם צירף, תגיד שאתה מתחיל ושזה לוקח דקה או שתיים בחלון "עיצוב ה-PDF". אם לא צירף, תגיד רק שפתחת לו חלון לבחור את הקובץ, ושאחרי הבחירה זה לוקח דקה או שתיים. אל תשאל אם צירף ואל תבקש את שם הקובץ.
- {{"type":"create_writing_skill","files":["<שמות קבצים שצורפו כדוגמאות כתיבה>"],"use_pasted":true/false,"samples":["<תחילת כל הודעה שהוא הדביק בשיחה כדוגמה, 4 עד 8 מילים מכל אחת, מילה במילה>"],"use_calls":true/false,"answers":"<סיכום מלא של מה שענה: טון, מילים שלא משתמש בהן, רשמיות, אימוג'י, חתימה, ותיקונים שביקש>","replace":true/false}}
  ליצור סקיל ניסוח חדש בקול של המשתמש, שטימי ינסח לפיו הודעות וואטסאפ ואת הטקסט ב-PDF.
  התהליך: כשמבקשים סקיל ניסוח (ועוד לא שאלת), תשאל בהודעה אחת 2 עד 3 שאלות קצרות: איזה טון (קליל / ישיר / מכובד), מילים שהוא אף פעם לא משתמש בהן, אימוג'י וחתימה. ובאותה הודעה תבקש דוגמאות: לצרף קבצים עם 📎 (הודעות, מיילים, מסמכים), להדביק כאן כמה הודעות שכתב, ו/או לתת לך ללמוד מאיך שהוא מדבר בשיחות שהקלטת (תשאל אם מותר).
  אחרי שענה (גם תשובה חלקית מספיקה) ויש לפחות מקור אחד (קובץ, טקסט שהדביק, או הסכמה לשיחות), תחזיר create_writing_skill בלי לשאול עוד. use_pasted=true אם הדביק בשיחה טקסט שהוא כתב. use_calls=true רק אם הסכים. ב-reply תגיד שאתה מתחיל ושזה לוקח כדקה.
  אם כבר מחובר סקיל ניסוח (ראה "סקילים מחוברים"), תשאל קודם אם להחליף אותו, ורק אחרי שאישר תחזיר replace:true. אם הסקיל המחובר הוא כזה שטימי יצר בשיחה הזאת ועכשיו מבקשים לתקן בו משהו ("פחות רשמי", "בלי אימוג'י"), זה אישור: replace:true עם התיקון ב-answers.
- אתה כן יכול ליצור סקילים חדשים (עיצוב PDF וניסוח) ולהשתמש בסקיל PDF קיים כמו שהוא. אף פעם אל תגיד שאתה לא יכול ליצור סקיל.
- לכל מטרה (pdf / writing) מחובר סקיל אחד בלבד: חיבור חדש מחליף את הקודם, וכל ה-PDF-ים (או כל הניסוח) עוברים לסקיל החדש. כשמחליפים, תגיד את זה בפשטות ("החלפתי את X ב-Y"). אל תמציא חלוקה כמו "זה למדריכים וזה לשאר".
- {{"type":"unlink_skill","purpose":"pdf" או "writing"}} לנתק סקיל מחובר. מה קורה אחרי ניתוק של סקיל ה-PDF תלוי במצב (ראה "סקילים מחוברים"):
  אם כתוב שהסקיל מייצר את ה-PDF בעצמו: מעכשיו ה-PDF-ים חוזרים לצאת בעיצוב של טימי (העיצוב שהיה לפני החיבור). תגיד את זה בפשטות.
  אחרת (טימי רק עיצב את התבנית שלו לפי הסקיל): העיצוב שכבר נעשה נשאר, ורק הבקשות הבאות לא ייעשו לפי הסקיל. מי שרוצה לחזור לעיצוב המקורי לוחץ "חזור לעיצוב המקורי" בחלון עיצוב ה-PDF.
- תמונות ב-PDF של שיחה (צילומי מסך, תמונות מסרטון) עוד לא זמינות בטימי לווינדוס. אם מבקשים, אל תחזיר פעולה, ותגיד בעדינות שזה יגיע בעדכון הבא, ושבינתיים אפשר לצרף את הלוגו או לשנות את עיצוב ה-PDF.
- {{"type":"share_hiding","on":true/false}} להסתיר את טימי (הבועה וכל החלונות) כשמשתפים מסך, למשל בגוגל מיט. עכשיו: {"מוסתר בשיתוף מסך" if hide else "רואים אותו בשיתוף מסך"}. כשמוסתר, טימי גם לא מופיע בצילומי מסך. "תראה את טימי בשיתוף מסך" / "אני רוצה להדגים את טימי" = on:false."""
        extra = "" if self.design else self.skills_block(t) + self.attachments_block()
        return f"""אתה טימי, אפליקציה למחשב (ווינדוס) שמקליטה שיחות, רושמת נקודות בזמן אמת וכותבת סיכום. {owner or "המשתמש"} כותב לך בצ'אט מה לשנות בך.
החזר JSON בלבד בשורה אחת: {{"reply": "...", "actions": [...]}}
reply: עברית בלבד (בלי מילים באנגלית, חוץ משמות של סקילים, קבצים או מוצרים), גוף ראשון בתור טימי, משפט או שניים, חם וטבעי. אומר בדיוק מה שינית, או שואל שאלת הבהרה אם לא ברור מה לעשות. בלי מקף ארוך.
עברית תקינה של דובר ילידי: עתיד בגוף ראשון הוא "אכתוב", "אלמד", "אצור", "אבנה" (לא "אני מכתוב", "אני ילמד", "שייצור"). בלי קודי צבע או מונחים טכניים ב-reply.
כינוי חייב להתאים במין ובמספר למה שהוא מחליף: על הודעות או דוגמאות (רבות, נקבה) "מהן", "אותן", "מכולן"; על קבצים או מסמכים "מהם"; על הודעה אחת "ממנה"; על קובץ אחד "ממנו". כשהכינוי מתייחס להודעות שהוא שולח, זה "מהן" ולא "ממנה". גם מספרים מתאימים במין: "שלושה דברים", "שלוש שאלות".
actions: רק מה שהמשתמש ביקש. אם לא ברור גם אחרי שקראת את השיחה, רשימה ריקה ושאלה ב-reply.

הכי חשוב: זו שיחה רציפה, לא הודעות בודדות. תבין את ההודעה החדשה לפי כל השיחה שלמטה.
הודעות קצרות מתייחסות למה שנאמר קודם: "כן" / "יאללה" = לעשות את מה שהצעת או שאלת. "עוד יותר" / "פחות" = אותו שינוי בדיוק, בעוצמה גדולה או קטנה יותר.
"תחזיר" / "תחזיר את מה שהיה" / "בעצם לא" = לבטל את השינוי האחרון: קח את הערך מ"המראה לפני" שמופיע בסוגריים בהודעות של טימי, ותחזיר בדיוק אותו.
"גם שם" / "גם את X" = אותו שינוי על יעד נוסף. "את זה" / "אותו דבר" = הדבר האחרון שדיברנו עליו. אבל אם צורפו קבצים להודעה החדשה, "את זה" / "אותן" = הקבצים האלה, כולם.
תהפוך את ההודעה לפעולות מדויקות עם ערכים מלאים (קוד צבע, טקסט הוראה מלא), ואל תשאל שאלה שהתשובה עליה כבר כתובה בשיחה.

פעולות:
- {{"type":"appearance","changes":{{...}}}} מפתחות: accent_color (#RRGGBB, כפתורים והדגשות), eye_color (#RRGGBB, העיניים והפה; "" = כמו צבע ההדגשה), background_color (#RRGGBB, כל צבע מותר, גם לבן או בהיר), animations (true/false), reset (true = המראה המקורי)
  ניגודיות (טימי עושה את זה לבד, אתה רק מספר בקצרה כשזה רלוונטי): על רקע בהיר הטקסט נהיה כהה ("הטקסט יהיה כהה כדי שיהיה קריא"). צבע הדגשה שלא רואים על הרקע (שחור על שחור, לבן על לבן) מוצג קצת בהיר או כהה יותר. העיניים תמיד על הבועה השחורה, אז עיניים כהות מוצגות קצת בהירות יותר. על כפתור בצבע ההדגשה הטקסט שחור או לבן, מה שקריא. אף פעם אל תגיד שרקע בהיר אסור או לא אפשרי.
  צבע בלי יעד ("תשנה ללבן", "ירוק", "אני רוצה בכחול"): אל תנחש צבע הדגשה. אם ההודעה הקודמת בשיחה שינתה או דיברה על יעד מסוים (עיניים / רקע / הדגשה), זה אותו יעד. אם אין הקשר ברור, actions ריק ושאלה אחת קצרה: "ללבן את הרקע, את העיניים או את צבע ההדגשה?".
  הודעה שמציינת רק יעד ("את הרקע", "התכוונתי לעיניים") אחרי שאלה שלך = התשובה לשאלה: אותו צבע על היעד הזה. אחרי שינוי שכבר עשית = תיקון: אותו צבע על היעד שהוא אמר, והיעד שטעית בו חוזר לערך שלו מ"המראה לפני".
{scope}
- {{"type":"remember","text":"..."}} עובדה קבועה או העדפה של המשתמש ששווה לזכור לכל הצ'אטים הבאים (שם העסק, תחום, טון כתיבה מועדף, צבע מועדף, מה הוא לא אוהב). משפט קצר, בלי המילה "המשתמש", למשל: "העסק נקרא סטודיו דנה" או "אוהב טון קליל וישיר". תעשה את זה ביוזמתך כשמשהו כזה עולה, אבל לא על כל הודעה, לא על בקשה חד פעמית, ולא על מה שכבר כתוב בזיכרון.
  הזיכרון משמש את הצ'אטים, את עיצוב ה-PDF, את הניסוח של הודעות הוואטסאפ ואת הטקסט שבתוך ה-PDF ללקוח. הוא לא משנה את הסיכומים או את הנקודות: אם העדפה צריכה לשנות גם אותם, תוסיף גם add_instruction, ואל תבטיח שינוי בסיכומים בלי זה.
  "תזכור ש..." / "תשכח ש..." = תמיד remember או forget (גם אם יש בהודעה צבע: אפשר גם appearance, אבל רק אם ביקש לשנות עכשיו).
- {{"type":"forget","text":"<הטקסט מהזיכרון>"}} כשהמשתמש אומר שמשהו בזיכרון כבר לא נכון, או מבקש לשכוח.
המראה עכשיו: {looks.look_state()}
ההוראות שאני זוכר:
{mem}
מה אני זוכר על המשתמש:
{memory.block()}
{extra}
השיחה עד עכשיו (הישנה למעלה, האחרונה למטה). מה שבסוגריים מרובעים [...] זה הערה פנימית בשבילך, אל תעתיק אותה ל-reply:
{hist}
ההודעה החדשה של המשתמש: "{t}\""""

    # ---- Claude ---------------------------------------------------------------------------------------------------
    def ask_claude(self, t: str):
        self.set_busy(BUSY)
        p = self.prompt(t)

        def work():
            r = procrun.run_claude(["-p", "--model", "haiku", "--tools", "", "--strict-mcp-config", "--no-session-persistence"], p, timeout=70)
            return r, procrun.last_json_object(r.out, "reply")
        self.spawn(work, lambda res: self._answered(t, *res))

    @staticmethod
    def drop_open_window_hint(s: str) -> str:
        """Removes sentences that ask the user to open the PDF design window (it opens by itself after linking a skill)."""
        opens = ("תפתח", "לפתוח", "פתח את", "תיכנס", "להיכנס", "היכנס", "תלחץ", "ללחוץ", "תעבור ל", "לעבור ל", "תציץ", "להציץ")
        out = []
        for line in s.split("\n"):
            sens = re.findall(r"[^.!?]+[.!?]*\s*", line)
            out.append("".join(x for x in sens if not (any(o in x for o in opens) and ("חלון" in x or "עיצוב ה-PDF" in x))))
        return "\n".join(out).strip()

    def _answered(self, t: str, r: procrun.ProcResult, o):
        self.busy = None
        if o is None:
            self.say("לא הספקתי לחשוב על זה בזמן. נסה שוב בעוד רגע." if r.timed_out else
                     ("לא הבנתי מה לשנות במראה. נסה למשל: רקע כחול כהה, או עיניים ירוקות." if self.design else
                      "לא הצלחתי לחשוב על זה עכשיו. בדוק ש-Claude מחובר ונסה שוב."), error=True)
            self.next()
            return
        lk = T.current()
        before = looks.look_state(lk)
        looks_before = {"accent_color": lk.accent_hex, "eye_color": lk.eye_hex, "background_color": lk.bg_hex}
        actions = [a for a in (o.get("actions") or []) if isinstance(a, dict)] if isinstance(o.get("actions"), list) else []
        raw = o.get("reply") if isinstance(o.get("reply"), str) else ""
        if not self.design and any(a.get("type") == "link_skill" and str(a.get("purpose") or "").lower() == "pdf" for a in actions):
            cleaned = self.drop_open_window_hint(raw)
            if cleaned:
                raw = cleaned
        res = self.apply(actions, t, raw)
        reply = re.sub(r"\s*\[(המראה לפני|סקיל)[^\]]*\]", "", raw.replace(LONG_DASH, "-")).strip()
        if res["honest"]:
            reply = res["honest"]
        elif res["image_reply"]:
            reply = res["image_reply"]
        if not reply:
            reply = "לא הבנתי מה לשנות במראה. נסה למשל: רקע כחול כהה." if self.design else "לא בטוח שהבנתי. תוכל לנסח אחרת?"
        if (not self.design and not res["linked"] and not any(a.get("type") == "link_skill" for a in actions)
                and any(("סקיל" in x or "skill" in x.lower()) for x in [t] + [m.text for m in self.msgs[-4:] if m.from_user])
                and any(w in reply for w in ("נתיב", "שם המדויק", "השם המדויק", "איך הוא נקרא", "איך קוראים לו"))):
            sug = skills.suggest_skills(" ".join([t] + [m.text for m in self.msgs[-4:] if m.from_user]))
            if sug and not any(s.name in reply for s in sug):
                reply = "יש לי במחשב כמה סקילים שאולי מתאימים: " + ", ".join(s.name for s in sug) + ". איזה מהם לחבר? אפשר לכתוב רק חלק מהשם."
        for p, s in res["linked"]:
            if s.name not in reply:
                reply += f"\n\nחיברתי את הסקיל {s.name} ל{skills.purpose_name(p)}."
        if res["note"]:
            reply += "\n\n" + res["note"]
        if res["looks"]:
            changed = {k: v for k, v in res["looks"].items() if not isinstance(v, str) or v.upper() != str(looks_before.get(k, "")).upper()}
            n = looks.contrast_note(changed) if changed else None
            if n and not any(w in reply for w in ("קריא", "כהה יותר", "בהיר יותר", "בהירות יותר")):
                reply += " " + n
        ctx = ". ".join(x for x in (self.look_ctx(before), res["ctx"]) if x)
        self.say(reply, ctx=ctx or None)
        if res["writing"] is not None:
            self.start_writing_skill(res["writing"], res["replacing"])
        elif res["restyle"]:
            self.restyle_last()
        else:
            self.next()

    def apply(self, actions: list[dict], user_text: str, reply_text: str = "") -> dict:
        lks, changed, restyle, pdf_text, pdf_logo = {}, False, False, None, None
        learned, forgot, linked, unlinked, problems = [], [], [], [], []
        catalog = None
        writing, replacing, make_pdf_skill, make_pdf_asked, share_note, image_reply = None, None, None, False, None, None
        cfg = tconfig.load()
        hide_now = True if cfg.get("hide_in_screen_share") is None else bool(cfg.get("hide_in_screen_share"))
        for a in actions:
            typ = a.get("type") if isinstance(a.get("type"), str) else ""
            text = (a.get("text") if isinstance(a.get("text"), str) else "").strip()
            if typ == "appearance":
                lks.update(looks.valid_looks(a.get("changes") or {}))
            elif typ == "add_instruction" and not self.design and text:
                if text not in self.items:
                    self.items.append(text)
                    changed = True
            elif typ == "remove_instruction" and not self.design and text:
                i = next((i for i, x in enumerate(self.items) if x == text), None)
                if i is None:
                    i = next((i for i, x in enumerate(self.items) if text.casefold() in x.casefold() or x.casefold() in text.casefold()), None)
                if i is not None:
                    self.items.pop(i)
                    changed = True
            elif typ == "pdf_instruction" and not self.design and text:
                pdf_text = text
                ln = (a.get("logo") if isinstance(a.get("logo"), str) else "").strip()
                pool = self.latest_image_batch + [f for f in self.attached_files if is_image(f)]
                hit = next((f for f in pool if ln and (os.path.basename(f) == ln or ln.casefold() in os.path.basename(f).casefold())), None)
                if hit:
                    pdf_logo = hit
                elif "לוגו" in text or "לוגו" in user_text:
                    pdf_logo = next((f for f in self.current_files if is_image(f)), None)
            elif typ == "apply_to_last_call" and not self.design:
                restyle = looks.asks_last_call(user_text) or any(looks.asks_last_call(m.text) for m in self.msgs[-4:])
            elif typ == "remember" and text:
                if len(learned) < 3:
                    learned += memory.remember([text])
            elif typ == "forget" and text:
                g = memory.forget(text)
                if g:
                    forgot.append(g)
            elif typ == "link_skill" and not self.design:
                purpose = str(a.get("purpose") or "").lower()
                q = str(a.get("skill") or a.get("name") or a.get("path") or "").strip()
                if purpose not in dict(skills.PURPOSES) or not q:
                    continue
                if catalog is None:
                    catalog = skills.list_claude_skills()
                found = skills.find_skills(q, catalog)
                if len(found) == 1:
                    skills.set_linked_skill(purpose, found[0])
                    linked.append((purpose, found[0]))
                elif not found:
                    problems.append(f"לא מצאתי במחשב סקיל בשם {q}. תכתוב לי את השם המדויק שלו, או את הנתיב לתיקייה שלו (זו שיש בה SKILL.md).")
                else:
                    problems.append(f"יש כמה סקילים שמתאימים ל-{q}: " + ", ".join(s.name for s in found[:4]) + f". איזה מהם לחבר ל{skills.purpose_name(purpose)}?")
            elif typ == "share_hiding" and not self.design:
                on = a.get("on")
                if isinstance(on, bool) and on != hide_now:
                    self.set_share_hiding(on)
                    hide_now = on
                    share_note = "מעכשיו מי שצופה בשיתוף המסך שלך לא יראה את טימי." if on else "מעכשיו רואים את טימי בשיתוף מסך."
            elif typ == "unlink_skill" and not self.design:
                purpose = str(a.get("purpose") or "").lower()
                if skills.linked_skill(purpose):
                    skills.set_linked_skill(purpose, None)
                    unlinked.append(purpose)
            elif typ == "create_pdf_skill" and not self.design:
                name = (a.get("file") if isinstance(a.get("file"), str) else "").strip()
                pdfs = [f for f in self.attached_files if f.lower().endswith(".pdf")]
                recent = {f for m in self.msgs[-4:] for f in (m.files or [])}
                pick = None
                if name:
                    pick = next((f for f in reversed(pdfs) if os.path.basename(f) == name or name.casefold() in os.path.basename(f).casefold()), None)
                if pick is None:
                    pick = next((f for f in reversed(pdfs) if f in recent), None)
                if pick is None and name and pdfs:
                    pick = pdfs[-1]
                make_pdf_skill, make_pdf_asked = pick, True
            elif typ == "create_writing_skill" and not self.design:
                names = [x for x in (a.get("files") or []) if isinstance(x, str)]
                docs = [f for f in self.attached_files if not is_image(f)]
                files = docs[-8:] if not names else [f for f in docs if any(os.path.basename(f) == n or n.casefold() in os.path.basename(f).casefold() for n in names)]
                if not files and names:
                    files = docs[-8:]
                pasted = []
                if a.get("use_pasted") is not False:
                    pasted = skills.pasted_writing_samples(self.msgs[-30:], [x for x in (a.get("samples") or []) if isinstance(x, str)])
                src = skills.WritingSources(files, pasted, a.get("use_calls") is True, str(a.get("answers") or "").strip(),
                                            history.chat_history(self.msgs[-24:]))
                w = skills.linked_skill("writing")
                if w:
                    if a.get("replace") is not True:
                        problems.append(f"כבר מחובר אצלי סקיל ניסוח ({LRI}{w[0]}{PDI}). להחליף אותו בסקיל חדש בסגנון שלך?")
                        continue
                    if skills.is_timmy_made(w[1]):
                        replacing = w[1]
                        src.previous = skills.skill_markdown(w[1], max_total=12000)
                writing = src
            elif typ in ("add_images", "image_edit", "video_frames") and not self.design:
                image_reply = "תמונות ב-PDF של שיחה עוד לא זמינות בטימי לווינדוס. זה יגיע בעדכון הבא."
        if pdf_text is not None and not any(p == "pdf" for p, _ in linked) and image_reply is None \
                and re.search(r"(הוספתי|שמתי|שיניתי|עדכנתי|הכנסתי|החלפתי|צבעתי|הגדלתי|הקטנתי)", reply_text):
            image_reply = "מעביר את זה לחלון \"עיצוב ה-PDF\"" + (" יחד עם הלוגו שצירפת" if pdf_logo else "") + ". שם תראה בעוד רגע איך המסמך נראה."
        if lks:
            T.apply(lks)
        if changed:
            self.save_items()
        if learned or forgot:
            self.memory = memory.load()
        if linked or unlinked:
            self.load_skills()
        ctx_extra = []
        pdf_link = next((s for p, s in reversed(linked) if p == "pdf"), None)
        if pdf_link is not None:
            self.open_pdf_design(adopt=pdf_link, then=pdf_text)
        elif pdf_text is not None:
            self.open_pdf_design(send=pdf_text, logo=pdf_logo)
        if make_pdf_asked:
            self.open_pdf_design(create_from=make_pdf_skill, pick=make_pdf_skill is None)
        note = []
        if learned:
            note.append("זכרתי: " + " · ".join(learned))
        if forgot:
            note.append("שכחתי: " + " · ".join(forgot))
        if unlinked and "ניתקתי" not in reply_text:
            note.append("ניתקתי את הסקיל של " + " ושל ".join(skills.purpose_name(p) for p in unlinked) + ".")
        if pdf_link is not None and not any(w in reply_text for w in ("חלון", "נפתח", "ייפתח", "פתחתי")):
            note.append("פתחתי את \"עיצוב ה-PDF\": אני בודק שם איך הסקיל מייצר PDF, ותראה שם איך סיכום שיחה נראה בו.")
        if share_note and not any(w in reply_text for w in ("שיתוף", "שתף", "גלוי", "מוסתר", "להסתיר", "יראו", "רואים", "יראה", "יופיע", "מופיע")):
            note.append(share_note)
        if make_pdf_asked:
            ctx_extra.append("סקיל: התחלתי ליצור סקיל עיצוב מ-" + (os.path.basename(make_pdf_skill) if make_pdf_skill else "PDF שהמשתמש יבחר")
                             + " בחלון עיצוב ה-PDF")
        ctx = [f"סקיל: חיברתי את {s.name} ל{skills.purpose_name(p)}" for p, s in linked] + ctx_extra
        ctx += [f"סקיל: ניתקתי את הסקיל של {skills.purpose_name(p)}" for p in unlinked]
        honest = None if not problems else ("\n".join(problems) if not linked else None)
        if not linked and problems:
            ctx.append("סקיל: לא חיברתי, " + " ".join(problems))
        if linked and problems:
            note += problems
        return {"restyle": restyle, "note": "\n".join(note), "honest": honest, "ctx": ". ".join(ctx) if ctx else None, "linked": linked,
                "looks": lks or None, "writing": writing, "replacing": replacing, "image_reply": image_reply if not problems else None}

    # ---- long jobs ------------------------------------------------------------------------------------------------
    def start_writing_skill(self, src, replacing):
        token = procrun.CancelToken()
        self.busy = "קורא את הדוגמאות שלך…"

        def cancel():
            token.cancel()
            self.set_busy("מבטל…")
        self.cancel_action = cancel
        self.notify()

        def progress(m):
            if not token.cancelled:
                self.busy = m
                self.notify()

        def done(res):
            self.cancel_action, self.busy = None, None
            if res.kind == "cancelled":
                self.say("ביטלתי. לא נוצר סקיל, והניסוח נשאר כמו שהיה.", ctx="סקיל: יצירת סקיל הניסוח בוטלה")
            elif res.kind == "failed":
                self.say(res.text, error=True, ctx="סקיל: לא יצרתי סקיל ניסוח")
            else:
                skills.set_linked_skill("writing", res.info)
                self.load_skills()
                text = (res.text + "\n\n" if res.text else "") + (
                    f"שמרתי את זה כסקיל ניסוח בשם {LRI}{res.info.name}{PDI} (גם Claude Code יכול להשתמש בו) וחיברתי אותו. "
                    "מעכשיו הודעות הוואטסאפ והטקסט ב-PDF ללקוח ינוסחו בקול שלך.\nאם משהו לא מרגיש כמוך, תגיד לי מה, ואכתוב אותו מחדש.")
                self.say(text, ctx=f"סקיל: יצרתי סקיל ניסוח {res.info.name} וחיברתי אותו לניסוח" + (" (הקודם עבר לתיקיית האשפה של טימי)" if replacing else ""))
            self.next()
        self.spawn(lambda: skills.create_writing_skill(src, replacing, token, progress), done)

    def restyle_last(self):
        if not self.items:
            self.say("עוד אין לי הוראות להחיל.")
            return self.next()
        md = last_summary_file()
        if not md:
            self.say("עוד אין שיחות שמורות, אז אין על מה להחיל.")
            return self.next()
        self.set_busy("מחיל על השיחה האחרונה…")

        def work():
            r = procrun.run_process([skills.child_python(), "-m", "timmy.engine.worker", "--restyle", md], None, timeout=200, env=skills.child_env())
            return r

        def done(r):
            from timmy.ui import calls as C
            self.busy = None
            if '"changed": true' in r.out:
                self.say("סידרתי גם את: " + C.call_title(md) + " ✓")
                self.reveal(md)
            elif '"changed": false' in r.out:
                self.say("לא הצלחתי לשכתב את הסיכום לפי ההוראות בלי לשבור את המבנה. נסה לנסח אחרת.", error=True)
            else:
                self.say("לא הצלחתי להריץ את השיפור. בדוק ש-Claude מחובר.", error=True)
            self.next()
        self.spawn(work, done)
