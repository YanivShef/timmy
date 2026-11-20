"""Summary Markdown -> blocks (port of Recorder.swift parseMarkdown) -> Qt rich text for the "השיחות שלי" pane.

Blocks: heading, para, bullet (with indent), task, numbered, table, quote, rule, image (caption + path relative to
the summary's folder). Inline: **bold**, *italic*, `code`, [text](url), [[wikilinks]] shown as plain names.
Qt's rich text is a subset of HTML 4 (no border-radius), so the look is simpler than SwiftUI but the structure and
the colors (accent markers, ink opacities) are the Mac ones.
"""
from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass

from timmy.ui.calls import plain_wikilinks


@dataclass
class Block:
    kind: str                 # heading | para | bullet | task | numbered | table | quote | rule | image
    text: str = ""
    level: int = 0            # heading level / bullet indent
    done: bool = False        # task
    num: str = ""             # numbered
    rows: list | None = None  # table
    path: str = ""            # image


def _table_row(t: str) -> list[str]:
    r = t.replace("\\|", "\0")
    if r.startswith("|"):
        r = r[1:]
    if r.endswith("|"):
        r = r[:-1]
    return [c.strip().replace("\0", "|") for c in r.split("|")]


def _image(t: str):
    m = re.match(r"^!\[([^\]\n]*)\]\((.+)\)$", t)
    if not m:
        return None
    p = m.group(2).strip()
    if p.startswith("<") and p.endswith(">"):
        p = p[1:-1].strip()
    if "%" in p:
        from urllib.parse import unquote
        p = unquote(p)
    return m.group(1).strip(), p


