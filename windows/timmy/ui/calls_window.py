""""השיחות שלי": every call in the recordings folder (Recorder.swift CallsView / CallsModel).

Right: search + the list (title, "עם: ...", date and length). Left: the selected call: title with a pencil (rename:
Enter saves, Esc cancels, the files are renamed too), date / length / person chips, actions ("הורד PDF",
"תמלול מלא", "פתח בתיקייה"), the PDF status line, and the summary. Above: the model pickers for summaries and the
live key points. The folder is checked every 2 s, so a call saved while the window is open appears by itself.
Build step 4 adds "שלח בוואטסאפ" (ui/app.py add_whatsapp, into `extra_actions`) and "עצב את טימי" (`open_design`).
"""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QSizePolicy, QTextBrowser, QVBoxLayout, QWidget)

from timmy import config as tconfig
from timmy.ui import calls as C
from timmy.ui import markdown as MD
from timmy.ui import pdf_render
from timmy.ui import shell
from timmy.ui import theme as T

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
MODEL_OPTIONS = [("haiku", "Haiku (מהיר)"), ("sonnet", "Sonnet (מאוזן)"), ("opus", "Opus (הכי חכם)")]
CREATOR_URL = "https://www.instagram.com/yaniv_shef/"


def he(s: str) -> str:
    return "\u200f" + (s or "")


def config_model(key: str, default: str) -> str:
    v = str(tconfig.load().get(key) or "").strip().lower()
    return v if v in dict(MODEL_OPTIONS) else default


def label(text="", name=None, size=None, bold=False, wrap=False) -> QLabel:
    lb = QLabel(he(text) if text else "")
    if name:
        lb.setObjectName(name)
    css = []
    if size:
        css.append(f"font-size:{size}px;")
    if bold:
        css.append("font-weight:700;")
    if css:
        lb.setStyleSheet("".join(css))
    lb.setWordWrap(wrap)
    return lb


