"""The calls in the recordings folder ("השיחות שלי"), without Qt: list, parse, search, rename.

Port of Recorder.swift `CallItem`, `loadCall`, `listCalls`, `callTitle`, `safeCallTitle`, `renamedBase`,
`retitledMarkdown`, `renameCall`, `callDateText`, `summaryBody`, with the Windows file-name rules of
timmy.engine.files on top (no trailing dots/spaces, no reserved device names, the whole path under MAX_PATH).

A call = "<YYYY-MM-DD HH-MM> [<person> - ]<title>.md" (+ " - תמלול.txt", ".mp3", ".pdf", " - תמונות" folder), and
optionally a copy of the .md in config `meetings_dir`.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field

from timmy import config as tconfig
from timmy import paths
from timmy.engine import files as F

TRANSCRIPT_SUFFIX = " - תמלול.txt"
IMAGES_SUFFIX = " - תמונות"
POINTS_SUFFIX = " נקודות.md"
TITLE_MAX = 60
MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]


def recordings_dir(cfg: dict | None = None) -> str:
    cfg = tconfig.load() if cfg is None else cfg
    p = str(cfg.get("output_dir") or "").strip()
    return os.path.expanduser(p) if p else str(paths.default_output_dir())


def meetings_dir(cfg: dict | None = None) -> str | None:
    cfg = tconfig.load() if cfg is None else cfg
    p = str(cfg.get("meetings_dir") or "").strip()
    if not p:
        return None
    p = os.path.expanduser(p)
    return p if os.path.isdir(p) else None


def plain_wikilinks(s: str) -> str:
    """[[note|name]] / [[name#section]] -> name"""
    s = re.sub(r"\[\[[^\]|]*\\?\|([^\]]+)\]\]", r"\1", s or "")
    return re.sub(r"\[\[([^\]#|]+)(#[^\]]*)?\]\]", r"\1", s)


def call_title(path: str) -> str:
    """'2026-11-17 21-40 דנה כהן - הצעת מחיר.md' -> 'דנה כהן - הצעת מחיר'"""
    n = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}(-\d{2})? ", "", n)


def safe_call_title(t: str) -> str:
    """Mac safeCallTitle (no \\ / : * ? " < > | and line breaks, single spaces, 60 characters) + Windows rules."""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "", t or "")
    s = re.sub(r" {2,}", " ", s).strip()[:TITLE_MAX].strip()
    s = re.sub(r"[\x00-\x1f\x7f]", "", s).rstrip(". ").strip()
    if s and s.upper().split(".")[0] in F.RESERVED:
        s = "_" + s
    return s


def _group(s: str, pattern: str, i: int = 1):
    m = re.search(pattern, s)
    return m.group(i) if m else None


@dataclass
class Call:
    path: str
    title: str
    person: str
    date: dt.datetime
    length: str
    modified: float
    file_title: str = ""
    text: str = field(default="", repr=False)

    @property
    def id(self) -> str:
        return self.path

    @property
    def base(self) -> str:
        return self.path[:-3]

    @property
    def transcript_path(self) -> str:
        return self.base + TRANSCRIPT_SUFFIX

    @property
    def pdf_path(self) -> str:
        return self.base + ".pdf"

    @property
    def mp3_path(self) -> str:
        return self.base + ".mp3"

    @property
    def has_transcript(self) -> bool:
        return os.path.exists(self.transcript_path)

    def matches(self, q: str) -> bool:
        q = (q or "").strip().casefold()
        return not q or q in self.title.casefold() or q in self.person.casefold()


def load_call(path: str, modified: float | None = None) -> Call:
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        text = ""
    if modified is None:
        try:
            modified = os.path.getmtime(path)
        except OSError:
            modified = 0.0
    lines = text.split("\n")
    meta = next((ln for ln in lines[:15] if "**תאריך:**" in ln or "**עם:**" in ln), "")
    person = plain_wikilinks(_group(meta, r"\*\*עם:\*\*\s*(.+?)\s*(·|$)") or "").strip()
    length = _group(meta, r"\*\*אורך:\*\*\s*(.+?)\s*(·|$)") or ""
    date = dt.datetime.fromtimestamp(modified or 0)
    stamp = _group(os.path.basename(path), r"^(\d{4}-\d{2}-\d{2} \d{2}-\d{2})")
    if stamp:
        try:
            date = dt.datetime.strptime(stamp, "%Y-%m-%d %H-%M")
        except ValueError:
            pass
    title = call_title(path)
    if person and " - " in title:
        pre, rest = title.split(" - ", 1)
        if person.startswith(pre) or pre.startswith(person.split(" ")[0] if person else person):
            title = rest
    shown = title
    head = next((ln for ln in lines[:5] if ln.startswith("# ")), None)
    if head:
        h = head[2:].strip()
        if h and safe_call_title(h) == safe_call_title(title):
            shown = h
    return Call(path=path, title=shown, person=person, date=date, length=length, modified=modified or 0.0,
                file_title=title, text=text)


