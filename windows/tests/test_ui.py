"""UI smoke offscreen: every island state and the "השיחות שלי" window, dark and light, saved as PNG screenshots in
out/screens (CI uploads them), plus clicks, hit-testing, the menu and screen-share hiding on every window."""
import json
import os
import shutil

import pytest

from tests.conftest import WIN, save_shot, wait_until

pytestmark = pytest.mark.ui
SAMPLE = WIN / "timmy" / "engine" / "_mac" / "pdf_sample.md"


@pytest.fixture
def look_default(app_dir):
    from timmy.ui import theme as T
    return T.Look({})


def bright_pixels(img, color=None):
    """How many pixels differ clearly from the backdrop (something was painted)."""
    from PySide6.QtGui import QColor
    bg = QColor("#3a4a5c")
    n = 0
    for y in range(0, img.height(), 3):
        for x in range(0, img.width(), 3):
            c = img.pixelColor(x, y)
            if abs(c.red() - bg.red()) + abs(c.green() - bg.green()) + abs(c.blue() - bg.blue()) > 60:
                n += 1
    return n


def make_island(qapp, look=None):
    from timmy.ui.island import Island
    from timmy.ui.recorder import Recorder
    r = Recorder()
    isl = Island(r, look)
    isl.show()
    qapp.processEvents()
    return r, isl


def test_island_states_screenshots(qapp, screens, look_default):
    r, isl = make_island(qapp, look_default)
    shots = {}
    shots["island-pill"] = save_shot(isl, screens / "island-pill.png")
    isl.forced_hover = True
    isl.update()
    shots["island-pill-hover"] = save_shot(isl, screens / "island-pill-hover.png")
    isl.forced_hover = None
    for msg, name in (("נשמר ✓", "saved"), ("לא נקלט דיבור", "no-speech"), ("בוטל", "cancelled"), ("אין מקום", "no-space")):
        r.flash(msg)
        shots[name] = save_shot(isl, screens / f"island-msg-{name}.png")
    r.demo()
    for _ in range(10):
        isl.sync()
    shots["card"] = save_shot(isl, screens / "island-card.png")
    r.demo(thinking=True)
    shots["thinking"] = save_shot(isl, screens / "island-card-thinking.png")
    r.demo(armed=True, notice="המיקרופון עבר ל: אוזניות Bluetooth")
    shots["armed"] = save_shot(isl, screens / "island-card-cancel-armed-mic-switched.png")
    r.demo(finishing=True)
    shots["finishing"] = save_shot(isl, screens / "island-card-saving.png")
    r.demo(compact=True)
    shots["compact"] = save_shot(isl, screens / "island-card-hidden-transcript.png")
    from timmy.ui import theme as T
    light = T.Look({"background_color": "#FFFFFF", "accent_color": "#3B82F6"})
    isl.restyle(light)
    r.demo(compact=False)
    shots["light"] = save_shot(isl, screens / "island-card-light.png", "#cfd8e3")
    sizes = {k: (v.width(), v.height()) for k, v in shots.items()}
    assert sizes["card"][1] > sizes["compact"][1] > 0 and sizes["card"][0] == sizes["compact"][0]
    for k, img in shots.items():
        assert bright_pixels(img) > 40, k          # each screenshot really shows something
    r.phase = "idle"
    isl.close()


def test_island_hit_targets_and_clicks(qapp, look_default):
    r, isl = make_island(qapp, look_default)
    r.demo()
    isl.grab()                                     # paints, which records the header button rects
    assert set(isl.tap_rects) == {"stop", "cancel", "eye"}
    for name, rect in isl.tap_rects.items():
        assert isl.hit(rect.center()) == name
    calls = []
    r.stop = lambda: calls.append("stop")
    r.cancel_tapped = lambda: calls.append("cancel")
    hidden = r.hide_live
    isl.click(isl.hit(isl.tap_rects["eye"].center()))
    assert r.hide_live is not hidden
    isl.click("stop")
    isl.click("cancel")
    isl.click(None)
    assert calls == ["stop", "cancel"]
    r.set_hide_live(False)
    r.phase = "finishing"
    isl.grab()
    assert "stop" not in isl.tap_rects and "cancel" not in isl.tap_rects   # saving: only the eye is live
    r.phase = "idle"
    isl.close()


