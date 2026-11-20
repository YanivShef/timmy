"""Build step 4 UI offscreen: the chat windows, "עיצוב ה-PDF", "שליחה בוואטסאפ" in every stage and the onboarding
steps, each saved as PNG in out/screens (CI uploads them), plus the app wiring (menu items, the WhatsApp button,
"no space" opens the folder step). Onboarding checks (onboard.py) run against the fake Claude Code and a fake mic."""
import json
import os
from pathlib import Path

import pytest

from tests.conftest import FIXTURES, save_shot, wait_until
from tests.test_greenolink import browser, mock  # noqa: F401 (fixture)
from tests.test_whatsapp import SUMMARY

pytestmark = pytest.mark.ui
LONG_DASH = chr(0x2014)


@pytest.fixture
def mem_secrets():
    from timmy.integrations import secrets
    store = secrets.use_memory({})
    yield store
    secrets._BACKEND.clear()


@pytest.fixture
def rec_dir(app_dir, tmp_path):
    from timmy import config as tconfig
    d = tmp_path / "rec"
    d.mkdir()
    md = d / "2026-11-20 10-30 דנה כהן - קביעת פגישה.md"
    md.write_text(SUMMARY, encoding="utf-8")
    (d / "2026-11-20 10-30 דנה כהן - קביעת פגישה - תמלול.txt").write_text("[00:01] רוני: שלום\n", encoding="utf-8")
    tconfig.save({"output_dir": str(d), "owner_name": "רוני"})
    return d


def texts_of(w):
    from PySide6.QtWidgets import QAbstractButton, QLabel
    out = [x.text() for x in w.findChildren(QLabel) if x.isVisibleTo(w)]
    out += [x.text() for x in w.findChildren(QAbstractButton) if x.isVisibleTo(w)]
    return "\n".join(out)


# ---- chats -------------------------------------------------------------------------------------------------------
def test_improve_chat_window(qapp, rec_dir, fake_claude, screens, monkeypatch):
    from timmy.chat import memory
    from timmy.ui.chat_window import TimmyChatWindow
    w = TimmyChatWindow(False)
    w.show()
    qapp.processEvents()
    assert w.m.msgs[0].text == w.m.intro and w.sugg.count() == 2
    save_shot(w, screens / "chat-improve-empty.png")
    monkeypatch.setenv("FAKE_CLAUDE_CHAT", json.dumps({"reply": "מעכשיו הנקודות יהיו קצרות, עד 8 מילים.", "actions": [
        {"type": "add_instruction", "text": "נקודות קצרות, עד 8 מילים"}, {"type": "remember", "text": "העסק נקרא סטודיו טל"}]}, ensure_ascii=False))
    w.input.setText("נקודות קצרות יותר, עד 8 מילים")
    w.submit()
    assert wait_until(qapp, lambda: w.m.busy is None and len(w.m.msgs) >= 3, 30)
    w.m.send("עיניים ירוקות")
    assert wait_until(qapp, lambda: w.scroll.texts()[-1].startswith("צבעתי את העיניים"), 10), w.scroll.texts()
    w.toggle_panel()
    qapp.processEvents()
    t = texts_of(w.panel)
    assert "נקודות קצרות, עד 8 מילים" in t and "העסק נקרא סטודיו טל" in t and memory.load() == ["העסק נקרא סטודיו טל"]
    save_shot(w, screens / "chat-improve.png")
    # a long job: the typing bubble has "בטל"
    w.m.busy, w.m.cancel_action = "כותב את סקיל הניסוח שלך. זה לוקח כדקה…", lambda: None
    w.refresh()
    qapp.processEvents()
    assert hasattr(w.scroll.lay.itemAt(w.scroll.lay.count() - 2).widget(), "cancel_btn")
    save_shot(w, screens / "chat-improve-busy.png")
    w.m.busy, w.m.cancel_action = None, None
    w.close()


