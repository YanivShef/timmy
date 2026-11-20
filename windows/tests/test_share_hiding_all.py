"""QA: every window the owner can open is hidden from screen sharing (WDA_EXCLUDEFROMCAPTURE), not only the island.

1. Any OS (offscreen): the real TimmyApp opens every window (island + recording card, menu, tooltip, "השיחות שלי",
   the WhatsApp send dialog, both chats, "עיצוב ה-PDF", the onboarding); every visible top-level window must have been
   given hide=True, and the menu switch must turn all of them off and back on.
2. Windows only (native QPA, own process, tests/share_probe_app.py): GetWindowDisplayAffinity on every visible
   top-level window the process owns must read 0x11, including the native folder picker.
"""
import json
import os
import subprocess
import sys

import pytest

from tests.conftest import WIN, wait_until

pytestmark = pytest.mark.ui


@pytest.fixture
def rec_with_call(app_dir, tmp_path):
    from timmy import config as tconfig
    d = tmp_path / "rec"
    d.mkdir()
    (d / "2026-11-20 10-30 דנה כהן - קביעת פגישה.md").write_text(
        "# קביעת פגישה\n\n## סיכום\n\nשיחה קצרה.\n\n## משימות\n\n- [ ] לשלוח הצעה\n", encoding="utf-8")
    tconfig.save({"output_dir": str(d), "owner_name": "רוני", "onboarding_done": True})
    return d


def test_every_app_window_gets_the_share_affinity(qapp, rec_with_call, fake_claude, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QToolTip

    from timmy.winsys import affinity
    calls = []
    monkeypatch.setattr(affinity, "set_hidden", lambda hwnd, hide=True: (calls.append((int(hwnd), bool(hide))), True)[1])
    monkeypatch.setenv("TIMMY_DRY_OPEN", "1")
    from timmy.ui.app import TimmyApp
    t = TimmyApp(qapp, background=True)
    opened = []
    try:
        t.rec.demo()
        t.open_calls()
        w = t.calls_window
        assert w.selected() is not None
        w.whatsapp_btn.click()
        t.open_improve()
        t.open_design()
        t.open_pdf_design()
        t.open_onboarding()
        QToolTip.showText(QPoint(200, 200), "טימי", w)
        t.popup_menu(QPoint(100, 100))
        wait_until(qapp, lambda: t.menu.isVisible(), 2)
        visible = [x for x in QApplication.topLevelWidgets() if x.isVisible()]
        opened = visible
        names = {type(x).__name__ for x in visible}
        for must in ("Island", "CallsWindow", "SendDialog", "TimmyChatWindow", "PdfDesignWindow", "OnboardingWindow"):
            assert must in names, (must, names)
        if t.menu.isVisible():                  # offscreen, a popup can be closed at once by another test's leftovers
            assert (int(t.menu.winId()), True) in calls
        hidden = {h for h, on in calls if on}
        missing = [type(x).__name__ for x in visible if int(x.winId()) not in hidden]
        assert not missing, f"windows never given WDA_EXCLUDEFROMCAPTURE: {missing}"

        # the menu switch: all off, then all back on
        calls.clear()
        t.set_hide_in_share(False)
        off = {h for h, on in calls if not on}
        assert all(int(x.winId()) in off for x in visible if x.isVisible())
        calls.clear()
        t.set_hide_in_share(True)
        on = {h for h, on in calls if on}
        assert all(int(x.winId()) in on for x in visible if x.isVisible())
        # a window opened after the switch was turned back on is hidden as well
        t.calls_window.hide()
        calls.clear()
        t.open_calls()
        assert wait_until(qapp, lambda: (int(t.calls_window.winId()), True) in calls, 2)
    finally:
        QToolTip.hideText()
        t.menu.close()
        for x in opened:
            x.close()
        t.island.close()
        t.tray.hide()


@pytest.fixture(scope="module")
def native_probe(tmp_path_factory, out_dir):
    if sys.platform != "win32":
        pytest.skip("needs a Windows host")
    tmp = tmp_path_factory.mktemp("share")
    appd = tmp / "Timmy"
    rec = tmp / "rec"
    appd.mkdir()
    rec.mkdir()
    (rec / "2026-11-20 10-30 דנה כהן - קביעת פגישה.md").write_text("# קביעת פגישה\n\n## סיכום\n\nשיחה קצרה.\n", encoding="utf-8")
    (appd / "config.json").write_text(json.dumps({"output_dir": str(rec), "onboarding_done": True}, ensure_ascii=False),
                                      encoding="utf-8")
    shots = out_dir / "screens"
    shots.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(WIN), "TIMMY_APP_DIR": str(appd), "TIMMY_DRY_OPEN": "1",
           "TIMMY_INSTANCE_NAME": f"timmy-share-probe-{os.getpid()}", "TIMMY_CLAUDE_SEARCH": str(tmp / "no-claude")}
    env.pop("QT_QPA_PLATFORM", None)
    r = subprocess.run([sys.executable, str(WIN / "tests" / "share_probe_app.py"), str(shots)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180, env=env, cwd=WIN)
    print(r.stdout[-6000:], r.stderr[-3000:])
    lines = [x for x in r.stdout.splitlines() if x.startswith("{")]
    assert lines, f"probe printed nothing (rc {r.returncode}): {r.stderr[-2000:]}"
    res = json.loads(lines[-1])
    (out_dir / "share-probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


@pytest.mark.windows_only
def test_native_every_visible_window_reads_exclude_from_capture(native_probe):
    res = native_probe
    assert res["error"] is None, res
    assert res["supported"], "WDA_EXCLUDEFROMCAPTURE not supported on this Windows build"
    assert len(res["windows"]) >= 6, res          # island, calls, send, 2 chats, pdf design, onboarding, menu, tooltip
    bad = [w for w in res["windows"] if w["affinity"] != 0x11]
    assert not bad, f"visible Timmy windows that a screen share would show: {bad}"


@pytest.mark.windows_only
def test_native_folder_picker_is_hidden_too(native_probe):
    """The onboarding / chat file pickers are the native Windows dialog (not a Qt widget), so the event filter never
    sees them. A picker opened during a call would show in Meet's "share entire screen"."""
    res = native_probe
    assert res["dialog"], f"the folder picker did not open: {res}"
    bad = [w for w in res["dialog"] if w["affinity"] != 0x11]
    assert not bad, f"native file dialog is visible in screen sharing: {bad}"
