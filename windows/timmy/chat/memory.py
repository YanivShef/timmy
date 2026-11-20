"""Shared memory: <app dir>/memory.json {"notes": [...]}. Things Timmy learned about the owner in any chat (business
name, preferred tone, favourite color). The vendored pdf.py reads the same file. Port of Recorder.swift: at most 40
notes (the oldest drop out), one clean line each, no duplicates (case-insensitive), forget by exact text or by one
clearly matching note. One lock for read-modify-write (the chats and the PDF design chat run on different threads).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading

from timmy import paths

MAX = 40
LONG_DASH = chr(0x2014)
_LOCK = threading.RLock()
_LISTENERS = []


def on_change(fn):
    """fn() after every save (the UI refreshes the "מה אני זוכר" panel)."""
    _LISTENERS.append(fn)


def load() -> list[str]:
    try:
        with open(paths.memory_path(), encoding="utf-8-sig") as f:
            o = json.load(f)
    except (OSError, ValueError):
        return []
    notes = o.get("notes") if isinstance(o, dict) else None
    return [n for n in (notes or []) if isinstance(n, str) and n]


def _save(notes: list[str]) -> None:
    p = paths.memory_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".memory-", suffix=".json", dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"notes": notes[-MAX:]}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
    for fn in list(_LISTENERS):
        try:
            fn()
        except Exception:
            pass


def clean_note(s: str) -> str:
    t = (s or "").replace("\n", " ").replace(LONG_DASH, "-").strip()
    while t[:1] in ("-", "•", "*") and t:
        t = t[1:].strip()
    return t[:200]


def remember(new: list[str]) -> list[str]:
    """Adds notes (no duplicates). Returns what was really added."""
    with _LOCK:
        notes, added = load(), []
        for raw in new:
            t = clean_note(raw)
            if len(t) < 3 or any(n.casefold() == t.casefold() for n in notes):
                continue
            notes.append(t)
            added.append(t)
        if added:
            _save(notes)
        return added


def _words(x: str) -> set:
    return {w for w in x.lower().split() if len(w) > 1}


def forget(text: str) -> str | None:
    """Removes a note: exact match first, then the one note that contains the text (or is contained in it), only for
    texts of 6+ characters and only when one note clearly matches, so "טון" never deletes a random note."""
    with _LOCK:
        t = clean_note(text)
        if not t:
            return None
        notes = load()
        idx = next((i for i, n in enumerate(notes) if n.casefold() == t.casefold()), None)
        if idx is None and len(t) >= 6:
            tl = t.casefold()
            hits = [i for i, n in enumerate(notes) if len(n) >= 6 and (tl in n.casefold() or n.casefold() in tl)]
            if len(hits) == 1:
                idx = hits[0]
            elif len(hits) > 1:
                tw = _words(t)
                scored = sorted(((i, len(_words(notes[i]) & tw)) for i in hits), key=lambda x: -x[1])
                if scored[0][1] > scored[1][1]:
                    idx = scored[0][0]
        if idx is None:
            return None
        gone = notes.pop(idx)
        _save(notes)
        return gone


def block() -> str:
    n = load()
    return "\n".join("- " + x for x in n) if n else "אין עדיין"
