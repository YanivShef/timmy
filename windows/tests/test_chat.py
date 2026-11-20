"""The chats ("שפר את טימי" / "עצב את טימי" / "עיצוב ה-PDF"), memory.json and linked / created Claude skills.
Tables pin the Mac rules (isSimpleLooks, parseAppearance, shade, colorWord, memory add / forget), flows run with a fake
Claude Code (tests/fake_claude.py) and a synchronous spawn."""
import json
import os
from pathlib import Path

import pytest

from tests.conftest import claude_calls
from timmy import config as tconfig
from timmy.chat import history, looks, memory, skills
from timmy.chat.history import ChatMsg
from timmy.chat.improve import TimmyChat
from timmy.chat.pdf_design import PDFDesign
from timmy.engine import procrun
from timmy.ui import theme as T

LONG_DASH = chr(0x2014)


# ---- looks fast path ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,recent,simple", [
    ("עיניים ירוקות", [], True),
    ("רקע כחול כהה", [], True),
    ("תחליף את הצבע לסגול", [], True),
    ("תחליף את הצבע לסגול", ["עיניים ירוקות"], False),     # the last messages talked about the eyes: Claude decides
    ("תשנה ללבן", [], False),                                # a color without a target goes to Claude
    ("רקע לא לבן", [], False),                               # negation next to a color
    ("כחול אבל לא כהה", [], False),
    ("תוריד אנימציות", [], True),
    ("בלי אנימציות", [], True),
    ("תפעיל אנימציות בכחול", [], False),
    ("רקע כחול ועיניים ירוקות", [], False),
    ("רקע כחול וגם עיניים", [], False),
    ("תעשה את הסיכום כחול", [], False),
    ("עיניים ירוקות או כחולות", [], False),
])
def test_is_simple_looks(text, recent, simple):
    assert looks.is_simple_looks(text, recent) is simple


@pytest.mark.parametrize("text,changes", [
    ("עיניים ירוקות", {"eye_color": "#22C55E"}),
    ("רקע כחול כהה", {"background_color": "#16315D"}),
    ("רקע ירוק בהיר", {"background_color": "#9CE5B7"}),
    ("עיניים צהובות בהירות", {"eye_color": "#FACC15"}),           # "בהירות" is not the word "בהיר" (Mac: the same)
    ("תחליף את הצבע לסגול", {"accent_color": "#A855F7"}),
    ("תוריד אנימציות", {"animations": False}),
    ("תחזיר את האנימציות המקוריות", {"animations": True}),
    ("תחזיר את הרקע המקורי", {"background_color": "#000000"}),
    ("עיניים מקוריות", {"eye_color": ""}),
    ("תאפס את המראה", {"reset": True}),
    ("כמו שהיה", {"reset": True}),
    ("רקע #1e3a8a", {"background_color": "#1E3A8A"}),
    ("כתום", {"accent_color": "#FFA046"}),
    ("קרקע", None),                                          # not "רקע", not a color
    ("תכתוב את הסיכום בגוף ראשון", None),
])
def test_parse_appearance(text, changes):
    assert looks.parse_appearance(text) == changes


def test_words_and_colors():
    assert looks.has_word("לרקע הזה", "רקע") and not looks.has_word("קרקע", "רקע") and not looks.has_word("ordered", "red", False)
    assert looks.color_word("#22C55E") == "לירוק" and looks.color_word("#FFA046") == "לכתום" and looks.color_word("#000000") == "לשחור"
    assert looks.color_word("#16315D") == "לכחול כהה" and looks.color_word("#123456") == "ל-#123456"
    assert looks.is_appearance_instruction("עיניים ירוקות") and looks.is_appearance_instruction("כחול")
    assert not looks.is_appearance_instruction("תכתוב את הסיכום בכחול")
    assert looks.is_appearance_instruction("טימי בכחול")
    assert looks.is_follow_up("גם את העיניים") and looks.is_follow_up("עוד יותר") and not looks.is_follow_up("עיניים ירוקות")
    assert looks.mentions_memory("תזכור שאני אוהב ירוק") and not looks.mentions_memory("עיניים ירוקות")
    assert looks.asks_last_call("תחיל על השיחה האחרונה") and looks.is_pdf_instruction("תשים לוגו במסמך")
    assert looks.valid_looks({"accent_color": "#abcdef", "eye_color": "", "background_color": "blue", "animations": "no", "x": 1,
                              "reset": True}) == {"accent_color": "#ABCDEF", "eye_color": "", "reset": True}


