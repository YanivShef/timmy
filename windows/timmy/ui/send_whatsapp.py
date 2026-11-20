""""שליחה בוואטסאפ" over "השיחות שלי": the Mac overlays (SendOverlay / ConnectOverlay) as one dialog.

Connection (top): linked (Timmy's own Greenolink token) / connected through Claude Code / missing -> "חבר וואטסאפ"
(one click: the browser opens, Greenolink login + QR from the phone, Timmy continues by itself), waiting ("מחכה לאישור
בדפדפן…", reopen the browser, cancel), "נתק".
Flow (integrations/whatsapp.py SendSession): choose what to send -> Claude drafts -> the owner reads / edits / asks for
changes -> "שלח" shows the confirm step with the exact contact (name · number) or a pick list -> only "שלח עכשיו" on
that step sends. Nothing is ever sent without that click.
"""
from __future__ import annotations

import os
import webbrowser

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)

from timmy.integrations import greenolink as G
from timmy.integrations import whatsapp as W
from timmy.ui import theme as T
from timmy.ui.bridge import Bridge
from timmy.ui.calls_window import he, label


class WAState:
    """App-wide WhatsApp connection state (Mac CallsModel.wa / link)."""

    def __init__(self):
        self.wa = "unknown"          # unknown | checking | linked | ("connected", server) | missing
        self.link = "idle"           # idle | waiting | ("failed", why)
        self.listeners = []

    def changed(self):
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    def check(self, spawn):
        if self.wa == "checking":
            return
        if G.has_token():
            self.wa = "linked"
            return self.changed()
        self.wa = "checking"
        self.changed()

        def done(r):
            if self.wa != "linked":
                self.wa = r
            self.changed()
        spawn(W.check_wa, done)

    def connect(self, spawn, open_url=None):
        if self.link == "waiting":
            return
        self.link = "waiting"
        self.changed()
        opener = open_url or webbrowser.open           # called from the worker thread: no Qt there
        gl = G.Greenolink.shared()

        def work():
            try:
                gl.connect(lambda u: opener(u))
                return None
            except G.GLError as e:
                return e

        def done(e):
            if e is None:
                self.wa, self.link = "linked", "idle"
            elif e.cancelled:
                self.link = "idle"
            else:
                self.link = ("failed", e.msg)
            self.changed()
        spawn(work, done)

    def cancel_connect(self):
        G.Greenolink.shared().cancel_connect()

    def disconnect(self, spawn):
        def done(_):
            self.wa = "missing"
            self.changed()
        spawn(lambda: G.Greenolink.shared().disconnect(), done)

    def auth_failed(self, e):
        if getattr(e, "auth", False) and self.wa == "linked" and not G.has_token():
            self.wa = "missing"
            self.changed()


STATE = WAState()