def _md_files(folder: str):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [n for n in names if n.endswith(".md") and not n.endswith(POINTS_SUFFIX) and not n.startswith(".")]


def list_calls(folder: str | None = None) -> list[Call]:
    folder = folder or recordings_dir()
    out = []
    for n in _md_files(folder):
        p = os.path.join(folder, n)
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        out.append(load_call(p, m))
    out.sort(key=lambda c: c.date, reverse=True)
    return out


def signature(folder: str | None = None) -> str:
    """Changes when a call is added, removed or edited (the window polls it every 2 s, like the Mac)."""
    folder = folder or recordings_dir()
    parts = []
    for n in _md_files(folder):
        try:
            parts.append(f"{n}@{os.path.getmtime(os.path.join(folder, n))}")
        except OSError:
            pass
    return folder + "|" + "|".join(sorted(parts))


def date_text(d: dt.datetime, now: dt.datetime | None = None) -> str:
    """Mac callDateText: 'היום, 14:03' / 'אתמול, 09:10' / '3 בנובמבר, 10:00' / '3 בנובמבר 2025, 10:00'."""
    now = now or dt.datetime.now()
    time = d.strftime("%H:%M")
    if d.date() == now.date():
        return "היום, " + time
    if d.date() == (now - dt.timedelta(days=1)).date():
        return "אתמול, " + time
    day = f"{d.day} ב{MONTHS[d.month - 1]}"
    if d.year != now.year:
        day += f" {d.year}"
    return day + ", " + time


def summary_body(text: str) -> str:
    """The summary without the title, the date line and the transcript line (they are in the window header)."""
    return "\n".join(ln for ln in (text or "").split("\n")
                     if not (ln.startswith("# ") or "**תאריך:**" in ln or ln.startswith("תמלול מלא:")))


# ---- rename ---------------------------------------------------------------------------------------------------------

def renamed_base(c: Call, title: str) -> str:
    """The new file base name (no folder, no extension): same stamp and person prefix, new title."""
    base = os.path.splitext(os.path.basename(c.path))[0]
    stamp = _group(base, r"^(\d{4}-\d{2}-\d{2} \d{2}-\d{2}(-\d{2})?) ")
    rest = call_title(c.path)
    ft = c.file_title or c.title
    prefix = ""
    if rest != ft and rest.endswith(ft) and len(rest) > len(ft):
        prefix = rest[:-len(ft)]
    return (stamp + " " if stamp else "") + prefix + title


def _parse_image_line(line: str):
    m = re.match(r"^!\[([^\]\n]*)\]\((.+)\)$", line.strip())
    if not m:
        return None
    p = m.group(2).strip()
    if p.startswith("<") and p.endswith(">"):
        p = p[1:-1].strip()
    if "%" in p:
        from urllib.parse import unquote
        p = unquote(p)
    return m.group(1).strip(), p


def clean_caption(s: str) -> str:
    t = re.sub(r"[\[\]\r\n\t]", " ", s or "").replace("\u2014", "-")
    t = re.sub(r" {2,}", " ", t).strip()
    if t.startswith('"') and t.endswith('"') and len(t) > 1:
        t = t[1:-1]
    if t.endswith(".") and not t.endswith(".."):
        t = t[:-1]
    return t[:180]


def retitled_markdown(text: str, title: str, new_base: str, old_base: str | None = None) -> str:
    lines = text.split("\n")
    headed = False
    old_imgs = (old_base + IMAGES_SUFFIX + "/") if old_base else None
    new_imgs = new_base + IMAGES_SUFFIX + "/"
    for i, ln in enumerate(lines):
        if not headed and ln.startswith("# "):
            lines[i] = "# " + title
            headed = True
        elif ln.startswith("תמלול מלא:"):
            lines[i] = "תמלול מלא: " + new_base + TRANSCRIPT_SUFFIX
        elif old_imgs:
            im = _parse_image_line(ln)
            if im and im[1].startswith(old_imgs):
                lines[i] = f"![{clean_caption(im[0])}]({new_imgs}{im[1][len(old_imgs):]})"
    return "\n".join(lines)


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _move_no_clobber(a: str, b: str) -> bool:
    try:
        if os.path.exists(b) and not _same(a, b):
            return False
        os.rename(a, b)          # Windows: fails if b exists; a case-only rename of the same file works
        return True
    except OSError:
        return False


