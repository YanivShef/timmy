"""The WhatsApp send flow (integrations/whatsapp.py): draft -> review -> confirm -> send, against the Greenolink mock
(direct) and through a fake Claude Code (the MCP fallback). The rule "never send without the confirm click" is checked
both by behaviour (the mock's log has no WOULD_SEND before confirm_send) and by structure (who calls what)."""
import ast
import json
import os
from pathlib import Path

import pytest

from tests.conftest import claude_calls
from tests.test_greenolink import browser, mock  # noqa: F401 (fixture)
from timmy.integrations import greenolink as G
from timmy.integrations import whatsapp as W
from timmy.ui import calls as C

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = """# קביעת פגישה

**תאריך:** 2026-11-20 10:30 · **אורך:** 12:04 דק׳ · **עם:** [[דנה כהן]]

## סיכום

דנה רוצה לקבוע פגישה ביום שלישי בעשר כדי לעבור על הצעת המחיר לאתר.

## משימות

- [ ] לשלוח זימון (אני)
"""


@pytest.fixture
def call(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    md = d / "2026-11-20 10-30 דנה כהן - קביעת פגישה.md"
    md.write_text(SUMMARY, encoding="utf-8")
    (d / "2026-11-20 10-30 דנה כהן - קביעת פגישה - תמלול.txt").write_text("[00:01] אני: שלום\n[00:03] דנה כהן: היי\n", encoding="utf-8")
    return C.load_call(str(md))


def fake_pdf(md, dest, done):
    Path(dest).write_bytes(b"%PDF-1.4\n" + b"1" * 2048)
    done(True)


# ---- pure ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,on", [
    ("תשלח לה גם את ה-PDF", True), ("תצרף את הסיכום", True), ("עם הקובץ המצורף", True), ("שלח את המסמך", True),
    ("בלי ה-PDF", False), ("לא לצרף קובץ", False), ("אל תשלח את המסמך", False), ("תזכיר לה את התשלום", False),
    ("תשלח את הפי די אף", True), ("without the pdf please", False),
])
def test_mentions_file(text, on):
    assert W.mentions_file(text) is on


def test_numbers_and_labels():
    assert W.is_number("050-123-4567") and W.is_number("+972 50 123 4567") and not W.is_number("דנה 0501234567")
    c = G.Candidate("דנה כהן", "972501234567@s.whatsapp.net", "972501234567")
    assert W.recipient_label(c) == "דנה כהן · 050-123-4567"
    assert W.recipient_label(G.Candidate("0501234567", "0501234567", "0501234567")) == "0501234567"


def test_clean_draft_drops_preambles(call, app_dir):
    s = W.SendSession(call, "missing", cfg={"owner_name": "רוני לוי"})
    assert s.clean_draft("הנה ההודעה:\nהיי דנה, נתראה ביום שלישי.") == "היי דנה, נתראה ביום שלישי."
    assert s.clean_draft("רוני, הנה הנוסח המעודכן: היי דנה, נתראה.") == "היי דנה, נתראה."
    assert s.clean_draft("```\nהיי " + chr(0x2014) + " דנה\n```") == "היי - דנה"
    assert s.clean_draft('"היי דנה"') == "היי דנה"


def test_prompts_carry_the_rules(call, app_dir):
    s = W.SendSession(call, "missing", cfg={"owner_name": "רוני"})
    assert s.recipient == "דנה כהן"
    p = s.draft_prompt()
    assert p.startswith("נסח הודעת וואטסאפ לדנה כהן עם סיכום השיחה") and "דנה רוצה לקבוע פגישה" in p and "[00:03] דנה כהן: היי" in p
    assert "אל תמציא עובדות" in p and "בקול של רוני" in p
    s.kind = "free"
    s.set_request("תזכיר לה את ה-PDF בעדינות")
    assert s.attach_pdf and "יישלח קובץ PDF" in s.draft_prompt()
    s.touch_pdf(False)
    s.set_request("תזכיר לה את ה-PDF")             # the switch was touched: the text no longer flips it
    assert not s.attach_pdf and "לא מצורף שום קובץ" in s.draft_prompt()
    r = s.draft_prompt(revise=["קצר יותר", "עוד יותר"], current="טיוטה")
    assert "2. עוד יותר   <- הבקשה החדשה" in r and "<<<טיוטה\nטיוטה\nטיוטה>>>" in r


