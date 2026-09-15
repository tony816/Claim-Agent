"""Remote MCP server for claude.ai custom connectors (streamable HTTP + OAuth), in front of the web workspace.

Run on the server (deploy/oci/cloud-init.yaml does this behind Caddy):
    CLAIM_AGENT_MCP_PUBLIC_URL=https://1-2-3-4.sslip.io CLAIM_AGENT_MCP_PASSPHRASE=... \
    python -m claim_agent.mcp_server --project-root /opt/claim-agent/app --port 8765
The connector URL to paste into claude.ai is `<public url>/mcp`.

Authentication is a single-user OAuth authorization server built on the SDK's provider interface: Claude registers
itself (dynamic client registration), and the only way to obtain an authorization code is the passphrase page. Clients
and tokens are persisted, tokens only as SHA-256 digests, so a restart does not force Claude to reconnect.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import uvicorn
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken, construct_redirect_uri
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .mcp_service import ClaimAgentService

SCOPE = "claim"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
LOGIN_TTL = 600
MAX_CLIENTS = 50
MAX_FAILURES, LOCKOUT_S = 5, 900
MAX_RESULT = 140_000          # claude.ai caps a tool result near 150,000 characters
MIN_PASSPHRASE = 20
CLAUDE_ORIGINS = ["https://claude.ai", "https://claude.com"]

INSTRUCTIONS = """Claim-Agent는 한국 특허 청구항 작성·검수 다중 에이전트 파이프라인이다.
사용 순서: claim_new_conversation → 자료가 있으면 claim_attach_text / claim_attach_file → claim_send →
claim_status(wait_seconds=120)를 status가 running이 아닐 때까지 반복 → answer를 사용자에게 그대로 전달한다.
같은 발명의 후속 요청은 같은 conversation_id를 쓴다. 중지(review)된 작업은 claim_resume으로 이어 간다.
청구항 문언, 게이트 판정, LOCK은 파이프라인이 반환한 것만 전달한다. 직접 청구항을 고쳐 쓰거나, 반환되지 않은 PASS·LOCK·등록 가능성을 말하지 않는다."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clip(text: str | None) -> str | None:
    if text is None or len(text) <= MAX_RESULT:
        return text
    return text[:MAX_RESULT] + f"\n\n[결과가 {len(text):,}자라 {MAX_RESULT:,}자에서 잘랐습니다. 전문은 서버의 runs/ 보고서에 있습니다.]"


