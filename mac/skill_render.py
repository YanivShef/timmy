#!/usr/bin/env python3
# TIMMY-SKILL-RENDER v6
# Renders a PDF from Markdown with this skill's design (assets/template.html), via headless Google Chrome.
# Made by Timmy (טימי). Python 3.8+, standard library only. Works from Claude Code and from Timmy.
#
# Usage:
#   python3 scripts/render.py --input doc.md --output doc.pdf --title "Title" [--kicker "סיכום שיחה"] [--meta "17 בנובמבר 2026 · 24 דק׳"]
#                             [--footer "050-1234567 · site.co.il"] [--logo logo.png] [--brand "שם העסק"] [--tagline "תיאור קצר"]
#                             [--html-only] [--chrome /path/to/chrome]
#
# Markdown: ## / ### headings, paragraphs, **bold**, *italic*, ==highlight==, - lists, 1. lists, "- [ ] task (owner)" tasks,
# GitHub tables, > quotes, --- rules, ":::lead ... :::" for an opening card, and ![caption](image path) on its own line for an image
# (embedded, up to half a page, caption under it; a relative path is relative to the Markdown file).
# The template must contain {{TITLE}} and {{CONTENT}}; optional {{FOOTER}}, {{BRAND_HEADER}} (logo + business name), {{LOGO_SRC}},
# {{BRAND_NAME}}, {{BRAND_TAGLINE}}. Without {{BRAND_HEADER}} the logo/name row goes above the title. Unknown {{...}} placeholders are left empty.
# Scripts never run in the page (Content-Security-Policy), and nothing loads from the network except Google Fonts.
import argparse, base64, hashlib, html, os, re, shutil, ssl, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "assets" / "template.html"
CHROMES = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
           "/Applications/Chromium.app/Contents/MacOS/Chromium",
           "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
           "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]


