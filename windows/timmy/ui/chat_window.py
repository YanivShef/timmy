"""The chat windows: "שפר את טימי" / "עצב את טימי" (chat/improve.py) and "עיצוב ה-PDF" (chat/pdf_design.py).
Port of the SwiftUI TimmyChatView / PDFDesignView: Timmy's bubbles on the right (like the other side in a Hebrew
WhatsApp), the user's on the left in the accent color, a typing bubble with "בטל" for long jobs, suggestion chips,
the 📎 button, drag and drop of files, and (improve / design) the "מה אני זוכר" panel: the instructions, the memory
notes, the linked skills and "חזור למראה המקורי". Every window follows the look live (theme bus).
"""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontMetrics, QIcon, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from timmy.chat import memory, skills
from timmy.chat.improve import TimmyChat
from timmy.chat.pdf_design import PDFDesign
from timmy.ui import pickers, shell
from timmy.ui import theme as T
from timmy.ui.bridge import Bridge
from timmy.ui.calls_window import ASSETS, CREATOR_URL, he, image, label

ATTACH_FILTER = "קבצים (*.pdf *.docx *.doc *.odt *.rtf *.txt *.md *.html *.htm *.png *.jpg *.jpeg *.webp *.gif)"
LOGO_FILTER = "תמונות (*.png *.jpg *.jpeg *.gif *.webp *.svg)"
MAX_TEXT = 470          # the widest a bubble's text gets (px)


def wrap_label(text: str, css: str = "") -> QLabel:
    lb = QLabel(he(text))
    lb.setWordWrap(True)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lb.setTextFormat(Qt.TextFormat.PlainText)
    if css:
        lb.setStyleSheet(css)
    return lb


class ClickImage(QLabel):
    def __init__(self, path: str, target: str | None, width: int):
        super().__init__()
        pm = QPixmap(path)
        if not pm.isNull():
            pm = pm.scaledToWidth(width * 2, Qt.TransformationMode.SmoothTransformation)
            pm.setDevicePixelRatio(2.0)
            self.setPixmap(pm)
        self.target = target
        if target:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("פתח את ה-PDF")

    def mouseReleaseEvent(self, e):
        if self.target:
            shell.open_path(self.target)