def test_design_chat_light_theme(qapp, rec_dir, fake_claude, screens):
    from timmy.ui import theme as T
    from timmy.ui.chat_window import TimmyChatWindow
    w = TimmyChatWindow(True)
    w.show()
    qapp.processEvents()
    assert not w.clip.isVisible()
    w.m.send("רקע לבן")
    qapp.processEvents()
    assert T.current().bg_is_light and "הטקסט יהיה כהה" in w.m.msgs[-1].text
    assert wait_until(qapp, lambda: "רקע ללבן" in "".join(w.scroll.texts()), 5)
    save_shot(w, screens / "chat-design-light.png")
    w.m.reset_looks()
    qapp.processEvents()
    assert T.current().is_default
    w.close()


def test_pdf_design_window_preview(qapp, rec_dir, fake_claude, screens):
    from timmy.ui.chat_window import PdfDesignWindow
    w = PdfDesignWindow()
    w.show()
    w.start()
    assert wait_until(qapp, lambda: w.m.busy is None and w.m.msgs[-1].image, 120), [m.text for m in w.m.msgs]
    assert Path(w.m.msgs[-1].image).exists() and Path(w.m.msgs[-1].pdf).exists()
    qapp.processEvents()
    save_shot(w, screens / "pdf-design.png")
    w.m.reset()
    assert wait_until(qapp, lambda: w.m.busy is None and w.m.msgs[-1].text == "זה כבר העיצוב המקורי", 60)
    qapp.processEvents()
    assert all(b.isEnabled() for b in (w.create_btn, w.undo_btn, w.reset_btn))
    w.close()


# ---- WhatsApp ----------------------------------------------------------------------------------------------------
def test_send_dialog_not_connected(qapp, rec_dir, fake_claude, screens, mem_secrets):
    from timmy.ui import calls as C
    from timmy.ui.send_whatsapp import SendDialog, WAState
    st = WAState()
    st.wa = "missing"
    c = C.list_calls(str(rec_dir))[0]
    d = SendDialog(c, state=st)
    d.show()
    qapp.processEvents()
    assert wait_until(qapp, lambda: st.wa == "missing", 20)
    d.refresh()
    assert d.connect_btn.isVisible() and not d.draft_btn.isEnabled()
    assert "כדי לשלוח בוואטסאפ צריך לחבר אותו פעם אחת" in d.conn_text.text()
    save_shot(d, screens / "wa-connect.png")
    st.link = "waiting"
    d.refresh()
    assert d.reopen_btn.isVisible() and d.cancel_conn_btn.isVisible() and not d.connect_btn.isVisible()
    save_shot(d, screens / "wa-waiting.png")
    d.reject()