def test_island_sizes_and_position(qapp, look_default):
    from timmy.ui import island as I
    r, isl = make_island(qapp, look_default)
    scr = isl.screen_for_placement().geometry()
    assert isl.y() == scr.y() and abs(isl.x() + isl.width() // 2 - (scr.x() + scr.width() // 2)) <= 1
    assert (isl.width(), isl.height()) == (I.PILL_W, I.PILL_H)
    r.demo()
    assert (isl.width(), isl.height()) == (I.CARD_W, I.CARD_H)
    r.set_hide_live(True)
    assert isl.height() == I.COMPACT_H
    flags = isl.windowFlags()
    from PySide6.QtCore import Qt
    for f in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.WindowDoesNotAcceptFocus,
              Qt.WindowType.Tool):
        assert flags & f
    r.set_hide_live(False)
    r.phase = "idle"
    isl.close()


@pytest.fixture
def calls_folder(tmp_path, app_dir):
    rec = tmp_path / "טימי - הקלטות"
    rec.mkdir()
    stem = "2026-11-17 10-30 דנה כהן - הצעת מחיר לאתר חנות"
    shutil.copyfile(SAMPLE, rec / (stem + ".md"))
    (rec / (stem + " - תמלול.txt")).write_text("[00:00:01] צד שני: שלום", encoding="utf-8")
    (rec / "2026-11-18 16-05 פגישת היכרות.md").write_text(
        "# פגישת היכרות\n\n**תאריך:** 2026-11-18 16:05 · **אורך:** 12:40 דק׳\n\n## סיכום\n\nשיחה ראשונה על שיתוף פעולה.\n\n"
        "## משימות\n\n- [ ] לשלוח מצגת\n", encoding="utf-8")
    (rec / "2026-11-15 09-00 שיחת מעקב.md").write_text("# שיחת מעקב\n\nקצר.\n", encoding="utf-8")
    (app_dir / "config.json").write_text(json.dumps({"output_dir": str(rec)}, ensure_ascii=False), encoding="utf-8")
    return rec


def test_calls_window_screens(qapp, screens, calls_folder, monkeypatch):
    monkeypatch.setenv("TIMMY_DRY_OPEN", "1")
    from timmy.ui import theme as T
    from timmy.ui.calls_window import CallsWindow
    w = CallsWindow()
    w.resize(1100, 760)
    w.show()
    qapp.processEvents()
    assert [c.title for c in w.calls] == ["פגישת היכרות", "הצעת מחיר לאתר חנות", "שיחת מעקב"]
    assert w.list.count() == 3 and w.selected().title == "פגישת היכרות"
    save_shot(w, screens / "calls-dark.png")
    w.select(w.calls[1].id)
    assert "דנה כהן" in w.selected().person
    assert "החלטות" in w.summary.toPlainText() and "# " not in w.summary.toPlainText()
    wait_until(qapp, lambda: False, 0.4)          # let the header relayout settle (else the shot catches a cut title)
    save_shot(w, screens / "calls-dark-with-person.png")
    w.search.setText("מעקב")
    assert w.list.count() == 1
    w.search.setText("אין כזה")
    assert w.list.count() == 0 and not w.no_match.isHidden()
    w.search.setText("")
    w.start_rename()
    assert w.editor.isVisibleTo(w) and w.title_edit.text() == "הצעת מחיר לאתר חנות"
    save_shot(w, screens / "calls-rename.png")
    w.title_edit.setText("הצעת מחיר: חנות אונליין")
    w.commit_rename()
    assert not w.renaming and w.selected().title == "הצעת מחיר: חנות אונליין"
    assert os.path.exists(calls_folder / "2026-11-17 10-30 דנה כהן - הצעת מחיר חנות אונליין.md")
    assert os.path.exists(calls_folder / "2026-11-17 10-30 דנה כהן - הצעת מחיר חנות אונליין - תמלול.txt")
    w.start_rename()
    w.title_edit.setText("פגישת היכרות")       # another call has a different stamp: allowed
    w.title_edit.setText("   ")
    assert not w.save_btn.isEnabled()
    w.cancel_rename()
    w.reveal()
    w.open_transcript()
    from timmy import paths
    log = (paths.app_dir() / "dry-open.log").read_text(encoding="utf-8")
    assert "reveal: " in log and "תמלול.txt" in log
    T.apply({"background_color": "#FFFFFF", "accent_color": "#22C55E"})
    qapp.processEvents()
    save_shot(w, screens / "calls-light.png")
    T.apply({"reset": True})
    w.close()