class Bubble(QWidget):
    def __init__(self, msg, look: T.Look, max_text: int = MAX_TEXT):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        box = QFrame()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(10)
        if msg.from_user:
            fg, bg = look.css(look.on_accent), look.css(look.amber)
        else:
            fg = look.css(look.err_ink) if msg.is_error else look.css(look.ink, 0.92)
            bg = look.css(look.ink, 0.08)
        box.setStyleSheet(f"QFrame {{ background: {bg}; border-radius: 16px; }}")
        txt = wrap_label(msg.text, f"color: {fg}; font-size: 14px; background: transparent;")
        txt.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        # a wrapping QLabel asks for a narrow width: give it the width of its longest line, up to the bubble maximum
        f = txt.font()
        f.setPixelSize(14)
        fm = QFontMetrics(f)
        txt.setMinimumWidth(min(max_text, max((fm.horizontalAdvance(x) for x in (msg.text or " ").split("\n")), default=0) + 6))
        bl.addWidget(txt)
        if msg.image and os.path.exists(msg.image):
            bl.addWidget(ClickImage(msg.image, msg.pdf, 70 if msg.from_user else 200), 0, Qt.AlignmentFlag.AlignRight)
        box.setMaximumWidth(max_text + 28)
        if msg.from_user:
            row.addStretch(1)
            row.addWidget(box)
        else:
            row.addWidget(image("timmy.png", 28), 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(box)
            row.addStretch(1)
        self.text = msg.text


class Typing(QWidget):
    cancel = Signal()

    def __init__(self, text: str, look: T.Look, can_cancel: bool):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(image("timmy.png", 28), 0, Qt.AlignmentFlag.AlignTop)
        box = QFrame()
        box.setStyleSheet(f"QFrame {{ background: {look.css(look.ink, 0.06)}; border-radius: 16px; }}")
        bl = QHBoxLayout(box)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(8)
        lb = QLabel(he("● " + text))
        lb.setStyleSheet(f"color: {look.css(look.ink, 0.6)}; font-size: 13.5px; background: transparent;")
        bl.addWidget(lb)
        if can_cancel:
            b = QPushButton("בטל")
            b.setObjectName("small")
            b.setToolTip("לעצור באמצע. שום דבר לא יישמר")
            b.clicked.connect(self.cancel.emit)
            bl.addWidget(b)
            self.cancel_btn = b
        row.addWidget(box)
        row.addStretch(1)


class ChatScroll(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.inner.setObjectName("chatInner")
        self.inner.setStyleSheet("QWidget#chatInner { background: transparent; }")
        self.viewport().setAutoFillBackground(False)
        self.lay = QVBoxLayout(self.inner)
        self.lay.setContentsMargins(0, 6, 6, 6)
        self.lay.setSpacing(14)
        self.lay.addStretch(1)
        self.setWidget(self.inner)
        self._sig = None

    def show_msgs(self, msgs, busy, can_cancel, look, on_cancel):
        # the widest text that still leaves the avatar and a 90 px gap on the other side (the Mac Spacer(minLength: 90))
        max_text = max(200, min(MAX_TEXT, self.viewport().width() - 28 - 10 - 28 - 90 - 12))
        sig = (tuple(m.id for m in msgs), busy, can_cancel, look.accent_hex, look.bg_hex, max_text)
        if sig == self._sig:
            return
        self._sig = sig
        while self.lay.count() > 1:
            it = self.lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        for m in msgs:
            self.lay.insertWidget(self.lay.count() - 1, Bubble(m, look, max_text))
        if busy:
            t = Typing(busy, look, can_cancel)
            t.cancel.connect(on_cancel)
            self.lay.insertWidget(self.lay.count() - 1, t)
        self._args = (msgs, busy, can_cancel, look, on_cancel)
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "_args", None):
            self.show_msgs(*self._args)

    def texts(self) -> list[str]:
        out = []
        for i in range(self.lay.count()):
            w = self.lay.itemAt(i).widget()
            if isinstance(w, Bubble):
                out.append(w.text)
        return out


class ChatBase(QWidget):
    """Window frame shared by both chats: header, scroll, input row, creator badge, drag and drop, live restyle."""

    def __init__(self, title: str, subtitle: str, placeholder: str):
        super().__init__()
        self.setObjectName("timmyWindow")
        self.setWindowTitle(title + " (טימי)")
        self.setWindowIcon(QIcon(os.path.join(ASSETS, "timmy.png")))
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.look = T.current()
        self.bridge = Bridge(self)
        self.bridge.changed.connect(self.refresh)
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 28, 36, 18)
        root.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(image("timmy.png", 52))
        tl = QVBoxLayout()
        tl.setSpacing(3)
        tl.addWidget(label(title, "h1"))
        tl.addWidget(label(subtitle, "muted", wrap=True))
        self.chip_row = QHBoxLayout()
        self.chip_row.setSpacing(6)
        tl.addLayout(self.chip_row)
        head.addLayout(tl, 1)
        root.addLayout(head)
        self.top = QHBoxLayout()
        self.top.setSpacing(8)
        root.addLayout(self.top)
        body = QHBoxLayout()
        body.setSpacing(18)
        self.scroll = ChatScroll()
        body.addWidget(self.scroll, 1)
        self.side = QVBoxLayout()
        body.addLayout(self.side)
        root.addLayout(body, 1)
        self.sugg = QVBoxLayout()
        self.sugg.setSpacing(6)
        root.addLayout(self.sugg)
        self.files_row = QHBoxLayout()
        self.files_row.setSpacing(6)
        root.addLayout(self.files_row)
        inp = QHBoxLayout()
        inp.setSpacing(8)
        self.clip = QPushButton("📎")
        self.clip.setObjectName("icon")
        self.clip.setFixedSize(38, 38)
        inp.addWidget(self.clip)
        self.input = QLineEdit()
        self.input.setPlaceholderText(placeholder)
        self.input.returnPressed.connect(self.submit)
        inp.addWidget(self.input, 1)
        self.send_btn = QPushButton("↑")
        self.send_btn.setObjectName("primary")
        self.send_btn.setFixedSize(38, 38)
        self.send_btn.clicked.connect(self.submit)
        inp.addWidget(self.send_btn)
        root.addLayout(inp)
        badge = QPushButton()
        badge.setObjectName("ghost")
        badge.setIcon(QIcon(os.path.join(ASSETS, "creator.png")))
        badge.setIconSize(QSize(22, 22))
        badge.setText(he("נבנה על ידי ") + "\u200e@yaniv_shef")
        badge.setStyleSheet("border: none;")
        badge.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(CREATOR_URL)))
        root.addWidget(badge, 0, Qt.AlignmentFlag.AlignHCenter)
        self.resize(860, 780)
        self.setMinimumSize(640, 640)
        T.bus().changed.connect(self.restyle)
        memory.on_change(self.bridge.notify)
        skills.on_change(self.bridge.notify)

    def restyle(self, look: T.Look):
        self.look = look
        self.setStyleSheet(look.stylesheet())
        self.scroll._sig = None
        self.refresh()

    def showEvent(self, e):
        self.restyle(T.current())
        super().showEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.dropped(files)

    def _chip(self, text: str, fn, tip: str = "") -> QPushButton:
        b = QPushButton(he(text))
        b.setObjectName("small")
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(fn)
        return b

    @staticmethod
    def _clear(lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
            elif it.layout():
                ChatBase._clear(it.layout())

    # subclass hooks
    def submit(self):
        ...

    def dropped(self, files):
        ...

    def refresh(self):
        ...


class TimmyChatWindow(ChatBase):
    """"שפר את טימי" (design=False) or "עצב את טימי" (design=True)."""

    def __init__(self, design: bool, open_pdf_design=None, set_share_hiding=None):
        title = "עצב את טימי" if design else "שפר את טימי"
        sub = ("תגיד לי איך אני נראה, ואני משתנה מיד." if design else
               "תכתוב לי מה לשנות: סיכומים, נקודות, ה-PDF, הניסוח או המראה. אני זוכר הכל לשיחות הבאות.")
        super().__init__(title, sub, "למשל: רקע כחול כהה" if design else "למשל: תכתוב את הסיכום בגוף ראשון")
        self.design = design
        self.m = TimmyChat(design, notify=self.bridge.notify, spawn=self.bridge.spawn, open_pdf_design=open_pdf_design or (lambda **kw: None),
                           set_share_hiding=set_share_hiding, reveal=shell.reveal)
        self.clip.setVisible(not design)
        self.clip.setToolTip("צרף קבצים: דוגמאות של איך שאתה כותב, PDF שאתה אוהב את העיצוב שלו, או הלוגו")
        self.clip.clicked.connect(self.attach)
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        self.panel.setFixedWidth(260)
        self.panel_lay = QVBoxLayout(self.panel)
        self.panel_lay.setContentsMargins(12, 12, 12, 12)
        self.panel_lay.setSpacing(6)
        self.side.addWidget(self.panel)
        self.mem_btn = self._chip("מה אני זוכר", self.toggle_panel)
        self.top.addWidget(self.mem_btn)
        self.top.addWidget(self._chip("חזור למראה המקורי", self.m.reset_looks))
        self.top.addStretch(1)
        self.panel.setVisible(False)
        self.m.load()

    def toggle_panel(self):
        self.panel.setVisible(not self.panel.isVisible())
        self.refresh()

    def attach(self):
        files = pickers.open_files(self, "צרף קבצים", os.path.expanduser("~"), ATTACH_FILTER)
        if files:
            self.m.add_attachments(files)

    def dropped(self, files):
        if not self.design:
            self.m.add_attachments(files)

    def submit(self):
        t = self.input.text()
        self.input.clear()
        self.m.send(None, typed=t)

    def refresh(self):
        m, lk = self.m, self.look
        self.scroll.show_msgs(m.msgs, m.busy, m.cancel_action is not None, lk, lambda: m.cancel_action and m.cancel_action())
        self._clear(self.sugg)
        if len(m.msgs) <= 1 and not m.busy:
            sug = m.suggestions
            for i in range(0, len(sug), 3):                # rows of three chips (the Mac FlowChips)
                row = QHBoxLayout()
                row.setSpacing(6)
                for s in sug[i:i + 3]:
                    row.addWidget(self._chip(s, lambda _=False, s=s: m.send(s)))
                row.addStretch(1)
                self.sugg.addLayout(row)
        self._clear(self.files_row)
        for f in m.attachments:
            self.files_row.addWidget(self._chip("📎 " + os.path.basename(f) + "  ✕", lambda _=False, f=f: self._drop_att(f)))
        if m.attachments:
            self.files_row.addStretch(1)
        self._clear(self.chip_row)
        for p, name in m.skills.items():
            c = QLabel(he(f"{skills.purpose_name(p)}: ") + name)
            c.setObjectName("accent")
            self.chip_row.addWidget(c)
        self.chip_row.addStretch(1)
        if self.panel.isVisible():
            self._fill_panel()

    def _drop_att(self, f):
        self.m.attachments = [x for x in self.m.attachments if x != f]
        self.refresh()

    def _fill_panel(self):
        lay, m = self.panel_lay, self.m
        self._clear(lay)
        if not self.design:
            lay.addWidget(label("הוראות לסיכומים ולנקודות", "accent"))
            if not m.items:
                lay.addWidget(label("עוד אין", "faint"))
            for it in m.items:
                lay.addLayout(self._row(it, lambda _=False, it=it: m.remove_item(it)))
        lay.addWidget(label("מה אני זוכר עליך", "accent"))
        if not m.memory:
            lay.addWidget(label("עוד אין", "faint"))
        for n in m.memory:
            lay.addLayout(self._row(n, lambda _=False, n=n: m.remove_note(n)))
        if not self.design:
            lay.addWidget(label("סקילים מחוברים", "accent"))
            if not m.skills:
                lay.addWidget(label("אין", "faint"))
            for p, name in m.skills.items():
                lay.addLayout(self._row(f"{skills.purpose_name(p)}: {name}", lambda _=False, p=p: m.unlink_skill(p), "נתק"))
        lay.addStretch(1)

    def _row(self, text, fn, btn="✕"):
        r = QHBoxLayout()
        r.setSpacing(4)
        lb = label(text, wrap=True)
        lb.setStyleSheet("font-size: 12.5px;")
        r.addWidget(lb, 1)
        b = QPushButton(btn)
        b.setObjectName("small")
        b.setToolTip("למחוק" if btn == "✕" else "לנתק את הסקיל")
        b.clicked.connect(fn)
        r.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        return r


class PdfDesignWindow(ChatBase):
    def __init__(self):
        super().__init__("עיצוב ה-PDF", "תכתוב מה לשנות, ואני אראה לך איך זה נראה. כל ה-PDF-ים הבאים ייצאו ככה.",
                         "למשל: תעשה את זה יותר מינימליסטי בכחול")
        self.builds = []
        self.m = PDFDesign(notify=self.bridge.notify, spawn=self.bridge.spawn, build_preview=self._build_preview, page_png=self._page_png)
        self.clip.setToolTip("צרף לוגו")
        self.clip.clicked.connect(self.attach_logo)
        self.create_btn = self._chip("צור סקיל עיצוב מקובץ PDF", self.pick_pdf, "בחר PDF שאתה אוהב את העיצוב שלו, ואבנה ממנו סקיל. אפשר גם לגרור PDF לחלון")
        self.have_btn = self._chip("יש לי כבר סקיל PDF ▾", self.skill_menu, "טימי ישתמש בסקיל שלך כמו שהוא: ה-PDF-ים ייצאו בדיוק כמו בקלוד קוד")
        self.undo_btn = self._chip("בטל שינוי אחרון", self.m.undo)
        self.reset_btn = self._chip("חזור לעיצוב המקורי", self.m.reset)
        for b in (self.create_btn, self.have_btn, self.undo_btn, self.reset_btn):
            self.top.addWidget(b)
        self.top.addStretch(1)

    def start(self, quiet=False):
        self.m.load(quiet=quiet)

    # ---- Qt services for the model --------------------------------------------------------------------------------
    def _build_preview(self, md, dest, local, token, done):
        from timmy.ui.pdf_render import PdfBuild
        b = PdfBuild(md, dest, local=local, preview=True)
        self.builds.append(b)

        def fin(ok, _claude, note):
            if b in self.builds:
                self.builds.remove(b)
            done(ok, note)
        b.finished.connect(fin)
        t = QTimer(b)
        t.setInterval(200)
        t.timeout.connect(lambda: token.cancelled and b.cancel())
        t.start()
        b.start()

    @staticmethod
    def _page_png(pdf, png, width=900):
        from timmy.ui.pdf_render import first_page_png
        try:
            return first_page_png(pdf, png, width)
        except Exception:
            return False

    # ---- actions ----------------------------------------------------------------------------------------------
    def attach_logo(self):
        f = pickers.open_file(self, "בחר את הלוגו של העסק", os.path.expanduser("~"), LOGO_FILTER)
        if f:
            self.m.attach_logo(f)

    def pick_pdf(self):
        f = pickers.open_file(self, "בחר PDF שאתה אוהב את העיצוב שלו", os.path.expanduser("~"), "PDF (*.pdf)")
        if f:
            self.m.create_skill(f)

    def skill_menu(self):
        menu = QMenu(self)
        menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        if not self.m.candidates:
            a = menu.addAction("לא מצאתי במחשב סקיל שמייצר PDF")
            a.setEnabled(False)
        else:
            sec = menu.addAction("הסקילים שלך בקלוד קוד")
            sec.setEnabled(False)
            for s in self.m.candidates:
                menu.addAction(skills.skill_menu_title(s, s.name == self.m.skill_name), lambda s=s: self.m.link_and_adopt(s))
        menu.popup(self.have_btn.mapToGlobal(self.have_btn.rect().bottomLeft()))

    def dropped(self, files):
        self.m.handle_drop(files)

    def submit(self):
        t = self.input.text()
        self.input.clear()
        self.m.send(None, typed=t)

    def refresh(self):
        m, lk = self.m, self.look
        self.scroll.show_msgs(m.msgs, m.busy, m.cancel_action is not None, lk, lambda: m.cancel_action and m.cancel_action())
        for b in (self.create_btn, self.have_btn, self.undo_btn, self.reset_btn):
            b.setEnabled(m.busy is None)
        self._clear(self.chip_row)
        if m.skill_name:
            c = QLabel(he("ה-PDF יוצא מהסקיל: " if m.skill_runs else "מעוצב לפי הסקיל: ") + m.skill_name)
            c.setObjectName("accent")
            c.setToolTip("הסקיל מחובר ב\"שפר את טימי\". אפשר לנתק אותו שם, בפאנל \"מה אני זוכר\"")
            self.chip_row.addWidget(c)
        self.chip_row.addStretch(1)
        self._clear(self.files_row)
        if m.pending_logo:
            self.files_row.addWidget(self._chip("לוגו מצורף  ✕", self._drop_logo))
            self.files_row.addStretch(1)

    def _drop_logo(self):
        self.m.pending_logo = None
        self.refresh()