def test_send_dialog_full_flow(qapp, rec_dir, fake_claude, screens, mock):   # noqa: F811
    from timmy.integrations import greenolink as G
    from timmy.ui import calls as C
    from timmy.ui.send_whatsapp import SendDialog, WAState
    st = WAState()
    st.connect(lambda fn, done: done(fn()), open_url=browser)        # one click: the mock "browser" approves
    assert st.wa == "linked" and G.has_token()
    c = C.list_calls(str(rec_dir))[0]
    d = SendDialog(c, state=st)
    d.show()
    qapp.processEvents()
    assert d.disconnect_btn.isVisible() and d.draft_btn.isEnabled() and d.recipient.text() == "דנה כהן"
    save_shot(d, screens / "wa-choose.png")
    d.kind_btns["free"].click()
    d.request.setPlainText("תזכיר לה בעדינות את ה-PDF")
    qapp.processEvents()
    assert d.attach.isChecked()
    save_shot(d, screens / "wa-free.png")
    d.attach.click()                                   # off: no file
    d.kind_btns["summary"].click()
    d.draft_btn.click()
    assert wait_until(qapp, lambda: d.s.stage == ("review",) and d.s.chosen is not None, 60), d.s.stage
    qapp.processEvents()
    assert d.draft.toPlainText().startswith("היי, סיכמנו") and d.send_btn.text() == "\u200fשלח ל-דנה כהן · 050-123-4567"
    save_shot(d, screens / "wa-review.png")
    d.revision.setText("קצר יותר")
    qapp.processEvents()
    assert not d.send_btn.isEnabled()                  # a written change request must be applied or deleted first
    d.revise_btn.click()
    assert wait_until(qapp, lambda: not d.s.revising and d.s.draft == "היי, נפגש ביום שלישי ב-10.", 60)
    qapp.processEvents()
    assert d.undo_btn.isVisible() and d.send_btn.isEnabled()
    d.send_btn.click()
    assert wait_until(qapp, lambda: d.s.stage[0] == "confirm", 30)
    qapp.processEvents()
    assert d.confirm.isVisible() and "דנה כהן · 050-123-4567" in d.confirm_to.text()
    assert mock.events("WOULD_SEND_TEXT") == []
    save_shot(d, screens / "wa-confirm.png")
    d.send_now.click()
    assert wait_until(qapp, lambda: d.s.stage[0] == "sent", 30), d.s.stage
    qapp.processEvents()
    sent = mock.events("WOULD_SEND_TEXT")
    assert len(sent) == 1 and sent[0]["message"] == "היי, נפגש ביום שלישי ב-10."
    assert "נשלח ל-דנה כהן" in d.result.text()
    save_shot(d, screens / "wa-sent.png")
    d.accept()


def test_send_dialog_pick_and_failure(qapp, rec_dir, fake_claude, screens, mock):   # noqa: F811
    from timmy.ui import calls as C
    from timmy.ui.send_whatsapp import SendDialog, WAState
    st = WAState()
    st.connect(lambda fn, done: done(fn()), open_url=browser)
    mock.mode(multi=True)
    d = SendDialog(C.list_calls(str(rec_dir))[0], state=st)
    d.show()
    d.draft_btn.click()
    assert wait_until(qapp, lambda: d.s.stage == ("review",) and d.s.resolved, 60)
    qapp.processEvents()
    d.send_btn.click()
    assert wait_until(qapp, lambda: d.s.stage[0] == "pick", 30)
    qapp.processEvents()
    assert "יש כמה אנשי קשר שמתאימים" in texts_of(d.pick)
    save_shot(d, screens / "wa-pick.png")
    d.s.set_stage("failed", "ההודעה לא נשלחה: WhatsApp not connected")
    qapp.processEvents()
    assert d.back_btn.isVisible()
    save_shot(d, screens / "wa-failed.png")
    assert mock.events("WOULD_SEND_TEXT") == []
    d.reject()


# ---- onboarding (checks) -----------------------------------------------------------------------------------------
def test_detect_claude_states(app_dir, fake_claude, monkeypatch):
    from timmy import config as tconfig
    from timmy import onboard as O
    st, path, _ = O.detect_claude({})
    assert st == "ok" and path == str(fake_claude[0]) and tconfig.load()["claude_path"] == str(fake_claude[0])
    tconfig.save({"claude_path": ""})
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "false")
    assert O.detect_claude({})[0] == "not_logged_in"
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "none")                    # an old version without "auth status"
    assert O.detect_claude({})[0] == "ok"
    monkeypatch.setenv("FAKE_CLAUDE_HEALTH", "auth")
    assert O.detect_claude({})[0] == "not_logged_in"
    monkeypatch.setenv("TIMMY_CLAUDE_SEARCH", "")
    assert O.detect_claude({})[0] == "not_installed"