def inline(s):
    s = re.sub(r"\[\[(?:[^\]|]*\\?\|)?([^\]#|]+)(?:#[^\]]*)?\]\]", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: "\x01" + m.group(1) + "\x02" + m.group(2) + "\x03", s)
    s = html.escape(s, quote=False)
    s = re.sub(r"\x01(.*?)\x02(.*?)\x03", lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"==(.+?)==", r"<mark>\1</mark>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r'<code dir="ltr">\1</code>', s)
    return s.replace("—", "-")


def task_html(t, done=False):
    owner = ""
    m = re.match(r"^(.*?)\s*\((?:אחראי:?\s*)?([^()]{1,30})\)\s*\.?$", t)
    if m: t, owner = m.group(1), m.group(2)
    o = f'<span class="owner">{inline(owner.strip())}</span>' if owner.strip() else ""
    cls = ' class="task done"' if done else ' class="task"'
    return f"<li><span{cls}>{inline(t.strip())}</span>{o}</li>"


IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
IMG_CSS = ("<style>figure.timmy-fig{margin:10px 0 16px;padding:0;text-align:center;break-inside:avoid;page-break-inside:avoid}"
           "figure.timmy-fig img{display:block;margin:0 auto;max-width:100%;max-height:95mm;width:auto;height:auto;object-fit:contain;"
           "border-radius:10px;border:1px solid rgba(0,0,0,.09)}"
           "figure.timmy-fig.tall img{max-height:85mm}"
           ".timmy-row{display:flex;direction:rtl;flex-direction:row;justify-content:center;align-items:flex-start;gap:8mm;margin:10px 0 16px;break-inside:avoid;page-break-inside:avoid}"
           ".timmy-row figure.timmy-fig{margin:0;flex:0 1 auto;max-width:48%}"
           "figure.timmy-fig figcaption{margin:7px auto 0;max-width:92%;font-size:.85em;line-height:1.5;opacity:.75;text-align:center}</style>")
BASE_DIR = [Path.cwd()]


def image_kind(data):
    """The real image type from the first bytes (not the file name), or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp"
    return None


def image_size(d):
    """(width, height) of a PNG / JPEG from its header, or None."""
    try:
        if d.startswith(b"\x89PNG"): return int.from_bytes(d[16:20], "big"), int.from_bytes(d[20:24], "big")
        if d.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(d):
                if d[i] != 0xFF: i += 1; continue
                m = d[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return int.from_bytes(d[i + 7:i + 9], "big"), int.from_bytes(d[i + 5:i + 7], "big")
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7: i += 2; continue
                i += 2 + int.from_bytes(d[i + 2:i + 4], "big")
    except Exception:
        pass
    return None


def figure_html(src, caption):
    src = src.strip()
    if src.startswith("<") and src.endswith(">"): src = src[1:-1].strip()
    path = Path(os.path.expanduser(src))
    if not path.is_absolute(): path = BASE_DIR[0] / path
    try:
        path = path.resolve()   # a link is checked by what it points to
        if path.suffix.lower() not in IMG_MIME or not path.is_file() or path.stat().st_size > 15_000_000: return ""
        raw = path.read_bytes()
    except Exception:
        return ""
    mime = image_kind(raw[:16])   # only a real image is embedded, whatever the file is called
    if not mime: return ""
    sz = image_size(raw[:256 * 1024])
    tall = bool(sz and sz[0] > 0 and sz[1] > sz[0] * 1.25)
    data = base64.b64encode(raw).decode()
    cap = f"<figcaption>{inline(caption)}</figcaption>" if caption.strip() else ""
    return f'<figure class="timmy-fig{" tall" if tall else ""}"><img src="data:{mime};base64,{data}" alt="">{cap}</figure>'


def pair_tall(out):
    """Two tall (phone) screenshots in a row sit side by side instead of a page each."""
    res, i = [], 0
    while i < len(out):
        a = out[i]
        if a.startswith('<figure class="timmy-fig tall"') and i + 1 < len(out) and out[i + 1].startswith('<figure class="timmy-fig tall"'):
            res.append('<div class="timmy-row">' + a + out[i + 1] + "</div>"); i += 2; continue
        res.append(a); i += 1
    return res


def split_row(r):
    r = r.strip().replace("\\|", "\x00")
    if r.startswith("|"): r = r[1:]
    if r.endswith("|"): r = r[:-1]
    return [c.strip().replace("\x00", "|") for c in r.split("|")]


def md_to_html(md):
    out, para, lst, rows, lead, in_section = [], [], None, [], None, False

    def close():
        nonlocal para, lst, rows
        if para: out.append("<p>" + inline(" ".join(para)) + "</p>"); para = []
        if lst: out.append("</ol>" if lst == "ol" else "</ul>"); lst = None
        if rows:
            head, rest = rows[0], rows[1:]
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rest) + "</tbody></table>")
            rows = []

    def open_list(kind):
        nonlocal lst
        if lst != kind:
            close(); out.append({"ul": "<ul>", "ol": "<ol>", "tasks": '<ul class="tasks">'}[kind]); lst = kind

    for raw in md.splitlines():
        t = raw.strip()
        if lead is not None:
            if t == ":::":
                out.append('<div class="lead">' + "".join(f"<p>{inline(x)}</p>" for x in " ".join(lead).split("\x00") if x.strip()) + "</div>")
                lead = None
            else:
                lead.append(t if t else "\x00")
            continue
        if t.startswith(":::lead"): close(); lead = []; continue
        if t.startswith("|"):
            cells = split_row(t)
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c): continue
            if not rows: close()
            rows.append(cells); continue
        if rows: close()
        if not t: close(); continue
        m = re.match(r"^(#{1,6})\s+(.*)$", t)
        if m:
            close()
            lvl = max(2, min(3, len(m.group(1))))
            if lvl == 2:
                if in_section: out.append("</section>")
                out.append("<section>"); in_section = True
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>"); continue
        m = re.match(r"^[-*+]\s+\[([ xX])\]\s+(.*)$", t)
        if m: open_list("tasks"); out.append(task_html(m.group(2), m.group(1) != " ")); continue
        m = re.match(r"^[-*+•]\s+(.*)$", t)
        if m: open_list("ul"); out.append(f"<li>{inline(m.group(1))}</li>"); continue
        m = re.match(r"^\d+[.)]\s+(.*)$", t)
        if m: open_list("ol"); out.append(f"<li>{inline(m.group(1))}</li>"); continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", t): close(); out.append("<hr>"); continue
        m = re.match(r"^!\[([^\]]*)\]\((.+)\)$", t)
        if m: close(); out.append(figure_html(m.group(2), m.group(1))); continue
        m = re.match(r"^>\s?(.*)$", t)
        if m: close(); out.append(f"<blockquote>{inline(m.group(1))}</blockquote>"); continue
        if lst: close()
        para.append(t)
    close()
    if in_section: out.append("</section>")
    return "\n".join(pair_tall([x for x in out if x]))


CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'none\'; object-src \'none\'; '
       'style-src \'unsafe-inline\' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; img-src data:">')
BRAND_CSS = ("<style>.brand-bar{display:flex;align-items:center;gap:4mm;margin:0 0 6mm}.brand-bar .logo{max-height:16mm;max-width:45mm;object-fit:contain}"
             ".brand-bar .brand-name{font-weight:700;font-size:13pt}.brand-bar .tagline{font-size:9.5pt;opacity:.75}</style>")
CLIP_RE = re.compile(r"(?:-webkit-)?background-clip\s*:\s*text", re.I)


def print_safe(css_text):
    """Gradient text (background-clip: text) prints as a solid box in Chrome's PDF output: keep the text, drop the gradient."""
    def fix(m):
        body = m.group(1)
        if not CLIP_RE.search(body) and not re.search(r"-webkit-text-fill-color\s*:\s*transparent", body, re.I): return m.group(0)
        first = re.search(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", body)
        decls = [d for d in body.split(";") if d.strip()]
        keep = [d for d in decls if not re.match(r"\s*(background(-image)?|(-webkit-)?background-clip|-webkit-text-fill-color)\s*:", d, re.I)]
        if not any(re.match(r"\s*color\s*:", d, re.I) for d in keep) and first: keep.append(" color: " + first.group(0))
        return "{" + ";".join(keep) + ";}"
    return re.sub(r"\{([^{}]*)\}", fix, css_text)


LOGO_CSS = ("<style>.brand-bar{height:auto!important;max-height:none!important;overflow:visible!important}"
            ".brand-bar img.logo,img[data-timmy-logo]{width:auto!important;height:auto!important;max-height:14mm!important;max-width:50mm!important;"
            "min-width:0!important;min-height:0!important;object-fit:contain!important;object-position:center!important;background:none!important;"
            "border:0!important;border-radius:0!important;padding:0!important;outline:0!important;box-shadow:none!important;clip-path:none!important;"
            "-webkit-mask:none!important;mask:none!important;filter:none!important;transform:none!important;opacity:1!important;aspect-ratio:auto!important;"
            "position:static!important;display:inline-block!important;vertical-align:middle!important;flex:none!important}</style>")
GFONT_RE = re.compile(r"https://fonts\.googleapis\.com/[^\s'\")<>]+")


def _fetch(url, ctx, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/4.0"})   # an old browser gets static TrueType files, not variable fonts
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read(6_000_000)


def inline_google_fonts(page):
    """Google Fonts serves variable fonts to Chrome, and Chrome prints those as Type 3 fonts: the text can't be selected or searched
    once the PDF is re-saved. Fetch the static TrueType files instead and embed them (cached). On any failure, leave the page as is."""
    urls = []
    for m in GFONT_RE.finditer(page):
        u = html.unescape(m.group(0))
        if u not in urls: urls.append(u)
    if not urls: return page
    try:
        try:
            import certifi; ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = ssl.create_default_context()
        cache = Path(tempfile.gettempdir()) / "timmy-skill-fonts"; cache.mkdir(exist_ok=True)
        deadline = time.time() + 25
        css_for = {}
        for u in urls:
            css = _fetch(u, ctx).decode("utf-8", "replace")
            if "@font-face" not in css: return page
            def data_uri(m):
                f = m.group(1)
                if not f.startswith("https://fonts.gstatic.com/"): raise ValueError("unexpected font host")
                c = cache / (hashlib.sha1(f.encode()).hexdigest() + ".font")
                if not c.exists():
                    if time.time() > deadline: raise TimeoutError("fonts took too long")
                    b = _fetch(f, ctx); t = c.with_suffix(".tmp%d" % os.getpid()); t.write_bytes(b); os.replace(t, c)
                mime = "font/woff2" if f.endswith(".woff2") else "font/woff" if f.endswith(".woff") else "font/ttf"
                return "url(data:%s;base64,%s)" % (mime, base64.b64encode(c.read_bytes()).decode())
            css_for[u] = re.sub(r"url\((https://[^)\s]+)\)", data_uri, css)
    except Exception:
        return page
    def link(m):
        u = (GFONT_RE.search(m.group(0)) or [None])[0]
        u = html.unescape(u) if u else u
        return f"<style>{css_for[u]}</style>" if u in css_for else m.group(0)
    page = re.sub(r"<link\b[^>]*https://fonts\.googleapis\.com/[^>]*>", link, page, flags=re.I)
    def imp(m):
        u = html.unescape(m.group(1))
        return css_for.get(u, m.group(0))
    page = re.sub(r"@import\s+(?:url\(\s*)?['\"]?(https://fonts\.googleapis\.com/[^'\")\s]+)['\"]?\s*\)?[^;]*;", imp, page, flags=re.I)
    return page


def logo_data_uri(path):
    if not path or not os.path.isfile(path): return ""
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp", "svg": "svg+xml"}.get(os.path.splitext(path)[1].lower().lstrip("."))
    if not mime or os.path.getsize(path) > 5_000_000: return ""
    with open(path, "rb") as f: return f"data:image/{mime};base64," + base64.b64encode(f.read()).decode()


def brand_header(logo, brand, tagline):
    if not logo and not brand: return ""
    img = f'<img class="logo" data-timmy-logo src="{logo}" alt="">' if logo else ""
    text = ""
    if brand or tagline:
        text = ('<div class="brand-text">' + (f'<div class="brand-name">{html.escape(brand)}</div>' if brand else "")
                + (f'<div class="tagline">{html.escape(tagline)}</div>' if tagline else "") + "</div>")
    return f'<div class="brand-bar">{img}{text}</div>'


def build_html(md, title, kicker, meta, footer, logo_path="", brand="", tagline=""):
    tpl = TEMPLATE.read_text(encoding="utf-8")
    tpl = re.sub(r"<!--.*?-->", "", tpl, flags=re.S)
    tpl = re.sub(r"(<style\b[^>]*>)(.*?)(</style>)", lambda m: m.group(1) + print_safe(m.group(2)) + m.group(3), tpl, flags=re.S | re.I)
    tpl = re.sub(r"<script\b.*?</script>", "", tpl, flags=re.S | re.I)
    logo = logo_data_uri(logo_path)
    bh = brand_header(logo, brand.strip(), tagline.strip())
    metas = "".join(f"<span>{inline(x.strip())}</span>" for x in (meta or "").split("·") if x.strip())
    head = ('<header class="doc-head">' + (f'<div class="kicker">{html.escape(kicker)}</div>' if kicker else "")
            + f"<h1>{inline(title)}</h1>" + (f'<div class="meta">{metas}</div>' if metas else "") + "</header>")
    content = head + "\n" + md_to_html(md)
    if bh and "{{BRAND_HEADER}}" not in tpl: content = bh + "\n" + content
    foot = f'<footer class="doc-foot">{inline(footer)}</footer>' if footer else ""
    if logo: tpl = re.sub(r"<img\b(?=[^>]*\{\{LOGO_SRC\}\})", "<img data-timmy-logo", tpl, flags=re.I)
    else: tpl = re.sub(r"<img\b[^>]*\{\{LOGO_SRC\}\}[^>]*>", "", tpl, flags=re.I)
    out = tpl.replace("{{TITLE}}", html.escape(title))
    if "{{FOOTER}}" in out: out = out.replace("{{FOOTER}}", foot)
    else: content += foot
    for k, v in {"BRAND_HEADER": bh, "LOGO_SRC": logo, "BRAND_NAME": html.escape(brand.strip()), "BRAND_TAGLINE": html.escape(tagline.strip())}.items():
        out = out.replace("{{" + k + "}}", v)
    out = re.sub(r"\{\{(?!CONTENT\}\})[A-Z_]+\}\}", "", out)
    extra = CSP + (BRAND_CSS if bh else "") + (IMG_CSS if "timmy-fig" in content else "")
    out = re.sub(r"<head\b[^>]*>", lambda m: m.group(0) + extra, out, count=1, flags=re.I) if re.search(r"<head\b", out, re.I) else extra + out
    if logo:   # a real logo keeps its own shape and proportions, even if the template styled ".logo" as a small circle or square
        out = re.sub(r"</head>", lambda m: LOGO_CSS + m.group(0), out, count=1, flags=re.I) if re.search(r"</head>", out, re.I) else out + LOGO_CSS
    out = inline_google_fonts(out)
    return out.replace("{{CONTENT}}", content)


def chrome_path(explicit):
    for c in [explicit] + CHROMES + [shutil.which("google-chrome") or "", shutil.which("chromium") or ""]:
        if c and os.path.exists(c): return c
    return ""


def main():
    p = argparse.ArgumentParser(description="Render a PDF from Markdown with this skill's design")
    p.add_argument("--input", required=True); p.add_argument("--output", required=True); p.add_argument("--title", required=True)
    p.add_argument("--kicker", default=""); p.add_argument("--meta", default=""); p.add_argument("--footer", default="")
    p.add_argument("--logo", default=""); p.add_argument("--brand", default=""); p.add_argument("--tagline", default="")
    p.add_argument("--html-only", action="store_true"); p.add_argument("--chrome", default="")
    a = p.parse_args()
    md = Path(a.input).read_text(encoding="utf-8")
    BASE_DIR[0] = Path(a.input).resolve().parent
    page = build_html(md, a.title, a.kicker, a.meta, a.footer, os.path.expanduser(a.logo), a.brand, a.tagline)
    out = Path(a.output).resolve()
    html_path = out.with_suffix(".html")
    html_path.write_text(page, encoding="utf-8")
    if a.html_only:
        print(f"wrote {html_path}"); return 0
    chrome = chrome_path(a.chrome)
    if not chrome:
        print(f"Google Chrome not found. The HTML is at {html_path}; open it and print to PDF.", file=sys.stderr); return 3
    prof = tempfile.mkdtemp(prefix="skill-render-chrome-")
    try:
        subprocess.run([chrome, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check", f"--user-data-dir={prof}",
                        "--no-pdf-header-footer", "--virtual-time-budget=8000", f"--print-to-pdf={out}", html_path.as_uri()],
                       check=True, capture_output=True, timeout=90)
    finally:
        shutil.rmtree(prof, ignore_errors=True)
        html_path.unlink(missing_ok=True)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