def image(name: str, size: int) -> QLabel:
    lb = QLabel()
    pm = QPixmap(os.path.join(ASSETS, name))
    if not pm.isNull():
        dpr = 2.0
        pm = pm.scaled(int(size * dpr), int(size * dpr), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        lb.setPixmap(pm)
    lb.setFixedSize(size, size)
    return lb


class CallRow(QWidget):
    def __init__(self, c: C.Call, look: T.Look, selected=False):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(3)
        t = label(c.title, wrap=True)
        t.setStyleSheet(f"font-size:14px; font-weight:600; color:{look.css(look.ink, 1 if selected else 0.88)};")
        t.setMaximumHeight(42)
        lay.addWidget(t)
        if c.person:
            p = label("עם: " + c.person)
            p.setStyleSheet(f"font-size:12px; font-weight:500; color:{look.css(look.amber, 0.9)};")
            lay.addWidget(p)
        d = label(C.date_text(c.date) + (" · " + c.length if c.length else ""))
        d.setStyleSheet(f"font-size:11.5px; color:{look.css(look.ink, 0.45)};")
        lay.addWidget(d)


class CallsWindow(QWidget):
    open_design = Signal()          # build step 4 ("עצב את טימי"), shown only when connected
    pdf_finished = Signal(str, bool)   # call id, ok (tests)

    def __init__(self, folder: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("timmyWindow")
        self.setWindowTitle("השיחות שלי (טימי)")
        self.setWindowIcon(QIcon(os.path.join(ASSETS, "timmy.png")))
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.resize(1040, 720)
        self.setMinimumSize(940, 640)
        self.folder_override = folder
        self.look = T.current()
        self.calls: list[C.Call] = []
        self.selected_id: str | None = None
        self.renaming = False
        self.rename_error = ""
        self.pdf_state: dict[str, tuple] = {}     # id -> ("working",) | ("done", path, claude) | ("failed", why)
        self.pdf_note: dict[str, str] = {}
        self.builds: dict[str, pdf_render.PdfBuild] = {}
        self._sig = ""
        self._build()
        self.restyle(self.look)
        T.bus().changed.connect(self.restyle)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.reload_if_changed)
        self.reload()

    # ---- data ---------------------------------------------------------------------------------------------------
    def folder(self) -> str:
        return self.folder_override or C.recordings_dir()

    def reload_if_changed(self):
        if C.signature(self.folder()) != self._sig:
            self.reload()

    def reload(self):
        self._sig = C.signature(self.folder())
        self.calls = C.list_calls(self.folder())
        if self.selected_id is None or not any(c.id == self.selected_id for c in self.calls):
            self.selected_id = self.calls[0].id if self.calls else None
            self.renaming = False
        self.fill_list()
        self.show_call()

    def filtered(self):
        q = self.search.text()
        return [c for c in self.calls if c.matches(q)]

    def selected(self) -> C.Call | None:
        return next((c for c in self.calls if c.id == self.selected_id), None)

    # ---- layout -------------------------------------------------------------------------------------------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 28, 36, 18)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(image("timmy.png", 52))
        tl = QVBoxLayout()
        tl.setSpacing(2)
        tl.addWidget(label("השיחות שלי", "h1"))
        self.subtitle = label("", "muted")
        tl.addWidget(self.subtitle)
        head.addLayout(tl)
        head.addStretch(1)
        root.addLayout(head)

        bar = QFrame()
        bar.setObjectName("panel")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(14, 8, 14, 8)
        bl.setSpacing(18)
        self.summary_model = self._model_picker(bl, "מודל לסיכומים ול-PDF:", "summary_model", "sonnet")
        self.bullets_model = self._model_picker(bl, "נקודות בזמן השיחה:", "bullets_model", "haiku")
        bl.addStretch(1)
        self.design_btn = QPushButton(he("✨ עצב את טימי"))
        self.design_btn.setObjectName("primary")
        self.design_btn.clicked.connect(self.open_design.emit)
        self.design_btn.setVisible(False)
        bl.addWidget(self.design_btn)
        root.addSpacing(18)
        root.addWidget(bar)
        root.addSpacing(22)

        # empty state
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.addStretch(1)
        for w in (image("timmy.png", 110), label("עוד אין שיחות.", "h2"),
                  label("לחץ על העיניים של טימי למעלה באמצע המסך כדי להקליט.", "muted")):
            el.addWidget(w, 0, Qt.AlignmentFlag.AlignHCenter)
        el.addStretch(1)

        # list + detail
        self.main = QWidget()
        ml = QHBoxLayout(self.main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(22)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(300)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(10, 10, 10, 10)
        sl.setSpacing(10)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("חיפוש לפי שם או נושא")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.fill_list())
        sl.addWidget(self.search)
        self.list = QListWidget()
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._row_changed)
        sl.addWidget(self.list, 1)
        self.no_match = label("לא נמצאו שיחות", "faint")
        self.no_match.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        sl.addWidget(self.no_match)
        ml.addWidget(side)

        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(12)
        # title row / editor
        self.title_row = QWidget()
        trl = QHBoxLayout(self.title_row)
        trl.setContentsMargins(0, 0, 0, 0)
        trl.setSpacing(10)
        self.title = label("", "h1")          # one line: titles are at most 60 characters (safe_call_title)
        self.title.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.title.mouseDoubleClickEvent = lambda _e: self.start_rename()
        self.title.setToolTip("לחיצה כפולה: שינוי כותרת")
        trl.addWidget(self.title, 0)
        self.pen = QPushButton("\u270e")
        self.pen.setObjectName("icon")
        self.pen.setToolTip("שנה כותרת")
        self.pen.clicked.connect(self.start_rename)
        trl.addWidget(self.pen, 0, Qt.AlignmentFlag.AlignVCenter)
        trl.addStretch(1)
        dl.addWidget(self.title_row)

        self.editor = QWidget()
        edl = QVBoxLayout(self.editor)
        edl.setContentsMargins(0, 0, 0, 0)
        edl.setSpacing(6)
        er = QHBoxLayout()
        er.setSpacing(10)
        self.title_edit = QLineEdit()
        self.title_edit.setObjectName("title")
        self.title_edit.setPlaceholderText("כותרת השיחה")
        self.title_edit.returnPressed.connect(self.commit_rename)
        self.title_edit.textChanged.connect(self._edit_changed)
        er.addWidget(self.title_edit, 1)
        self.save_btn = QPushButton("שמור")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.commit_rename)
        er.addWidget(self.save_btn)
        cancel = QPushButton("ביטול")
        cancel.clicked.connect(self.cancel_rename)
        er.addWidget(cancel)
        edl.addLayout(er)
        self.rename_hint = label("", "faint", wrap=True)
        edl.addWidget(self.rename_hint)
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.title_edit)
        esc.setContext(Qt.ShortcutContext.WidgetShortcut)
        esc.activated.connect(self.cancel_rename)
        dl.addWidget(self.editor)

        self.chips = QHBoxLayout()
        self.chips.setSpacing(8)
        dl.addLayout(self.chips)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.pdf_btn = QPushButton(he("⬇ הורד PDF"))
        self.pdf_btn.clicked.connect(self.make_pdf)
        self.transcript_btn = QPushButton(he("≡ תמלול מלא"))
        self.transcript_btn.clicked.connect(self.open_transcript)
        self.folder_btn = QPushButton(he("\U0001f4c1 פתח בתיקייה"))
        self.folder_btn.clicked.connect(self.reveal)
        for b in (self.pdf_btn, self.transcript_btn, self.folder_btn):
            actions.addWidget(b)
        self.extra_actions = actions                     # step 4 adds "שלח בוואטסאפ" here
        actions.addStretch(1)
        dl.addLayout(actions)

        pl = QHBoxLayout()
        pl.setSpacing(8)
        self.pdf_line = label("", "muted", wrap=True)
        pl.addWidget(self.pdf_line, 1)
        self.pdf_open = QPushButton("פתח")
        self.pdf_open.setObjectName("link")
        self.pdf_open.clicked.connect(self.open_pdf)
        pl.addWidget(self.pdf_open)
        self.pdf_cancel = QPushButton("בטל")
        self.pdf_cancel.setObjectName("small")
        self.pdf_cancel.setToolTip("לעצור את הכנת ה-PDF. מה שבסיכום נשמר")
        self.pdf_cancel.clicked.connect(self.cancel_pdf)
        pl.addWidget(self.pdf_cancel)
        dl.addLayout(pl)
        rule = QFrame()
        rule.setObjectName("rule")
        dl.addWidget(rule)
        self.summary = QTextBrowser()
        self.summary.setOpenExternalLinks(True)
        self.summary.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        dl.addWidget(self.summary, 1)
        self.pick_hint = label("בחר שיחה מהרשימה", "faint")
        self.pick_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(self.pick_hint, 1)
        self.detail = detail
        ml.addWidget(detail, 1)

        root.addWidget(self.empty, 1)
        root.addWidget(self.main, 1)

        badge = QPushButton()
        badge.setObjectName("ghost")
        badge.setCursor(Qt.CursorShape.PointingHandCursor)
        badge.setIcon(QIcon(os.path.join(ASSETS, "creator.png")))
        badge.setIconSize(QSize(22, 22))
        badge.setText(he("נבנה על ידי ") + "\u200e@yaniv_shef")
        badge.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(CREATOR_URL)))
        root.addSpacing(14)
        root.addWidget(badge, 0, Qt.AlignmentFlag.AlignHCenter)

    def _model_picker(self, lay, text, key, default):
        lay.addWidget(label(text, "muted"))
        cb = QComboBox()
        for mid, name in MODEL_OPTIONS:
            cb.addItem(name, mid)
        cb.setCurrentIndex([m for m, _ in MODEL_OPTIONS].index(config_model(key, default)))
        cb.currentIndexChanged.connect(lambda _i, cb=cb, key=key: tconfig.save({key: cb.currentData()}))
        lay.addWidget(cb)
        return cb

    def restyle(self, look: T.Look):
        self.look = look
        self.setStyleSheet(look.stylesheet())
        self.fill_list()
        self.show_call()

    def showEvent(self, e):
        super().showEvent(e)
        self.reload()
        self.timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.timer.stop()

    # ---- list ---------------------------------------------------------------------------------------------------
    def fill_list(self):
        if not hasattr(self, "list"):
            return
        n = len(self.calls)
        count = "שיחה אחת" if n == 1 else f"{n} שיחות"
        self.subtitle.setText(he(count + " · נשמרות ב: " + os.path.basename(os.path.normpath(self.folder()))))
        self.empty.setVisible(n == 0)
        self.main.setVisible(n > 0)
        items = self.filtered()
        self.list.blockSignals(True)
        self.list.clear()
        sel_row = -1
        for i, c in enumerate(items):
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, c.id)
            row = CallRow(c, self.look, c.id == self.selected_id)
            it.setSizeHint(QSize(276, row.sizeHint().height() + 4))
            self.list.addItem(it)
            self.list.setItemWidget(it, row)
            if c.id == self.selected_id:
                sel_row = i
        self.list.setCurrentRow(sel_row)
        self.list.blockSignals(False)
        self.no_match.setVisible(bool(self.calls) and not items)

    def _row_changed(self, row):
        it = self.list.item(row)
        if it is None:
            return
        cid = it.data(Qt.ItemDataRole.UserRole)
        if cid != self.selected_id:
            self.selected_id = cid
            self.renaming = False
            self.rename_error = ""
            self.fill_list()
            self.show_call()

    def select(self, cid: str):
        self.selected_id = cid
        self.fill_list()
        self.show_call()

    # ---- detail -------------------------------------------------------------------------------------------------
    def _chip(self, text, accent=False):
        lb = label(text)
        lk = self.look
        color = lk.css(lk.amber) if accent else lk.css(lk.ink, 0.65)
        bg = lk.css(lk.amber, 0.12) if accent else lk.css(lk.ink, 0.06)
        lb.setStyleSheet(f"color:{color}; background:{bg}; border-radius:11px; padding:3px 10px; font-size:12px; font-weight:500;")
        return lb

    def show_call(self):
        if not hasattr(self, "summary"):
            return
        c = self.selected()
        has = c is not None
        for w in (self.title_row, self.summary, self.pdf_btn, self.transcript_btn, self.folder_btn):
            w.setVisible(has)
        self.pick_hint.setVisible(not has)
        while self.chips.count():
            it = self.chips.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        if not has:
            self.editor.setVisible(False)
            self.pdf_line.setVisible(False)
            self.pdf_open.setVisible(False)
            self.pdf_cancel.setVisible(False)
            return
        self.title.setText(he(c.title))
        self.title_row.setVisible(not self.renaming)
        self.editor.setVisible(self.renaming)
        self._edit_changed()
        chips = [self._chip("\U0001f4c5 " + C.date_text(c.date))]
        if c.length:
            chips.append(self._chip("⏱ " + c.length))
        if c.person:
            chips.append(self._chip("\U0001f464 עם: " + c.person, accent=True))
        for ch in chips:
            self.chips.addWidget(ch)
            ch.show()
        self.chips.addStretch(1)
        self.transcript_btn.setEnabled(c.has_transcript)
        self._pdf_status(c)
        blocks = MD.parse(C.summary_body(c.text))
        bar = self.summary.verticalScrollBar().value() if getattr(self, "_shown_id", None) == c.id else 0
        self.summary.setHtml(MD.to_html(blocks, self.look, os.path.dirname(c.path)))
        self.summary.verticalScrollBar().setValue(bar)
        self._shown_id = c.id
        self.layout().activate()

    def _pdf_status(self, c):
        st = self.pdf_state.get(c.id)
        self.pdf_open.setVisible(False)
        self.pdf_cancel.setVisible(False)
        working = bool(st and st[0] == "working")
        self.pdf_btn.setEnabled(not working)
        self.pdf_btn.setText(he("מכין PDF…" if working else "⬇ הורד PDF"))
        note = self.pdf_note.get(c.id, "")
        if st is None:
            self.pdf_line.setVisible(bool(note))
            self.pdf_line.setObjectName("muted")
            self.pdf_line.setText(he(note))
        elif working:
            self.pdf_line.setVisible(True)
            self.pdf_line.setObjectName("muted")
            self.pdf_line.setText(he("מכין PDF… (Claude מעצב את המסמך, עד שלוש דקות)"))
            self.pdf_cancel.setVisible(True)
        elif st[0] == "done":
            self.pdf_line.setVisible(True)
            self.pdf_line.setObjectName("ok")
            self.pdf_line.setText(he("✓ ה-PDF נשמר ליד הסיכום" + ("" if st[2] else " (בעיצוב בסיסי, בלי Claude)")
                                     + ("\n" + note if note else "")))
            self.pdf_open.setVisible(True)
        else:
            self.pdf_line.setVisible(True)
            self.pdf_line.setObjectName("err")
            self.pdf_line.setText(he(st[1]))
        self.pdf_line.style().unpolish(self.pdf_line)
        self.pdf_line.style().polish(self.pdf_line)

    # ---- rename -------------------------------------------------------------------------------------------------
    def start_rename(self):
        c = self.selected()
        if not c:
            return
        self.renaming = True
        self.rename_error = ""
        self.title_edit.setText(c.title)
        self.show_call()
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def cancel_rename(self):
        self.renaming = False
        self.rename_error = ""
        self.show_call()

    def _edit_changed(self, *_):
        ok = bool(C.safe_call_title(self.title_edit.text()))
        self.save_btn.setEnabled(ok)
        if self.rename_error:
            self.rename_hint.setObjectName("err")
            self.rename_hint.setText(he(self.rename_error))
        else:
            self.rename_hint.setObjectName("faint")
            self.rename_hint.setText(he("Enter שומר · Esc מבטל · השם מתעדכן גם בקבצים (סיכום, תמלול, הקלטה ו-PDF)"))
        self.rename_hint.style().unpolish(self.rename_hint)
        self.rename_hint.style().polish(self.rename_hint)

    def commit_rename(self):
        c = self.selected()
        if not c:
            return
        busy = "ה-PDF של השיחה עוד בהכנה. אפשר לשנות כותרת בעוד רגע" if self.pdf_state.get(c.id, ("",))[0] == "working" else None
        err, new = C.rename_call(c, self.title_edit.text(), busy=busy)
        if err:
            self.rename_error = err
            self._edit_changed()
            return
        if new != c.id:
            st = self.pdf_state.pop(c.id, None)
            if st and st[0] == "done":
                st = ("done", new[:-3] + ".pdf", st[2])
            if st:
                self.pdf_state[new] = st
            if c.id in self.pdf_note:
                self.pdf_note[new] = self.pdf_note.pop(c.id)
        self.selected_id = new
        self.renaming = False
        self.rename_error = ""
        self.reload()

    # ---- actions ------------------------------------------------------------------------------------------------
    def reveal(self):
        c = self.selected()
        if c:
            shell.reveal(c.path)

    def open_transcript(self):
        c = self.selected()
        if c and c.has_transcript:
            shell.open_path(c.transcript_path)

    def open_pdf(self):
        c = self.selected()
        st = self.pdf_state.get(c.id) if c else None
        if st and st[0] == "done":
            shell.open_path(st[1])

    def make_pdf(self, local: bool = False):
        c = self.selected()
        if not c or self.pdf_state.get(c.id, ("",))[0] == "working":
            return
        cid, dest = c.id, c.pdf_path
        self.pdf_state[cid] = ("working",)
        self.pdf_note.pop(cid, None)
        b = pdf_render.PdfBuild(c.path, dest, local=local)
        self.builds[cid] = b

        def done(ok, claude, note, cid=cid, dest=dest, b=b):
            self.builds.pop(cid, None)
            if b.cancelled:
                self.pdf_state.pop(cid, None)
            else:
                if note and (note.startswith("לא הצלחתי להפעיל את הסקיל") or note.startswith("לא מצאתי")):
                    self.pdf_note[cid] = note
                self.pdf_state[cid] = ("done", dest, claude) if ok else ("failed", "לא הצלחתי ליצור PDF")
                if ok:
                    shell.reveal(dest)
            if self.selected_id == cid:
                self.show_call()
            self.pdf_finished.emit(cid, ok and not b.cancelled)

        b.finished.connect(done)
        b.start()
        self.show_call()

    def cancel_pdf(self):
        c = self.selected()
        b = self.builds.get(c.id) if c else None
        if b:
            b.cancel()