def test_apply_looks_sentences(app_dir):
    s = looks.apply_looks({"eye_color": "#22C55E"})
    assert s == "צבעתי את העיניים לירוק. זה כבר מאחורי החלון."
    assert tconfig.load()["eye_color"] == "#22C55E"
    s = looks.apply_looks({"background_color": "#F5F5F5"})
    assert s.startswith("שיניתי את הרקע ללבן.") and "הטקסט יהיה כהה כדי שיהיה קריא" in s
    s = looks.apply_looks({"eye_color": "#1E3A8A"})
    assert "על הבועה השחורה העיניים יהיו קצת בהירות יותר" in s
    assert looks.apply_looks({"reset": True}).startswith("חזרתי למראה המקורי")
    assert T.current().is_default
    assert LONG_DASH not in looks.look_state()


# ---- memory ------------------------------------------------------------------------------------------------------
def test_memory_rules(app_dir):
    assert memory.remember(["- העסק נקרא סטודיו דנה", "אוהב טון קליל " + LONG_DASH + " וישיר", "אב"]) == \
        ["העסק נקרא סטודיו דנה", "אוהב טון קליל - וישיר"]
    assert memory.remember(["העסק נקרא סטודיו דנה"]) == []          # case-insensitive duplicate
    assert memory.load() == ["העסק נקרא סטודיו דנה", "אוהב טון קליל - וישיר"]
    assert memory.forget("טון") is None                              # too short to match part of a note
    assert memory.forget("טון קליל") == "אוהב טון קליל - וישיר"
    memory.remember([f"פתק מספר {i}" for i in range(50)])
    n = memory.load()
    assert len(n) == 40 and n[-1] == "פתק מספר 49"
    assert memory.block().startswith("- ") and memory.forget("לא קיים בכלל") is None
    raw = json.loads((app_dir / "memory.json").read_text(encoding="utf-8"))
    assert list(raw) == ["notes"]


def test_memory_forget_picks_the_clear_one(app_dir):
    memory.remember(["אוהב צבע ירוק בהיר", "אוהב צבע ירוק כהה מאוד בכותרות"])
    assert memory.forget("צבע ירוק") is None                         # two notes match equally: nothing deleted
    assert memory.forget("ירוק כהה מאוד בכותרות") == "אוהב צבע ירוק כהה מאוד בכותרות"


def test_history_format_and_prompt_block(app_dir):
    p = app_dir / "chat-improve.json"
    msgs = [ChatMsg(False, "היי"), ChatMsg(True, "x" * 700, ctx="המראה לפני: א"), ChatMsg(False, "שגיאה", is_error=True)]
    history.save(p, msgs)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert set(raw[1]) == {"id", "fromUser", "text", "isError", "ctx"} and raw[1]["fromUser"] is True
    back = history.load(p)
    assert [m.text for m in back] == [m.text for m in msgs] and back[1].ctx == "המראה לפני: א"
    h = history.chat_history(back)
    assert h.split("\n")[1].startswith("משתמש: " + "x" * 600 + "… [המראה לפני: א]") and h.split("\n")[2] == "טימי (שגיאה): שגיאה"
    assert history.chat_history([]) == "(זו ההודעה הראשונה)"


