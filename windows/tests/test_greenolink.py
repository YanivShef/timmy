"""Greenolink (OAuth 2.1 PKCE loopback + MCP over Streamable HTTP) and the chunked secrets store, against
tests/mock_greenolink.py only: never the real server, never a real WhatsApp message (the mock logs WOULD_SEND_*)."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from timmy.integrations import greenolink as G
from timmy.integrations import secrets

MOCK = Path(__file__).resolve().parent / "mock_greenolink.py"


@pytest.fixture
def mem_secrets():
    store = secrets.use_memory({})
    yield store
    secrets._BACKEND.clear()


@pytest.fixture
def mock(tmp_path, monkeypatch, mem_secrets):
    logf = tmp_path / "mock.log"
    env = dict(os.environ, MOCK_GL_LOG=str(logf), PYTHONUTF8="1")
    p = subprocess.Popen([sys.executable, str(MOCK), "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    line = p.stdout.readline().decode()
    base = line.strip().split(" on ")[-1]
    assert base.startswith("http://127.0.0.1:"), line
    monkeypatch.setenv("TIMMY_GREENOLINK_BASE", base)
    G.Greenolink._SHARED.clear()

    class M:
        url = base
        log = logf

        @staticmethod
        def mode(**kw):
            urllib.request.urlopen(urllib.request.Request(base + "/_mode", json.dumps(kw).encode(), method="POST")).read()

        @staticmethod
        def post(path):
            urllib.request.urlopen(urllib.request.Request(base + path, b"{}", method="POST")).read()

        @staticmethod
        def events(kind=None):
            ev = [json.loads(x) for x in logf.read_text(encoding="utf-8").splitlines() if x.strip()] if logf.exists() else []
            return [e for e in ev if kind is None or e["kind"] == kind]
    yield M
    p.kill()
    p.wait(5)


def browser(url):
    """The user's browser: follows the mock's redirects down to Timmy's loopback callback."""
    def go():
        try:
            urllib.request.urlopen(url, timeout=10).read()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


# ---- secrets ---------------------------------------------------------------------------------------------------
def test_secrets_short_and_chunked_round_trip(mem_secrets):
    assert secrets.set("acc", "short")
    assert secrets.get("acc") == "short"
    big = ("x" * 997 + "éא") * 6            # 6 kB of mixed text: does not fit one Credential Manager blob
    with pytest.raises(ValueError):
        mem_secrets_backend = secrets._BACKEND[0]
        mem_secrets_backend.set_password("svc", "u", big)
    assert secrets.set("acc", big)
    assert secrets.get("acc") == big
    keys = sorted(k[1] for k in mem_secrets)
    assert "acc#n" in keys and "acc" not in keys and len([k for k in keys if k.startswith("acc#")]) == int(mem_secrets[(secrets.service(), "acc#n")]) + 1
    assert secrets.set("acc", "short again")      # back to one plain item, no chunks left behind
    assert sorted(k[1] for k in mem_secrets) == ["acc"]
    secrets.delete("acc")
    assert secrets.get("acc") is None and not mem_secrets


def test_secrets_json_and_service_override(mem_secrets, monkeypatch):
    monkeypatch.setenv("TIMMY_KEYCHAIN_SERVICE", "Timmy Test")
    assert secrets.save_json("a", {"access_token": "t" * 3000, "n": 1})
    assert secrets.load_json("a")["n"] == 1
    assert all(k[0] == "Timmy Test" for k in mem_secrets)
    assert secrets.load_json("missing") == {}


# ---- pure helpers ------------------------------------------------------------------------------------------------
def test_rpc_message_json_and_sse():
    assert G.rpc_message(b'{"jsonrpc":"2.0","id":3,"result":{"a":1}}', "application/json", 3)["result"] == {"a": 1}
    assert G.rpc_message(b'{"jsonrpc":"2.0","id":4,"result":{}}', "application/json", 3) is None
    sse = b'event: message\r\ndata: {"jsonrpc":"2.0","id":"7","result":{"ok":true}}\r\n\r\n'
    assert G.rpc_message(sse, "text/event-stream", 7)["result"] == {"ok": True}
    two = b'data: {"jsonrpc":"2.0","method":"notifications/x"}\n\ndata: {"jsonrpc":"2.0","id":2,"error":{"message":"bad"}}\n\n'
    assert G.rpc_message(two, "text/event-stream", 2)["error"]["message"] == "bad"
    assert G.rpc_message(b"garbage", "application/json", 1) is None