@pytest.mark.parametrize("mode,ok,err", [("ok", True, ""), ("outdated", False, "ישנה מדי")])
def test_claude_login(app_dir, fake_claude, monkeypatch, mode, ok, err):
    import time
    from timmy import onboard as O
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN", mode)
    if mode == "outdated":
        monkeypatch.setenv("FAKE_CLAUDE_AUTH", "false")
    lg = O.ClaudeLogin(str(fake_claude[0])).start(timeout=30)
    t = time.time()
    while not lg.done and time.time() - t < 30:
        time.sleep(0.1)
    assert lg.done and lg.ok is ok and err in lg.error
    if ok:
        assert lg.url.startswith("https://claude.com/cai/oauth/authorize")


def test_claude_login_with_code(app_dir, fake_claude, monkeypatch):
    import time
    from timmy import onboard as O
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN", "code")
    lg = O.ClaudeLogin(str(fake_claude[0])).start(timeout=30)
    t = time.time()
    while not lg.url and time.time() - t < 20:
        time.sleep(0.1)
    lg.send_code("GOODCODE")
    while not lg.done and time.time() - t < 30:
        time.sleep(0.1)
    assert lg.ok


def test_login_failure_kinds():
    from timmy import onboard as O
    assert O.login_failure_kind("error: unknown option '--claudeai'", True) == "outdated"
    assert O.login_failure_kind("getaddrinfo ENOTFOUND api.anthropic.com", False) == "network"
    assert O.login_failure_kind("OAuth error: Invalid code", False) == "badcode"
    assert O.login_failure_kind("something", False) == "other"
    assert LONG_DASH not in "".join(O.login_failure_text(k) for k in ("outdated", "network", "badcode", "timeout", "other"))


def test_mic_test_with_fake_mic(app_dir):
    from timmy import onboard as O
    r = O.mic_test(2.0, extra_args=["--fake-mic", str(FIXTURES / "he_short.wav")])
    assert r["state"] == "ok" and r["peak"] > 0.01, r
    assert O.mic_consent() in ("unknown", "allow", "deny")


def test_folder_and_steps(app_dir, tmp_path):
    from timmy import config as tconfig
    from timmy import onboard as O
    d = tmp_path / "הקלטות חדשות"
    assert O.set_folder(str(d)) and d.is_dir() and tconfig.load()["output_dir"] == str(d)
    assert O.recordings_dir() == str(d) and O.free_gb(str(d)) > 0
    O.save_step(3)
    assert O.step_saved() == 3


# ---- onboarding (window) -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("step,env,expect", [
    (0, {}, "טימי מקליט את השיחות שלך"),
    (1, {"TIMMY_DEMO_MIC": "denied"}, "ווינדוס חוסם את המיקרופון"),
    (2, {"TIMMY_DEMO_CHROME": "0"}, "לא מצאתי את גוגל כרום"),
    (3, {"TIMMY_DEMO_CLAUDE": "expired", "TIMMY_DEMO_GIT": "0"}, "צריך להתחבר ל-Claude"),
    (3, {"TIMMY_DEMO_CLAUDE": "ok", "TIMMY_DEMO_GIT": "1"}, "Claude Code מותקן ומחובר"),
    (3, {"TIMMY_DEMO_CLAUDE": "missing", "TIMMY_DEMO_GIT": "0"}, "לא מצאתי את Claude Code"),
    (4, {}, "ההקלטות והסיכומים נשמרים ב"),
    (5, {}, "הכל מוכן"),
])
def test_onboarding_steps(qapp, rec_dir, screens, monkeypatch, step, env, expect):
    from timmy.ui.onboarding import OnboardingWindow
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    w = OnboardingWindow(start_step=step)
    w.show()
    qapp.processEvents()
    w.refresh()
    qapp.processEvents()
    t = texts_of(w.pages[step])
    assert expect in t, t
    assert LONG_DASH not in texts_of(w)
    tag = "-".join(f"{v}" for v in env.values()) or "default"
    save_shot(w, screens / f"onboarding-{step}-{tag}.png")
    if step == 3 and env.get("TIMMY_DEMO_CLAUDE") == "expired":
        assert w.login_btn.isVisible() and w.get_git.isVisible()
    if step == 1:
        assert w.mic_settings.isVisible()
    w.close()


