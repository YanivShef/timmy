"""Client PDF (ui/pdf_render.py): the Timmy line, page margins, page footer with numbers, QtWebEngine printing
offscreen, and the whole flow through the vendored pdf.py (local mode, no Claude)."""
import re
import shutil

import pytest

from tests.conftest import WIN, wait_until

pytestmark = pytest.mark.ui
SAMPLE = WIN / "timmy" / "engine" / "_mac" / "pdf_sample.md"


def R():
    from timmy.ui import pdf_render
    return pdf_render


def doc(body, page_css="@page { size: A4; margin: 20mm 19mm 24mm 19mm; }"):
    return (f'<!doctype html><html dir="rtl" lang="he"><head><meta charset="utf-8"><style>{page_css} '
            f"body{{font-family:Arial,sans-serif;font-size:11pt}}</style></head><body>{body}</body></html>")


def test_footer_injection_removes_tampering(qapp):
    page = doc('<div id="timmy-footer" style="display:none">old</div><p class="x timmy-footer">x</p>'
               '<style>#timmy-footer{display:none}</style><img src="data:image/png;base64,' + "A" * 5000 + '">')
    out = R().inject_timmy_footer(page)
    assert out.count('id="timmy-footer"') == 1 and ">old<" not in out and 'class="x timmy-footer"' not in out
    assert "display:none}" not in out.replace(" ", "")
    assert "A" * 5000 in out                                    # the big data: URI survived untouched
    assert out.index('id="timmy-footer"') < out.lower().rindex("</body>")
    assert R().TIMMY_URL in out and "נשלח על ידי טימי" in out


def test_footer_injection_is_fast_on_huge_data_uris(qapp):
    import time
    page = doc('<img src="data:image/png;base64,' + "QUJD" * 600_000 + '">')
    t = time.time()
    R().inject_timmy_footer(page)
    assert time.time() - t < 3


@pytest.mark.parametrize("css,expected", [
    ("@page { size: A4; margin: 20mm 19mm 24mm 19mm; }", (20, 19, 24, 19)),
    ("@page{margin:1cm}", (10, 10, 10, 10)),
    ("@page :first { margin: 0 } @page { margin: 72pt 0.5in }", (0, 0, 0, 0)),
    ("@page { size: A4 } @page { margin: 96px 48px 0 }", (25.4, 12.7, 0, 12.7)),
    ("body { margin: 5mm }", (20, 19, 24, 19)),
    ("@page { margin: auto }", (20, 19, 24, 19)),
])
def test_page_margins(css, expected, qapp):
    assert R().page_margins_mm(doc("x", css)) == pytest.approx(expected)


def test_prepare_adds_footer_room_and_boxes(qapp):
    h, m = R().prepare(doc("x", "@page { margin: 10mm }"), 'דנה "סטודיו" · 050-0000000')
    assert m[2] == R().MIN_BOTTOM_MM and m[0] == 10
    assert "@bottom-left" in h and "counter(pages)" in h and '\\"סטודיו\\"' in h
    h2, _ = R().prepare(doc("x"), "")
    assert "@bottom-right" not in h2 and "@bottom-left" in h2


def print_to(qapp, page, dest, contact=""):
    res = []
    job = R().print_html(page, str(dest), contact, True, done=res.append)
    assert wait_until(qapp, lambda: res, timeout=90), "printing did not finish"
    assert job is not None
    return res[0]


def test_print_long_document_numbers_every_page(qapp, tmp_path, out_dir):
    body = "".join(f"<h2>סעיף {i}</h2><p>" + "שלום עולם, זה טקסט בעברית לבדיקה של ה-PDF. " * 45 + "</p>" for i in range(9))
    dest = tmp_path / "long.pdf"
    assert print_to(qapp, doc(body), dest, "סטודיו לדוגמה · 03-0000000") is True
    pages = R().pdf_pages_text(str(dest))
    n = len(pages)
    assert n >= 3
    for i, t in enumerate(pages, 1):
        assert f"עמוד {i} מתוך {n}" in t, (i, t[:80])
        assert "סטודיו לדוגמה" in t
    assert "נשלח על ידי טימי" in pages[-1] and "לחץ כאן" in pages[-1]
    assert sum("נשלח על ידי טימי" in t for t in pages) == 1
    assert "שלום עולם" in pages[0]                              # Hebrew text is real, extractable text
    shot = out_dir / "screens"
    shot.mkdir(parents=True, exist_ok=True)
    assert R().first_page_png(str(dest), str(shot / "pdf-long-page1.png"))


