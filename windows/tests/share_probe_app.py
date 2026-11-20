"""Run by tests/test_share_hiding_all.py in its own process with the native Windows QPA (not offscreen).

Starts the real TimmyApp (TIMMY_APP_DIR, TIMMY_DRY_OPEN and a fake Claude come from the test), opens every window the
owner can reach (the island and the recording card in demo mode, the menu, a tooltip, "השיחות שלי", "שלח בוואטסאפ",
"שפר את טימי", "עצב את טימי", "עיצוב ה-PDF", the onboarding), then reads GetWindowDisplayAffinity of every VISIBLE
top-level window this process owns (EnumWindows, not only the Qt list, so native windows are caught too). A second
pass opens the folder picker of the onboarding (QFileDialog, the native Windows dialog) the same way.

Prints one JSON line: {"windows": [...], "dialog": [...], "error": ...}. Takes screenshots when argv[1] is a folder.
"""
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

os.environ["QT_QPA_PLATFORM"] = "windows"
from PySide6.QtCore import QPoint, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QToolTip  # noqa: E402

from timmy.ui.app import TimmyApp, make_app  # noqa: E402
from timmy.winsys import affinity  # noqa: E402

u32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindows = u32.EnumWindows
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def own_visible_windows():
    me = os.getpid()
    out = []

    def cb(hwnd, _lp):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me and u32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            title = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, title, 256)
            r = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left > 0 and r.bottom - r.top > 0:
                out.append({"hwnd": int(hwnd), "class": cls.value, "title": title.value,
                            "size": [r.right - r.left, r.bottom - r.top], "affinity": affinity.get(int(hwnd))})
        return True

    EnumWindows(WNDENUMPROC(cb), 0)
    return out


def pump(app, secs):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main():
    shots = sys.argv[1] if len(sys.argv) > 1 else ""
    res = {"supported": affinity.supported(), "windows": [], "dialog": [], "error": None}
    app = make_app(["probe"])
    t = TimmyApp(app, background=True)
    try:
        pump(app, 1.0)
        t.rec.demo()
        t.open_calls()
        w = t.calls_window
        if w.selected() is not None and getattr(w, "whatsapp_btn", None) is not None:
            w.whatsapp_btn.click()
        t.open_improve()
        t.open_design()
        t.open_pdf_design()
        t.open_onboarding()
        t.popup_menu(QPoint(200, 200))
        QToolTip.showText(QPoint(300, 300), "טימי")
        pump(app, 2.0)
        res["qt_windows"] = sorted({type(x).__name__ for x in QApplication.topLevelWidgets() if x.isVisible()})
        res["windows"] = own_visible_windows()
        if shots:
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.primaryScreen().grabWindow(0).save(os.path.join(shots, "share-probe-desktop.png"))
        t.menu.close()
        QToolTip.hideText()

        # native folder picker (onboarding "בחר תיקייה"): shown non-blocking, read, then closed
        d = QFileDialog(t.onboarding, "בחר את תיקיית ההקלטות")
        d.setFileMode(QFileDialog.FileMode.Directory)
        QTimer.singleShot(0, d.open)
        pump(app, 3.0)
        known = {x["hwnd"] for x in res["windows"]}
        res["dialog"] = [x for x in own_visible_windows() if x["hwnd"] not in known]
        d.reject()
        pump(app, 0.5)
    except Exception as e:     # report, never hang the test
        res["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(res, ensure_ascii=False), flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