def test_onboarding_mic_button(qapp, rec_dir, monkeypatch):
    from timmy.ui.onboarding import OnboardingWindow
    monkeypatch.setenv("TIMMY_DEMO_MIC", "ok")
    w = OnboardingWindow(start_step=1)
    w.show()
    w.mic_btn.click()
    assert wait_until(qapp, lambda: w.mic is not None, 20)
    w.refresh()
    assert "המיקרופון עובד" in w.line["mic"].text()
    w.go_next()
    assert w.step == 2
    from timmy import onboard as O
    assert O.step_saved() == 2
    w.show_step(5)
    w.go_next()
    from timmy import config as tconfig
    assert tconfig.load()["onboarding_done"] is True and not w.isVisible()


# ---- app wiring --------------------------------------------------------------------------------------------------
def test_app_step4_menu_and_buttons(qapp, rec_dir, fake_claude, monkeypatch):
    monkeypatch.setenv("TIMMY_DRY_OPEN", "1")
    from timmy import config as tconfig
    from timmy.ui.app import TimmyApp
    tconfig.save({"onboarding_done": True})
    t = TimmyApp(qapp)
    try:
        acts = {a.text(): a for a in t.menu.actions() if not a.isSeparator()}
        for k in ("שפר את טימי…", "עצב את טימי…", "הרשאות והגדרה (טימי)"):
            assert acts[k].isEnabled(), k
        acts["שפר את טימי…"].trigger()
        assert t.chats["improve"].isVisible()
        acts["עצב את טימי…"].trigger()
        assert t.chats["design"].isVisible() and t.chats["design"].design
        acts["הרשאות והגדרה (טימי)"].trigger()
        assert t.onboarding.isVisible() and t.onboarding.step == 0
        t._no_space()
        assert t.onboarding.step == 4
        t.open_calls()
        w = t.calls_window
        assert w.whatsapp_btn.text().endswith("שלח בוואטסאפ") and w.design_btn.isVisible()
        assert w.extra_actions.indexOf(w.whatsapp_btn) == 0
        t.open_pdf_design(send=None)
        assert t.chats["pdf"].isVisible()
        for x in list(t.chats.values()) + [t.onboarding, w]:
            x.close()
    finally:
        t.island.close()
        t.tray.hide()


def test_first_launch_opens_onboarding(qapp, app_dir, monkeypatch):
    monkeypatch.setenv("TIMMY_DRY_OPEN", "1")
    from timmy.ui.app import TimmyApp
    t = TimmyApp(qapp)
    try:
        assert wait_until(qapp, lambda: t.onboarding is not None and t.onboarding.isVisible(), 5)
        t.onboarding.close()
    finally:
        t.island.close()
        t.tray.hide()
    t2 = TimmyApp(qapp, background=True)
    try:
        qapp.processEvents()
        import time
        time.sleep(0.6)
        qapp.processEvents()
        assert t2.onboarding is None
    finally:
        t2.island.close()
        t2.tray.hide()


def test_no_personal_data_in_step4_strings():
    """Step 4 modules: no long dash, no emails (the repo is public); the vendored Mac texts excluded."""
    root = Path(__file__).resolve().parents[1] / "timmy"
    for p in list((root / "chat").glob("*.py")) + list((root / "integrations").glob("*.py")) + [root / "onboard.py"] + \
            [root / "ui" / n for n in ("chat_window.py", "send_whatsapp.py", "onboarding.py", "bridge.py")]:
        s = p.read_text(encoding="utf-8")
        assert LONG_DASH not in s, p
        assert "@gmail" not in s and "yaniv" not in s.lower().replace("yaniv_shef", ""), p
    assert os.path.exists(root / "ui" / "assets" / "timmy.png")
