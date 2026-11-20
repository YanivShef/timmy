"""The "עיצוב ה-PDF" chat: changes the owner's template (<app dir>/pdf/template.html + brand.json) through the vendored
pdf.py --design / --undo / --reset, shows page 1 of a preview after every change, attaches the logo, uses a PDF skill
that already exists (pdf.py --skill-setup) and creates a new design skill from a sample PDF. Port of Recorder.swift
PDFDesignModel (same texts, same history file <app dir>/pdf/chat.json, same queue rules).

No Qt here: build_preview(md, dest, local, token, done(ok, note)) and page_png(pdf, png) -> bool come from the UI
(ui/pdf_render.py); slow work goes through spawn(fn, done).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid

from timmy import paths
from timmy.chat import history, memory, skills
from timmy.chat.history import ChatMsg
from timmy.engine import procrun

LRI, PDI = chr(0x2066), chr(0x2069)
INTRO = ("אני מעצב את ה-PDF שאתה שולח ללקוחות. תכתוב לי מה לשנות, במילים שלך. למשל:\n• שם העסק: סטודיו דנה\n"
         "• תשים את הלוגו שלי למעלה (צרף אותו עם 📎)\n• יותר מינימליסטי, בכחול\n• בתחתית: 050-1234567 · studio.co.il\n• המשימות בטבלה")
LOGO_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def pdf_dir() -> str:
    return os.path.join(paths.app_dir(), "pdf")


def _sync_spawn(fn, done):
    done(fn())


def sample_md() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine", "_mac", "pdf_sample.md")


class PDFDesign:
    def __init__(self, notify=None, spawn=None, build_preview=None, page_png=None):
        self.notify = notify or (lambda: None)
        self.spawn = spawn or _sync_spawn
        self.build_preview = build_preview
        self.page_png = page_png
        self.msgs: list[ChatMsg] = []
        self.pending_logo: str | None = None
        self.busy: str | None = None
        self.skill_name: str | None = None
        self.skill_runs = False
        self.cancel_action = None
        self.candidates: list = []
        self.queue: list[tuple] = []
        self.loaded = False

    @property
    def chat_file(self) -> str:
        return os.path.join(pdf_dir(), "chat.json")

    @property
    def previews(self) -> str:
        return os.path.join(pdf_dir(), "previews")

    def set_busy(self, b):
        self.busy = b
        self.notify()

    def refresh_skills(self):
        lk = skills.linked_skill("pdf")
        self.skill_name = lk[0] if lk else None
        self.skill_runs = skills.skill_runs_pdf()
        self.candidates = skills.pdf_skill_candidates()

    def load(self, quiet: bool = False):
        self.refresh_skills()
        if not self.loaded:
            self.loaded = True
            self.msgs = history.load(self.chat_file)
            if not self.msgs:
                self.msgs = [ChatMsg(False, INTRO)]
                if not quiet:
                    self.preview("ככה ה-PDF נראה עכשיו:", local=True)
        self.notify()

    def save(self):
        history.save(self.chat_file, self.msgs, 80)

    def add(self, m: ChatMsg):
        self.msgs.append(m)
        self.save()
        self.notify()

    # ---- logo -----------------------------------------------------------------------------------------------------
    def copy_logo(self, src: str) -> str | None:
        """The logo is copied to <app dir>/brand so it stays even if the original is deleted."""
        d = os.path.join(paths.app_dir(), "brand")
        try:
            os.makedirs(d, exist_ok=True)
            dest = os.path.join(d, f"logo-{int(time.time())}{os.path.splitext(src)[1].lower()}")
            shutil.copyfile(src, dest)
            return dest
        except OSError:
            return None

    def attach_logo(self, src: str):
        self.pending_logo = self.copy_logo(src)
        self.notify()

    def handle_drop(self, files: list[str]):
        pdf = next((f for f in files if f.lower().endswith(".pdf")), None)
        if pdf:
            return self.create_skill(pdf)
        img = next((f for f in files if f.lower().endswith(LOGO_EXT)), None)
        if img:
            return self.attach_logo(img)
        if files:
            self.add(ChatMsg(False, "אפשר לגרור לכאן PDF (ואני אבנה ממנו סקיל עיצוב) או תמונה של הלוגו.", is_error=True))

    # ---- design requests ------------------------------------------------------------------------------------------
    def send(self, text: str | None = None, logo: str | None = None, silent: bool = False, typed: str = ""):
        t = (typed if text is None else text).strip()
        lg = logo or (self.pending_logo if text is None else None)
        if not t and not lg:
            return
        if text is None:
            self.pending_logo = None
        if self.busy is not None:
            self.queue.append((("__silent__" if silent else "") + t, lg))
            self.notify()
            return
        if not t:
            t = "הנה הלוגו שלי, תשים אותו בראש המסמך"
        hist = self.write_history(self.msgs)
        if not silent:
            self.add(ChatMsg(True, t, image=lg))
        args = ["--design", t]
        if lg:
            args += ["--logo", lg]
        if hist:
            args += ["--history", hist]
        self.run_pdf_py(args, "מעצב…", cleanup=hist)

    def write_history(self, lst: list[ChatMsg]) -> str | None:
        items = []
        for m in lst[-20:]:
            t = m.text.strip()
            if not t:
                continue
            if len(t) > 600:
                t = t[:600] + "…"
            if m.from_user and m.image:
                t += " (צורף לוגו)"
            items.append({"role": "user" if m.from_user else ("timmy_error" if m.is_error else "timmy"), "text": t})
        if not items:
            return None
        p = os.path.join(tempfile.gettempdir(), f"timmy-pdf-history-{uuid.uuid4().hex}.json")
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
            return p
        except OSError:
            return None

    def undo(self):
        if self.busy is None:
            self.add(ChatMsg(True, "בטל שינוי אחרון"))
            self.run_pdf_py(["--undo"], "מבטל…")

    def reset(self):
        if self.busy is None:
            self.add(ChatMsg(True, "חזור לעיצוב המקורי"))
            self.run_pdf_py(["--reset"], "מחזיר…")

    def run_pdf_py(self, args: list[str], busy_text: str, cleanup: str | None = None):
        self.set_busy(busy_text)

        def work():
            skills.linked_skill("pdf")          # a skill path that moved is healed before pdf.py reads config.json
            r = procrun.run_process([skills.child_python(), "-m", "timmy.engine.pdf"] + args, None, timeout=200, env=skills.child_env())
            if cleanup:
                try:
                    os.remove(cleanup)
                except OSError:
                    pass
            o = procrun.last_json_object(r.out, "ok") or {"ok": False, "error": "לא קיבלתי תשובה בזמן" if r.timed_out else "משהו השתבש"}
            learned = memory.remember([x for x in (o.get("remember") or []) if isinstance(x, str)][:3])
            for f in [x for x in (o.get("forget") or []) if isinstance(x, str)][:3]:
                memory.forget(f)
            return o, learned

        def done(res):
            o, learned = res
            summary = o.get("summary") if isinstance(o.get("summary"), str) else ""
            if learned:
                summary += ("\n\n" if summary else "") + "זכרתי: " + " · ".join(learned)
            if o.get("ok") is not True:
                self.add(ChatMsg(False, str(o.get("error") or "לא הצלחתי"), is_error=True))
                self.busy = None
                self.next()
            elif o.get("changed") is True:
                self.preview(summary)
            else:
                self.add(ChatMsg(False, summary or "לא שיניתי כלום"))
                self.busy = None
                self.next()
        self.spawn(work, done)

    def next(self):
        if not self.queue:
            self.notify()
            return
        t, lg = self.queue.pop(0)
        if t.startswith("__adopt__"):
            parts = t[9:].split("\x01")
            extra = parts[1] if len(parts) > 1 and parts[1] else None
            sk = skills.read_skill(parts[0], "")
            if sk:
                self.adopt_skill(sk, then=extra)
            else:
                self.next()
        elif t.startswith("__create__"):
            self.create_skill(t[10:])
        elif t.startswith("__silent__"):
            self.send(t[10:], logo=lg, silent=True)
        else:
            self.send(t, logo=lg)

    # ---- skills ---------------------------------------------------------------------------------------------------
    def create_skill(self, pdf: str):
        if self.busy is not None:
            self.queue.append(("__create__" + pdf, None))
            self.notify()
            return
        os.makedirs(self.previews, exist_ok=True)
        thumb = os.path.join(self.previews, f"source-{int(time.time() * 1000)}.png")
        has_thumb = bool(self.page_png and self.page_png(pdf, thumb, 300))
        self.add(ChatMsg(True, f"תבנה סקיל עיצוב מהקובץ {LRI}{os.path.basename(pdf)}{PDI}", image=thumb if has_thumb else None))
        token = procrun.CancelToken()
        self.busy = "קורא את ה-PDF…"

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
                self.add(ChatMsg(False, "ביטלתי. לא נוצר סקיל ולא שיניתי כלום."))
                self.next()
            elif res.kind == "failed":
                self.add(ChatMsg(False, res.text, is_error=True))
                self.next()
            else:
                learned = res.text + "\n\n" if res.text else ""
                self.add(ChatMsg(False, learned + f"שמרתי את זה כסקיל חדש של Claude בשם {LRI}{res.info.name}{PDI}, כך שגם Claude Code יכול "
                                                  "לעצב לפיו. חיברתי אותו לטימי."))
                skills.set_linked_skill("pdf", res.info)
                self.adopt_skill(res.info, created=True)
        self.spawn(lambda: skills.create_pdf_skill(pdf, token, progress), done)

    def link_and_adopt(self, s):
        """"יש לי כבר סקיל PDF": the menu item of a skill."""
        if s.name != self.skill_name:
            skills.set_linked_skill("pdf", s)
            self.skill_name = s.name
            self.adopt_skill(s)

    def adopt_skill(self, s, created: bool = False, then: str | None = None):
        """A linked PDF skill: with a script of its own, the client PDFs come out of it; otherwise Timmy's template is
        designed by its instructions."""
        if self.busy is not None:
            self.queue.append(("__adopt__" + s.dir + ("\x01" + then if then else ""), None))
            self.notify()
            return
        self.busy = f"בודק איך הסקיל {s.name} מייצר PDF…"
        token = procrun.CancelToken()

        def cancel():
            token.cancel()
            self.set_busy("מבטל…")
        self.cancel_action = cancel
        self.notify()

        def work():
            skills.linked_skill("pdf")
            r = procrun.run_process([skills.child_python(), "-m", "timmy.engine.pdf", "--skill-setup"], None, timeout=200, token=token,
                                    env=skills.child_env())
            return r, procrun.last_json_object(r.out, "ok") or {}

        def done(res):
            r, o = res
            self.busy, self.cancel_action = None, None
            if r.cancelled or token.cancelled:
                lk = skills.linked_skill("pdf")
                if lk and os.path.normpath(lk[1]) == os.path.normpath(s.dir):
                    skills.set_linked_skill("pdf", None)
                self.refresh_skills()
                self.add(ChatMsg(False, f"ביטלתי. לא חיברתי את הסקיל {LRI}{s.name}{PDI}, וה-PDF נשאר כמו שהיה."
                                 + (" הסקיל עצמו שמור בקלוד קוד, ואפשר לחבר אותו מתי שתרצה." if created else "")))
                return self.next()
            self.skill_runs = skills.skill_runs_pdf()
            if o.get("runner") is True:
                cap = ("מעכשיו כל ה-PDF-ים שלי יוצאים בעיצוב הזה. ככה סיכום שיחה נראה בו:" if created else
                       f"מעכשיו כל ה-PDF-ים יוצאים מהסקיל {LRI}{s.name}{PDI} עצמו, בדיוק כמו כשהוא רץ בקלוד קוד. ככה סיכום שיחה נראה:")
                if then:
                    self.queue.append((then, None))
                self.preview(cap)
                return
            why = o.get("reason") if isinstance(o.get("reason"), str) else ""
            if o.get("ok") is not True and r.timed_out:
                self.add(ChatMsg(False, "לקח לי יותר מדי זמן לבדוק את הסקיל, אז אני מעצב את התבנית שלי לפי ההוראות שלו."))
            elif why:
                self.add(ChatMsg(False, f"ניסיתי להריץ את הסקריפט של הסקיל {LRI}{s.name}{PDI} ולא הצלחתי ({why.split(':')[0]}), "
                                        "אז אני מעצב את התבנית שלי לפי ההוראות שלו."))
            elif not created:
                self.add(ChatMsg(False, f"לסקיל {LRI}{s.name}{PDI} אין סקריפט שאני יכול להריץ כדי לייצר PDF, אז אני מעצב את התבנית שלי לפי ההוראות שלו."))
            else:
                self.add(ChatMsg(False, f"אני מעצב עכשיו את ה-PDF לפי הסקיל החדש {LRI}{s.name}{PDI}."))
            self.send(f"תעצב את ה-PDF בדיוק כמו בסקיל {s.name}" + (". וגם: " + then if then else ""), silent=True)
        self.spawn(work, done)

    # ---- preview --------------------------------------------------------------------------------------------------
    def preview(self, caption: str, local: bool = False):
        self.busy = "מכין תצוגה מקדימה מהסקיל…" if self.skill_runs else "מכין תצוגה מקדימה…"
        token = procrun.CancelToken()
        if not local:
            def cancel():
                token.cancel()
                self.set_busy("מבטל…")
            self.cancel_action = cancel
        self.notify()
        from timmy.ui import calls as C
        lst = C.list_calls()
        md = lst[0].path if lst else (sample_md() if os.path.exists(sample_md()) else None)
        if md is None or self.build_preview is None:
            self.add(ChatMsg(False, caption))
            self.busy, self.cancel_action = None, None
            return self.next()
        os.makedirs(self.previews, exist_ok=True)
        stamp = int(time.time() * 1000)
        pdf = os.path.join(self.previews, f"preview-{stamp}.pdf")
        png = os.path.join(self.previews, f"preview-{stamp}.png")

        def built(ok, note=""):
            self.cancel_action = None
            if token.cancelled:
                self.add(ChatMsg(False, caption + "\n(עצרתי את התצוגה המקדימה. השינוי נשמר, ותראה אותו ב-PDF הבא)"))
                self.busy = None
                return self.next()
            cap = caption
            if note and note.startswith("לא הצלחתי להפעיל את הסקיל"):
                cap += "\n(" + note + ")"
            if ok and self.page_png and self.page_png(pdf, png, 900):
                self.add(ChatMsg(False, cap, image=png, pdf=pdf))
            else:
                self.add(ChatMsg(False, cap + "\n(לא הצלחתי להכין תצוגה מקדימה)"))
            self.busy = None
            self.next()
        self.build_preview(md, pdf, local, token, built)