def test_mcp_list_parsing_and_pretty_phone():
    out = ("Checking MCP server health...\n\nclaude.ai whatsapp: https://mcp.greenolink.com/mcp - ✔ Connected\n"
           "greenolink: https://mcp.greenolink.com/mcp (HTTP) - ! Needs authentication\nother: npx x - ✗ Failed to connect\n")
    lst = G.parse_mcp_list(out)
    assert [s.name for s in lst] == ["claude.ai whatsapp", "greenolink", "other"]
    s = G.find_greenolink(lst)
    assert s.connected and s.tool_prefix == "mcp__claude_ai_whatsapp__"
    assert lst[1].needs_auth and not lst[1].connected
    assert G.find_greenolink(G.parse_mcp_list("No MCP servers configured.")) is None
    assert G.pretty_phone("972501234567") == "050-123-4567"
    assert G.pretty_phone("12025550100@s.whatsapp.net") == "12025550100"


def test_pkce():
    v = G.random_token(32)
    assert len(v) >= 43 and "=" not in v
    import base64
    import hashlib
    assert G.pkce_challenge(v) == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


# ---- the whole flow against the mock -----------------------------------------------------------------------------
def test_connect_search_send_refresh_and_disconnect(mock):
    gl = G.Greenolink()
    assert not G.has_token()
    gl.connect(browser, timeout=20)
    assert G.has_token()
    kc = G.load_store()
    assert kc["client_id"].startswith("cl_") and kc["redirect_uri"].startswith("http://127.0.0.1:") and kc["access_token"].startswith("wamcp_")
    ev = mock.events()
    reg = next(e for e in ev if e["kind"] == "register")
    assert reg["auth"] == "client_secret_post" and reg["redirect_uris"] == [kc["redirect_uri"]]
    assert any(e["kind"] == "authorize" and e["resource"] == mock.url + "/mcp" for e in ev)
    assert any(e["kind"] == "token_issued" and e["grant"] == "authorization_code" for e in ev)

    c = gl.search_contacts("דנה")
    assert len(c) == 1 and c[0].jid.endswith("@s.whatsapp.net") and c[0].phone == c[0].jid.split("@")[0]
    mock.mode(multi=True)
    assert len(gl.search_contacts("דנה")) == 2
    assert gl.search_contacts("אין כזה") == []
    assert [e for e in mock.events() if e["kind"].startswith("WOULD_SEND")] == []      # searching never sends

    gl.send_text(c[0].jid, "בדיקה בלבד")
    assert mock.events("WOULD_SEND_TEXT")[-1]["message"] == "בדיקה בלבד"
    proto = [e.get("proto") for e in mock.events("tool")]
    assert proto and all(p == "2025-06-18" for p in proto)

    # expired access token: the next call refreshes it once and goes on (the refresh token rotates)
    mock.post("/_expire")
    gl.send_text(c[0].jid, "אחרי חידוש")
    assert any(e["kind"] == "token_issued" and e["grant"] == "refresh_token" for e in mock.events())
    assert mock.events("WOULD_SEND_TEXT")[-1]["message"] == "אחרי חידוש"
    # a session the server forgot: a new initialize, then the call
    n_init = len(mock.events("mcp_initialize"))
    mock.post("/_kill_sessions")
    gl.search_contacts("אבי")
    assert len(mock.events("mcp_initialize")) == n_init + 1

    # plain JSON answers instead of SSE
    mock.mode(sse=False)
    assert gl.search_contacts("אבי")[0].name == "אבי לוי"

    gl.disconnect()
    assert not G.has_token()
    assert any(e["kind"] == "revoke" and e["family_killed"] for e in mock.events())
    with pytest.raises(G.GLError) as e:
        gl.search_contacts("דנה")
    assert e.value.auth