class SendDialog(QDialog):
    def __init__(self, call, parent=None, state: WAState | None = None, build_pdf=None, open_url=None):
        super().__init__(parent)
        self.setObjectName("timmyWindow")
        self.setWindowTitle("שליחה בוואטסאפ (טימי)")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setModal(True)
        self.resize(640, 640)
        self.state = state or STATE
        self.open_url = open_url
        self.bridge = Bridge(self)
        self.bridge.changed.connect(self.refresh)
        self.state.listeners.append(self.bridge.notify)
        self.s = W.SendSession(call, self.state.wa, G.Greenolink.shared(), notify=self.bridge.notify, spawn=self.bridge.spawn,
                               build_pdf=build_pdf)
        self.s.auth_failed = self.state.auth_failed
        self.look = T.current()
        self._build()
        self.setStyleSheet(self.look.stylesheet())
        if self.state.wa in ("unknown", "missing"):
            self.state.check(self.bridge.spawn)
        self.refresh()

    def done(self, r):
        try:
            self.state.listeners.remove(self.bridge.notify)
        except ValueError:
            pass
        super().done(r)

    # ---- layout -----------------------------------------------------------------------------------------------
    def _btn(self, text, fn, name=None):
        b = QPushButton(he(text))
        if name:
            b.setObjectName(name)
        b.clicked.connect(fn)
        return b

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(12)
        root.addWidget(label("שליחה בוואטסאפ", "h2"))
        self.call_line = label(self.s.call.title, "muted")
        root.addWidget(self.call_line)

        # connection panel
        self.conn = QFrame()
        self.conn.setObjectName("panel")
        cl = QVBoxLayout(self.conn)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)
        self.conn_text = label("", wrap=True)
        cl.addWidget(self.conn_text)
        cr = QHBoxLayout()
        self.connect_btn = self._btn("חבר וואטסאפ", lambda: self.state.connect(self.bridge.spawn, self.open_url), "primary")
        self.reopen_btn = self._btn("הדפדפן לא נפתח? פתח שוב", lambda: G.Greenolink.shared().reopen_browser(
            self.open_url or (lambda u: QDesktopServices.openUrl(QUrl(u)))), "link")
        self.cancel_conn_btn = self._btn("ביטול", self.state.cancel_connect, "small")
        self.register_btn = self._btn("אין לך חשבון? הרשמה ל-Greenolink", lambda: QDesktopServices.openUrl(QUrl(G.REGISTER_URL)), "link")
        self.disconnect_btn = self._btn("נתק", lambda: self.state.disconnect(self.bridge.spawn), "small")
        for b in (self.connect_btn, self.reopen_btn, self.cancel_conn_btn, self.register_btn, self.disconnect_btn):
            cr.addWidget(b)
        cr.addStretch(1)
        cl.addLayout(cr)
        root.addWidget(self.conn)

        # choose
        self.choose = QWidget()
        chl = QVBoxLayout(self.choose)
        chl.setContentsMargins(0, 0, 0, 0)
        chl.setSpacing(8)
        kinds = QHBoxLayout()
        self.kind_btns = {}
        for k in W.KINDS:
            b = QPushButton(he(W.TITLES[k]))
            b.setCheckable(True)
            b.setToolTip(W.SUBS[k])
            b.clicked.connect(lambda _=False, k=k: self._set_kind(k))
            kinds.addWidget(b)
            self.kind_btns[k] = b
        chl.addLayout(kinds)
        self.kind_sub = label("", "muted", wrap=True)
        chl.addWidget(self.kind_sub)
        rr = QHBoxLayout()
        rr.addWidget(label("נמען:"))
        self.recipient = QLineEdit(self.s.recipient)
        self.recipient.setPlaceholderText("שם איש הקשר או מספר טלפון")
        self.recipient.textChanged.connect(lambda t: setattr(self.s, "recipient", t))
        rr.addWidget(self.recipient, 1)
        chl.addLayout(rr)
        self.request = QPlainTextEdit()
        self.request.setPlaceholderText("למשל: תזכיר לה בעדינות את התשלום, תדגיש שהשיעור הבא ביום חמישי ב-10, טון חם וקליל")
        self.request.setFixedHeight(90)
        self.request.textChanged.connect(self._request_changed)
        chl.addWidget(self.request)
        self.attach = QCheckBox("לצרף את ה-PDF")
        self.attach.clicked.connect(lambda on: (self.s.touch_pdf(on), self.refresh()))
        chl.addWidget(self.attach)
        self.guidance = QLineEdit()
        self.guidance.setPlaceholderText("תכוון את טימי: מה לכתוב, באיזה טון, מה להדגיש (לא חובה)")
        self.guidance.textChanged.connect(lambda t: setattr(self.s, "guidance", t))
        chl.addWidget(self.guidance)
        self.draft_btn = self._btn("נסח הודעה", self.s.make_draft, "primary")
        chl.addWidget(self.draft_btn, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self.choose)

        # working line (drafting / sending)
        self.working = label("", "muted")
        root.addWidget(self.working)

        # review
        self.review = QWidget()
        rl = QVBoxLayout(self.review)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        self.draft = QPlainTextEdit()
        self.draft.textChanged.connect(self._draft_changed)
        rl.addWidget(self.draft, 1)
        self.pdf_note = label("", "muted")
        rl.addWidget(self.pdf_note)
        rv = QHBoxLayout()
        self.revision = QLineEdit()
        self.revision.setPlaceholderText("בקש שינוי: למשל קצר יותר, בלי אימוג'י")
        self.revision.textChanged.connect(self._revision_changed)
        self.revision.returnPressed.connect(self.s.revise)
        rv.addWidget(self.revision, 1)
        self.revise_btn = self._btn("עדכן נוסח", self.s.revise)
        rv.addWidget(self.revise_btn)
        self.undo_btn = self._btn("החזר את הנוסח הקודם", self.s.undo_revision, "link")
        rv.addWidget(self.undo_btn)
        rl.addLayout(rv)
        self.revise_err = label("", "err", wrap=True)
        rl.addWidget(self.revise_err)
        self.rec_line = label("", "muted", wrap=True)
        rl.addWidget(self.rec_line)
        ra = QHBoxLayout()
        self.send_btn = self._btn("שלח", self.s.resolve_recipient, "primary")
        ra.addWidget(self.send_btn)
        ra.addWidget(self._btn("נסח מחדש", self.s.make_draft))
        ra.addWidget(self._btn("חזרה", lambda: self.s.set_stage("choose")))
        ra.addStretch(1)
        rl.addLayout(ra)
        root.addWidget(self.review, 1)

        # confirm
        self.confirm = QFrame()
        self.confirm.setObjectName("panel")
        fl = QVBoxLayout(self.confirm)
        fl.setContentsMargins(16, 14, 16, 14)
        fl.addWidget(label("לשלוח את ההודעה ל:", "muted"))
        self.confirm_to = label("", "h2")
        fl.addWidget(self.confirm_to)
        fr = QHBoxLayout()
        self.send_now = self._btn("שלח עכשיו", self._confirm_clicked, "primary")
        fr.addWidget(self.send_now)
        fr.addWidget(self._btn("לא זה, חזרה", lambda: self.s.set_stage("review")))
        fr.addStretch(1)
        fl.addLayout(fr)
        root.addWidget(self.confirm)

        # pick
        self.pick = QWidget()
        self.pick_lay = QVBoxLayout(self.pick)
        self.pick_lay.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.pick)

        # sent / failed
        self.result = label("", "h2", wrap=True)
        root.addWidget(self.result)
        self.result_row = QHBoxLayout()
        self.back_btn = self._btn("חזרה לטיוטה", lambda: self.s.set_stage("review" if self.s.draft.strip() else "choose"))
        self.close_btn = self._btn("סגור", self.accept, "primary")
        self.result_row.addWidget(self.back_btn)
        self.result_row.addWidget(self.close_btn)
        self.result_row.addStretch(1)
        root.addLayout(self.result_row)
        root.addStretch(0)

    def _draft_changed(self):
        if self.s.draft != self.draft.toPlainText():
            self.s.draft = self.draft.toPlainText()
            self.refresh()

    def _revision_changed(self, t):
        if self.s.revision != t:
            self.s.revision = t
            self.refresh()

    def _set_kind(self, k):
        self.s.kind = k
        self.refresh()

    def _request_changed(self):
        self.s.set_request(self.request.toPlainText())
        self.attach.setChecked(self.s.attach_pdf)

    def _confirm_clicked(self):
        st = self.s.stage
        if st[0] == "confirm":
            self.s.confirm_send(st[1])          # the only place in the UI that can send

    # ---- state -> widgets -------------------------------------------------------------------------------------
    def refresh(self):
        s, st = self.s, self.s.stage
        if self.state.wa not in ("checking", "unknown"):
            s.wa = self.state.wa
        wa, link = self.state.wa, self.state.link
        linked = wa == "linked"
        connected = isinstance(wa, tuple)
        waiting = link == "waiting"
        if wa in ("checking", "unknown"):
            t = "בודק את החיבור לוואטסאפ…"
        elif linked:
            t = "מחובר ✓  טימי שולח ישירות דרך Greenolink. החיבור שמור במנהל האישורים של ווינדוס."
        elif connected:
            t = "מחובר ✓  (דרך Claude Code)"
        elif waiting:
            t = "מחכה לאישור בדפדפן…\nבדפדפן: מתחברים ל-Greenolink, ואז בטלפון: וואטסאפ ← מכשירים מקושרים ← קישור מכשיר, וסורקים את ה-QR. טימי ימשיך לבד."
        else:
            t = "כדי לשלוח בוואטסאפ צריך לחבר אותו פעם אחת. לחיצה אחת, ממשיכים בדפדפן."
            if isinstance(link, tuple):
                t += "\n" + link[1]
        self.conn_text.setText(he(t))
        self.connect_btn.setVisible(not linked and not connected and not waiting and wa not in ("checking", "unknown"))
        self.register_btn.setVisible(self.connect_btn.isVisible())
        self.reopen_btn.setVisible(waiting)
        self.cancel_conn_btn.setVisible(waiting)
        self.disconnect_btn.setVisible(linked)
        can = linked or connected
        kind = st[0]
        self.choose.setVisible(kind == "choose")
        for k, b in self.kind_btns.items():
            b.setChecked(k == s.kind)
            b.setObjectName("primary" if k == s.kind else "")
            b.style().unpolish(b)
            b.style().polish(b)
        self.kind_sub.setText(he(W.SUBS[s.kind]))
        self.request.setVisible(s.kind == "free")
        self.attach.setVisible(s.kind == "free")
        self.attach.setChecked(s.attach_pdf)
        self.guidance.setVisible(s.kind != "free")
        self.draft_btn.setEnabled(can)
        self.working.setVisible(kind in ("drafting", "sending"))
        self.working.setText(he(st[1] if kind == "drafting" else "שולח…" if kind == "sending" else ""))
        self.review.setVisible(kind == "review")
        if kind == "review" and self.draft.toPlainText() != s.draft:
            self.draft.setPlainText(s.draft)
        self.pdf_note.setVisible(s.pdf_path is not None)
        self.pdf_note.setText(he("📎 מצורף: " + os.path.basename(s.pdf_path or "")))
        if self.revision.text() != s.revision:
            self.revision.setText(s.revision)
        self.revise_btn.setEnabled(not s.revising and s.pending_revision)
        self.revise_btn.setText(he("מעדכן…" if s.revising else "עדכן נוסח"))
        self.undo_btn.setVisible(s.can_undo_revision)
        self.revise_err.setVisible(bool(s.revise_error))
        self.revise_err.setText(he(s.revise_error or ""))
        if s.resolving:
            rl = f"מחפש את {s.resolved_query} באנשי הקשר…"
        elif s.resolve_error:
            rl = s.resolve_error
        elif s.chosen is not None:
            rl = "לשלוח ל: " + W.recipient_label(s.chosen)
        elif s.resolved == [] and s.resolved_query:
            rl = f"לא מצאתי איש קשר בשם {s.resolved_query}. אפשר לכתוב מספר טלפון."
        else:
            rl = ""
        self.rec_line.setText(he(rl))
        self.send_btn.setEnabled(can and not s.revising and not s.pending_revision and bool(s.draft.strip() or s.pdf_path))
        self.send_btn.setText(he("שלח ל-" + W.recipient_label(s.chosen) if s.chosen is not None else "שלח"))
        self.send_btn.setToolTip("יש בקשת שינוי שעוד לא הוחלה: לחץ \"עדכן נוסח\" או מחק אותה" if s.pending_revision else "")
        self.confirm.setVisible(kind == "confirm")
        if kind == "confirm":
            self.confirm_to.setText(he(W.recipient_label(st[1])))
        self.pick.setVisible(kind == "pick")
        if kind == "pick":
            while self.pick_lay.count():
                it = self.pick_lay.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            self.pick_lay.addWidget(label("יש כמה אנשי קשר שמתאימים. למי לשלוח?", "muted"))
            for c in st[1]:
                self.pick_lay.addWidget(self._btn(W.recipient_label(c), lambda _=False, c=c: self.s.pick(c)))
            self.pick_lay.addWidget(self._btn("חזרה", lambda: self.s.set_stage("review"), "link"))
        self.result.setVisible(kind in ("sent", "failed"))
        if kind == "sent":
            self.result.setObjectName("ok")
            self.result.setText(he("נשלח ל-" + st[1] + " ✓"))
        elif kind == "failed":
            self.result.setObjectName("err")
            self.result.setText(he(st[1]))
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        self.back_btn.setVisible(kind == "failed")
        self.close_btn.setVisible(kind in ("sent", "failed"))


def pdf_builder(owner):
    """build_pdf for SendSession: the client PDF through the same engine as "הורד PDF"."""
    def build(md, dest, done):
        from timmy.ui.pdf_render import PdfBuild
        b = PdfBuild(md, dest)
        b.finished.connect(lambda ok, _c, _n: done(ok))
        b.start()
        owner._wa_builds = getattr(owner, "_wa_builds", []) + [b]
    return build

