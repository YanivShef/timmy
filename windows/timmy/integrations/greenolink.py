"""WhatsApp through Greenolink, directly from Timmy: OAuth 2.1 + PKCE with a loopback redirect, and a small MCP client
over Streamable HTTP. Port of Recorder.swift "Greenolink ישירות" (same endpoints, parameters, errors and texts).

Flow (connect): listen on 127.0.0.1:<port>/callback -> dynamic client registration (once per redirect URI) ->
authorize URL probed without following the redirect (302 = fine, 4xx = the saved client is unknown -> register again)
-> the browser opens the login page -> the callback brings ?code&state (state must match) -> token (code + verifier)
-> tokens in the Credential Manager (integrations/secrets.py, one JSON item per server, chunked).
MCP: initialize -> notifications/initialized -> tools/call, JSON or text/event-stream answers, Mcp-Session-Id header,
a 401 refreshes the token once, a 404 starts a new session once. Tools (names from Greenolink's main.py):
search_contacts, send_message, request_file_upload (+ POST of the bytes to upload_url, only to Greenolink's own host),
send_file.

Sending is never done here on its own: send_text / send_file are called only from the confirm click of the send
flow (integrations/whatsapp.py SendSession.confirm_send), which a test enforces.
TIMMY_GREENOLINK_BASE points everything at another server (tests: tests/mock_greenolink.py, never the real one).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets as pysecrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import server as httpserver

from timmy.integrations import secrets

REGISTER_URL = "https://www.greenolink.com/register"
MCP_URL = "https://mcp.greenolink.com/mcp"


def base() -> str:
    s = (os.environ.get("TIMMY_GREENOLINK_BASE") or "").strip() or "https://mcp.greenolink.com"
    return s.rstrip("/")


class GLError(Exception):
    def __init__(self, msg: str, auth: bool = False, cancelled: bool = False):
        super().__init__(msg)
        self.msg, self.auth, self.cancelled = msg, auth, cancelled


def need_reconnect() -> GLError:
    return GLError("החיבור לוואטסאפ פג. לחץ \"חבר וואטסאפ\" כדי לחבר מחדש (לחיצה אחת).", auth=True)


# ---- storage -------------------------------------------------------------------------------------------------------
def account(b: str | None = None) -> str:
    u = urllib.parse.urlsplit(b or base())
    return (u.hostname or "greenolink") + (f":{u.port}" if u.port else "")


def load_store() -> dict:
    return secrets.load_json(account())


def save_store(o: dict) -> bool:
    return secrets.save_json(account(), o)


def has_token() -> bool:
    kc = load_store()
    return bool(kc.get("refresh_token") or kc.get("access_token"))


# ---- HTTP ----------------------------------------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}) if os.environ.get("TIMMY_GREENOLINK_BASE") else urllib.request.ProxyHandler())
_OPEN_NOFOLLOW = urllib.request.build_opener(_NoRedirect)


def http(method: str, url: str, body: bytes | None = None, headers: dict | None = None, follow: bool = True,
         timeout: float = 30) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with (_OPEN if follow else _OPEN_NOFOLLOW).open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:
            data = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data
    except urllib.error.URLError as e:
        r = e.reason
        if isinstance(r, (socket.timeout, TimeoutError)):
            raise GLError("Greenolink לא ענה בזמן. נסה שוב בעוד רגע") from None
        if isinstance(r, socket.gaierror):
            raise GLError("לא הצלחתי להגיע לשרת של Greenolink") from None
        if isinstance(r, ConnectionRefusedError):
            raise GLError("לא הצלחתי להגיע לשרת של Greenolink") from None
        raise GLError("אין חיבור לאינטרנט" if "network" in str(r).lower() else "שגיאת רשת מול Greenolink") from None
    except (socket.timeout, TimeoutError):
        raise GLError("Greenolink לא ענה בזמן. נסה שוב בעוד רגע") from None
    except OSError:
        raise GLError("שגיאת רשת מול Greenolink") from None


def _json(d: bytes):
    try:
        o = json.loads(d.decode("utf-8"))
        return o if isinstance(o, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def b64url(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


def random_token(n: int) -> str:
    return b64url(pysecrets.token_bytes(n))


def pkce_challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode()).digest())


def form(p: dict) -> bytes:
    return urllib.parse.urlencode(p, quote_via=urllib.parse.quote, safe="-._~").encode()


# ---- loopback (RFC 8252) -------------------------------------------------------------------------------------------
class OneShot:
    def __init__(self):
        self._ev = threading.Event()
        self.value = None
        self.error = None

    @property
    def done(self) -> bool:
        return self._ev.is_set()

    def finish(self, value=None, error: Exception | None = None):
        if self._ev.is_set():
            return
        self.value, self.error = value, error
        self._ev.set()

    def wait(self, timeout=None):
        if not self._ev.wait(timeout):
            return None
        if self.error:
            raise self.error
        return self.value


def page(ok: bool, text: str, accent: str = "#FFA046") -> str:
    """The page the browser shows after the redirect: Hebrew, dark, Timmy's accent color."""
    import html
    return ("<!doctype html><html dir=\"rtl\" lang=\"he\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\"><title>טימי</title><style>body{margin:0;min-height:100vh;display:flex;"
            "align-items:center;justify-content:center;background:#0b0b0c;color:#f2f2f2;font-family:'Segoe UI','Heebo',Arial,sans-serif}"
            ".c{background:#161618;border:1px solid #2a2a2e;border-radius:18px;padding:34px 30px;width:340px;text-align:center}"
            ".i{width:56px;height:56px;border-radius:50%;margin:0 auto 16px;display:flex;align-items:center;justify-content:center;"
            f"font-size:30px;font-weight:700;color:#0b0b0c;background:{accent if ok else '#ff5a5a'}}}"
            "h1{font-size:20px;margin:0 0 8px}p{color:#a1a1a6;font-size:14px;line-height:1.6;margin:0}</style></head>"
            f"<body><div class=\"c\"><div class=\"i\">{'&#10003;' if ok else '!'}</div><h1>{'מחובר ✓' if ok else 'לא התחבר'}</h1>"
            f"<p>{html.escape(text)}</p></div></body></html>")


