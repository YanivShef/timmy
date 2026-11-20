"""Run by tests/test_winsys_ui.py in its own process with the native Windows QPA (not offscreen): shows a solid red
window with WDA_EXCLUDEFROMCAPTURE and a solid blue one without, reads the affinity back and takes a desktop
screenshot. Prints one JSON line."""
import json
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "windows"
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from timmy.winsys import affinity  # noqa: E402


def solid(color, x):
    w = QWidget(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    w.setStyleSheet(f"background:{color};")
    w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    w.setGeometry(x, 120, 220, 160)
    w.show()
    return w


def count(img, rgb):
    n = 0
    for y in range(0, img.height(), 4):
        for x in range(0, img.width(), 4):
            c = img.pixelColor(x, y)
            if abs(c.red() - rgb[0]) < 30 and abs(c.green() - rgb[1]) < 30 and abs(c.blue() - rgb[2]) < 30:
                n += 1
    return n


app = QApplication(sys.argv)
red, blue = solid("#ff0000", 100), solid("#0000ff", 400)
for _ in range(20):
    app.processEvents()
ok = affinity.set_hidden(int(red.winId()), True)
err = affinity.last_error()
for _ in range(20):
    app.processEvents()
import time  # noqa: E402
time.sleep(0.8)
app.processEvents()
shot = QGuiApplication.primaryScreen().grabWindow(0)
img = shot.toImage()
out = {"set_ok": ok, "error": err, "red_affinity": affinity.get(int(red.winId())), "blue_affinity": affinity.get(int(blue.winId())),
       "red_px": count(img, (255, 0, 0)), "blue_px": count(img, (0, 0, 255)), "screen": [img.width(), img.height()]}
shot.save(sys.argv[1]) if len(sys.argv) > 1 else None
affinity.set_hidden(int(red.winId()), False)
out["red_affinity_after_show"] = affinity.get(int(red.winId()))
print(json.dumps(out))