def test_send_file_uploads_the_pdf(mock, tmp_path):
    gl = G.Greenolink()
    gl.connect(browser, timeout=20)
    pdf = tmp_path / "סיכום.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 5000)
    gl.send_file("972541112233@s.whatsapp.net", str(pdf))
    up = mock.events("upload")[-1]
    assert up["bytes"] == pdf.stat().st_size and up["is_pdf"]
    sent = mock.events("WOULD_SEND_FILE")[-1]
    assert sent["filename"] == "סיכום.pdf" and sent["is_pdf"]


def test_failures_are_hebrew_errors(mock):
    gl = G.Greenolink()
    mock.mode(deny=True)
    with pytest.raises(G.GLError) as e:
        gl.connect(browser, timeout=20)
    assert "לא אושר" in e.value.msg and not G.has_token()
    mock.mode(deny=False)
    gl.connect(browser, timeout=20)
    mock.mode(fail_send=True)
    with pytest.raises(G.GLError) as e:
        gl.send_text("972501234567@s.whatsapp.net", "x")
    assert e.value.msg.startswith("ההודעה לא נשלחה") and "WhatsApp not connected" in e.value.msg
    assert mock.events("WOULD_SEND_TEXT") == []


def test_second_connect_reuses_client_and_port(mock):
    gl = G.Greenolink()
    gl.connect(browser, timeout=20)
    first = G.load_store()
    gl.connect(browser, timeout=20)
    second = G.load_store()
    assert second["client_id"] == first["client_id"] and second["redirect_uri"] == first["redirect_uri"]
    assert len(mock.events("register")) == 1


def test_cancel_connect_and_foreign_state(mock):
    gl = G.Greenolink()
    opened = []

    def no_browser(u):
        opened.append(u)
        # a callback with the wrong state is refused (and does not finish the flow)
        port = gl.flow.port
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=x&state=nope", timeout=5).read()
        except urllib.error.HTTPError as e:
            assert e.code == 400
        threading.Timer(0.3, gl.cancel_connect).start()
    with pytest.raises(G.GLError) as e:
        gl.connect(no_browser, timeout=20)
    assert e.value.cancelled and opened and not G.has_token()


def test_upload_only_to_greenolink_host(mock, monkeypatch, tmp_path):
    gl = G.Greenolink()
    gl.connect(browser, timeout=20)
    real = gl.call_tool

    def evil(name, args):
        r = real(name, args)
        if name == "request_file_upload":
            r = dict(r, upload_url="http://example.com/upload/x")
        return r
    monkeypatch.setattr(gl, "call_tool", evil)
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-")
    with pytest.raises(G.GLError) as e:
        gl.send_file("972501234567@s.whatsapp.net", str(f))
    assert "לא של Greenolink" in e.value.msg
    assert mock.events("upload") == [] and mock.events("WOULD_SEND_FILE") == []


def test_unreachable_server(monkeypatch, mem_secrets):
    monkeypatch.setenv("TIMMY_GREENOLINK_BASE", "http://127.0.0.1:9")
    t = time.time()
    with pytest.raises(G.GLError) as e:
        G.Greenolink().connect(browser, timeout=5)
    assert "Greenolink" in e.value.msg and time.time() - t < 30


@pytest.mark.windows_only
def test_real_credential_manager_chunking(monkeypatch):
    """The real Windows vault with a throwaway service name and a dummy value: a 6 kB value only fits as chunks."""
    keyring = pytest.importorskip("keyring")
    monkeypatch.setenv("TIMMY_KEYCHAIN_SERVICE", f"Timmy Test {os.getpid()}")
    secrets._BACKEND[:] = [keyring]
    try:
        big = "dummy-token-" + "x" * 6000
        assert secrets.set("ci-account", big)
        assert secrets.get("ci-account") == big
        assert keyring.get_password(secrets.service(), "ci-account#n") is not None
        assert secrets.set("ci-account", "short")
        assert secrets.get("ci-account") == "short" and keyring.get_password(secrets.service(), "ci-account#n") is None
    finally:
        secrets.delete("ci-account")
        secrets._BACKEND.clear()
    assert keyring.get_password(f"Timmy Test {os.getpid()}", "ci-account") is None
