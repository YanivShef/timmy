#!/usr/bin/env python3
"""Greenolink מדומה לבדיקות טימי (עותק של qa-connect/mock_greenolink.py, שלא נכנס לריפו; אנשי הקשר והמספרים מומצאים): OAuth (metadata/register/authorize/token/revoke) + /mcp (Streamable HTTP) + /upload.
לא נוגע בשרת האמיתי ולא שולח שום דבר בוואטסאפ. כל אירוע נרשם ל-mock.log.
מצבים (POST /_mode עם JSON): sse (bool), multi (bool), deny (bool), ttl (שניות), slow (שניות לכל tools/call), fail_send (bool)."""
import base64, hashlib, json, os, secrets, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765   # 0 = any free port (printed on the first line)
BASE = f"http://127.0.0.1:{PORT}"
LOG = os.environ.get("MOCK_GL_LOG") or os.path.join(__import__("tempfile").gettempdir(), "timmy-mock-greenolink.log")
L = threading.Lock()
S = {"clients": {}, "pending": {}, "codes": {}, "access": {}, "refresh": {}, "sessions": set(), "uploads": {},
     "mode": {"sse": True, "multi": False, "deny": False, "ttl": 3600, "slow": 0, "fail_send": False}, "events": []}

def log(kind, **kw):
    e = {"t": time.strftime("%H:%M:%S"), "kind": kind, **kw}
    with L:
        S["events"].append(e)
        with open(LOG, "a", encoding="utf-8") as f: f.write(json.dumps(e, ensure_ascii=False) + "\n")