def parse(text: str) -> list[Block]:
    blocks, para, table = [], [], []

    def flush_para():
        if para:
            blocks.append(Block("para", " ".join(para)))
            para.clear()

    def flush_table():
        if table:
            blocks.append(Block("table", rows=list(table)))
            table.clear()

    for raw in (text or "").split("\n"):
        line = raw.replace("\r", "")
        t = line.strip()
        if t.startswith("|"):
            flush_para()
            cells = _table_row(t)
            if all(not c or re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            table.append(cells)
            continue
        flush_table()
        if not t:
            flush_para()
            continue
        indent = (len(line) - len(line.lstrip(" \t"))) // 2
        im = _image(t)
        if im:
            flush_para()
            blocks.append(Block("image", im[0], path=im[1]))
        elif m := re.match(r"^(#{1,6})\s+(.*)$", t):
            flush_para()
            blocks.append(Block("heading", m.group(2), level=len(m.group(1))))
        elif m := re.match(r"^[-*+]\s+\[([ xX])\]\s+(.*)$", t):
            flush_para()
            blocks.append(Block("task", m.group(2), done=m.group(1) != " "))
        elif re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", t):
            flush_para()
            blocks.append(Block("rule"))
        elif m := re.match(r"^[-*+•]\s+(.*)$", t):
            flush_para()
            blocks.append(Block("bullet", m.group(1), level=indent))
        elif m := re.match(r"^(\d+)[.)]\s+(.*)$", t):
            flush_para()
            blocks.append(Block("numbered", m.group(2), num=m.group(1)))
        elif m := re.match(r"^>\s?(.*)$", t):
            flush_para()
            blocks.append(Block("quote", m.group(1)))
        else:
            para.append(t)
    flush_para()
    flush_table()
    return blocks


def inline(s: str, link_color: str = "") -> str:
    """Escape, then **bold** / *italic* / `code` / [text](url). Wikilinks become their plain names."""
    t = html.escape(plain_wikilinks(s or ""), quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", t)
    style = f' style="color:{link_color}"' if link_color else ""
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{m.group(2)}"{style}>{m.group(1)}</a>', t)
    return t


def to_html(blocks: list[Block], look, base_dir: str = "") -> str:
    """Qt rich text, RTL. `look` = timmy.ui.theme.Look."""
    ink = lambda a=1.0: look.css(look.ink, a)          # noqa: E731
    amber = lambda a=1.0: look.css(look.amber, a)      # noqa: E731

    def solid(c, a):
        """A fill for a cell / box as an opaque color over the window background. Qt's rich text paints a CSS
        `background` on <td> / <table> behind the text only (QA: a darker box around each word); the bgcolor attribute
        fills the whole cell, but it takes no rgba(), so the soft tint is mixed with the background here."""
        bg = look.bg
        return "#%02x%02x%02x" % tuple(round((bg[i] * (1 - a) + c[i] * a) * 255) for i in range(3))
    out = [f'<div dir="rtl" style="color:{ink(0.86)}; font-size:14.5px;">']
    for b in blocks:
        if b.kind == "heading":
            size = 20 if b.level <= 1 else (16.5 if b.level == 2 else 14.5)
            mark = f'<span style="color:{amber()};">▎</span> ' if b.level <= 2 else ""
            out.append(f'<p style="margin-top:{16 if b.level <= 2 else 10}px; margin-bottom:4px; font-size:{size}px; '
                       f'font-weight:700; color:{ink()};">{mark}{inline(b.text, amber())}</p>')
        elif b.kind == "para":
            out.append(f'<p style="margin:0 0 9px 0; line-height:150%;">{inline(b.text, amber())}</p>')
        elif b.kind == "bullet":
            pad = 4 + b.level * 18
            out.append(f'<p style="margin:2px {pad}px 4px 0; line-height:145%;"><span style="color:{amber(0.85)};">●</span>'
                       f'&nbsp;&nbsp;{inline(b.text, amber())}</p>')
        elif b.kind == "numbered":
            out.append(f'<p style="margin:2px 4px 4px 0; line-height:145%;"><span style="color:{amber()}; font-weight:600;">'
                       f'{html.escape(b.num)}.</span>&nbsp;&nbsp;{inline(b.text, amber())}</p>')
        elif b.kind == "task":
            box = f'<span style="color:{amber()};">☑</span>' if b.done else f'<span style="color:{ink(0.55)};">☐</span>'
            body = inline(b.text, amber())
            if b.done:
                body = f'<s style="color:{ink(0.5)};">{body}</s>'
            out.append(f'<table width="100%" cellpadding="7" style="margin:3px 0;"><tr>'
                       f'<td bgcolor="{solid(look.ink, 0.045)}">{box}&nbsp;&nbsp;{body}</td></tr></table>')
        elif b.kind == "quote":
            out.append(f'<table cellpadding="0" style="margin:4px 0;"><tr><td width="3" bgcolor="{solid(look.amber, 0.7)}"></td>'
                       f'<td style="padding-right:10px; color:{ink(0.7)};">{inline(b.text, amber())}</td></tr></table>')
        elif b.kind == "rule":
            out.append(f'<hr style="background:{ink(0.1)}; height:1px; border:none;"/>')
        elif b.kind == "table":
            rows = b.rows or []
            cols = max((len(r) for r in rows), default=0)
            out.append('<table width="100%" cellspacing="0" cellpadding="7" style="margin:6px 0; border-collapse:collapse;">')
            for i, r in enumerate(rows):
                # Qt lays out a full-width rich-text table left to right whatever the direction: first column on the right
                cells = list(reversed(r + [""] * (cols - len(r))))
                tag = "th" if i == 0 else "td"
                bg = solid(look.amber, 0.16) if i == 0 else (solid(look.ink, 0.03) if i % 2 == 0 else "")
                fill = f' bgcolor="{bg}"' if bg else ""
                out.append("<tr>" + "".join(
                    f'<{tag} align="right"{fill} style="border-bottom:1px solid {ink(0.08)}; font-weight:{700 if i == 0 else 400};">'
                    f"{inline(c, amber())}</{tag}>" for c in cells) + "</tr>")
            out.append("</table>")
        elif b.kind == "image":
            p = os.path.expanduser(b.path)
            if not os.path.isabs(p) and base_dir:
                p = os.path.join(base_dir, p)
            if os.path.exists(p):
                from PySide6.QtCore import QUrl
                src = QUrl.fromLocalFile(p).toString()
                out.append(f'<p align="center" style="margin:10px 0 2px 0;"><img src="{html.escape(src)}" width="420"/></p>')
            else:
                out.append(f'<p align="center" style="color:{ink(0.45)}; font-size:12px;">התמונה לא נמצאה</p>')
            if b.text:
                out.append(f'<p align="center" style="color:{ink(0.6)}; font-size:12px; margin-bottom:10px;">{inline(b.text)}</p>')
    out.append("</div>")
    return "\n".join(out)