class Loopback:
    """127.0.0.1 only, one path (/callback), answers once with the result page."""

    def __init__(self, port: int = 0):
        self.expected_state = ""
        self.result = OneShot()
        outer = self

        class H(httpserver.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                if u.path != "/callback":
                    return self._send(404, "")
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                if q.get("state") != outer.expected_state:
                    return self._send(400, page(False, "הקישור הזה לא שייך לחיבור הנוכחי. חזור לטימי ולחץ שוב \"חבר וואטסאפ\"."))
                code = q.get("code") or ""
                if code:
                    self._send(200, page(True, "וואטסאפ מחובר לטימי. אפשר לסגור את הלשונית ולחזור לטימי."))
                    outer.result.finish(code)
                    return
                denied = q.get("error") == "access_denied"
                self._send(200, page(False, "החיבור לא אושר. אפשר לחזור לטימי ולנסות שוב." if denied else "משהו השתבש בחיבור. חזור לטימי ונסה שוב."))
                outer.result.finish(error=GLError("החיבור לא אושר בדפדפן" if denied else
                                                  "Greenolink החזיר שגיאה: " + (q.get("error_description") or q.get("error") or "לא ידוע")))

            def _send(self, code, body):
                b = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b)

        try:
            self.server = httpserver.ThreadingHTTPServer(("127.0.0.1", port), H)
        except OSError:
            raise GLError("לא הצלחתי לפתוח חיבור מקומי לקבלת האישור") from None
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self.thread.start()

    def stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass


# ---- the client ----------------------------------------------------------------------------------------------------
@dataclass
class Candidate:
    name: str
    jid: str
    phone: str


