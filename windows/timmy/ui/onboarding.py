""""הרשאות והגדרה (טימי)": the onboarding window (onboard.py has the checks). One step at a time, the current step is
remembered (config.json onboarding_step) so reopening continues where the owner stopped. Cheap checks (mic privacy
switch, Chrome, free space) run every second while the window is open; Claude Code is checked in the background.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from timmy import config as tconfig
from timmy import onboard as O
from timmy.ui import pickers, shell
from timmy.ui import theme as T
from timmy.ui.bridge import Bridge
from timmy.ui.calls_window import ASSETS, he, image, label

TITLES = ["ברוך הבא לטימי", "ווינדוס ומיקרופון", "גוגל כרום", "Claude Code", "תיקייה והגדרות", "הכל מוכן"]


class OnboardingWindow(QWidget):
    def __init__(self, set_share_hiding=None, start_step: int | None = None):
        super().__init__()
        self.setObjectName("timmyWindow")
        self.setWindowTitle("הרשאות והגדרה (טימי)")
        self.setWindowIcon(QIcon(os.path.join(ASSETS, "timmy.png")))
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.resize(720, 620)
        self.set_share_hiding = set_share_hiding or (lambda on: tconfig.save({"hide_in_screen_share": bool(on)}))
        self.bridge = Bridge(self)
        self.bridge.changed.connect(self.refresh)
        self.step = O.step_saved() if start_step is None else start_step
        self.step = max(0, min(O.STEPS - 1, self.step))
        self.mic = None                 # last mic test result
        self.mic_testing = False
        self.claude = ("unknown", None, "")
        self.claude_checking = False
        self.login: O.ClaudeLogin | None = None
        self.login_error = ""
        self.git = ""
        self._build()
        T.bus().changed.connect(self.restyle)
        self.restyle(T.current())
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.enter_step()

    def restyle(self, look):
        self.look = look
        self.setStyleSheet(look.stylesheet())

    def showEvent(self, e):
        self.timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    # ---- layout -----------------------------------------------------------------------------------------------
    def _btn(self, text, fn, name=None):
        b = QPushButton(he(text))
        if name:
            b.setObjectName(name)
        b.clicked.connect(fn)
        return b

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(40, 30, 40, 22)
        root.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(image("timmy.png", 56))
        tl = QVBoxLayout()
        self.title = label("", "h1")
        tl.addWidget(self.title)
        self.dots = label("", "faint")
        tl.addWidget(self.dots)
        head.addLayout(tl, 1)
        root.addLayout(head)
        self.card = QFrame()
        self.card.setObjectName("panel")
        self.body = QVBoxLayout(self.card)
        self.body.setContentsMargins(22, 20, 22, 20)
        self.body.setSpacing(12)
        root.addWidget(self.card, 1)
        nav = QHBoxLayout()
        self.next_btn = self._btn("המשך", self.go_next, "primary")
        self.back_btn = self._btn("חזרה", self.go_back)
        nav.addWidget(self.next_btn)
        nav.addWidget(self.back_btn)
        nav.addStretch(1)
        root.addLayout(nav)

        # widgets that keep state across refreshes
        self.line = {}
        for k in ("intro", "win", "mic", "mic_hint", "chrome", "claude", "claude_note", "login_url", "git", "folder", "space", "done", "tray"):
            self.line[k] = label("", wrap=True)
        self.mic_btn = self._btn("בדוק את המיקרופון (4 שניות)", self.test_mic)
        self.mic_settings = self._btn("פתח את הגדרות הפרטיות של המיקרופון", lambda: shell.open_path("ms-settings:privacy-microphone"), "link")
        self.chrome_btn = self._btn("הורד את גוגל כרום", lambda: QDesktopServices.openUrl(QUrl(O.CHROME_URL)), "link")
        self.claude_btn = self._btn("בדוק שוב", self.check_claude)
        self.login_btn = self._btn("התחבר ל-Claude", self.start_login, "primary")
        self.cancel_login = self._btn("ביטול", self.stop_login, "small")
        self.code = QLineEdit()
        self.code.setPlaceholderText("הדף בדפדפן הציג קוד? הדבק אותו כאן ולחץ Enter")
        self.code.returnPressed.connect(self.send_code)
        self.get_claude = self._btn("להורדת Claude (כולל Claude Code)", lambda: QDesktopServices.openUrl(QUrl(O.CLAUDE_URL)), "link")
        self.get_git = self._btn("הורד את Git for Windows", lambda: QDesktopServices.openUrl(QUrl(O.GIT_URL)), "link")
        self.folder_btn = self._btn("בחר תיקייה אחרת", self.pick_folder)
        self.open_folder = self._btn("פתח את התיקייה", lambda: shell.open_folder(O.recordings_dir()), "link")
        self.login_box = QCheckBox("להפעיל את טימי כשנכנסים למחשב")
        self.login_box.clicked.connect(lambda on: (O.set_launch_at_login(on), self.refresh()))
        self.share_box = QCheckBox("להסתיר את טימי כשמשתפים מסך (מי שצופה בשיתוף לא רואה אותו)")
        self.share_box.clicked.connect(lambda on: self.set_share_hiding(on))
        self.pages = []
        for st in range(O.STEPS):
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(10)
            for item in self._items(st):
                if isinstance(item, QHBoxLayout):
                    lay.addLayout(item)
                else:
                    lay.addWidget(item)
            lay.addStretch(1)
            self.body.addWidget(w)
            self.pages.append(w)

    def _row(self, *ws):
        r = QHBoxLayout()
        r.setSpacing(8)
        for w in ws:
            r.addWidget(w)
        r.addStretch(1)
        return r

    def _items(self, st):
        L = self.line
        if st == 0:
            L["intro"].setText(he("טימי מקליט את השיחות שלך בגוגל מיט (או כל שיחה בכרום), כותב תמלול חי בעברית, רושם נקודות בזמן השיחה, "
                                  "ובסוף כותב סיכום, משימות ו-PDF ללקוח. רק הקול של כרום והמיקרופון שלך, שום דבר אחר במחשב.\n\n"
                                  "בכמה צעדים קצרים נוודא שהכל מוכן. זה לוקח שתי דקות."))
            return [L["intro"]]
        if st == 1:
            return [L["win"], L["mic"], self._row(self.mic_btn, self.mic_settings), L["mic_hint"]]
        if st == 2:
            return [L["chrome"], self._row(self.chrome_btn)]
        if st == 3:
            return [L["claude"], L["claude_note"], self._row(self.login_btn, self.claude_btn, self.cancel_login, self.get_claude),
                    L["login_url"], self.code, L["git"], self._row(self.get_git)]
        if st == 4:
            return [L["folder"], L["space"], self._row(self.folder_btn, self.open_folder), self.login_box, self.share_box]
        return [L["done"], L["tray"]]

    # ---- navigation -------------------------------------------------------------------------------------------
    def go_next(self):
        if self.step >= O.STEPS - 1:
            O.save_step(O.STEPS - 1)
            tconfig.save({"onboarding_done": True})
            self.close()
            return
        self.step += 1
        O.save_step(self.step)
        self.enter_step()

    def go_back(self):
        if self.step > 0:
            self.step -= 1
            O.save_step(self.step)
            self.enter_step()

    def show_step(self, n: int):
        self.step = max(0, min(O.STEPS - 1, n))
        self.enter_step()

    def enter_step(self):
        if self.step == 3 and self.claude[0] == "unknown" and not self.claude_checking:
            self.check_claude()
        self.refresh()

    # ---- checks -----------------------------------------------------------------------------------------------
    def test_mic(self):
        if self.mic_testing:
            return
        self.mic_testing = True
        self.refresh()

        def done(r):
            self.mic, self.mic_testing = r, False
        self.bridge.spawn(O.mic_test, done)

    def check_claude(self):
        if self.claude_checking or (self.login and not self.login.done):
            return
        demo = os.environ.get("TIMMY_DEMO_CLAUDE")
        if demo:
            self.claude = ({"missing": "not_installed", "ok": "ok", "expired": "not_logged_in", "problem": "problem"}.get(demo, "not_logged_in"),
                           "claude.exe", O.problem_text("network") if demo == "problem" else "")
            self.git = O.git_bash()
            return self.refresh()
        self.claude_checking = True
        self.refresh()

        def work():
            return O.detect_claude(), O.git_bash()

        def done(r):
            self.claude, self.git = r
            self.claude_checking = False
        self.bridge.spawn(work, done)

    def start_login(self):
        path = self.claude[1]
        if not path:
            return
        self.login_error = ""
        self.login = O.ClaudeLogin(path, on_update=self.bridge.notify).start()
        self.refresh()

    def stop_login(self):
        if self.login:
            self.login.cancel()
        self.refresh()

    def send_code(self):
        if self.login:
            self.login.send_code(self.code.text())
            self.code.clear()

    def pick_folder(self):
        d = pickers.existing_directory(self, "בחר את תיקיית ההקלטות", O.recordings_dir())
        if d:
            O.set_folder(d)
            self.refresh()

    # ---- state -> widgets -------------------------------------------------------------------------------------
    def refresh(self):
        st, L = self.step, self.line
        self.title.setText(he(TITLES[st]))
        self.dots.setText(he("  ".join("●" if i == st else "○" for i in range(O.STEPS))))
        for i, p in enumerate(self.pages):
            p.setVisible(i == st)
        self.back_btn.setVisible(st > 0)
        self.next_btn.setText(he("סיום" if st == O.STEPS - 1 else "המשך"))
        if st == 1:
            ok, name = O.windows_ok()
            L["win"].setText(he(("✓ " if ok else "✗ ") + name + ("" if ok else ". צריך לעדכן את ווינדוס (הגדרות ← Windows Update) כדי שטימי יוכל להקליט רק את כרום.")))
            consent = O.mic_consent()
            if consent == "deny":
                L["mic"].setText(he("✗ ווינדוס חוסם את המיקרופון לאפליקציות. פתח את הגדרות הפרטיות והדלק את \"לאפשר לאפליקציות שולחן עבודה לגשת למיקרופון\"."))
            elif self.mic_testing:
                L["mic"].setText(he("מקשיב למיקרופון… תגיד משהו"))
            elif self.mic:
                s = self.mic["state"]
                L["mic"].setText(he({"ok": "✓ המיקרופון עובד" + (f" ({self.mic['name']})" if self.mic.get("name") else ""),
                                     "silent": "המיקרופון פתוח אבל לא שמעתי כלום. בדוק שהוא לא מושתק ונסה שוב.",
                                     "none": "✗ לא מצאתי מיקרופון. חבר אוזניות או מיקרופון ונסה שוב.",
                                     "denied": "✗ ווינדוס חוסם את המיקרופון. פתח את הגדרות הפרטיות ונסה שוב."}.get(s, "לא הצלחתי לבדוק את המיקרופון. נסה שוב.")))
            else:
                L["mic"].setText(he("המיקרופון: לחץ \"בדוק\" ותגיד משהו."))
            self.mic_btn.setEnabled(not self.mic_testing)
            self.mic_settings.setVisible(consent == "deny" or bool(self.mic and self.mic["state"] == "denied"))
            L["mic_hint"].setText(he("ווינדוס לא שואל אפליקציות כמו טימי לגבי המיקרופון: יש רק מתג אחד בהגדרות הפרטיות, והוא צריך להיות דלוק."))
            L["mic_hint"].setObjectName("faint")
        elif st == 2:
            ok = O.chrome_installed()
            L["chrome"].setText(he("✓ גוגל כרום מותקן. טימי מקליט רק את הקול של כרום (השיחה בגוגל מיט) ואת המיקרופון שלך." if ok else
                                   "✗ לא מצאתי את גוגל כרום. טימי מקליט את הקול של השיחה מתוך כרום, אז צריך להתקין אותו."))
            self.chrome_btn.setVisible(not ok)
        elif st == 3:
            self._claude_ui()
        elif st == 4:
            d = O.recordings_dir()
            L["folder"].setText(he("ההקלטות והסיכומים נשמרים ב:\n") + "\u200e" + d)
            g = O.free_gb(d) if os.path.isdir(d) or os.path.isdir(os.path.dirname(d)) else 0
            L["space"].setText(he(f"מקום פנוי: {g} GB" + ("" if g >= 1 else ". מעט מדי: כדאי לפנות מקום או לבחור דיסק אחר.")))
            L["space"].setObjectName("muted" if g >= 1 else "err")
            self.login_box.setChecked(O.launch_at_login())
            self.login_box.setEnabled(os.name == "nt")
            hide = tconfig.load().get("hide_in_screen_share")
            self.share_box.setChecked(True if hide is None else bool(hide))
        elif st == 5:
            L["done"].setText(he("הכל מוכן. כשמתחילה שיחה בכרום, לוחצים על העיניים של טימי למעלה באמצע המסך, וטימי מתחיל להקליט. "
                                 "לחיצה נוספת עוצרת, והסיכום נשמר לבד."))
            L["tray"].setText(he("טימי גר גם ליד השעון (באזור ההתראות). בווינדוס 11 הוא מוסתר לפעמים מתחת לחץ ^: אפשר לגרור אותו משם "
                                 "לשורת המשימות. לחיצה עליו פותחת את \"השיחות שלי\", לחיצה ימנית את התפריט."))
            L["tray"].setObjectName("muted")
        for w in L.values():
            w.style().unpolish(w)
            w.style().polish(w)

    def _claude_ui(self):
        L = self.line
        state, _path, note = self.claude
        lg = self.login
        logging_in = lg is not None and not lg.done
        if lg is not None and lg.done:
            if lg.ok:
                self.login = None
                self.claude = ("unknown", None, "")
                QTimer.singleShot(0, self.check_claude)
            else:
                self.login_error = lg.error
                self.login = None
            logging_in = False
        if self.claude_checking:
            t = "בודק את Claude Code…"
        elif logging_in:
            t = "מחכה לאישור בדפדפן… (נפתח דף של Claude: לחץ שם על Authorize, וטימי ימשיך לבד)"
        else:
            t = {"ok": "✓ Claude Code מותקן ומחובר. ממנו טימי כותב את הנקודות, הסיכומים וה-PDF.",
                 "not_installed": "✗ לא מצאתי את Claude Code. הדרך הכי פשוטה: להתקין את אפליקציית Claude ולפתוח בה את הלשונית Code פעם אחת.",
                 "not_logged_in": "צריך להתחבר ל-Claude פעם אחת (לוקח דקה, בדפדפן).",
                 "problem": note or O.BROKEN,
                 "unknown": "בודק את Claude Code…"}[state]
        L["claude"].setText(he(t))
        L["claude_note"].setText(he(self.login_error))
        L["claude_note"].setObjectName("err")
        L["claude_note"].setVisible(bool(self.login_error))
        self.login_btn.setVisible(state == "not_logged_in" and not logging_in and not self.claude_checking)
        self.cancel_login.setVisible(logging_in)
        self.code.setVisible(logging_in)
        url = lg.url if logging_in and lg else ""
        L["login_url"].setVisible(bool(url))
        L["login_url"].setText(he("הדפדפן לא נפתח? פתח את הקישור: ") + "\u200e" + url)
        L["login_url"].setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.claude_btn.setVisible(not logging_in and not self.claude_checking)
        self.get_claude.setVisible(state == "not_installed")
        if os.name == "nt" or os.environ.get("TIMMY_DEMO_GIT"):
            ok = bool(self.git)
            L["git"].setText(he("✓ Git for Windows מותקן (Claude Code צריך אותו בווינדוס)." if ok else
                                "✗ Claude Code בווינדוס צריך את Git for Windows. מורידים, מתקינים עם ברירות המחדל, ולוחצים \"בדוק שוב\"."))
            L["git"].setVisible(not self.claude_checking)
            self.get_git.setVisible(not ok and not self.claude_checking)
        else:
            L["git"].setVisible(False)
            self.get_git.setVisible(False)