# ---- direct (Timmy's own Greenolink token) -----------------------------------------------------------------------
def test_direct_flow_never_sends_before_confirm(call, app_dir, fake_claude, mock):   # noqa: F811
    gl = G.Greenolink()
    gl.connect(browser, timeout=20)
    s = W.SendSession(call, "linked", gl, cfg={"owner_name": "רוני"})
    s.make_draft()
    assert s.stage == ("review",)
    assert s.draft.startswith("היי, סיכמנו") and "הנה ההודעה" not in s.draft
    assert s.chosen is not None and s.chosen.name == "דנה כהן"        # resolved while drafting (search only)
    draft_call = [c for c in claude_calls(fake_claude[1]) if c["kind"] == "draft"][0]
    assert draft_call["argv"][:4] == ["-p", "--model", "sonnet", "--tools"] and "--strict-mcp-config" in draft_call["argv"]
    # an edit by hand + a revision request
    s.draft = s.draft + " (נערך)"
    s.revision = "קצר יותר"
    s.revise()
    assert s.draft == "היי, נפגש ביום שלישי ב-10." and s.revisions == ["קצר יותר"] and s.can_undo_revision
    rev = [c for c in claude_calls(fake_claude[1]) if c["kind"] == "draft"][-1]["prompt"]
    assert "(נערך)" in rev and "1. קצר יותר   <- הבקשה החדשה" in rev
    s.undo_revision()
    assert s.draft.endswith("(נערך)")
    s.resolve_recipient()
    assert s.stage[0] == "confirm" and s.stage[1].name == "דנה כהן"
    assert mock.events("WOULD_SEND_TEXT") == []                       # still nothing sent
    s.confirm_send(G.Candidate("מישהו אחר", "972541112233@s.whatsapp.net", "972541112233"))
    assert mock.events("WOULD_SEND_TEXT") == [] and s.stage[0] == "confirm"     # only the candidate that is shown
    s.confirm_send(s.stage[1])
    assert s.stage == ("sent", "דנה כהן · 050-123-4567")
    sent = mock.events("WOULD_SEND_TEXT")
    assert len(sent) == 1 and sent[0]["message"] == s.draft and sent[0]["to"] == "972501234567@s.whatsapp.net"
    s.confirm_send(s.stage[1] if len(s.stage) > 1 and isinstance(s.stage[1], G.Candidate) else G.Candidate("x", "y", "z"))
    assert len(mock.events("WOULD_SEND_TEXT")) == 1                   # no second send from a stale click


def test_direct_pick_when_several_match_and_pdf(call, app_dir, fake_claude, mock):   # noqa: F811
    gl = G.Greenolink()
    gl.connect(browser, timeout=20)
    mock.mode(multi=True)
    s = W.SendSession(call, "linked", gl, build_pdf=fake_pdf)
    s.kind = "pdf"
    s.make_draft()
    assert s.stage == ("review",) and s.pdf_path == call.pdf_path and os.path.exists(call.pdf_path)
    assert s.chosen is None and len(s.resolved) == 2
    s.resolve_recipient()
    assert s.stage[0] == "pick" and [c.name for c in s.stage[1]] == ["דנה כהן", "דנה כהן סטודיו"]
    s.pick(s.stage[1][1])
    assert s.stage[0] == "confirm" and mock.events("WOULD_SEND_TEXT") == []
    s.confirm_send(s.stage[1])
    assert s.stage[0] == "sent"
    assert mock.events("WOULD_SEND_TEXT")[-1]["to"].startswith("972527654321")
    f = mock.events("WOULD_SEND_FILE")[-1]
    assert f["is_pdf"] and f["filename"].endswith(".pdf") and f["to"].startswith("972527654321")


def test_direct_number_recipient_and_failures(call, app_dir, fake_claude, mock):   # noqa: F811
    gl = G.Greenolink()
    gl.connect(browser, timeout=20)
    s = W.SendSession(call, "linked", gl)
    s.recipient = "054-111-2233"
    s.make_draft()
    s.resolve_recipient()
    assert s.stage[0] == "confirm" and s.stage[1].jid == "0541112233"
    mock.mode(fail_send=True)
    s.confirm_send(s.stage[1])
    assert s.stage[0] == "failed" and "ההודעה לא נשלחה" in s.stage[1]
    s.recipient = "אין כזה איש קשר"
    s.set_stage("review")
    s.resolve_recipient()
    assert s.stage == ("failed", "לא מצאתי איש קשר בשם אין כזה איש קשר. אפשר לכתוב מספר טלפון")


def test_free_request_needs_text(call, app_dir, fake_claude):
    s = W.SendSession(call, "linked", None)
    s.kind = "free"
    s.make_draft()
    assert s.stage == ("failed", "תכתוב לטימי מה לנסח ומה לשלוח")


def test_pending_revision_blocks_nothing_sends(call, app_dir, fake_claude, mock):   # noqa: F811
    gl = G.Greenolink()
    gl.connect(browser, timeout=20)
    s = W.SendSession(call, "linked", gl)
    s.make_draft()
    s.draft = ""
    s.resolve_recipient()
    assert s.stage == ("failed", "ההודעה ריקה") and mock.events("WOULD_SEND_TEXT") == []


