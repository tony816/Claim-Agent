"""그래프 조회(Neo4j GraphRAG MCP) 작업 도구: 클라이언트의 MCP 왕복, 답 형식, 비밀값 비노출."""
from __future__ import annotations

import json

import pytest

from claim_agent.graph import graph_tool
from claim_agent.graph.neo4j_mcp import GraphMcpClient, GraphMcpError


class Resp:
    def __init__(self, status=200, body=None, headers=None, sse=False):
        self.status_code = status
        self.headers = headers or {}
        payload = json.dumps(body or {}, ensure_ascii=False)
        if sse:
            self.headers["Content-Type"] = "text/event-stream"
            self.content = f"event: message\ndata: {payload}\n\n".encode("utf-8")
        else:
            self.headers.setdefault("Content-Type", "application/json")
            self.content = payload.encode("utf-8")

    def json(self):
        return json.loads(self.content)


class FakeHttp:
    """initialize → notifications → tools/call. results: tool 이름 → 결과 dict."""

    def __init__(self, results, init_status=200):
        self.results, self.init_status, self.posts = results, init_status, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((headers, json))
        if json["method"] == "initialize":
            return Resp(self.init_status, {"jsonrpc": "2.0", "id": json["id"], "result": {}}, {"mcp-session-id": "sess-1234"})
        if json["method"].startswith("notifications/"):
            return Resp(202)
        name = json["params"]["name"]
        result = {"content": [{"type": "text", "text": "{}"}], "structuredContent": self.results[name]}
        return Resp(200, {"jsonrpc": "2.0", "id": json["id"], "result": result}, sse=True)


SEARCH = {"query": "진보성", "dataset": "kr-oa-300", "relationships": [{"source": "통지1", "relation": "CITES", "target": "D1"}],
          "chunks": [{"chunk_id": "c1", "dataset": "kr-oa-300", "source": "case-001.md", "text": "진보성 거절 뒤 가이드 홈을 더해 등록",
                      "score": 0.91, "keyword_hit": True, "entities": ["가이드 홈 [Feature]"]}]}


def test_search_sends_bearer_and_dataset_and_formats_chunks():
    http = FakeHttp({"kg_search": SEARCH})
    answer = graph_tool.run("kg_search", text="@kr-oa-300 진보성", client=GraphMcpClient(url="https://g/mcp", token="tok-secret", http=http))
    headers, body = http.posts[-1]
    assert headers["Authorization"] == "Bearer tok-secret" and headers["mcp-session-id"] == "sess-1234"
    assert body["params"] == {"name": "kg_search", "arguments": {"query": "진보성", "dataset": "kr-oa-300", "top_k": graph_tool.TOP_K}}
    assert answer.startswith("**그래프 참고 (미검토 — 게이트·근거 아님)** · 원문 검색 · 데이터셋: kr-oa-300")
    assert "> 진보성 거절 뒤 가이드 홈을 더해 등록" in answer and "통지1 —CITES→ D1" in answer and "가이드 홈 [Feature]" in answer


def test_datasets_needs_no_text_and_unwraps_result():
    http = FakeHttp({"kg_datasets": {"result": {"datasets": [{"name": "kr-oa-300", "description": "OA 300건", "documents": 300,
                                                               "chunks": 300, "entities": 5000, "entity_types": [{"type": "Feature", "count": 900}]}]}}})
    answer = graph_tool.run("kg_datasets", client=GraphMcpClient(url="https://g/mcp", token="", http=http))
    assert "| `kr-oa-300` | OA 300건 | 300 | 300 | 5000 | Feature 900 |" in answer
    assert "Authorization" not in http.posts[-1][0]


def test_query_tools_need_text_and_only_read_tools_are_allowed():
    with pytest.raises(ValueError, match="개체 이름"):
        graph_tool.run("kg_entity", text="@kr-oa-300 ", client=GraphMcpClient(url="https://g/mcp", http=FakeHttp({})))
    with pytest.raises(GraphMcpError, match="허용하지 않은"):
        GraphMcpClient(url="https://g/mcp", http=FakeHttp({})).call("kg_page", {})


def test_missing_url_and_rejected_token_are_plain_errors_without_secrets():
    with pytest.raises(ValueError, match="CLAIM_AGENT_NEO4J_MCP_URL"):
        graph_tool.run("kg_datasets", client=GraphMcpClient(url="", http=FakeHttp({})))
    with pytest.raises(ValueError) as exc:
        graph_tool.run("kg_datasets", client=GraphMcpClient(url="https://g/mcp", token="tok-secret", http=FakeHttp({}, init_status=401)))
    assert "HTTP 401" in str(exc.value) and "tok-secret" not in str(exc.value)


def test_entity_formats_relationships():
    http = FakeHttp({"kg_entity": {"entities": [{"name": "진보성", "type": "RejectionGround", "dataset": "kr-oa-300", "degree": 3,
                                                 "properties": {}, "sources": ["case-001.md"],
                                                 "relationships": [{"direction": "in", "relation": "REJECTED_FOR", "other": "통지1", "other_type": "Notice"}]}]}})
    answer = graph_tool.run("kg_entity", text="진보성", client=GraphMcpClient(url="https://g/mcp", http=http))
    assert "**진보성** [RejectionGround]" in answer and "← REJECTED_FOR 통지1 [Notice]" in answer and "출처: case-001.md" in answer
    assert http.posts[-1][1]["params"]["arguments"] == {"name": "진보성"}


def test_graph_turns_show_in_the_conversation_but_never_reach_a_model(tmp_path, monkeypatch):
    from claim_agent import free_work, web

    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    http = FakeHttp({"kg_search": SEARCH})
    original = graph_tool.run
    monkeypatch.setattr(graph_tool, "run", lambda tool, **kw: original(tool, client=GraphMcpClient(url="https://g/mcp", http=http), **kw))
    ws = web.Workspace(tmp_path / "web")
    try:
        sid = ws.create(None, free_work.FREE)["id"]
        folder = ws.directory(sid)
        ws.sessions[sid]["messages"] = [dict(id="u", role="user", text="발명 설명")]
        ws.caselaw(sid, {"tool": "kg_search", "text": "@kr-oa-300 진보성"})
        asked, answer = ws.sessions[sid]["messages"][-2:]
        assert asked["mode"] == answer["mode"] == web.CASELAW and asked["text"] == "[그래프 조회 · 원문 검색] @kr-oa-300 진보성"
        assert answer["caselaw"] == dict(tool="kg_search", source=None) and "가이드 홈" in answer["text"]
        assert [m["id"] for m in web.conversational(ws.sessions[sid]["messages"])] == ["u"]
        assert not (folder / "history.json").exists()
    finally:
        ws.close()
