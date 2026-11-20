"""The Timmy app (python -m timmy / pythonw -m timmy): one process per user with the island at the top of the screen,
a tray icon with the menu, and the windows (DESIGN.md 7).

Menu (tray icon and right click on the island; the Mac right-click menu): while recording "בטל הקלטה" (with an
explicit confirmation item), hide / show the live transcript, "להסתיר את טימי בשיתוף מסך" (on by default),
"השיחות שלי…", "פתח תיקיית הקלטות", "שפר את טימי…", "עצב את טימי…", "הרשאות והגדרה (טימי)", "הפעל עם הכניסה
למחשב", "יציאה". Build step 4 registers the chats and the onboarding (`step4`); the first launch (no
`onboarding_done` in config.json) opens the onboarding by itself, unless started with --background.

Screen-share hiding: ShareHider is an application event filter that sets WDA_EXCLUDEFROMCAPTURE on every top-level
window when it is shown or gets a new native handle (island, windows, menus, tooltips, dialogs), so nothing can be
forgotten; the menu switch applies the new value to every open window at once.
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from timmy import config as tconfig
from timmy.ui import calls as C
from timmy.ui import shell
from timmy.ui import theme as T
from timmy.winsys import affinity, startup

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def hide_in_share_setting() -> bool:
    v = tconfig.load().get("hide_in_screen_share")
    return True if v is None else bool(v)


class ShareHider(QObject):
    """Event filter: every top-level window gets the screen-share affinity (DESIGN.md 7.3)."""

    def __init__(self, hide: bool = True, setter=None, parent=None):
        super().__init__(parent)
        self.hide = hide
        self.setter = setter or affinity.set_hidden
        self.applied: dict[int, bool] = {}

    def apply(self, w: QWidget) -> None:
        try:
            if not w.isWindow() or not w.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
                return
            hwnd = int(w.winId())
        except RuntimeError:
            return
        ok = self.setter(hwnd, self.hide)
        self.applied[hwnd] = bool(ok)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Type.Show, QEvent.Type.WinIdChange) and isinstance(obj, QWidget) and obj.isWindow():
            self.apply(obj)
        return False

    def set_hide(self, on: bool) -> None:
        self.hide = bool(on)
        for w in QApplication.topLevelWidgets():
            if w.isVisible():
                self.apply(w)


class TimmyApp(QObject):
    def __init__(self, app: QApplication, background: bool = False):
        super().__init__(app)
        from timmy.ui.island import Island
        from timmy.ui.recorder import Recorder
        self.app = app
        self.look = T.current()
        self.hider = ShareHider(hide_in_share_setting(), parent=self)
        app.installEventFilter(self.hider)
        self.rec = Recorder(self)
        self.island = Island(self.rec)
        self.island.menu_requested.connect(self.popup_menu)
        self.calls_window = None
        self.chats = {}                 # "improve" / "design" -> TimmyChatWindow, "pdf" -> PdfDesignWindow
        self.onboarding = None
        self.step4 = {"improve": self.open_improve, "design": self.open_design, "onboarding": self.open_onboarding}
        self.tray = QSystemTrayIcon(QIcon(os.path.join(ASSETS, "timmy.png")), self)
        self.tray.setToolTip("טימי")
        self.menu = QMenu()
        self.menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.menu.aboutToShow.connect(self.build_menu)
        self.build_menu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.rec.need_folder.connect(self._no_space)
        self.rec.calls_changed.connect(self._calls_changed)
        T.bus().changed.connect(self.restyle)
        self.restyle(self.look)
        app.aboutToQuit.connect(self.rec.shutdown)
        for s in QGuiApplication.screens():
            s.geometryChanged.connect(lambda *_: self.island.place())
        QGuiApplication.instance().screenAdded.connect(lambda *_: self.island.place())
        QGuiApplication.instance().screenRemoved.connect(lambda *_: QTimer.singleShot(200, self.island.place))
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.island.show()
        self.island.place()
        if not background and not tconfig.load().get("onboarding_done"):
            QTimer.singleShot(400, self.open_onboarding)

    # ---- look ---------------------------------------------------------------------------------------------------
    def restyle(self, look: T.Look):
        self.look = look
        self.menu.setStyleSheet(look.stylesheet())

    # ---- menu ---------------------------------------------------------------------------------------------------
    def build_menu(self):
        m = self.menu
        m.clear()
        if self.rec.phase == "recording":
            sub = m.addMenu("בטל הקלטה")
            warn = sub.addAction("ההקלטה תימחק ולא תישמר")
            warn.setEnabled(False)
            sub.addAction("כן, למחוק את ההקלטה", self.rec.cancel)
        m.addAction("הצג תמלול חי" if self.rec.hide_live else "הסתר תמלול חי", lambda: self.rec.set_hide_live(not self.rec.hide_live))
        share = QAction("להסתיר את טימי בשיתוף מסך", m)
        share.setCheckable(True)
        share.setChecked(self.hider.hide)
        share.setToolTip("כשאתה משתף מסך (למשל בגוגל מיט), מי שצופה לא יראה את טימי. טימי גם לא יופיע בצילומי מסך. "
                         "כבה אם אתה רוצה להראות את טימי או לצלם אותו")
        share.toggled.connect(self.set_hide_in_share)
        m.addAction(share)
        m.setToolTipsVisible(True)
        m.addSeparator()
        m.addAction("השיחות שלי…", self.open_calls)
        m.addAction("פתח תיקיית הקלטות", lambda: shell.open_folder(C.recordings_dir()))
        for key, title in (("improve", "שפר את טימי…"), ("design", "עצב את טימי…"), ("onboarding", "הרשאות והגדרה (טימי)")):
            a = m.addAction(title)
            fn = self.step4.get(key)
            if fn:
                a.triggered.connect(lambda _checked=False, fn=fn: fn())
            else:
                a.setEnabled(False)
                a.setToolTip("יגיע בעדכון הבא של טימי לווינדוס")
        if sys.platform == "win32":
            login = QAction("הפעל עם הכניסה למחשב", m)
            login.setCheckable(True)
            login.setChecked(startup.is_enabled())
            login.toggled.connect(self.set_launch_at_login)
            m.addAction(login)
        m.addSeparator()
        m.addAction("יציאה", self.quit)

    def popup_menu(self, pos):
        self.build_menu()
        self.menu.popup(pos)

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_calls()

    # ---- actions ------------------------------------------------------------------------------------------------
    def set_hide_in_share(self, on: bool):
        tconfig.save({"hide_in_screen_share": bool(on)})
        self.hider.set_hide(on)

    def set_launch_at_login(self, on: bool):
        if startup.set_enabled(on):
            tconfig.save({"launch_at_login": bool(on)})

    def open_calls(self):
        from timmy.ui.calls_window import CallsWindow
        if self.calls_window is None:
            self.calls_window = CallsWindow()
            if "design" in self.step4:
                self.calls_window.design_btn.setVisible(True)
                self.calls_window.open_design.connect(self.step4["design"])
            add_whatsapp(self.calls_window)
        self._front(self.calls_window)

    @staticmethod
    def _front(w):
        w.show()
        w.raise_()
        w.activateWindow()

    # ---- build step 4: chats and onboarding ----------------------------------------------------------------------
    def chat(self, key: str):
        from timmy.ui.chat_window import PdfDesignWindow, TimmyChatWindow
        w = self.chats.get(key)
        if w is None:
            w = PdfDesignWindow() if key == "pdf" else TimmyChatWindow(key == "design", open_pdf_design=self.open_pdf_design,
                                                                         set_share_hiding=self.set_hide_in_share)
            self.chats[key] = w
        return w

    def open_improve(self):
        self._front(self.chat("improve"))

    def open_design(self):
        self._front(self.chat("design"))

    def open_pdf_design(self, send=None, logo=None, adopt=None, then=None, create_from=None, pick=False):
        """The "עיצוב ה-PDF" chat, opened by "שפר את טימי": a design request (+ the logo file), a skill to adopt, a sample
        PDF to build a design skill from (or the file picker)."""
        w = self.chat("pdf")
        quiet = adopt is not None or create_from is not None or pick
        w.start(quiet=quiet)
        self._front(w)
        if adopt is not None:
            w.m.adopt_skill(adopt, then=then)
        elif send:
            w.m.send(send, logo=w.m.copy_logo(logo) if logo else None)
        if create_from:
            w.m.create_skill(create_from)
        elif pick:
            QTimer.singleShot(300, w.pick_pdf)

    def open_onboarding(self, step: int | None = None):
        from timmy.ui.onboarding import OnboardingWindow
        if self.onboarding is None:
            self.onboarding = OnboardingWindow(set_share_hiding=self.set_hide_in_share)
        if step is not None:
            self.onboarding.show_step(step)
        self._front(self.onboarding)

    def _calls_changed(self):
        if self.calls_window is not None and self.calls_window.isVisible():
            self.calls_window.reload_if_changed()

    def _no_space(self):
        """Not enough space to start: the folder step of the onboarding (the Mac opens the same step)."""
        self.open_onboarding(4)

    def handle_message(self, msg: str):
        if msg in ("show", "calls"):
            self.open_calls()

    def quit(self):
        self.app.quit()


def add_whatsapp(win) -> None:
    """"שלח בוואטסאפ" next to "הורד PDF" in "השיחות שלי" (Mac CallDetailPane)."""
    from PySide6.QtWidgets import QPushButton

    from timmy.ui.calls_window import he
    b = QPushButton(he("\U0001f4ac שלח בוואטסאפ"))
    b.setObjectName("primary")
    b.setToolTip("טימי מנסח הודעה ללקוח, אתה מאשר, ורק אז הוא שולח")

    def open_send():
        from timmy.ui.send_whatsapp import SendDialog, pdf_builder
        c = win.selected()
        if c is None:
            return
        d = SendDialog(c, win, build_pdf=pdf_builder(win))
        win._send_dialog = d
        d.finished.connect(lambda _r: win.reload_if_changed())
        d.open()
    b.clicked.connect(open_send)
    win.extra_actions.insertWidget(0, b)
    win.whatsapp_btn = b


def make_app(argv=None) -> QApplication:
    app = QApplication.instance() or QApplication(list(argv or sys.argv[:1]))
    app.setApplicationName("Timmy")
    app.setApplicationDisplayName("טימי")
    app.setOrganizationName("Timmy")
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    app.setQuitOnLastWindowClosed(False)
    f = QFont()
    f.setFamilies(["Segoe UI", "Segoe UI Variable Text", "Arial"])
    f.setPointSizeF(10)
    app.setFont(f)
    app.setWindowIcon(QIcon(os.path.join(ASSETS, "timmy.png")))
    return app


def run(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Timmy.Recorder")   # own taskbar / tray identity
        except Exception:
            pass
    app = make_app()
    from timmy.winsys.instance import SingleInstance, send_to_running
    inst = SingleInstance()
    if not inst.listen():
        send_to_running("show")
        return 0
    timmy = TimmyApp(app, background="--background" in argv)
    inst.message.connect(timmy.handle_message)
    if "--show-calls" in argv:
        timmy.open_calls()
    rc = app.exec()
    inst.close()
    return rc
