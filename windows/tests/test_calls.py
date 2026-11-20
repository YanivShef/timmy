"""ui/calls.py (the "השיחות שלי" model) and ui/markdown.py: parsing, listing, rename with rollback (Recorder.swift
loadCall / renameCall / parseMarkdown). Fictional names only."""
import datetime as dt
import json
import os

import pytest

from timmy.ui import calls as C
from timmy.ui import markdown as MD

SUMMARY = """# הצעת מחיר לאתר

**תאריך:** 2026-11-17 10:30 · **אורך:** 24:18 דק׳ · **עם:** [[דנה כהן]]

## סיכום

דנה רוצה **חנות** אונליין. [אתר](https://example.com)

## משימות

- [ ] לשלוח הצעה (יניב)
- [x] לקבוע שיחה
- נקודה רגילה
  - תת נקודה
1. ראשון

| נושא | מה סוכם |
|---|---|
| מחיר | 6,500 |

> ציטוט

---

![צילום מסך](2026-11-17 10-30 דנה כהן - הצעת מחיר לאתר - תמונות/a.png)

תמלול מלא: 2026-11-17 10-30 דנה כהן - הצעת מחיר לאתר - תמלול.txt
"""


def make_call(folder, stem="2026-11-17 10-30 דנה כהן - הצעת מחיר לאתר", text=SUMMARY, extra=(".mp3", " - תמלול.txt")):
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / (stem + ".md")
    md.write_text(text, encoding="utf-8")
    for sfx in extra:
        (folder / (stem + sfx)).write_text("x", encoding="utf-8")
    return md


def test_load_call_fields(tmp_path):
    md = make_call(tmp_path)
    c = C.load_call(str(md))
    assert c.title == "הצעת מחיר לאתר" and c.file_title == "הצעת מחיר לאתר"
    assert c.person == "דנה כהן" and c.length == "24:18 דק׳"
    assert c.date == dt.datetime(2026, 11, 17, 10, 30)
    assert c.has_transcript and c.pdf_path.endswith("הצעת מחיר לאתר.pdf")
    assert c.matches("דנה") and c.matches("מחיר") and not c.matches("משה")


def test_title_from_heading_keeps_punctuation(tmp_path):
    md = make_call(tmp_path, "2026-11-18 09-00 מה הלאה", "# מה הלאה?\n\nטקסט\n", ())
    assert C.load_call(str(md)).title == "מה הלאה?"