class PassphraseOAuthProvider:
    """OAuthAuthorizationServerProvider for one owner. The passphrase page is the only way to turn an authorization
    request into a code; everything else is the SDK's standard flow (DCR, PKCE S256, refresh rotation, revocation)."""

    def __init__(self, store: Path, public_url: str, passphrase: str):
        self.store = store
        self.public_url = public_url.rstrip("/")
        self.passphrase = passphrase
        self.lock = threading.Lock()
        self.pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.failures: list[float] = []
        data = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
        self.clients = {k: OAuthClientInformationFull.model_validate(v) for k, v in data.get("clients", {}).items()}
        self.access = {k: AccessToken.model_validate(v) for k, v in data.get("access", {}).items()}
        self.refresh = {k: RefreshToken.model_validate(v) for k, v in data.get("refresh", {}).items()}

    def _save(self) -> None:
        now = time.time()
        self.access = {k: v for k, v in self.access.items() if not v.expires_at or v.expires_at > now}
        self.refresh = {k: v for k, v in self.refresh.items() if not v.expires_at or v.expires_at > now}
        payload = {"clients": {k: v.model_dump(mode="json") for k, v in self.clients.items()},
                   "access": {k: v.model_dump(mode="json") for k, v in self.access.items()},
                   "refresh": {k: v.model_dump(mode="json") for k, v in self.refresh.items()}}
        self.store.parent.mkdir(parents=True, exist_ok=True)
        temp = self.store.with_suffix(".tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.store)

    # ---- clients (dynamic registration)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self.lock:
            self.clients[client_info.client_id] = client_info
            if len(self.clients) > MAX_CLIENTS:          # registration is open, so keep it bounded
                oldest = sorted(self.clients.values(), key=lambda c: c.client_id_issued_at or 0)[: len(self.clients) - MAX_CLIENTS]
                for client in oldest:
                    self.clients.pop(client.client_id, None)
            self._save()

    # ---- authorization: park the request, send the browser to the passphrase page

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        with self.lock:
            now = time.time()
            self.pending = {k: v for k, v in self.pending.items() if now - v[0] < LOGIN_TTL}
            rid = secrets.token_urlsafe(24)
            self.pending[rid] = (now, client.client_id, params)
        return f"{self.public_url}/login?rid={rid}"

    def locked_out(self) -> bool:
        now = time.time()
        self.failures = [t for t in self.failures if now - t < LOCKOUT_S]
        return len(self.failures) >= MAX_FAILURES

    def complete_login(self, rid: str, passphrase: str) -> str | None:
        """The redirect back to Claude with a code, or None for a wrong passphrase / unknown or expired request."""
        with self.lock:
            entry = self.pending.get(rid)
            if self.locked_out() or not entry or time.time() - entry[0] >= LOGIN_TTL:
                return None
            if not secrets.compare_digest(passphrase.encode("utf-8"), self.passphrase.encode("utf-8")):
                self.failures.append(time.time())
                return None
            del self.pending[rid]
            _, client_id, p = entry
            code = secrets.token_urlsafe(32)
            self.codes[code] = AuthorizationCode(
                code=code, scopes=p.scopes or [SCOPE], expires_at=time.time() + CODE_TTL, client_id=client_id,
                code_challenge=p.code_challenge, redirect_uri=p.redirect_uri,
                redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly, resource=p.resource, subject="owner")
            return construct_redirect_uri(str(p.redirect_uri), code=code, state=p.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        if code and code.client_id == client.client_id and code.expires_at > time.time():
            return code
        return None

    # ---- tokens

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        self.access[_digest(access)] = AccessToken(token=_digest(access), client_id=client_id, scopes=scopes,
                                                   expires_at=now + ACCESS_TTL, resource=resource, subject="owner")
        self.refresh[_digest(refresh)] = RefreshToken(token=_digest(refresh), client_id=client_id, scopes=scopes,
                                                      expires_at=now + REFRESH_TTL, resource=resource, subject="owner")
        self._save()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        with self.lock:
            self.codes.pop(authorization_code.code, None)       # single use
            return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        stored = self.refresh.get(_digest(refresh_token))
        if stored and stored.client_id == client.client_id:
            return stored.model_copy(update={"token": refresh_token})
        return None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self.lock:
            self.refresh.pop(_digest(refresh_token.token), None)  # rotate
            return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = self.access.get(_digest(token))
        if stored and (not stored.expires_at or stored.expires_at > time.time()):
            return stored.model_copy(update={"token": token})
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self.lock:
            self.access.pop(_digest(token.token), None)
            self.refresh.pop(_digest(token.token), None)
            self._save()


def _login_page(rid: str, message: str = "") -> HTMLResponse:
    note = f"<p class=err>{html.escape(message)}</p>" if message else ""
    body = f"""<!doctype html><html lang=ko><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Claim-Agent 연결</title><style>body{{font-family:system-ui,sans-serif;max-width:420px;margin:12vh auto;padding:0 20px;color:#202124}}
input,button{{font:inherit;width:100%;padding:10px;margin:8px 0;box-sizing:border-box}}button{{background:#284b42;color:#fff;border:0;border-radius:6px}}.err{{color:#9c4f37}}</style>
<h1>Claim-Agent 연결</h1><p>Claude가 이 서버의 청구항 파이프라인을 사용하도록 허용합니다. 서버에 설정한 접속 암호를 입력하세요.</p>{note}
<form method=post action="/login?rid={html.escape(rid)}"><input type=password name=passphrase autocomplete=current-password required autofocus>
<button type=submit>허용</button></form></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                       "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})


def build_server(service: ClaimAgentService, public_url: str, passphrase: str, store: Path) -> tuple[MCPServer, PassphraseOAuthProvider]:
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"CLAIM_AGENT_MCP_PASSPHRASE는 {MIN_PASSPHRASE}자 이상이어야 합니다.")
    public_url = public_url.rstrip("/")
    provider = PassphraseOAuthProvider(store, public_url, passphrase)
    server = MCPServer(
        "claim-agent", instructions=INSTRUCTIONS, auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=public_url, resource_server_url=f"{public_url}/mcp", required_scopes=[SCOPE], validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True)),
    )

    @server.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request):
        rid = request.query_params.get("rid", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", rid):
            return HTMLResponse("잘못된 요청입니다.", status_code=400)
        if request.method == "GET":
            return _login_page(rid)
        if provider.locked_out():
            return _login_page(rid, "실패가 반복되어 잠시 잠겼습니다. 15분 뒤 Claude에서 다시 연결하세요.")
        form = await request.form()
        redirect = provider.complete_login(rid, str(form.get("passphrase", "")))
        if redirect is None:
            await anyio.sleep(1)                        # slow down guessing
            return _login_page(rid, "암호가 틀렸거나 연결 요청이 만료되었습니다. 만료되었다면 Claude에서 다시 연결하세요.")
        return RedirectResponse(redirect, status_code=302)

    async def call(fn, *args):
        return await anyio.to_thread.run_sync(fn, *args)

    @server.tool()
    async def claim_list_conversations(limit: int = 20) -> list[dict]:
        """최근 Claim-Agent 대화 목록(conversation_id, 제목, 연결된 run_id, 실행 중 여부)."""
        return await call(service.list_conversations, limit)

    @server.tool()
    async def claim_new_conversation() -> dict:
        """새 대화를 만든다. 한 발명의 작성·수정·재개는 같은 conversation_id에서 이어 간다."""
        return await call(service.new_conversation)

    @server.tool()
    async def claim_attach_text(conversation_id: str, name: str, content: str) -> dict:
        """텍스트 자료(발명 설명, 기존 청구항 등)를 .md/.txt 파일로 첨부한다. 반환된 attachment_id를 claim_send에 넘긴다."""
        return await call(service.attach_text, conversation_id, name, content)

    @server.tool()
    async def claim_attach_file(conversation_id: str, name: str, content_base64: str) -> dict:
        """PDF·DOCX·HWPX·HWP·PNG·JPG·WEBP 자료를 base64로 첨부한다(파일당 20MB)."""
        return await call(service.attach_file, conversation_id, name, content_base64)

    @server.tool()
    async def claim_send(conversation_id: str, text: str, attachment_ids: list[str] | None = None, target_mode: str | None = None,
                         target_claim: int | None = None, target_segments: str | None = None) -> dict:
        """요청 한 턴을 시작한다. 요청은 자동 분류되어 일반 답변·청구항 작성·검토 파이프라인으로 간다.
        target_mode(independent | single | range)는 작성 대상 힌트다: single은 target_claim(항 번호), range는 target_segments('2~4,6').
        반환된 message_id로 claim_status를 wait_seconds=120으로 호출해 결과를 기다린다."""
        return await call(service.send, conversation_id, text, attachment_ids, target_mode, target_claim, target_segments)

    @server.tool()
    async def claim_status(conversation_id: str, message_id: str | None = None, wait_seconds: float = 120) -> dict:
        """응답 상태. running이면 최대 wait_seconds(120 이하)초 기다린다. 끝나면 answer에 웹 화면과 같은 답변(청구항·요약·사용량)이 온다."""
        result = await call(service.status, conversation_id, message_id, wait_seconds)
        result["answer"] = _clip(result.get("answer"))
        return result

    @server.tool()
    async def claim_full_report(conversation_id: str, message_id: str | None = None) -> dict:
        """해당 응답의 파이프라인 전체 보고서(게이트 표, 역할별 원 보고서, 근거표)."""
        result = await call(service.full_report, conversation_id, message_id)
        result["report"] = _clip(result.get("report"))
        return result

    @server.tool()
    async def claim_resume(conversation_id: str, kind: str, decision: str = "", attachment_ids: list[str] | None = None,
                           scope: str | None = None) -> dict:
        """중지되었거나 끝난 작업을 사용자 결정으로 이어 간다. kind: none(같은 단계 재실행) | style(스타일만) | meaning(의미 수정) |
        redesign(재설계) | restart(처음부터) | accept_unverified(비게이팅 미검증 수용). scope: INDEPENDENT | DEPENDENT."""
        return await call(service.resume, conversation_id, kind, decision, attachment_ids, scope)

    @server.tool()
    async def claim_stop(conversation_id: str) -> dict:
        """실행 중인 응답을 중지한다."""
        return await call(service.stop, conversation_id)

    return server, provider


def build_app(service: ClaimAgentService, public_url: str, passphrase: str, store: Path):
    server, _ = build_server(service, public_url, passphrase, store)
    host = urlsplit(public_url).netloc
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[host],
                                         allowed_origins=[public_url.rstrip("/"), *CLAUDE_ORIGINS])
    return server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=security)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Claim-Agent remote MCP server (claude.ai custom connector)")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    public_url = os.environ.get("CLAIM_AGENT_MCP_PUBLIC_URL", "").strip()
    passphrase = os.environ.get("CLAIM_AGENT_MCP_PASSPHRASE", "")
    if not public_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        print("CLAIM_AGENT_MCP_PUBLIC_URL must be the public https URL (e.g. https://1-2-3-4.sslip.io).", file=sys.stderr)
        return 2
    service = ClaimAgentService.open(args.project_root, args.config)
    try:
        app = build_app(service, public_url, passphrase, args.project_root.resolve() / ".tui" / "mcp" / "oauth.json")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning", proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    finally:
        service.workspace.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
