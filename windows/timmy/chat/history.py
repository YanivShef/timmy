"""Chat messages and their files: <app dir>/chat-improve.json, chat-design.json, pdf/chat.json. Same JSON keys as the
Mac ChatMsg (id, fromUser, text, image, pdf, isError, ctx, files), so the files look the same on both systems.
`ctx` is a hidden note for the next prompt (the look before / after a change, which skill was linked): never shown.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field


@dataclass
class ChatMsg:
    from_user: bool
    text: str
    image: str | None = None       # preview (page 1 as PNG) or the attached logo
    pdf: str | None = None
    is_error: bool = False
    ctx: str | None = None
    files: list[str] | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()).upper())

    def to_json(self) -> dict:
        o = {"id": self.id, "fromUser": self.from_user, "text": self.text, "isError": self.is_error}
        for k, v in (("image", self.image), ("pdf", self.pdf), ("ctx", self.ctx), ("files", self.files)):
            if v is not None:
                o[k] = v
        return o

    @staticmethod
    def from_json(o: dict) -> "ChatMsg | None":
        if not isinstance(o, dict) or not isinstance(o.get("text"), str):
            return None
        files = o.get("files")
        return ChatMsg(bool(o.get("fromUser")), o["text"], o.get("image") if isinstance(o.get("image"), str) else None,
                       o.get("pdf") if isinstance(o.get("pdf"), str) else None, bool(o.get("isError")),
                       o.get("ctx") if isinstance(o.get("ctx"), str) else None,
                       [f for f in files if isinstance(f, str)] if isinstance(files, list) else None,
                       str(o.get("id") or uuid.uuid4()).upper())


def load(path) -> list[ChatMsg]:
    try:
        with open(path, encoding="utf-8-sig") as f:
            arr = json.load(f)
    except (OSError, ValueError):
        return []
    return [m for m in (ChatMsg.from_json(x) for x in (arr if isinstance(arr, list) else [])) if m is not None]


def save(path, msgs: list[ChatMsg], limit: int = 60) -> None:
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".chat-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([m.to_json() for m in msgs[-limit:]], f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def chat_history(msgs: list[ChatMsg], limit: int = 20, max_chars: int = 600) -> str:
    """The last messages for a prompt, each one cut to max_chars, the hidden ctx in [brackets]."""
    lines = []
    for m in msgs[-limit:]:
        t = m.text.strip()
        if len(t) > max_chars:
            t = t[:max_chars] + "…"
        if m.ctx:
            t += " [" + m.ctx + "]"
        lines.append(("משתמש: " if m.from_user else ("טימי (שגיאה): " if m.is_error else "טימי: ")) + t)
    return "\n".join(lines) if lines else "(זו ההודעה הראשונה)"