# ---- skills ------------------------------------------------------------------------------------------------------
def write_skill(root: Path, rel: str, name: str, desc: str, body: str = "גוף הסקיל", extra=None):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8")
    for k, v in (extra or {}).items():
        (d / k).parent.mkdir(parents=True, exist_ok=True)
        (d / k).write_text(v, encoding="utf-8")
    return d


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    h = tmp_path / "claude home"
    write_skill(h, "skills/client-pdf", "client-pdf", "מעצב PDF ללקוחות בסגנון נקי", extra={"refs/colors.md": "כחול ולבן"})
    write_skill(h, "skills/guide-writer", "guide-writer", "\"כותב מדריכים, PDF ומסמכים\"")
    write_skill(h, "skills/synced/acc/voice", "voice", "|\n  הקול שלי\n  בהודעות")
    write_skill(h, "plugins/cache/mk/gravity/1.2.0/skills/seller-calls", "seller-calls", "old")
    write_skill(h, "plugins/cache/mk/gravity/1.10.0/skills/seller-calls", "seller-calls", "new version")
    monkeypatch.setenv("TIMMY_CLAUDE_HOME", str(h))
    return h


def test_frontmatter_variants():
    assert skills.frontmatter("---\nname: a\ndescription: \"q \\\"x\\\"\"\n---\n") == ("a", 'q "x"')
    assert skills.frontmatter("---\nname: 'b'\ndescription: >\n  שורה\n  שנייה\n---\n") == ("b", "שורה שנייה")
    assert skills.frontmatter("no front matter") == ("", "")


def test_list_find_suggest(claude_home, app_dir):
    lst = skills.list_claude_skills()
    names = [s.name for s in lst]
    assert names == ["client-pdf", "guide-writer", "voice", "gravity:seller-calls"]
    assert lst[3].description == "new version" and lst[2].source == "claude.ai" and lst[2].description == "הקול שלי בהודעות"
    assert [s.name for s in skills.find_skills("seller-calls")] == ["gravity:seller-calls"]
    assert [s.name for s in skills.find_skills("CLIENT-PDF")] == ["client-pdf"]
    assert [s.name for s in skills.find_skills("pdf")] == ["client-pdf"]
    p = str(claude_home / "skills" / "guide-writer" / "SKILL.md")
    assert [s.name for s in skills.find_skills(p)] == ["guide-writer"]
    assert [s.name for s in skills.suggest_skills("יש לי סקיל של מדריכים")] == ["guide-writer"]
    assert [s.name for s in skills.pdf_skill_candidates()] == ["client-pdf", "guide-writer"]
    md = skills.skill_markdown(str(claude_home / "skills" / "client-pdf"))
    assert "===== refs/colors.md =====\nכחול ולבן" in md


def test_link_heal_and_guide(claude_home, app_dir):
    s = skills.find_skills("client-pdf")[0]
    skills.set_linked_skill("writing", s)
    assert skills.linked_skill("writing") == ("client-pdf", s.dir)
    assert "כללי ניסוח מהסקיל client-pdf" in skills.skill_guide("writing") and "<<<הסקיל" in skills.skill_guide("writing")
    assert skills.linked_skills_line() == "עיצוב ה-PDF: אין · ניסוח: client-pdf"
    # the folder moved (a plugin update): found again by name, the new path saved
    os.rename(s.dir, str(claude_home / "skills" / "client-pdf-2"))
    write_skill(claude_home, "skills/client-pdf-2", "client-pdf", "moved")
    got = skills.linked_skill("writing")
    assert got[0] == "client-pdf" and got[1].endswith("client-pdf-2")
    assert tconfig.load()["linked_skills"]["writing"]["path"].endswith("client-pdf-2")
    skills.set_linked_skill("writing", None)
    assert skills.linked_skill("writing") is None and skills.skill_guide("writing") == ""


def test_slug_and_unique_dir(claude_home):
    assert skills.skill_slug("Studio Dana PDF", "x") == "studio-dana-pdf"
    assert skills.skill_slug("סטודיו", "fallback") == "fallback"
    assert skills.skill_slug("Café Ñoño", "x") == "cafe-nono"
    d = skills.unique_skill_dir("client-pdf")
    assert os.path.basename(d) == "client-pdf-2"
    assert os.path.basename(skills.unique_skill_dir("seller-calls")) == "seller-calls-2"     # a plugin skill has the name