def pretty_phone(p: str) -> str:
    """"972501234567" -> "050-123-4567" (Israeli mobile), anything else as it is (without the @domain)."""
    d = "".join(ch for ch in p if ch.isdigit())
    if not (d.startswith("972") and len(d) == 12):
        return p.split("@")[0]
    local = "0" + d[3:]
    return f"{local[:3]}-{local[3:6]}-{local[6:]}"


def rpc_message(d: bytes, content_type: str, rid: int):
    """The JSON-RPC answer to request `rid` in a plain JSON body or a text/event-stream body."""
    def match(o):
        if isinstance(o, list):
            for x in o:
                m = match(x)
                if m is not None:
                    return m
            return None
        if not isinstance(o, dict) or ("result" not in o and "error" not in o):
            return None
        i = o.get("id")
        if isinstance(i, int) and not isinstance(i, bool):
            return o if i == rid else None
        if isinstance(i, str):
            return o if i == str(rid) else None
        return o

    text = d.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or "") or text.startswith("event:") or text.startswith("data:"):
        data = []
        for raw in (text + "\n\n").split("\n"):
            line = raw[:-1] if raw.endswith("\r") else raw
            if not line:
                if data:
                    try:
                        m = match(json.loads("\n".join(data)))
                    except ValueError:
                        m = None
                    if m is not None:
                        return m
                data = []
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        return None
    try:
        return match(json.loads(text))
    except ValueError:
        return None