def first_pass(qapp, page, dest):
    """What print_html prints first (before its lone-line check)."""
    res = []
    final, margins = R().prepare(page, "", True)
    job = R().HtmlToPdf(final, str(dest), margins)
    job.finished.connect(res.append)
    assert wait_until(qapp, lambda: res, timeout=90)
    return R().pdf_pages_text(str(dest))


def test_timmy_line_never_alone_on_last_page(qapp, tmp_path):
    """Content that fills the page exactly: the line must not end up alone on a new page (re-printed tighter)."""
    pr = R()
    dest = tmp_path / "fit.pdf"
    found = None
    for h in range(780, 960, 8):              # a body height that pushes the normal line onto page 2
        page = doc(f'<div style="height:{h}px;background:#eee"></div>')
        pages = first_pass(qapp, page, dest)
        if len(pages) == 2 and pr.lone_timmy_line(pages[1]):
            found = page
            break
    if found is None:
        pytest.skip("could not provoke a lone Timmy line with this font setup")
    assert print_to(qapp, found, dest, "")
    pages = pr.pdf_pages_text(str(dest))
    assert len(pages) == 1 and "נשלח על ידי טימי" in pages[0], [p[:60] for p in pages]


def test_full_flow_local_engine(qapp, tmp_path, app_dir, monkeypatch, out_dir):
    monkeypatch.setenv("TIMMY_CLAUDE_SEARCH", "")
    md = tmp_path / "2026-11-17 10-30 דנה כהן - הצעת מחיר לאתר.md"
    shutil.copyfile(SAMPLE, md)
    dest = tmp_path / (md.stem + ".pdf")
    done = []
    b = R().PdfBuild(str(md), str(dest), local=True).start()
    b.finished.connect(lambda ok, claude, note: done.append((ok, claude, note)))
    assert wait_until(qapp, lambda: done, timeout=150)
    ok, claude, note = done[0]
    assert ok and claude is False and note == ""
    pages = R().pdf_pages_text(str(dest))
    assert "הצעת מחיר לאתר חנות" in pages[0] and f"עמוד 1 מתוך {len(pages)}" in pages[0]
    assert "נשלח על ידי טימי" in pages[-1]
    assert R().first_page_png(str(dest), str(out_dir / "screens" / "pdf-sample-page1.png"))


def test_engine_missing_falls_back_to_plain_html(qapp, tmp_path, monkeypatch):
    md = tmp_path / "s.md"
    md.write_text("# כותרת\n\n- נקודה\n\nתמלול מלא: x", encoding="utf-8")
    dest = tmp_path / "s.pdf"
    from timmy.ui import recorder
    monkeypatch.setattr(recorder, "child_python", lambda: str(tmp_path / "no-python.exe"))
    done = []
    b = R().PdfBuild(str(md), str(dest)).start()
    b.finished.connect(lambda ok, c, n: done.append((ok, n)))
    assert wait_until(qapp, lambda: done, timeout=90)
    assert done[0] == (True, "pdf.py לא רץ")
    text = "".join(R().pdf_pages_text(str(dest)))
    assert "כותרת" in text and "תמלול מלא" not in text and "נשלח על ידי טימי" in text


def test_cancel_kills_engine(qapp, tmp_path, monkeypatch, fake_claude):
    """A slow Claude: "בטל" ends the build quickly and nothing is written."""
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "60")
    md = tmp_path / "s.md"
    shutil.copyfile(SAMPLE, md)
    dest = tmp_path / "s.pdf"
    done = []
    b = R().PdfBuild(str(md), str(dest)).start()
    b.finished.connect(lambda ok, c, n: done.append((ok, n)))
    wait_until(qapp, lambda: False, timeout=3)
    b.cancel()
    assert wait_until(qapp, lambda: done, timeout=30)
    assert done[0] == (False, "בוטל") and not dest.exists()


def test_html_has_no_long_dash():
    src = (WIN / "timmy" / "ui" / "pdf_render.py").read_text(encoding="utf-8")
    assert "\u2014" not in src and not re.search(r"[\u2014]", R().TIMMY_LEAD)