def test_split_sections_and_json():
    s = procrun.split_sections("x\n===NAME===\nabc\n===SKILL===\n```markdown\n## a\n```\n")
    assert s == {"NAME": "abc", "SKILL": "## a"}
    assert procrun.last_json_object('טקסט {"a": 1} ועוד {"reply": "כן", "actions": []} סוף', "reply") == {"reply": "כן", "actions": []}
    assert procrun.last_json_object("nothing", "reply") is None


# ---- "שפר את טימי" flows -----------------------------------------------------------------------------------------
@pytest.fixture
def chat_env(app_dir, fake_claude, claude_home, tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()
    tconfig.save({"output_dir": str(rec), "owner_name": "רוני"})
    monkeypatch.setenv("TIMMY_DRY_OPEN", "1")
    opened = []
    share = []
    m = TimmyChat(False, open_pdf_design=lambda **kw: opened.append(kw), set_share_hiding=share.append)
    m.load()
    return m, opened, share, fake_claude[1], rec


def reply(monkeypatch, text, actions=()):
    monkeypatch.setenv("FAKE_CLAUDE_CHAT", json.dumps({"reply": text, "actions": list(actions)}, ensure_ascii=False))


def test_fast_path_needs_no_claude(chat_env):
    m, _, _, logf, _ = chat_env
    assert m.msgs[0].text == m.intro
    m.send("עיניים ירוקות")
    assert m.msgs[-1].text == "צבעתי את העיניים לירוק. זה כבר מאחורי החלון."
    assert m.msgs[-1].ctx.startswith("המראה לפני: ") and "עיניים #22C55E" in m.msgs[-1].ctx
    assert claude_calls(logf) == []
    assert json.loads(Path(m.chat_file).read_text(encoding="utf-8"))[-1]["text"].startswith("צבעתי")


def test_instruction_memory_and_prompt(chat_env, monkeypatch):
    m, _, _, logf, _ = chat_env
    reply(monkeypatch, "הוספתי " + LONG_DASH + " מעכשיו נקודות קצרות. [המראה לפני: x]",
          [{"type": "add_instruction", "text": "נקודות קצרות, עד 8 מילים"}, {"type": "remember", "text": "אוהב טון קליל"}])
    m.send("נקודות קצרות יותר, עד 8 מילים")
    last = m.msgs[-1]
    assert last.text == "הוספתי - מעכשיו נקודות קצרות.\n\nזכרתי: אוהב טון קליל"
    assert tconfig.load()["custom_instructions"] == ["נקודות קצרות, עד 8 מילים"] and memory.load() == ["אוהב טון קליל"]
    c = claude_calls(logf)[-1]
    assert c["argv"] == ["-p", "--model", "haiku", "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    p = c["prompt"]
    assert "אתה טימי, אפליקציה למחשב (ווינדוס)" in p and 'ההודעה החדשה של המשתמש: "נקודות קצרות יותר, עד 8 מילים"' in p
    assert "add_instruction" in p and "create_writing_skill" in p and "עוד לא זמינות בטימי לווינדוס" in p
    assert "רוני כותב לך בצ'אט" in p and "סקילים מחוברים: עיצוב ה-PDF: אין · ניסוח: אין" in p
    # the next prompt has the conversation and the instruction
    reply(monkeypatch, "הסרתי", [{"type": "remove_instruction", "text": "עד 8 מילים"}])
    m.send("תוריד את ההוראה על הנקודות")
    p2 = claude_calls(logf)[-1]["prompt"]
    assert "1. נקודות קצרות, עד 8 מילים" in p2 and "משתמש: נקודות קצרות יותר" in p2 and "- אוהב טון קליל" in p2
    assert tconfig.load()["custom_instructions"] == []


def test_link_skill_opens_pdf_design(chat_env, monkeypatch):
    m, opened, _, logf, _ = chat_env
    reply(monkeypatch, "חיברתי את הסקיל. תפתח את חלון עיצוב ה-PDF כדי לראות.", [{"type": "link_skill", "purpose": "pdf", "skill": "client-pdf"}])
    m.send("יש לי כבר סקיל PDF בקלוד קוד, תשתמש בו")
    assert skills.linked_skill("pdf")[0] == "client-pdf" and m.skills == {"pdf": "client-pdf"}
    assert opened[-1]["adopt"].name == "client-pdf" and opened[-1]["then"] is None
    t = m.msgs[-1].text
    assert "תפתח את חלון" not in t and "חיברתי את הסקיל client-pdf לעיצוב ה-PDF." in t and "פתחתי את \"עיצוב ה-PDF\"" in t
    assert "סקיל: חיברתי את client-pdf לעיצוב ה-PDF" in m.msgs[-1].ctx
    assert "הסקילים של Claude במחשב (שם: תיאור):\n- client-pdf: מעצב PDF" in claude_calls(logf)[-1]["prompt"]


def test_link_skill_not_found_is_honest(chat_env, monkeypatch):
    m, opened, _, _, _ = chat_env
    reply(monkeypatch, "חיברתי!", [{"type": "link_skill", "purpose": "writing", "skill": "no-such-skill"}])
    m.send("תחבר את הסקיל no-such-skill לניסוח")
    assert m.msgs[-1].text.startswith("לא מצאתי במחשב סקיל בשם no-such-skill")
    assert skills.linked_skill("writing") is None and not opened


def test_pdf_instruction_goes_to_the_design_chat(chat_env, monkeypatch, tmp_path):
    m, opened, _, _, _ = chat_env
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n")
    m.add_attachments([str(logo)])
    reply(monkeypatch, "הוספתי את הלוגו.", [{"type": "pdf_instruction", "text": "הלוגו בראש המסמך", "logo": "logo.png"}])
    m.send(None, typed="זה הלוגו שלי, תשים אותו ב-PDF")
    assert opened[-1] == {"send": "הלוגו בראש המסמך", "logo": str(logo)}
    assert m.msgs[-2].files == [str(logo)] and "📎" in m.msgs[-2].text
    assert m.msgs[-1].text == "מעביר את זה לחלון \"עיצוב ה-PDF\" יחד עם הלוגו שצירפת. שם תראה בעוד רגע איך המסמך נראה."


def test_share_hiding_and_images_not_yet(chat_env, monkeypatch):
    m, _, share, _, _ = chat_env
    reply(monkeypatch, "בסדר.", [{"type": "share_hiding", "on": False}])
    m.send("אני רוצה להדגים את טימי בשיתוף מסך")
    assert share == [False] and m.msgs[-1].text == "בסדר.\n\nמעכשיו רואים את טימי בשיתוף מסך."
    reply(monkeypatch, "מוסיף את התמונה", [{"type": "add_images", "call": 1, "files": []}])
    m.send("תוסיף צילום מסך ל-PDF של השיחה")
    assert m.msgs[-1].text == "תמונות ב-PDF של שיחה עוד לא זמינות בטימי לווינדוס. זה יגיע בעדכון הבא."


def test_claude_failure_and_queue(chat_env, monkeypatch):
    m, _, _, _, _ = chat_env
    monkeypatch.setenv("FAKE_CLAUDE_CHAT", "לא JSON")
    m.send("משהו")
    assert m.msgs[-1].is_error and "בדוק ש-Claude מחובר" in m.msgs[-1].text
    m.busy = "טימי כותב…"
    m.send("ראשונה")
    assert m.queue == ["ראשונה"]
    m.busy = None
    reply(monkeypatch, "קיבלתי")
    m.next()
    assert m.queue == [] and m.msgs[-1].text == "קיבלתי" and m.msgs[-2].text == "ראשונה"


def test_restyle_last_call(chat_env, monkeypatch):
    m, _, _, logf, rec = chat_env
    md = rec / "2026-11-20 10-30 דנה כהן - קביעת פגישה.md"
    md.write_text("# קביעת פגישה\n\n**תאריך:** 2026-11-20 · **עם:** [[דנה כהן]]\n\n## סיכום\n\nקבענו פגישה.\n", encoding="utf-8")
    (rec / "2026-11-20 10-30 דנה כהן - קביעת פגישה - תמלול.txt").write_text("[00:01] רוני: היי\n", encoding="utf-8")
    m.items = ["תכתוב בגוף ראשון"]
    m.save_items()
    reply(monkeypatch, "כן, מחיל על השיחה האחרונה.", [{"type": "apply_to_last_call"}])
    m.send("תחיל את זה על השיחה האחרונה")
    assert m.msgs[-1].text == "סידרתי גם את: דנה כהן - קביעת פגישה ✓", [x.text for x in m.msgs[-3:]]
    assert "נכתב מחדש לפי ההוראות" in md.read_text(encoding="utf-8")
    assert any(c["kind"] == "restyle" for c in claude_calls(logf))


def test_writing_skill_created_and_linked(chat_env, monkeypatch, tmp_path, claude_home):
    m, _, _, logf, _ = chat_env
    sample = tmp_path / "הודעות שלי.txt"
    sample.write_text("אהלן, סגרנו ליום שלישי. יאללה מתקדמים, אני שולח לך את ההצעה עד הערב ונדבר מחר בבוקר.\n" * 3, encoding="utf-8")
    m.add_attachments([str(sample)])
    reply(monkeypatch, "מתחיל, זה לוקח כדקה.", [{"type": "create_writing_skill", "files": ["הודעות שלי.txt"], "use_pasted": False,
                                                "use_calls": False, "answers": "טון ישיר, בלי בברכה", "replace": False}])
    m.send(None, typed="תבנה לי סקיל ניסוח מזה")
    t = m.msgs[-1].text
    assert "שמרתי את זה כסקיל ניסוח בשם" in t and "test-writing-voice" in t and t.startswith("אתה כותב קצר וישיר")
    d = claude_home / "skills" / "test-writing-voice"
    md = (d / "SKILL.md").read_text(encoding="utf-8")
    assert "created-by: timmy" in md and "## הקול" in md and LONG_DASH not in md
    assert skills.linked_skill("writing")[1] == str(d) and skills.is_timmy_made(str(d))
    wc = [c for c in claude_calls(logf) if c["kind"] == "writing_skill"][0]
    assert "[הודעות שלי.txt]\nאהלן, סגרנו" in wc["prompt"] and "טון ישיר, בלי בברכה" in wc["prompt"] and wc["argv"][2] == "sonnet"
    # a second request without replace: Timmy asks first
    reply(monkeypatch, "מתחיל.", [{"type": "create_writing_skill", "files": [], "use_pasted": False, "replace": False}])
    m.send("תעשה לי עוד סקיל ניסוח")
    assert m.msgs[-1].text.startswith("כבר מחובר אצלי סקיל ניסוח")
    # replace: the old Timmy skill goes to Timmy's trash folder, the new one takes its place
    reply(monkeypatch, "מתקן.", [{"type": "create_writing_skill", "files": [], "use_pasted": False, "replace": True, "answers": "פחות רשמי"}])
    m.send("פחות רשמי בבקשה")
    assert "שמרתי את זה כסקיל ניסוח" in m.msgs[-1].text and d.is_dir()
    assert list((Path(os.environ["TIMMY_APP_DIR"]) / "trash" / "skills").iterdir())
    last = [c for c in claude_calls(logf) if c["kind"] == "writing_skill"][-1]["prompt"]
    assert "הגרסה הקודמת של הסקיל" in last


def test_writing_skill_needs_a_sample(app_dir, fake_claude, claude_home):
    r = skills.create_writing_skill(skills.WritingSources([], ["קצר"]), None, procrun.CancelToken(), lambda m: None)
    assert r.kind == "failed" and "אני צריך דוגמה" in r.text
    tok = procrun.CancelToken()
    tok.cancel()
    r = skills.create_writing_skill(skills.WritingSources([], ["x" * 200]), None, tok, lambda m: None)
    assert r.kind == "cancelled"


def test_design_mode_is_looks_only(app_dir, fake_claude, monkeypatch):
    m = TimmyChat(True)
    m.load()
    reply(monkeypatch, "צבעתי.", [{"type": "appearance", "changes": {"accent_color": "#3b82f6"}},
                                  {"type": "add_instruction", "text": "לא אמור להישמר"}])
    m.send("תעשה אותו קצת יותר כחול כמו הים")
    assert T.current().accent_hex == "#3B82F6" and not tconfig.load().get("custom_instructions")
    p = claude_calls(fake_claude[1])[-1]["prompt"]
    assert "אתה במצב \"עצב את טימי\"" in p and "create_writing_skill" not in p
    m.add_attachments(["/tmp/x.pdf"])
    m.send(None, typed="")                                           # design mode never sends files
    assert m.msgs[-1].text == "צבעתי." or m.msgs[-1].text.startswith("צבעתי")


# ---- "עיצוב ה-PDF" -----------------------------------------------------------------------------------------------
def test_pdf_design_reset_logo_preview(app_dir, fake_claude, tmp_path):
    built = []

    def build_preview(md, dest, local, token, done):
        built.append((md, local))
        Path(dest).write_bytes(b"%PDF-1.4\n")
        done(True, "")

    def page_png(pdf, png, w=900):
        Path(png).write_bytes(b"png")
        return True
    d = PDFDesign(build_preview=build_preview, page_png=page_png)
    d.load()
    assert d.msgs[0].text.startswith("אני מעצב את ה-PDF") and built and built[0][1] is True     # first open: a quick local preview
    assert d.msgs[-1].text == "ככה ה-PDF נראה עכשיו:" and d.msgs[-1].image.endswith(".png") and d.msgs[-1].pdf.endswith(".pdf")
    d.reset()
    assert d.msgs[-1].text == "זה כבר העיצוב המקורי" and d.busy is None
    src = tmp_path / "Logo.PNG"
    src.write_bytes(b"\x89PNG")
    d.attach_logo(str(src))
    assert d.pending_logo and Path(d.pending_logo).parent.name == "brand" and d.pending_logo.endswith(".png")
    d.handle_drop([str(tmp_path / "notes.txt")])
    assert d.msgs[-1].is_error
    assert json.loads((app_dir / "pdf" / "chat.json").read_text(encoding="utf-8"))[0]["fromUser"] is False


def test_pdf_design_history_and_queue(app_dir, fake_claude):
    d = PDFDesign()
    d.load(quiet=True)
    d.msgs += [ChatMsg(True, "כחול"), ChatMsg(False, "צבעתי"), ChatMsg(True, "x" * 700, image="/l.png")]
    p = d.write_history(d.msgs)
    items = json.loads(Path(p).read_text(encoding="utf-8"))
    assert items[-1]["role"] == "user" and items[-1]["text"].endswith("… (צורף לוגו)") and items[0]["role"] == "timmy"
    d.busy = "מעצב…"
    d.send("עוד יותר כחול")
    assert d.queue == [("עוד יותר כחול", None)]


def test_pdf_design_adopt_skill_without_script(app_dir, fake_claude, claude_home):
    d = PDFDesign()
    d.load(quiet=True)
    s = skills.find_skills("client-pdf")[0]
    skills.set_linked_skill("pdf", s)
    d.adopt_skill(s)
    texts = [m.text for m in d.msgs]
    assert any("אין סקריפט שאני יכול להריץ" in t for t in texts), texts
    assert d.busy is None and not d.skill_runs


# ---- PDF design skill from a sample PDF (QtPdf) ------------------------------------------------------------------
def make_sample_pdf(path):
    """A one-page A4 PDF written by hand: a blue band, a thin red line and green text in the standard Helvetica font.
    (QPdfWriter text from the offscreen platform on windows-latest has no extractable text, so a test PDF made with Qt
    read back empty there; real PDFs from Word / Canva / browsers carry a text layer.)"""
    content = (b"0.118 0.227 0.541 rg 0 722 595 120 re f\n"          # #1E3A8A
               b"0.937 0.267 0.267 rg 40 674 200 8 re f\n"           # #EF4444
               b"BT /F1 18 Tf 0.133 0.773 0.369 rg 40 620 Td (Sample Brand Document) Tj ET\n")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    Path(path).write_bytes(bytes(out))


def test_create_pdf_skill(qapp, app_dir, fake_claude, claude_home, tmp_path):
    pdf = tmp_path / "brand.pdf"
    make_sample_pdf(pdf)
    sample, err = skills.analyze_pdf(str(pdf), str(tmp_path / "pages"))
    assert sample is not None, err
    assert sample.page_count == 1 and sample.page_size.endswith("(A4)") and "Sample Brand" in sample.text
    hexes = [h for h, _ in sample.colors]
    assert "#FFFFFF" in hexes and any(abs(int(h[1:3], 16) - 0x1E) < 6 and abs(int(h[5:7], 16) - 0x8A) < 6 for h in hexes), hexes
    assert any(int(h[1:3], 16) > 0xD0 and int(h[3:5], 16) < 0x60 for h in hexes), hexes        # the thin red line (an accent)
    steps = []
    r = skills.create_pdf_skill(str(pdf), procrun.CancelToken(), steps.append)
    assert r.kind == "ok", r.text
    d = Path(r.info.dir)
    assert d.name == "blue-test-pdf-style" and (d / "assets" / "template.html").exists() and (d / "references" / "page-1.png").exists()
    assert (d / "scripts" / "render.py").exists()
    md = (d / "SKILL.md").read_text(encoding="utf-8")
    assert "created-by: timmy" in md and "## איך מייצרים PDF בעיצוב הזה" in md and "references/page-1.png" in md
    assert steps[0] == "קורא את ה-PDF…" and r.text.startswith("למדתי עיצוב כחול")
    call = [c for c in claude_calls(fake_claude[1]) if c["kind"] == "pdf_skill"][0]
    msg = json.loads(call["prompt"].strip())
    blocks = msg["message"]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png" and blocks[-1]["type"] == "text"
    assert "גודל עמוד: 210×297 מ\"מ (A4)" in blocks[-1]["text"]


def test_analyze_pdf_rejects_bad_files(qapp, tmp_path):
    e = tmp_path / "empty.pdf"
    e.write_bytes(b"")
    assert "ריק" in skills.analyze_pdf(str(e), str(tmp_path / "o"))[1]
    f = tmp_path / "fake.pdf"
    f.write_text("hello", encoding="utf-8")
    assert skills.analyze_pdf(str(f), str(tmp_path / "o"))[1].startswith("זה לא באמת קובץ PDF")
    assert skills.analyze_pdf(str(tmp_path / "missing.pdf"), str(tmp_path / "o"))[1].startswith("לא מצאתי")


def test_read_sample_text_formats(qapp, tmp_path):
    import zipfile
    docx = tmp_path / "a.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>שלום מהוורד</w:t></w:r></w:p></w:body></w:document>")
    assert skills.read_sample_text(str(docx)) == "שלום מהוורד"
    h = tmp_path / "a.html"
    h.write_text("<html><style>x{}</style><body><p>טקסט &amp; עוד</p></body></html>", encoding="utf-8")
    assert skills.read_sample_text(str(h)) == "טקסט & עוד"
    t = tmp_path / "a.txt"
    t.write_bytes("שלום".encode("cp1255"))
    assert skills.read_sample_text(str(t)) == "שלום"
    pdf = tmp_path / "p.pdf"
    make_sample_pdf(pdf)
    assert "Sample Brand" in skills.read_sample_text(str(pdf))
