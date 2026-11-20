"""Secrets in the Windows Credential Manager (through keyring), the twin of the Mac Keychain item "Timmy Greenolink".

Credential Manager keeps at most 2560 bytes per credential blob (stored as UTF-16, so about 1280 characters), and an
OAuth token set with a refresh token is often longer. Values longer than CHUNK characters are therefore stored as
"<account>#0" .. "<account>#n-1" plus "<account>#n" = n; short values stay one plain credential. Reading joins the
parts back; writing removes whatever layout was there before, so switching between the two never leaves junk.

Tests never touch the real vault: TIMMY_SECRETS=memory (or use_memory()) swaps in an in-memory backend that enforces
the same 2560-byte limit, so a value that would not fit in the real vault fails the tests too.
"""
from __future__ import annotations

import json
import os
import threading

CHUNK = 1000
BLOB_LIMIT = 2560
_LOCK = threading.RLock()
_MEMORY = {}


def service() -> str:
    return os.environ.get("TIMMY_KEYCHAIN_SERVICE") or "Timmy Greenolink"


class _MemoryKeyring:
    """keyring-compatible in-memory backend with the Credential Manager size limit."""

    def __init__(self, store: dict):
        self.store = store

    def get_password(self, svc, user):
        return self.store.get((svc, user))

    def set_password(self, svc, user, value):
        if len(value.encode("utf-16-le")) > BLOB_LIMIT:
            raise ValueError("credential blob too large (Credential Manager limit)")
        self.store[(svc, user)] = value

    def delete_password(self, svc, user):
        if (svc, user) not in self.store:
            raise KeyError(user)
        del self.store[(svc, user)]


_BACKEND = []


def use_memory(store: dict | None = None):
    """Switch to the in-memory backend (tests). Returns its dict."""
    s = _MEMORY if store is None else store
    _BACKEND[:] = [_MemoryKeyring(s)]
    return s


def _kr():
    if not _BACKEND:
        if (os.environ.get("TIMMY_SECRETS") or "").lower() == "memory":
            use_memory()
        else:
            import keyring
            _BACKEND.append(keyring)
    return _BACKEND[0]


def _del(kr, user):
    try:
        kr.delete_password(service(), user)
    except Exception:
        pass


def get(account: str) -> str | None:
    with _LOCK:
        kr = _kr()
        try:
            n = kr.get_password(service(), account + "#n")
            if n:
                parts = [kr.get_password(service(), f"{account}#{i}") for i in range(int(n))]
                if any(p is None for p in parts):
                    return None
                return "".join(parts)
            return kr.get_password(service(), account)
        except Exception:
            return None


def delete(account: str) -> None:
    with _LOCK:
        kr = _kr()
        try:
            n = int(kr.get_password(service(), account + "#n") or 0)
        except Exception:
            n = 0
        for i in range(max(n, 0)):
            _del(kr, f"{account}#{i}")
        _del(kr, account + "#n")
        _del(kr, account)


def set(account: str, value: str) -> bool:     # noqa: A001 (same verb as keyring.set_password)
    with _LOCK:
        kr = _kr()
        delete(account)
        try:
            if len(value) <= CHUNK:
                kr.set_password(service(), account, value)
                return True
            parts = [value[i:i + CHUNK] for i in range(0, len(value), CHUNK)]
            for i, p in enumerate(parts):
                kr.set_password(service(), f"{account}#{i}", p)
            kr.set_password(service(), account + "#n", str(len(parts)))   # written last: a half write reads as nothing
            return True
        except Exception:
            delete(account)
            return False


def load_json(account: str) -> dict:
    v = get(account)
    if not v:
        return {}
    try:
        o = json.loads(v)
        return o if isinstance(o, dict) else {}
    except ValueError:
        return {}


def save_json(account: str, obj: dict) -> bool:
    return set(account, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