class Greenolink:
    """One per app (shared()). Every method blocks: call it from a worker thread, never the Qt main thread."""

    _SHARED = []

    @classmethod
    def shared(cls) -> "Greenolink":
        if not cls._SHARED or cls._SHARED[0].base != base():
            cls._SHARED[:] = [cls()]
        return cls._SHARED[0]

    def __init__(self, base_url: str | None = None):
        self.base = (base_url or base()).rstrip("/")
        self.meta = None
        self.flow: Loopback | None = None
        self.last_auth_url = ""
        self.session_id = None
        self.protocol_version = None
        self.ready = False
        self.rpc_id = 0
        self._lock = threading.RLock()          # one RPC / refresh at a time (the Swift actor)
        self._refresh_lock = threading.Lock()

    # ---- OAuth ------------------------------------------------------------------------------------------------
    def endpoint(self, key: str, default: str) -> str:
        if self.meta is None:
            st, _, d = http("GET", self.base + "/.well-known/oauth-authorization-server", timeout=20)
            self.meta = (_json(d) or {}) if st == 200 else {}
        s = self.meta.get(key)
        b = urllib.parse.urlsplit(self.base)
        if isinstance(s, str):
            u = urllib.parse.urlsplit(s)
            if u.hostname == b.hostname and u.scheme == b.scheme and u.port == b.port:   # secrets only to the same server
                return s
        return self.base + "/" + default

    def register(self, redirect: str) -> dict:
        body = json.dumps({"client_name": "Timmy (טימי)", "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
                           "response_types": ["code"], "token_endpoint_auth_method": "client_secret_post"}).encode()
        st, _, d = http("POST", self.endpoint("registration_endpoint", "register"), body, {"Content-Type": "application/json"})
        o = _json(d) or {}
        if st not in (200, 201) or not isinstance(o.get("client_id"), str):
            raise GLError(f"Greenolink לא אישר את טימי כאפליקציה ({st}). נסה שוב מאוחר יותר")
        return {"client_id": o["client_id"], "client_secret": o.get("client_secret") or "", "redirect_uri": redirect}

    def connect(self, open_url, timeout: float = 15 * 60) -> None:
        """Whole login: loopback -> register (once) -> browser -> code -> tokens. open_url(url) opens the browser."""
        self.cancel_connect()
        kc = load_store()
        pref = 0
        try:
            pref = urllib.parse.urlsplit(kc.get("redirect_uri") or "").port or 0
        except ValueError:
            pref = 0
        cb = None
        if pref:
            try:
                cb = Loopback(pref)
            except GLError:
                cb = None
        if cb is None:
            cb = Loopback(0)
        self.flow = cb
        try:
            redirect = f"http://127.0.0.1:{cb.port}/callback"
            verifier = random_token(32)
            challenge = pkce_challenge(verifier)
            cb.expected_state = random_token(16)
            open_this = None
            for attempt in range(2):
                if not kc.get("client_id") or kc.get("redirect_uri") != redirect:
                    kc = self.register(redirect)            # a new client: old tokens belonged to the previous one
                    save_store(kc)
                q = urllib.parse.urlencode({"response_type": "code", "client_id": kc["client_id"], "redirect_uri": redirect,
                                            "code_challenge": challenge, "code_challenge_method": "S256", "state": cb.expected_state,
                                            "resource": self.base + "/mcp"})
                auth = self.endpoint("authorization_endpoint", "authorize") + "?" + q
                st, h, _ = http("GET", auth, follow=False, timeout=20)
                if 300 <= st <= 399 and h.get("location"):
                    u = urllib.parse.urljoin(auth, h["location"])
                    host = urllib.parse.urlsplit(u).hostname
                    open_this = u if host in (urllib.parse.urlsplit(self.base).hostname, "127.0.0.1") else auth
                    break
                if st == 200:
                    open_this = auth
                    break
                if attempt == 0 and 400 <= st <= 499:
                    kc = {}
                    continue
                raise GLError(f"Greenolink לא פתח את דף ההתחברות ({st}). נסה שוב מאוחר יותר")
            if not open_this:
                raise GLError("Greenolink לא פתח את דף ההתחברות")
            if cb.result.done:
                raise GLError("בוטל", cancelled=True)
            self.last_auth_url = open_this
            open_url(open_this)
            code = cb.result.wait(timeout)
            if code is None:
                raise GLError("עברו 15 דקות בלי אישור בדפדפן. לחץ שוב \"חבר וואטסאפ\"")
            self.token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "code_verifier": verifier})
            self.reset_session()
        finally:
            cb.stop()
            if self.flow is cb:
                self.flow = None

    def cancel_connect(self):
        if self.flow is not None:
            self.flow.result.finish(error=GLError("בוטל", cancelled=True))

    def reopen_browser(self, open_url):
        if self.last_auth_url and self.flow is not None:
            open_url(self.last_auth_url)

    def token(self, p: dict) -> str:
        kc = load_store()
        allp = dict(p)
        allp["client_id"] = kc.get("client_id") or ""
        if kc.get("client_secret"):
            allp["client_secret"] = kc["client_secret"]
        st, _, d = http("POST", self.endpoint("token_endpoint", "token"), form(allp),
                        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        o = _json(d) or {}
        at = o.get("access_token")
        if st != 200 or not isinstance(at, str) or not at:
            if o.get("error") in ("invalid_grant", "invalid_client", "unauthorized_client") or st == 401:
                raise need_reconnect()
            raise GLError(f"Greenolink החזיר שגיאה בהתחברות ({st}). נסה שוב")
        kc["access_token"] = at
        if isinstance(o.get("refresh_token"), str) and o["refresh_token"]:
            kc["refresh_token"] = o["refresh_token"]
        try:
            kc["expires_at"] = time.time() + float(o.get("expires_in") or 3600)
        except (TypeError, ValueError):
            kc["expires_at"] = time.time() + 3600
        if not save_store(kc):
            raise GLError("לא הצלחתי לשמור את החיבור במנהל האישורים של ווינדוס")
        return at

    def access_token(self, force: bool = False) -> str:
        """A valid token: refreshed by itself 2 minutes before it expires (or when the server said 401). One refresh at a time."""
        kc = load_store()
        at = kc.get("access_token")
        if not at:
            raise GLError("וואטסאפ לא מחובר", auth=True)
        if not force and float(kc.get("expires_at") or 0) - 120 > time.time():
            return at
        with self._refresh_lock:
            kc2 = load_store()
            if kc2.get("access_token") != at and not force:      # another thread refreshed meanwhile
                return kc2["access_token"]
            rt = kc2.get("refresh_token")
            if not rt:
                if force:
                    raise need_reconnect()
                return at
            try:
                return self.token({"grant_type": "refresh_token", "refresh_token": rt})
            except GLError as e:
                if e.auth:
                    self.clear_tokens()
                raise

    def clear_tokens(self):
        kc = load_store()
        for k in ("access_token", "refresh_token", "expires_at"):
            kc.pop(k, None)
        save_store(kc)
        self.reset_session()

    def disconnect(self):
        """"נתק": revoke at the server (best effort) and delete the tokens."""
        kc = load_store()
        try:
            url = self.endpoint("revocation_endpoint", "revoke")
            for k in ("refresh_token", "access_token"):
                t = kc.get(k)
                if not t:
                    continue
                p = {"token": t, "token_type_hint": k, "client_id": kc.get("client_id") or ""}
                if kc.get("client_secret"):
                    p["client_secret"] = kc["client_secret"]
                try:
                    http("POST", url, form(p), {"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
                except GLError:
                    pass
        except GLError:
            pass
        self.clear_tokens()

    # ---- MCP --------------------------------------------------------------------------------------------------
    def reset_session(self):
        self.session_id, self.protocol_version, self.ready = None, None, False

    def _post(self, body: dict, token: str):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return http("POST", self.base + "/mcp", json.dumps(body, ensure_ascii=False).encode("utf-8"), h, timeout=90)

    def _ensure_session(self):
        if self.ready:
            return
        res = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "timmy", "version": "1.0"}})
        self.protocol_version = res.get("protocolVersion") if isinstance(res.get("protocolVersion"), str) else None
        st, _, _ = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.access_token())
        if st == 401:
            raise need_reconnect()
        self.ready = True

    def rpc(self, method: str, params: dict, retry_auth: bool = True, retry_session: bool = True) -> dict:
        with self._lock:
            if method != "initialize":
                self._ensure_session()
            self.rpc_id += 1
            rid = self.rpc_id
            st, h, d = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, self.access_token())
            if st == 401:
                if not retry_auth:
                    raise need_reconnect()
                self.access_token(force=True)
                return self.rpc(method, params, retry_auth=False, retry_session=retry_session)
            if st == 404 and self.session_id and retry_session:     # the session expired at the server: a new one
                self.reset_session()
                return self.rpc(method, params, retry_auth=retry_auth, retry_session=False)
            if not 200 <= st <= 299:
                raise GLError(f"Greenolink החזיר שגיאה ({st}). נסה שוב בעוד רגע")
            if method == "initialize" and h.get("mcp-session-id"):
                self.session_id = h["mcp-session-id"]
            msg = rpc_message(d, h.get("content-type", ""), rid)
            if msg is None:
                raise GLError("קיבלתי תשובה לא מובנת מ-Greenolink")
            if isinstance(msg.get("error"), dict):
                raise GLError("Greenolink: " + str(msg["error"].get("message") or "שגיאה"))
            r = msg.get("result")
            return r if isinstance(r, dict) else {}

    def call_tool(self, name: str, args: dict):
        res = self.rpc("tools/call", {"name": name, "arguments": args})
        texts = [c.get("text") for c in (res.get("content") or []) if isinstance(c, dict) and isinstance(c.get("text"), str)]
        if res.get("isError") is True:
            raise GLError("Greenolink: " + (texts[0] if texts else "הפעולה נכשלה"))
        sc = res.get("structuredContent")
        if sc is not None:
            if isinstance(sc, dict) and len(sc) == 1 and "result" in sc:
                return sc["result"]
            return sc
        parsed = []
        for t in texts:
            try:
                parsed.append(json.loads(t))
            except ValueError:
                pass
        if len(parsed) == 1:
            return parsed[0]
        return parsed if parsed else "\n".join(texts)

    # ---- the tools --------------------------------------------------------------------------------------------
    def search_contacts(self, q: str) -> list[Candidate]:
        r = self.call_tool("search_contacts", {"query": q})
        if isinstance(r, dict):
            lst = [r]
        elif isinstance(r, list):
            lst = [x for x in r if isinstance(x, dict)]
        else:
            lst = []
        if lst and isinstance(lst[0].get("error"), str):
            raise GLError("לא הצלחתי לחפש באנשי הקשר: " + lst[0]["error"])
        out = []
        for c in lst:
            j = c.get("jid")
            if not isinstance(j, str) or not j:
                continue
            ph = c.get("phone_number") if isinstance(c.get("phone_number"), str) and c.get("phone_number") else j.split("@")[0]
            n = c.get("name") if isinstance(c.get("name"), str) and c.get("name") else pretty_phone(ph)
            out.append(Candidate(n, j, ph))
        return out[:8]

    @staticmethod
    def _ok(r, what: str):
        o = r if isinstance(r, dict) else {}
        if o.get("success") is not True:
            m = o.get("message")
            raise GLError(what + (": " + m if isinstance(m, str) and m else ""))

    def send_text(self, to: str, text: str) -> None:
        self._ok(self.call_tool("send_message", {"recipient": to, "message": text}), "ההודעה לא נשלחה")

    def send_file(self, to: str, path: str) -> None:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            raise GLError("לא הצלחתי לקרוא את הקובץ " + os.path.basename(path)) from None
        r = self.call_tool("request_file_upload", {"filename": os.path.basename(path)})
        r = r if isinstance(r, dict) else {}
        uid, us = r.get("upload_id"), r.get("upload_url")
        if not isinstance(uid, str) or not isinstance(us, str):
            m = r.get("message")
            raise GLError("Greenolink לא הכין העלאה לקובץ" + (": " + m if isinstance(m, str) else ""))
        u, b = urllib.parse.urlsplit(us), urllib.parse.urlsplit(self.base)
        if u.hostname != b.hostname or u.scheme != b.scheme:
            raise GLError("כתובת ההעלאה לא של Greenolink, לא שלחתי את הקובץ")
        st, _, _ = http("POST", us, data, {"Content-Type": "application/octet-stream"}, timeout=120)
        if not 200 <= st <= 299:
            raise GLError("הקובץ גדול מדי לשליחה" if st == 413 else f"העלאת הקובץ נכשלה ({st})")
        self._ok(self.call_tool("send_file", {"recipient": to, "upload_id": uid}), "הקובץ לא נשלח")


