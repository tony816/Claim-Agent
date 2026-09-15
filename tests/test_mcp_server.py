"""The claude.ai connector flow against the real app over HTTP: discovery, 401, DCR + PKCE, passphrase login, tokens,
tools, Host checking, persistence and lockout. Skipped where the optional `mcp` extra is not installed."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytest.importorskip("mcp")

import uvicorn  # noqa: E402

from claim_agent import mcp_server  # noqa: E402
from claim_agent.mcp_service import ClaimAgentService  # noqa: E402
from claim_agent.web import Workspace  # noqa: E402

PASSPHRASE = "correct horse battery staple 2026"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-mcp-secret")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    workspace = Workspace(tmp_path)
    store = tmp_path / ".tui" / "mcp" / "oauth.json"
    app = mcp_server.build_app(ClaimAgentService(workspace), base, PASSPHRASE, store)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield base, store, workspace
    server.should_exit = True
    thread.join(timeout=10)
    workspace.close()


def request(base, method, path, body=None, headers=None, form=False):
    data = None
    if body is not None:
        data = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
    url = path if path.startswith("http") else base + path
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
    try:
        with _opener.open(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def rpc(base, token, method, params=None, extra=None):
    headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token, "MCP-Protocol-Version": "2025-06-18", **(extra or {})}
    return request(base, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers)


def connect(base, passphrase=PASSPHRASE):
    """What claude.ai does: register, authorize with PKCE, the owner signs in, exchange the code."""
    status, _, body = request(base, "POST", "/register", {"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
                                                          "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "client_name": "Claude"})
    assert status == 201
    client_id = json.loads(body)["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urllib.parse.urlencode(dict(response_type="code", client_id=client_id, redirect_uri=CALLBACK, code_challenge=challenge,
                                        code_challenge_method="S256", state="s1", scope=mcp_server.SCOPE, resource=base + "/mcp"))
    status, headers, _ = request(base, "GET", "/authorize?" + query)
    assert status == 302 and "/login?rid=" in headers["location"]
    login = headers["location"]
    status, _, page = request(base, "GET", login)
    assert status == 200 and 'type=password' in page
    status, headers, _ = request(base, "POST", login, {"passphrase": passphrase}, form=True)
    if status != 302:
        return None, client_id
    redirect = urllib.parse.urlparse(headers["location"])
    assert headers["location"].startswith(CALLBACK)
    params = urllib.parse.parse_qs(redirect.query)
    assert params["state"] == ["s1"]
    status, _, body = request(base, "POST", "/token", dict(grant_type="authorization_code", code=params["code"][0], redirect_uri=CALLBACK,
                                                           client_id=client_id, code_verifier=verifier, resource=base + "/mcp"), form=True)
    assert status == 200
    return json.loads(body), client_id


def test_claude_connector_flow_end_to_end(served):
    base, store, workspace = served
    status, _, body = request(base, "GET", "/.well-known/oauth-protected-resource/mcp")
    assert status == 200 and json.loads(body)["resource"] == base + "/mcp"
    status, _, body = request(base, "GET", "/.well-known/oauth-authorization-server")
    assert status == 200 and "S256" in json.loads(body)["code_challenge_methods_supported"]
    status, headers, _ = request(base, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"Accept": "application/json, text/event-stream"})
    assert status == 401 and "resource_metadata=" in headers["www-authenticate"]

    tokens, client_id = connect(base)
    status, _, body = rpc(base, tokens["access_token"], "tools/list")
    names = {t["name"] for t in json.loads(body)["result"]["tools"]}
    assert status == 200 and {"claim_new_conversation", "claim_send", "claim_status", "claim_resume", "claim_full_report"} <= names
    status, _, body = rpc(base, tokens["access_token"], "tools/call", {"name": "claim_new_conversation", "arguments": {}})
    created = json.loads(json.loads(body)["result"]["content"][0]["text"])
    assert status == 200 and created["conversation_id"] in workspace.sessions

    # A request that reaches the app with some other Host (DNS rebinding) is refused.
    status, _, _ = rpc(base, tokens["access_token"], "tools/list", extra={"Host": "evil.example"})
    assert status == 421

    # Refresh rotates: the new pair works, the old refresh token does not.
    status, _, body = request(base, "POST", "/token", dict(grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=client_id), form=True)
    assert status == 200
    rotated = json.loads(body)
    status, _, body = request(base, "POST", "/token", dict(grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=client_id), form=True)
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"

    # Only digests are on disk, and a restarted provider still accepts the live token.
    saved = store.read_text(encoding="utf-8")
    assert rotated["access_token"] not in saved and rotated["refresh_token"] not in saved
    restarted = mcp_server.PassphraseOAuthProvider(store, base, PASSPHRASE)
    import anyio
    assert anyio.run(restarted.load_access_token, rotated["access_token"]) is not None
    assert anyio.run(restarted.load_access_token, tokens["access_token"]) is not None     # not revoked by refresh, expires on its own


def test_wrong_passphrase_never_issues_a_code_and_repeated_failures_lock_the_page(served):
    base, _, _ = served
    for _ in range(mcp_server.MAX_FAILURES):
        tokens, _ = connect(base, passphrase="wrong passphrase guess 123")
        assert tokens is None
    tokens, _ = connect(base)                                      # even the right passphrase is refused while locked
    assert tokens is None


def test_short_passphrase_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline")
    workspace = Workspace(tmp_path)
    try:
        with pytest.raises(ValueError, match="20자"):
            mcp_server.build_app(ClaimAgentService(workspace), "https://1-2-3-4.sslip.io", "short", tmp_path / "oauth.json")
    finally:
        workspace.close()