def test_calls_window_pdf_and_empty(qapp, screens, calls_folder, monkeypatch, out_dir):
    monkeypatch.setenv("TIMMY_DRY_OPEN", "1")
    monkeypatch.setenv("TIMMY_CLAUDE_SEARCH", "")
    from timmy.ui.calls_window import CallsWindow
    w = CallsWindow()
    w.resize(1100, 760)
    w.show()
    w.select(w.calls[1].id)
    cid = w.selected_id
    w.make_pdf()
    assert w.pdf_state[cid] == ("working",) and not w.pdf_btn.isEnabled()
    save_shot(w, screens / "calls-pdf-working.png")
    assert wait_until(qapp, lambda: w.pdf_state.get(cid, ("working",))[0] != "working", 150)
    assert w.pdf_state[cid][0] == "done", w.pdf_state[cid]
    assert os.path.exists(w.pdf_state[cid][1]) and w.pdf_open.isVisibleTo(w)
    assert "בעיצוב בסיסי" in w.pdf_line.text()
    save_shot(w, screens / "calls-pdf-done.png")
    w.close()
    for f in os.listdir(calls_folder):
        os.remove(calls_folder / f)
    w2 = CallsWindow()
    w2.show()
    qapp.processEvents()
    assert w2.calls == [] and w2.empty.isVisibleTo(w2) and not w2.main.isVisibleTo(w2)
    save_shot(w2, screens / "calls-empty.png")
    w2.close()


def test_share_hider_covers_every_window(qapp, app_dir):
    from PySide6.QtWidgets import QMenu, QWidget
    from timmy.ui.app import ShareHider
    seen = []
    hider = ShareHider(True, setter=lambda hwnd, hide: (seen.append((hwnd, hide)), True)[1])
    qapp.installEventFilter(hider)
    try:
        r, isl = make_island(qapp)
        win = QWidget()
        win.show()
        menu = QMenu()
        menu.addAction("x")
        menu.popup(isl.pos())
        qapp.processEvents()
        hwnds = {int(isl.winId()), int(win.winId()), int(menu.winId())}
        assert hwnds <= {h for h, on in seen if on}
        child = QWidget(win)
        child.show()
        qapp.processEvents()
        assert int(child.winId()) not in {h for h, _ in seen}     # only top-level windows
        seen.clear()
        hider.set_hide(False)
        assert hwnds <= {h for h, on in seen if on is False}
        menu.close()
        win.close()
        isl.close()
    finally:
        qapp.removeEventFilter(hider)


def test_app_menu_and_tray(qapp, app_dir, screens, monkeypatch):
    monkeypatch.setenv("TIMMY_DRY_OPEN", "1")
    from timmy import config as tconfig
    from timmy.ui.app import TimmyApp
    t = TimmyApp(qapp)
    try:
        titles = [a.text() for a in t.menu.actions() if not a.isSeparator()]
        assert titles[:2] == ["הסתר תמלול חי", "להסתיר את טימי בשיתוף מסך"]
        assert "השיחות שלי…" in titles and "יציאה" == titles[-1]
        assert "\u2014" not in "".join(titles)
        share = next(a for a in t.menu.actions() if a.text() == "להסתיר את טימי בשיתוף מסך")
        assert share.isCheckable() and share.isChecked()
        share.setChecked(False)
        assert tconfig.load()["hide_in_screen_share"] is False and t.hider.hide is False
        t.rec.demo()
        t.build_menu()
        sub = t.menu.actions()[0]
        assert sub.text() == "בטל הקלטה" and [a.text() for a in sub.menu().actions()] == ["ההקלטה תימחק ולא תישמר", "כן, למחוק את ההקלטה"]
        t.menu.popup(t.island.pos())
        qapp.processEvents()
        save_shot(t.menu, screens / "menu-recording.png")
        t.menu.close()
        t.rec.phase = "idle"
        t.open_calls()
        assert t.calls_window.isVisible()
        t.handle_message("show")
        t.calls_window.close()
    finally:
        t.island.close()
        t.tray.hide()


def test_app_process_smoke(tmp_path, app_dir):
    """`python -m timmy` starts and stays up; a second launch hands over ("show") and exits 0 at once."""
    import subprocess
    import sys
    import time
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "TIMMY_INSTANCE_NAME": f"timmy-smoke-{os.getpid()}",
           "TIMMY_APP_DIR": str(app_dir), "PYTHONPATH": str(WIN)}
    log = open(tmp_path / "app.log", "wb")
    p = subprocess.Popen([sys.executable, "-m", "timmy", "--background"], cwd=WIN, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(5)
        assert p.poll() is None, (tmp_path / "app.log").read_text(errors="replace")
        t = time.time()
        r = subprocess.run([sys.executable, "-m", "timmy"], cwd=WIN, env=env, capture_output=True, timeout=60)
        assert r.returncode == 0 and time.time() - t < 30
        assert p.poll() is None
    finally:
        p.kill()
        p.wait(10)
        log.close()