# ---- the Claude Code fallback: Greenolink connected as an MCP server of Claude Code ----------------------------------
@dataclass
class MCPServer:
    name: str
    target: str
    status: str

    @property
    def connected(self) -> bool:
        return "Connected" in self.status and "Failed" not in self.status

    @property
    def needs_auth(self) -> bool:
        return "auth" in self.status.lower()

    @property
    def tool_prefix(self) -> str:
        return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", self.name) + "__"


def parse_mcp_list(out: str) -> list[MCPServer]:
    """Lines of `claude mcp list`: "claude.ai whatsapp: https://mcp.greenolink.com/mcp - <check> Connected"."""
    res = []
    for line in (out or "").split("\n"):
        ln = line.strip()
        dash = ln.rfind(" - ")
        colon = ln.find(": ")
        if dash < 0 or colon < 0 or colon + 2 > dash:
            continue
        name, target = ln[:colon], ln[colon + 2:dash]
        status = ln[dash + 3:].strip(" " + chr(0x2714) + chr(0x2713) + "!" + chr(0x2717) + chr(0x2718) + chr(0xD7)).strip()
        if name:
            res.append(MCPServer(name, target, status))
    return res


def find_greenolink(lst: list[MCPServer]) -> MCPServer | None:
    c = [s for s in lst if "greenolink" in s.target.lower() or "greenolink" in s.name.lower() or "whatsapp" in s.name.lower()]
    return next((s for s in c if s.connected), c[0] if c else None)
