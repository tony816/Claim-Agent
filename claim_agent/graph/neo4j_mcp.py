"""Neo4j GraphRAG MCP 커넥터(graph-it `kg_tools`: kg_datasets·kg_search·kg_entity)를 앱이 직접 부르는 읽기 전용 클라이언트.

claude.ai에 붙은 같은 커넥터는 claude.ai 계정의 OAuth로만 열리므로, 앱은 `.env`의 주소와 토큰으로 따로 접속한다.
- `CLAIM_AGENT_NEO4J_MCP_URL`   streamable HTTP MCP 주소(예: https://서버/mcp)
- `CLAIM_AGENT_NEO4J_MCP_TOKEN` 앞단 인증 프록시가 받는 Bearer 토큰(인증 없는 사내 주소면 비워 둔다)
- `CLAIM_AGENT_NEO4J_MCP_TIMEOUT` 초(기본 60)

토큰·세션 id는 프로세스 메모리에만 두고 오류 문구에 넣지 않는다. 호출할 수 있는 도구는 READ_TOOLS로 막는다.
"""
from __future__ import annotations

import json
import os
import threading

import requests

from ..prior_art.wips import _parse, _session, _short

PROTOCOL = "2025-06-18"
CLIENT_NAME = "claim-agent"
READ_TOOLS = {"kg_datasets", "kg_search", "kg_entity"}


class GraphMcpError(RuntimeError):
    pass


def server_url() -> str:
    return (os.getenv("CLAIM_AGENT_NEO4J_MCP_URL") or "").strip()


def _token() -> str:
    return (os.getenv("CLAIM_AGENT_NEO4J_MCP_TOKEN") or "").strip().strip("\"'")


def timeout_s() -> int:
    try:
        return int(os.getenv("CLAIM_AGENT_NEO4J_MCP_TIMEOUT") or 60)
    except ValueError:
        return 60


def available() -> bool:
    return bool(server_url())


class GraphMcpClient:
    """tools/call 한 겹. http: 시험용 가짜 세션."""

    def __init__(self, url: str | None = None, token: str | None = None, http=None):
        self.url = url if url is not None else server_url()
        self.token = token if token is not None else _token()
        self.http = http or _session()
        self.session_id = ""
        self._id = 0
        self._lock = threading.RLock()

    def _scrub(self, text: str) -> str:
        for secret in (self.token, self.session_id):
            if secret and len(secret) >= 4:
                text = text.replace(secret, "[redacted]")
        return text

    def _post(self, method: str, params: dict | None = None, notify: bool = False):
        body: dict = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self._id += 1
            body["id"] = self._id
        if params is not None:
            body["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return self.http.post(self.url, headers=headers, json=body, timeout=timeout_s())

    def _open(self) -> None:
        r = self._post("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": CLIENT_NAME, "version": "1"}})
        if r.status_code in (401, 403):
            raise GraphMcpError(f"Neo4j 그래프 서버가 인증을 거절했습니다(HTTP {r.status_code}). .env의 CLAIM_AGENT_NEO4J_MCP_TOKEN을 확인하세요.")
        if r.status_code >= 400:
            raise GraphMcpError(f"Neo4j 그래프 서버 연결 실패(HTTP {r.status_code}).")
        self.session_id = r.headers.get("mcp-session-id", "")
        self._post("notifications/initialized", notify=True)

    def _call(self, tool: str, args: dict) -> dict:
        with self._lock:
            for attempt in (0, 1):
                if not self.session_id:
                    self._open()
                r = self._post("tools/call", {"name": tool, "arguments": args})
                if r.status_code == 404 and attempt == 0 and self.session_id:   # 세션 만료
                    self.session_id = ""
                    continue
                break
        if r.status_code >= 400:
            raise GraphMcpError(f"그래프 {tool} 실패(HTTP {r.status_code}).")
        msg = _parse(r) or {}
        if "error" in msg:
            err = msg["error"]
            raise GraphMcpError(f"그래프 {tool} 실패: {_short(str(err.get('message', err) if isinstance(err, dict) else err), 300)}")
        res = msg.get("result") or {}
        data = res.get("structuredContent")
        if data is None:
            text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            try:
                data = json.loads(text)
            except ValueError:
                data = {"error": text} if res.get("isError") else {"text": text}
        if isinstance(data, dict) and set(data) == {"result"}:      # FastMCP가 dict 반환을 한 겹 싸는 경우
            data = data["result"]
        if not isinstance(data, dict):
            data = {"data": data}
        if res.get("isError"):
            raise GraphMcpError(f"그래프 {tool} 실패: {_short(str(data.get('error') or data), 300)}")
        return data

    def call(self, tool: str, args: dict) -> dict:
        """읽기 도구 하나를 불러 결과 dict를 돌려준다. 실패는 모두 GraphMcpError(비밀값을 지운 문구)."""
        if tool not in READ_TOOLS:
            raise GraphMcpError(f"허용하지 않은 그래프 도구입니다: {tool}")
        if not self.url:
            raise GraphMcpError(".env에 CLAIM_AGENT_NEO4J_MCP_URL이 없습니다(Neo4j GraphRAG MCP 주소).")
        try:
            return self._call(tool, {k: v for k, v in args.items() if v not in (None, "")})
        except GraphMcpError as e:
            raise GraphMcpError(self._scrub(str(e))) from None
        except requests.RequestException as e:
            raise GraphMcpError(f"Neo4j 그래프 서버에 닿지 못했습니다({type(e).__name__}).") from None
        except (ValueError, KeyError, TypeError) as e:
            raise GraphMcpError(f"그래프 {tool} 응답을 읽지 못했습니다({type(e).__name__}).") from None