def _write_text(path: str, text: str) -> None:
    tmp = path + ".timmy-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _fit(folder: str, new_base: str, title: str, c: Call) -> tuple[str, str]:
    """Shorten the title until the longest file of the call stays under MAX_PATH (long paths are off on most PCs)."""
    budget = F.MAX_PATH - 1 - len(TRANSCRIPT_SUFFIX)
    while len(os.path.join(folder, new_base)) > budget and len(title) > 8:
        title = title[:-1].rstrip(". ")
        new_base = renamed_base(c, title)
    return new_base, title


class RenameBusy(Exception):
    pass


def rename_call(c: Call, new_title: str, busy: str | None = None, cfg: dict | None = None) -> tuple[str | None, str]:
    """Rename a call: the title inside the summary + every file of the call + the meetings-folder copy.
    Returns (Hebrew error or None, new .md path). Nothing changes when anything fails (rollback).
    `busy`: a Hebrew reason the call cannot be renamed right now (PDF being made...)."""
    raw = " ".join((new_title or "").splitlines()).strip()[:120]
    t = safe_call_title(raw)
    if not t:
        return "צריך לכתוב כותרת", c.path
    if busy:
        return busy, c.path
    folder = os.path.dirname(c.path)
    old_base = os.path.splitext(os.path.basename(c.path))[0]
    new_base, t = _fit(folder, renamed_base(c, t), t, c)
    mdir = meetings_dir(cfg)
    other_meetings = mdir and os.path.normcase(os.path.abspath(mdir)) != os.path.normcase(os.path.abspath(folder))

    if new_base == old_base:
        # the file name stays (for example only a "?" was added), the title inside the summary changes
        if raw != c.title:
            try:
                with open(c.path, encoding="utf-8-sig") as f:
                    old = f.read()
                _write_text(c.path, retitled_markdown(old, raw, new_base))
                if other_meetings:
                    a = os.path.join(mdir, old_base + ".md")
                    if os.path.exists(a):
                        with open(a, encoding="utf-8-sig") as f:
                            _write_text(a, retitled_markdown(f.read(), raw, new_base))
            except OSError:
                return "לא הצלחתי לעדכן את הסיכום. שום דבר לא השתנה", c.path
        return None, c.path

    plan = []
    for sfx in (".md", TRANSCRIPT_SUFFIX, ".mp3", ".pdf", IMAGES_SUFFIX):
        a = os.path.join(folder, old_base + sfx)
        if os.path.exists(a):
            plan.append((a, os.path.join(folder, new_base + sfx)))
    copy = None
    if other_meetings:
        a = os.path.join(mdir, old_base + ".md")
        if os.path.exists(a):
            copy = (a, os.path.join(mdir, new_base + ".md"))
            plan.append(copy)
    if not plan or os.path.normcase(plan[0][0]) != os.path.normcase(c.path):
        return "לא מצאתי את קובץ הסיכום. אולי הוא הועבר או נמחק", c.path
    for a, b in plan:
        if os.path.exists(b) and not _same(a, b):
            return f'כבר יש קובץ בשם "{os.path.basename(b)}". תבחר כותרת אחרת', c.path

    def read(p):
        try:
            with open(p, encoding="utf-8-sig") as f:
                return f.read()
        except OSError:
            return None

    old_text = read(c.path)
    old_copy = read(copy[0]) if copy else None
    done = []

    def rollback():
        for a, b in reversed(done):
            try:
                os.rename(b, a)
            except OSError:
                pass

    for a, b in plan:
        if not _move_no_clobber(a, b):
            rollback()
            if copy and a == copy[0]:
                return "לא הצלחתי לשנות את השם של העותק בתיקיית הפגישות. שום דבר לא השתנה", c.path
            return (f'לא הצלחתי לשנות את השם של "{os.path.basename(a)}" (אולי הוא פתוח בתוכנה אחרת). '
                    "שום דבר לא השתנה"), c.path
        done.append((a, b))
    try:
        if old_text is not None:
            _write_text(plan[0][1], retitled_markdown(old_text, raw, new_base, old_base))
        if copy and old_copy is not None:
            _write_text(copy[1], retitled_markdown(old_copy, raw, new_base))
    except OSError:
        try:
            if old_text is not None:
                _write_text(plan[0][1], old_text)
            if copy and old_copy is not None:
                _write_text(copy[1], old_copy)
        except OSError:
            pass
        rollback()
        return "לא הצלחתי לעדכן את הסיכום. שום דבר לא השתנה", c.path
    return None, plan[0][1]