def test_list_calls_newest_first_and_skips_points(tmp_path):
    make_call(tmp_path, "2026-11-17 10-30 א", "# א\n", ())
    make_call(tmp_path, "2026-11-19 08-00 ב", "# ב\n", ())
    (tmp_path / "2026-11-19 08-00-01 כרום נקודות.md").write_text("- x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    calls = C.list_calls(str(tmp_path))
    assert [c.title for c in calls] == ["ב", "א"]
    s1 = C.signature(str(tmp_path))
    make_call(tmp_path, "2026-11-20 08-00 ג", "# ג\n", ())
    assert C.signature(str(tmp_path)) != s1


def test_date_text():
    now = dt.datetime(2026, 11, 21, 12, 0)
    assert C.date_text(dt.datetime(2026, 11, 21, 9, 5), now) == "היום, 09:05"
    assert C.date_text(dt.datetime(2026, 11, 20, 23, 59), now) == "אתמול, 23:59"
    assert C.date_text(dt.datetime(2026, 3, 3, 10, 0), now) == "3 במרץ, 10:00"
    assert C.date_text(dt.datetime(2025, 12, 31, 10, 0), now) == "31 בדצמבר 2025, 10:00"


def test_summary_body_drops_header_lines():
    body = C.summary_body(SUMMARY)
    assert "# הצעת" not in body and "**תאריך:**" not in body and "תמלול מלא:" not in body and "## סיכום" in body


def test_safe_call_title_windows_rules():
    assert C.safe_call_title('מחיר / חבילה: "גדולה"?') == "מחיר חבילה גדולה"
    assert C.safe_call_title("סוף.  ") == "סוף"
    assert C.safe_call_title("CON") == "_CON"
    assert C.safe_call_title("א" * 80) == "א" * 60
    assert C.safe_call_title(" \n ") == ""


def test_rename_moves_every_file_and_updates_summary(tmp_path, app_dir):
    meetings = tmp_path / "פגישות"
    meetings.mkdir()
    (app_dir / "config.json").write_text(json.dumps({"meetings_dir": str(meetings)}, ensure_ascii=False), encoding="utf-8")
    rec = tmp_path / "rec"
    stem = "2026-11-17 10-30 דנה כהן - הצעת מחיר לאתר"
    md = make_call(rec, stem, extra=(".mp3", " - תמלול.txt", ".pdf"))
    (rec / (stem + " - תמונות")).mkdir()
    (rec / (stem + " - תמונות") / "a.png").write_bytes(b"png")
    (meetings / (stem + ".md")).write_text(SUMMARY, encoding="utf-8")
    c = C.load_call(str(md))
    err, new = C.rename_call(c, "הצעה מעודכנת: חנות?")
    assert err is None
    nstem = "2026-11-17 10-30 דנה כהן - הצעה מעודכנת חנות"
    assert new == str(rec / (nstem + ".md"))
    assert sorted(os.listdir(rec)) == sorted([nstem + s for s in (".md", ".mp3", " - תמלול.txt", ".pdf", " - תמונות")])
    text = (rec / (nstem + ".md")).read_text(encoding="utf-8")
    assert text.startswith("# הצעה מעודכנת: חנות?\n")                       # the shown title keeps the punctuation
    assert f"תמלול מלא: {nstem} - תמלול.txt" in text
    assert f"]({nstem} - תמונות/a.png)" in text                               # the images folder moved with the name
    assert os.listdir(meetings) == [nstem + ".md"]
    assert (meetings / (nstem + ".md")).read_text(encoding="utf-8").startswith("# הצעה מעודכנת: חנות?")
    again = C.load_call(new)
    assert again.title == "הצעה מעודכנת: חנות?" and again.person == "דנה כהן"


def test_rename_title_only_change_keeps_files(tmp_path, app_dir):
    md = make_call(tmp_path, "2026-11-18 09-00 מה הלאה", "# מה הלאה\n\nטקסט\n", ())
    err, new = C.rename_call(C.load_call(str(md)), "מה הלאה?")
    assert err is None and new == str(md)
    assert md.read_text(encoding="utf-8").startswith("# מה הלאה?")


def test_rename_errors(tmp_path, app_dir):
    md = make_call(tmp_path)
    c = C.load_call(str(md))
    assert C.rename_call(c, "  ")[0] == "צריך לכתוב כותרת"
    assert C.rename_call(c, "חדש", busy="עסוק")[0] == "עסוק"
    make_call(tmp_path, "2026-11-17 10-30 דנה כהן - תפוס", "# תפוס\n", ())
    err, new = C.rename_call(c, "תפוס")
    assert err.startswith("כבר יש קובץ בשם") and new == str(md)
    os.remove(md)
    assert C.rename_call(c, "אחר")[0] == "לא מצאתי את קובץ הסיכום. אולי הוא הועבר או נמחק"


def test_rename_rolls_back_when_a_move_fails(tmp_path, app_dir, monkeypatch):
    stem = "2026-11-17 10-30 דנה כהן - הצעת מחיר לאתר"
    md = make_call(tmp_path, stem)
    before = sorted(os.listdir(tmp_path))
    real = C._move_no_clobber
    calls = []

    def flaky(a, b):
        calls.append(a)
        if len(calls) == 3:          # the mp3 is "open in a player"
            return False
        return real(a, b)

    monkeypatch.setattr(C, "_move_no_clobber", flaky)
    err, new = C.rename_call(C.load_call(str(md)), "חדש")
    assert "שום דבר לא השתנה" in err and new == str(md)
    assert sorted(os.listdir(tmp_path)) == before
    assert md.read_text(encoding="utf-8") == SUMMARY


def test_rename_keeps_long_paths_under_max_path(tmp_path, app_dir):
    room = 259 - 1 - len(C.TRANSCRIPT_SUFFIX) - len(str(tmp_path)) - 1 - len("2026-11-17 10-30 ") - 20 - 1
    if room < 5:
        pytest.skip("temp folder path too long for this check")
    deep = tmp_path / ("d" * room)
    md = make_call(deep, "2026-11-17 10-30 קצר", "# קצר\n", (" - תמלול.txt",))
    err, new = C.rename_call(C.load_call(str(md)), "כותרת ארוכה מאוד " * 5)
    assert err is None
    assert len(new[:-3] + C.TRANSCRIPT_SUFFIX) < 260
    assert os.path.basename(new).startswith("2026-11-17 10-30 כותרת ארוכה")


def test_markdown_blocks():
    kinds = [b.kind for b in MD.parse(C.summary_body(SUMMARY))]
    assert kinds == ["heading", "para", "heading", "task", "task", "bullet", "bullet", "numbered", "table", "quote", "rule", "image"]
    b = MD.parse("  - פנימי")[0]
    assert b.kind == "bullet" and b.level == 1
    t = MD.parse("| a \\| b | c |\n|:--|--:|\n| 1 | 2 |")[0]
    assert t.rows == [["a | b", "c"], ["1", "2"]]


def test_markdown_inline_and_html(qapp):
    from timmy.ui import theme as T
    assert MD.inline("**בולט** ו-*נטוי* [[פתק|דנה]] <x>") == "<b>בולט</b> ו-<i>נטוי</i> דנה &lt;x&gt;"
    h = MD.to_html(MD.parse(C.summary_body(SUMMARY)), T.Look({}), "/nowhere")
    assert 'dir="rtl"' in h and "<b>חנות</b>" in h and 'href="https://example.com"' in h
    assert "התמונה לא נמצאה" in h and "6,500" in h and "☑" in h and "☐" in h


@pytest.mark.parametrize("raw,expected", [("סוף.", "סוף"), ('"ציטוט"', "ציטוט"), ("a\u2014b", "a-b"), ("[x]", "x")])
def test_clean_caption(raw, expected):
    assert C.clean_caption(raw) == expected