CONTACTS = [{"phone_number": "972501234567", "name": "דנה כהן", "jid": "972501234567@s.whatsapp.net"},
            {"phone_number": "972527654321", "name": "דנה כהן סטודיו", "jid": "972527654321@s.whatsapp.net"},
            {"phone_number": "972541112233", "name": "אבי לוי", "jid": "972541112233@s.whatsapp.net"}]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""
    def send(self, code, obj=None, ctype="application/json", headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(obj, ensure_ascii=False).encode() if obj is not None else b"")
        self.send_response(code)
        if data or obj is not None: self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def redirect(self, url): self.send(302, headers={"Location": url})

    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/.well-known/oauth-authorization-server":
            return self.send(200, {"issuer": BASE + "/", "authorization_endpoint": BASE + "/authorize", "token_endpoint": BASE + "/token",
                                   "registration_endpoint": BASE + "/register", "revocation_endpoint": BASE + "/revoke",
                                   "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                                   "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
                                   "code_challenge_methods_supported": ["S256"]})
        if u.path == "/authorize":
            c = S["clients"].get(q.get("client_id", ""))
            if not c: log("authorize_bad_client"); return self.send(400, {"error": "invalid_request", "error_description": "Client ID not found"})
            if q.get("redirect_uri") not in c["redirect_uris"]:   # כמו ה-SDK: התאמה מדויקת
                log("authorize_bad_redirect", got=q.get("redirect_uri")); return self.send(400, {"error": "invalid_request", "error_description": "Redirect URI not registered"})
            if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"): return self.send(400, {"error": "invalid_request"})
            pid = secrets.token_urlsafe(12); S["pending"][pid] = q
            log("authorize", pid=pid, client=q["client_id"], resource=q.get("resource"))
            return self.redirect(f"{BASE}/oauth/login?pending={pid}")
        if u.path == "/oauth/login":   # "דף התחברות" שמאשר לבד (במקום אימייל+סיסמה+QR)
            p = S["pending"].pop(q.get("pending", ""), None)
            if not p: return self.send(400, raw=b"expired", ctype="text/plain")
            if S["mode"]["deny"]:
                log("login_denied"); return self.redirect(p["redirect_uri"] + "?" + urlencode({"error": "access_denied", "state": p.get("state", "")}))
            code = secrets.token_urlsafe(16); S["codes"][code] = p
            log("login_ok_code_issued")
            return self.redirect(p["redirect_uri"] + "?" + urlencode({"code": code, "state": p.get("state", "")}))
        if u.path == "/_state": return self.send(200, {"events": S["events"], "mode": S["mode"], "access": len(S["access"]), "refresh": len(S["refresh"])})
        self.send(404, {"detail": "not found"})

    def do_POST(self):
        u = urlparse(self.path); raw = self.body()
        if u.path == "/_mode":
            S["mode"].update(json.loads(raw or b"{}")); log("mode", **S["mode"]); return self.send(200, S["mode"])
        if u.path == "/_expire":
            for t in S["access"].values(): t["exp"] = 0
            log("expired_all_access"); return self.send(200, {})
        if u.path == "/_kill_sessions":
            S["sessions"].clear(); log("killed_sessions"); return self.send(200, {})
        if u.path == "/register":
            m = json.loads(raw or b"{}")
            uris = m.get("redirect_uris") or []
            cid, sec = "cl_" + secrets.token_hex(6), secrets.token_hex(16)
            S["clients"][cid] = {"redirect_uris": uris, "secret": sec, "auth": m.get("token_endpoint_auth_method", "client_secret_post")}
            log("register", client=cid, redirect_uris=uris, name=m.get("client_name"), auth=m.get("token_endpoint_auth_method"))
            return self.send(201, {"client_id": cid, "client_secret": sec, **m})
        if u.path in ("/token", "/revoke"):
            f = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            c = S["clients"].get(f.get("client_id", ""))
            if not c or (c["auth"] != "none" and f.get("client_secret") != c["secret"]):
                log(u.path[1:] + "_bad_client"); return self.send(401, {"error": "invalid_client"})
            if u.path == "/revoke":
                tok = f.get("token", "")
                fam = (S["access"].get(tok) or S["refresh"].get(tok) or {}).get("fam")
                for d in (S["access"], S["refresh"]):
                    for k in [k for k, v in d.items() if v["fam"] == fam]: del d[k]
                log("revoke", hint=f.get("token_type_hint"), family_killed=bool(fam)); return self.send(200, {})
            g = f.get("grant_type")
            if g == "authorization_code":
                p = S["codes"].pop(f.get("code", ""), None)
                if not p or p["client_id"] != f["client_id"] or p["redirect_uri"] != f.get("redirect_uri"):
                    log("token_bad_code"); return self.send(400, {"error": "invalid_grant"})
                ch = base64.urlsafe_b64encode(hashlib.sha256(f.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                if ch != p["code_challenge"]: log("token_bad_pkce"); return self.send(400, {"error": "invalid_grant", "error_description": "PKCE"})
                fam = secrets.token_hex(4)
            elif g == "refresh_token":
                r = S["refresh"].pop(f.get("refresh_token", ""), None)   # רוטציה: הישן מת
                if not r: log("refresh_invalid"); return self.send(400, {"error": "invalid_grant"})
                fam = r["fam"]
            else:
                return self.send(400, {"error": "unsupported_grant_type"})
            at, rt = "wamcp_" + secrets.token_hex(12), "wamcp_r_" + secrets.token_hex(12)
            S["access"][at] = {"fam": fam, "exp": time.time() + S["mode"]["ttl"]}; S["refresh"][rt] = {"fam": fam}
            log("token_issued", grant=g)
            return self.send(200, {"access_token": at, "token_type": "Bearer", "expires_in": S["mode"]["ttl"], "refresh_token": rt})
        if u.path.startswith("/upload/"):
            tok = u.path.split("/")[-1]
            up = next((x for x in S["uploads"].values() if x["token"] == tok and x["status"] == "pending"), None)
            if not up: return self.send(404, {"detail": "invalid or expired upload token"})
            up.update(status="uploaded", size=len(raw), sha=hashlib.sha256(raw).hexdigest()[:12], pdf=raw[:5] == b"%PDF-")
            log("upload", upload_id=up["id"], bytes=len(raw), is_pdf=up["pdf"])
            return self.send(200, {"success": True, "upload_id": up["id"], "bytes": len(raw)})
        if u.path == "/mcp": return self.mcp(raw)
        self.send(404, {"detail": "not found"})

    def mcp(self, raw):
        tok = (self.headers.get("Authorization") or "")[7:]
        a = S["access"].get(tok)
        if not a or a["exp"] < time.time():
            log("mcp_401", expired=bool(a)); return self.send(401, {"detail": "Unauthorized"}, headers={"WWW-Authenticate": f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"'})
        msg = json.loads(raw or b"{}")
        method = msg.get("method"); sid = self.headers.get("Mcp-Session-Id")
        if method == "initialize":
            sid = secrets.token_hex(8); S["sessions"].add(sid); log("mcp_initialize", accept=self.headers.get("Accept"))
            return self.reply(msg, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "whatsapp", "version": "mock"}}, {"Mcp-Session-Id": sid})
        if not sid: return self.send(400, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Bad Request: Missing session ID"}})
        if sid not in S["sessions"]: log("mcp_404_session"); return self.send(404, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Session not found"}})
        if "id" not in msg: log("mcp_notification", method=method); return self.send(202)
        if method == "tools/call":
            if S["mode"]["slow"]: time.sleep(S["mode"]["slow"])
            name, args = msg["params"]["name"], msg["params"].get("arguments", {})
            log("tool", name=name, args={k: (v if k != "message" else v[:60]) for k, v in args.items()},
                proto=self.headers.get("MCP-Protocol-Version"))
            return self.reply(msg, self.tool(name, args))
        return self.reply(msg, {})

    def tool(self, name, args):
        def ok(obj, structured=True):
            r = {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}], "isError": False}
            if structured: r["structuredContent"] = obj
            return r
        if name == "search_contacts":
            q = args.get("query", "")
            res = [c for c in CONTACTS if q and q in c["name"]]
            if not S["mode"]["multi"]: res = res[:1]
            # כמו FastMCP: כל פריט ב-TextContent נפרד + structuredContent {"result": [...]}
            return {"content": [{"type": "text", "text": json.dumps(c, ensure_ascii=False)} for c in res],
                    "structuredContent": {"result": res}, "isError": False}
        if name == "send_message":
            if S["mode"]["fail_send"]: return ok({"success": False, "message": "WhatsApp not connected"}, structured=False)
            log("WOULD_SEND_TEXT", to=args.get("recipient"), message=args.get("message"))
            return ok({"success": True, "message": f"Message sent to {args.get('recipient')}"}, structured=False)
        if name == "request_file_upload":
            uid, t = "up_" + secrets.token_hex(4), secrets.token_urlsafe(10)
            S["uploads"][uid] = {"id": uid, "token": t, "status": "pending", "filename": args.get("filename")}
            return ok({"success": True, "upload_id": uid, "upload_url": f"{BASE}/upload/{t}", "expires_in": 900})
        if name == "send_file":
            up = S["uploads"].get(args.get("upload_id", ""))
            if not up or up["status"] != "uploaded": return ok({"success": False, "message": "No matching uploaded file"})
            log("WOULD_SEND_FILE", to=args.get("recipient"), filename=up["filename"], bytes=up["size"], is_pdf=up["pdf"])
            return ok({"success": True, "message": "File sent"})
        return {"content": [{"type": "text", "text": f"Unknown tool {name}"}], "isError": True}

    def reply(self, msg, result, headers=None):
        env = {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
        if S["mode"]["sse"]:
            data = f"event: message\r\ndata: {json.dumps(env, ensure_ascii=False)}\r\n\r\n".encode()
            return self.send(200, raw=data, ctype="text/event-stream", headers=headers)
        return self.send(200, env, headers=headers)

if __name__ == "__main__":
    open(LOG, "w").close()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    PORT = srv.server_address[1]
    BASE = f"http://127.0.0.1:{PORT}"
    print(f"mock greenolink on {BASE}", flush=True)
    srv.serve_forever()