# ---- the Claude Code fallback ------------------------------------------------------------------------------------
def test_fallback_through_claude(call, app_dir, fake_claude, monkeypatch, mem_secrets):
    monkeypatch.setenv("FAKE_CLAUDE_MCP_LIST", "claude.ai whatsapp: https://mcp.greenolink.com/mcp - ✔ Connected")
    wa = W.check_wa({})
    assert isinstance(wa, tuple) and wa[0] == "connected" and wa[1].tool_prefix == "mcp__claude_ai_whatsapp__"
    assert W.check_wa({"greenolink_fallback_off": True}) == "missing"
    s = W.SendSession(call, wa, None, cfg={})
    s.make_draft()
    assert s.stage == ("review",) and s.chosen.name == "דנה כהן"
    search = [c for c in claude_calls(fake_claude[1]) if c["kind"] == "search"][0]
    assert "--allowedTools" in search["argv"] and search["argv"][search["argv"].index("--allowedTools") + 1] == "mcp__claude_ai_whatsapp__search_contacts"
    assert "אסור לשלוח שום דבר" in search["prompt"]
    assert not [c for c in claude_calls(fake_claude[1]) if c["kind"] == "send"]
    s.resolve_recipient()
    s.confirm_send(s.stage[1])
    assert s.stage[0] == "sent"
    send = [c for c in claude_calls(fake_claude[1]) if c["kind"] == "send"]
    assert len(send) == 1
    a = send[0]["argv"]
    assert a[a.index("--allowedTools") + 1] == "mcp__claude_ai_whatsapp__*" and "Bash(curl:*)" not in a
    assert "<<<הודעה\n" + s.draft.strip() + "\nהודעה>>>" in send[0]["prompt"]
    assert "הנמען הוא 972501234567@s.whatsapp.net" in send[0]["prompt"]


def test_check_wa_without_claude(app_dir, monkeypatch, mem_secrets):
    monkeypatch.setenv("TIMMY_CLAUDE_SEARCH", "")
    assert W.check_wa({}) == "missing"
    G.save_store({"access_token": "a", "refresh_token": "r"})
    assert W.check_wa({}) == "linked"


@pytest.fixture
def mem_secrets():
    from timmy.integrations import secrets
    store = secrets.use_memory({})
    yield store
    secrets._BACKEND.clear()


# ---- structure: sending is reachable only from the confirm click -------------------------------------------------
def _calls_in(tree, names):
    """{function name: [called attribute/function names in `names`]} for every def in the tree."""
    out = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hits = []
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    f = n.func
                    nm = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
                    if nm in names:
                        hits.append(nm)
                elif isinstance(n, ast.Attribute) and n.attr in names and not isinstance(getattr(n, "ctx", None), ast.Store):
                    hits.append(n.attr)
            if hits:
                out.setdefault(fn.name, []).extend(hits)
    return out


def test_send_only_from_confirm_click():
    src = {p: ast.parse(p.read_text(encoding="utf-8")) for p in (ROOT / "timmy").rglob("*.py") if "_mac" not in p.parts}
    send_api = {"send_text", "send_file"}
    for p, tree in src.items():
        users = _calls_in(tree, send_api)
        users = {k: v for k, v in users.items() if not (p.name == "greenolink.py" and k in send_api)}   # the definitions
        if p.name == "whatsapp.py":
            assert set(users) == {"_send_direct"}, users
        else:
            assert not users, (p, users)
    for p, tree in src.items():
        users = _calls_in(tree, {"_send_direct", "_send_via_claude"})
        if p.name == "whatsapp.py":
            assert set(users) == {"confirm_send"}, users
        else:
            assert not users, (p, users)
    for p, tree in src.items():
        users = _calls_in(tree, {"confirm_send"})
        if p.name == "send_whatsapp.py":
            assert set(users) == {"_confirm_clicked"}, users
        else:
            assert not users, (p, users)
    # the Claude send prompt is only built inside _send_via_claude
    for p, tree in src.items():
        users = _calls_in(tree, {"send_prompt", "send_args"})
        if p.name == "whatsapp.py":
            assert set(users) == {"_send_via_claude"}, users
        else:
            assert not users, (p, users)
    # and the dialog connects _confirm_clicked to exactly one button: "שלח עכשיו"
    sw = (ROOT / "timmy" / "ui" / "send_whatsapp.py").read_text(encoding="utf-8")
    assert sw.count("self._confirm_clicked") == 1 and 'self._btn("שלח עכשיו", self._confirm_clicked' in sw


def test_mock_contacts_are_fictional():
    """The copied mock may only hold placeholder data (the repo is public)."""
    txt = (Path(__file__).parent / "mock_greenolink.py").read_text(encoding="utf-8")
    nums = set(__import__("re").findall(r"972\d{9}", txt))
    assert nums <= {"972501234567", "972527654321", "972541112233"}
    assert json.dumps(sorted(nums))
