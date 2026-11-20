"""Client PDF: summary.md -> pdf engine (vendored mac/pdf.py) -> HTML -> A4 PDF with QtWebEngine (DESIGN.md 8).
Port of Recorder.swift buildPDF / HTMLToPDF / injectTimmyFooter / stampFooter.

* The engine runs as `python -m timmy.engine.pdf <md> --out <tmp.html>` in a Job Object (a cancel or a timeout
  kills Claude and node with it), and answers one JSON line.
* The "נשלח על ידי טימי" line is injected here, after the user's template and after Claude, right before printing,
  so nothing from the PDF design chat can remove or hide it (the Mac rule).
* QtWebEngine ignores CSS @page margins (the QPageLayout margins win, measured on Qt 6.12), so the template's first
  `@page { margin: ... }` is read and passed as the page layout. Page footer (contact on the right half, "עמוד X
  מתוך N" on the left, a light rule above) = CSS page-margin boxes (@bottom-right / @bottom-left), which Chromium
  fills per page, so the text stays real, selectable text.
* After printing, the PDF is checked with QtPdf: if the last page holds only the Timmy line, it is printed again
  with that line tightened onto the previous page (the Mac moves it into the footer).
* A linked PDF skill that made the PDF itself: the file is moved into place unchanged (stamping the Timmy line onto
  a foreign PDF is part of the linked-skills work in build step 4).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid

from PySide6.QtCore import QMarginsF, QObject, QTimer, QUrl, Signal

from timmy.ui import markdown as MD
from timmy.ui import recorder as R
from timmy.winsys import jobs

TIMMY_URL = "https://yanivshef.com/timmy"
TIMMY_LEAD = "נשלח על ידי טימי \u00b7 רוצה לנסות? "
TIMMY_LINK = "לחץ כאן"
DEFAULT_MARGINS_MM = (20.0, 19.0, 24.0, 19.0)      # pdf_template.html: @page { margin: 20mm 19mm 24mm 19mm }
MIN_BOTTOM_MM = 14.0                                # room for the page footer
ENGINE_TIMEOUT = 210                                # pdf.py keeps its own budget of about 3 minutes
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


# ---- the Timmy line ---------------------------------------------------------------------------------------------
_DATA = re.compile(r"data:[A-Za-z0-9.+/-]*;base64,[A-Za-z0-9+/=]+")
_logo = []


def logo_uri() -> str:
    if not _logo:
        try:
            import base64
            from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
            from PySide6.QtGui import QImage
            img = QImage(os.path.join(ASSETS, "timmy.png"))
            if img.isNull():
                raise OSError("no logo")
            img = img.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            _logo.append("data:image/png;base64," + base64.b64encode(bytes(ba)).decode("ascii"))
        except Exception:
            _logo.append("")
    return _logo[0]


def inject_timmy_footer(page: str, compact: bool = False) -> str:
    """Remove anything that targets the line (an element named timmy-footer, CSS rules that mention it), then add the
    line with its own forced CSS before </body>. data: URIs are moved out of the way first, so the regexes never
    run over megabytes of base64 (the Mac fix for a PDF that took minutes)."""
    stash = []

    def keep(m):
        if len(m.group(0)) < 200:
            return m.group(0)
        stash.append(m.group(0))
        return f"__timmy_data_{len(stash) - 1}__"

    h = _DATA.sub(keep, page or "")
    h = re.sub(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*timmy-footer[^>]*>[\s\S]*?</\1\s*>", "", h, flags=re.I)
    h = re.sub(r"<[^>]*timmy-footer[^>]*>", "", h, flags=re.I)
    h = re.sub(r"[^{}<>]*timmy-footer[^{}<>]*\{[^}]*\}", "", h, flags=re.I)
    for i, d in enumerate(stash):
        h = h.replace(f"__timmy_data_{i}__", d)
    force = ("display:block !important;visibility:visible !important;opacity:1 !important;position:static !important;"
             "transform:none !important;filter:none !important;clip-path:none !important;height:auto !important;"
             "max-height:none !important;overflow:visible !important")
    gap = "margin:4px 0 0 !important;padding:2px 0 0 !important;" if compact else "margin:34px 0 0 !important;padding:9px 0 0 !important;"
    css = ("<style>#timmy-footer,#timmy-footer *{all:revert !important}"
           f"#timmy-footer{{{force};{gap}border:0 !important;border-top:1px solid #ececf0 !important;"
           "background:transparent !important;font:7.5pt/16px Heebo,'Segoe UI',Arial,sans-serif !important;color:#a1a1a6 !important;"
           "text-align:center !important;direction:rtl !important;break-inside:avoid !important;page-break-inside:avoid !important"
           + (";break-before:avoid !important;page-break-before:avoid !important" if compact else "") + "}"
           "#timmy-footer img{display:inline-block !important;visibility:visible !important;opacity:1 !important;width:14px !important;"
           "height:14px !important;vertical-align:middle !important;margin:0 0 0 6px !important;border-radius:3px !important}"
           "#timmy-footer span{display:inline !important;visibility:visible !important;vertical-align:middle !important;"
           "color:#a1a1a6 !important;font:inherit !important}"
           "#timmy-footer a{display:inline !important;visibility:visible !important;color:#7c7c82 !important;"
           "text-decoration:underline !important;font:inherit !important}</style>")
    uri = logo_uri()
    img = f'<img src="{uri}" alt="" style="display:inline-block !important;width:14px !important;height:14px !important">' if uri else ""
    footer = css + f'<div id="timmy-footer" style="{force}">{img}<span>{TIMMY_LEAD}<a href="{TIMMY_URL}">{TIMMY_LINK}</a></span></div>'
    i = h.lower().rfind("</body>")
    return h[:i] + footer + "\n" + h[i:] if i >= 0 else h + footer


# ---- page layout ------------------------------------------------------------------------------------------------
_UNITS_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "px": 25.4 / 96, "pc": 25.4 / 6, "q": 0.25}


def _mm(v: str):
    m = re.fullmatch(r"\s*(-?[\d.]+)\s*([a-zA-Z]*)\s*", v)
    if not m:
        return None
    n, u = float(m.group(1)), (m.group(2) or "px").lower()
    if n == 0:
        return 0.0
    return n * _UNITS_MM[u] if u in _UNITS_MM else None


def page_margins_mm(page: str):
    """(top, right, bottom, left) in mm from the first `@page { margin: ... }` of the HTML, else the Mac default."""
    for m in re.finditer(r"@page\b[^{]*\{([^{}]*)", page or ""):
        mm = re.search(r"(?<![-\w])margin\s*:\s*([^;}!]+)", m.group(1))
        if not mm:
            continue
        vals = [_mm(x) for x in mm.group(1).split()]
        if not vals or any(v is None for v in vals):
            continue
        if len(vals) == 1:
            vals = vals * 4
        elif len(vals) == 2:
            vals = [vals[0], vals[1], vals[0], vals[1]]
        elif len(vals) == 3:
            vals = [vals[0], vals[1], vals[2], vals[1]]
        return tuple(max(0.0, v) for v in vals[:4])
    return DEFAULT_MARGINS_MM


def _css_str(s: str) -> str:
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ") + '"'


def footer_css(contact: str, page_numbers: bool = True) -> str:
    """Page-margin boxes: contact on the right half, "עמוד X מתוך N" on the left, like the Mac stampFooter."""
    box = ("font-family:'Segoe UI',Heebo,Arial,sans-serif;font-size:8pt;color:#808080;direction:rtl;"
           "vertical-align:top;padding-top:6px;border-top:0.6px solid #e6e6e6;")
    parts = ["@page{"]
    if page_numbers:
        parts.append('@bottom-left{content:"עמוד " counter(page) " מתוך " counter(pages);' + box + "text-align:left;}")
    if contact.strip():
        parts.append("@bottom-right{content:" + _css_str("\u200f" + contact.strip()) + ";" + box + "text-align:right;}")
    if page_numbers and contact.strip():
        parts.append("@bottom-center{content:\"\";" + box + "}")
    parts.append("}")
    return "<style>" + "".join(parts) + "</style>"


def prepare(page: str, contact: str = "", page_numbers: bool = True, compact_footer: bool = False):
    """Final HTML (Timmy line + footer CSS) and the page margins in mm."""
    top, right, bottom, left = page_margins_mm(page)
    h = inject_timmy_footer(page, compact=compact_footer)
    if page_numbers or contact.strip():
        bottom = max(bottom, MIN_BOTTOM_MM)
        css = footer_css(contact, page_numbers)
        i = h.lower().find("</head>")
        h = h[:i] + css + h[i:] if i >= 0 else css + h
    return h, (top, right, bottom, left)


def fallback_html(md_text: str, accent: str = "#FFA046") -> str:
    """Last resort when the engine did not run at all (Mac swiftFallbackHTML)."""
    import html as H
    out = []
    md_text = "\n".join(ln for ln in md_text.split("\n") if not ln.startswith("תמלול מלא:"))
    for b in MD.parse(md_text):
        t = MD.inline(b.text)
        if b.kind == "heading":
            n = min(b.level, 3)
            out.append(f"<h{n}>{t}</h{n}>")
        elif b.kind in ("para", "quote"):
            out.append(f"<p>{t}</p>")
        elif b.kind in ("bullet", "numbered"):
            out.append(f"<p>\u2022 {t}</p>")
        elif b.kind == "task":
            out.append(f"<p>\u2610 {t}</p>")
        elif b.kind == "table":
            out.append("<table>" + "".join("<tr>" + "".join(f"<td>{MD.inline(c)}</td>" for c in r) + "</tr>" for r in b.rows or []) + "</table>")
        elif b.kind == "rule":
            out.append("<hr>")
        elif b.kind == "image" and b.text:
            out.append(f'<p style="text-align:center;color:#888">{H.escape(b.text)}</p>')
    return ('<!doctype html><html dir="rtl" lang="he"><head><meta charset="utf-8"><style>@page{size:A4;margin:20mm 19mm 24mm}'
            "body{font-family:'Segoe UI',Arial,sans-serif;font-size:11pt;line-height:1.7;color:#1c1c1e}"
            f"h1,h2,h3{{color:{accent}}}td{{border-bottom:1px solid #eee;padding:6px 8px}}table{{border-collapse:collapse;width:100%}}"
            "</style></head><body>" + "".join(out) + "</body></html>")


def squash(s: str) -> str:
    return re.sub(r"\s", "", s or "")


def lone_timmy_line(page_text: str, contact: str = "") -> bool:
    """The page holds nothing but the Timmy line (and the page footer)."""
    t = re.sub(r"עמוד\d+מתוך\d+", "", squash(page_text))
    if contact.strip():
        t = t.replace(squash(contact), "")
    return t.startswith("נשלחעלידיטימי") and len(t) < 45


def pdf_pages_text(path: str) -> list[str]:
    from PySide6.QtPdf import QPdfDocument
    d = QPdfDocument()
    d.load(path)
    try:
        return [d.getAllText(i).text() for i in range(d.pageCount())]
    finally:
        d.close()


# ---- printing ---------------------------------------------------------------------------------------------------
_PROFILE = []
_LIVE = set()          # running prints / builds keep themselves alive here until they finished (no early GC)


def _profile():
    """One off-the-record profile for every print (no cache / cookies on disk), alive for the whole app, so a page
    is never left without its profile."""
    if not _PROFILE:
        from PySide6.QtWebEngineCore import QWebEngineProfile
        _PROFILE.append(QWebEngineProfile())
    return _PROFILE[0]


class HtmlToPdf(QObject):
    """Print one HTML document to an A4 PDF with QtWebEngine (offscreen capable). `finished(ok)`."""
    finished = Signal(bool)

    def __init__(self, page_html: str, dest: str, margins_mm, timeout_s: float = 25, parent=None):
        super().__init__(None)          # owned by _LIVE until it finished, never by a parent that may go away first
        _LIVE.add(self)
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
        self.dest, self.margins = dest, margins_mm
        self._printed = self._done = False
        fd, self.tmp = tempfile.mkstemp(prefix="timmy-print-", suffix=".html")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page_html)
        self.page = QWebEnginePage(_profile())
        s = self.page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)   # Google Fonts (Heebo)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.PrintElementBackgrounds, True)
        self.page.loadFinished.connect(self._loaded)
        self.page.pdfPrintingFinished.connect(self._printed_cb)
        self._guard = QTimer(self, singleShot=True)
        self._guard.timeout.connect(self._print)
        self._guard.start(int(timeout_s * 1000))
        self._hard = QTimer(self, singleShot=True)
        self._hard.timeout.connect(lambda: self._finish(False))
        self._hard.start(int((timeout_s + 40) * 1000))
        self.page.load(QUrl.fromLocalFile(self.tmp))     # a file, not setHtml: no 2 MB limit for embedded images

    def _loaded(self, ok):
        if self._done:
            return
        if not ok:
            self._finish(False)
            return
        # wait for the fonts (Heebo), but not for long
        self.page.runJavaScript("document.fonts && document.fonts.ready.then(()=>true); true",
                                0, lambda _r: QTimer.singleShot(600, self._print))
        QTimer.singleShot(6000, self._print)

    def _print(self):
        if self._printed or self._done or self.page is None:
            return
        self._printed = True
        from PySide6.QtGui import QPageLayout, QPageSize
        t, r, b, left = self.margins
        lay = QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait, QMarginsF(left, t, r, b),
                          QPageLayout.Unit.Millimeter)
        try:
            if os.path.exists(self.dest):
                os.remove(self.dest)
        except OSError:
            pass
        self.page.printToPdf(self.dest, lay)

    def _printed_cb(self, path, ok):
        self._finish(bool(ok) and os.path.exists(self.dest) and os.path.getsize(self.dest) > 0)

    def _finish(self, ok):
        if self._done:
            return
        self._done = True
        self._guard.stop()
        self._hard.stop()
        try:
            os.remove(self.tmp)
        except OSError:
            pass
        page, self.page = self.page, None
        try:
            page.loadFinished.disconnect(self._loaded)
            page.pdfPrintingFinished.disconnect(self._printed_cb)
        except (RuntimeError, TypeError):
            pass
        try:
            self.finished.emit(ok)
        finally:
            page.deleteLater()
            _LIVE.discard(self)


def print_html(page_html: str, dest: str, contact: str = "", page_numbers: bool = True, done=None, parent=None):
    """HTML -> PDF with the Timmy line and the page footer; done(ok). Re-prints once if the Timmy line ended up alone
    on a last page. Returns the first HtmlToPdf (tests keep a reference)."""
    final, margins = prepare(page_html, contact, page_numbers)

    def second(ok):
        if done:
            done(ok)

    def first(ok):
        if ok:
            try:
                pages = pdf_pages_text(dest)
            except Exception:
                pages = []
            if len(pages) > 1 and lone_timmy_line(pages[-1], contact):
                again, m2 = prepare(page_html, contact, page_numbers, compact_footer=True)
                job2 = HtmlToPdf(again, dest, m2, parent=parent)
                job2.finished.connect(second)
                return
        if done:
            done(ok)

    job = HtmlToPdf(final, dest, margins, parent=parent)
    job.finished.connect(first)
    return job


# ---- the whole flow ---------------------------------------------------------------------------------------------
class PdfBuild(QObject):
    """md -> engine -> print. `finished(ok, used_claude, note)`. cancel() kills the engine and everything it started."""
    finished = Signal(bool, bool, str)
    _engine_done = Signal(object)

    def __init__(self, md_path: str, dest: str, local: bool = False, preview: bool = False, parent=None):
        super().__init__(None)          # kept alive by _LIVE while it runs (see HtmlToPdf)
        self.md, self.dest, self.local, self.preview = os.path.abspath(md_path), os.path.abspath(dest), local, preview
        self.cancelled = False
        self._job = None
        self._printer = None
        self._engine_done.connect(self._after_engine)

    def start(self):
        _LIVE.add(self)
        self.finished.connect(lambda *_: _LIVE.discard(self))
        threading.Thread(target=self._run_engine, name="pdf-engine", daemon=True).start()
        return self

    def cancel(self):
        self.cancelled = True
        if self._job:
            self._job.kill()

    def _run_engine(self):
        tmp = os.path.join(tempfile.gettempdir(), f"timmy-pdf-{uuid.uuid4().hex}.html")
        cmd = [R.child_python(), "-m", "timmy.engine.pdf", self.md, "--out", tmp]
        if self.local:
            cmd.append("--local")
        if self.preview:
            cmd.append("--preview")
        out, rc = "", None
        try:
            self._job = jobs.Job()
            p = self._job.popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=R.child_env(),
                                cwd=tempfile.gettempdir())
            try:
                o, _ = p.communicate(timeout=ENGINE_TIMEOUT)
                out, rc = (o or b"").decode("utf-8", "replace"), p.returncode
            except subprocess.TimeoutExpired:
                self._job.kill()
        except OSError:
            pass
        finally:
            if self._job:
                self._job.kill()
                self._job.close()
        res = {}
        for ln in reversed(out.splitlines()):
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    res = json.loads(ln)
                    break
                except ValueError:
                    continue
        page = ""
        try:
            with open(tmp, encoding="utf-8") as f:
                page = f.read()
        except OSError:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        self._engine_done.emit({"res": res, "html": page, "rc": rc})

    def _after_engine(self, r):
        if self.cancelled:
            self.finished.emit(False, False, "בוטל")
            return
        res, page = r["res"] or {}, r["html"]
        used = bool(res.get("claude"))
        note = res.get("error") or ""
        miss = res.get("missing_images") or 0
        if miss and not note:
            note = ("לא מצאתי את הקובץ של " + ("תמונה אחת" if miss == 1 else f"{miss} תמונות") + " מהסיכום (אולי נמחק או הועבר), אז "
                    + ("היא לא נכנסה" if miss == 1 else "הן לא נכנסו") + " ל-PDF.")
        made = res.get("pdf")
        if made and os.path.exists(made):
            try:
                if os.path.exists(self.dest):
                    os.remove(self.dest)
                os.replace(made, self.dest)
                ok = True
            except OSError:
                import shutil
                try:
                    shutil.copyfile(made, self.dest)
                    ok = True
                except OSError:
                    ok = False
            self.finished.emit(ok, used, note)
            return
        if res.get("skill_failed"):
            note = f"לא הצלחתי להפעיל את הסקיל {res.get('skill') or ''}, אז הכנתי את ה-PDF בעיצוב הרגיל של טימי."
        if not page:
            try:
                with open(self.md, encoding="utf-8-sig") as f:
                    page = fallback_html(f.read())
            except OSError:
                page = fallback_html("")
            note = note or "pdf.py לא רץ"
        contact = str(res.get("footer") or "")
        self._printer = print_html(page, self.dest, contact, True, done=lambda ok: self.finished.emit(ok, used, note))


def first_page_png(pdf: str, png: str, width: int = 900) -> bool:
    """The first page as an image (previews, CI screenshots)."""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtPdf import QPdfDocument
    d = QPdfDocument()
    d.load(pdf)
    if d.pageCount() < 1:
        return False
    s = d.pagePointSize(0)
    img = d.render(0, QSize(width, int(width * s.height() / max(1.0, s.width()))))
    out = QImage(img.size(), QImage.Format.Format_RGB32)
    out.fill(QColor("white"))
    p = QPainter(out)
    p.drawImage(0, 0, img)
    p.end()
    d.close()
    return out.save(png)


if __name__ == "__main__":       # manual: python -m timmy.ui.pdf_render <summary.md> <out.pdf> [--local]
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv[:1])
    b = PdfBuild(sys.argv[1], sys.argv[2], local="--local" in sys.argv)
    b.finished.connect(lambda ok, c, n: (print(json.dumps({"ok": ok, "claude": c, "note": n}, ensure_ascii=False)), app.quit()))
    b.start()
    sys.exit(app.exec())
