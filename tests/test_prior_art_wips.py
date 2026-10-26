"""WIPS MCP 연결(네트워크 없이 가짜 서버): OAuth 대행, SSE, 401 재인증, 404 세션 재개, 검색·서지 합치기, 전문 조립,
search_all의 나라별 실패 경고, on_call 기록의 비밀값 배제."""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest
import requests

from claim_agent.prior_art import quotes, wips

KEY = "good-key-0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAIM_AGENT_WIPS_URL", "CLAIM_AGENT_WIPS_TIMEOUT", "WIPS_API_KEY"):
        monkeypatch.delenv(name, raising=False)


class Resp:
    def __init__(self, status=200, body=None, headers=None, sse=False, text=None):
        self.status_code, self.headers = status, dict(headers or {})
        if sse:
            self.headers["Content-Type"] = "text/event-stream"
            self.content = f"event: message\r\ndata: {json.dumps(body, ensure_ascii=False)}\r\n\r\n".encode()
        elif text is not None:
            self.headers.setdefault("Content-Type", "text/html")
            self.content = text.encode()
        else:
            self.headers.setdefault("Content-Type", "application/json")
            self.content = json.dumps(body).encode() if body is not None else b""

    @property
    def text(self):
        return self.content.decode()

    def json(self):
        return json.loads(self.content)


class FakeServer:
    """WIPSCLIP MCP 서버 흉내. tools: 도구 이름 → (arguments → structuredContent)."""

    def __init__(self, tools, key=KEY):
        self.tools, self.key = tools, key
        self.tokens, self.sessions, self.log = set(), set(), []
        self.n = self.opened = 0
        self.client_names = []

    def post(self, url, json=None, data=None, headers=None, timeout=None, allow_redirects=True):
        path = urlparse(url).path
        self.log.append(("POST", path, (json or {}).get("method")))
        if path == "/register":
            self.client_names.append(json["client_name"])
            return Resp(201, {"client_id": "cid"})
        if path == "/login":
            if data["mcp_key"] != self.key or data["req"] != "REQ&1":
                return Resp(200, text="<form>키가 틀렸습니다</form>")
            return Resp(302, headers={"Location": wips.REDIRECT + "?code=CODE&state=s"})
        if path == "/token":
            assert data["code"] == "CODE" and data["code_verifier"]
            self.n += 1
            tok = f"tok{self.n}-secret"
            self.tokens.add(tok)
            return Resp(200, {"access_token": tok, "token_type": "Bearer"})
        assert path == "/mcp"
        if headers.get("Authorization", "")[7:] not in self.tokens:
            return Resp(401, {"error": "Unauthorized"})
        method = json["method"]
        if method == "initialize":
            assert json["params"]["clientInfo"]["name"] == "claim-agent"
            self.opened += 1
            sid = f"sess{self.opened}-secret"
            self.sessions.add(sid)
            return Resp(200, {"jsonrpc": "2.0", "id": json["id"], "result": {}}, {"mcp-session-id": sid}, sse=True)
        if method == "notifications/initialized":
            return Resp(202)
        if headers.get("mcp-session-id") not in self.sessions:
            return Resp(404, {"error": "session"})
        p = json["params"]
        out = self.tools[p["name"]](p["arguments"])
        return Resp(200, {"jsonrpc": "2.0", "id": json["id"],
                          "result": {"content": [{"type": "text", "text": "…"}], "structuredContent": out, "isError": False}}, sse=True)

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        path = urlparse(url).path
        self.log.append(("GET", path, None))
        if path == "/authorize":
            assert params["code_challenge_method"] == "S256" and params["resource"] == wips.DEFAULT_URL
            return Resp(302, headers={"Location": "/login?req=abc"})
        return Resp(200, text='<input type="hidden" name="req" value="REQ&amp;1"><input name="mcp_key">')


class Stub:
    """search·fetch용 가짜 클라이언트: 도구 이름 → (arguments → data)."""

    def __init__(self, tools):
        self.tools, self.calls = tools, []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return self.tools[tool](args)


def test_auth_session_and_reconnect():
    srv = FakeServer({"echo": lambda a: {"success": True, "data": {"한글": a["x"]}}})
    c = wips.WipsClient(KEY, http=srv)
    assert c.call("echo", {"x": "가"}) == {"success": True, "data": {"한글": "가"}}  # SSE를 UTF-8로 읽는다
    assert [e[1] for e in srv.log[:5]] == ["/register", "/authorize", "/login", "/login", "/token"]
    assert srv.client_names == ["claim-agent"]
    assert c.token == "tok1-secret" and c.session_id == "sess1-secret"
    srv.sessions.clear()                        # 세션 만료(404) → 같은 토큰으로 세션만 다시
    c.call("echo", {"x": "1"})
    assert c.token == "tok1-secret" and c.session_id == "sess2-secret" and srv.n == 1
    srv.tokens.clear()                          # 토큰 만료(401) → 다시 로그인
    c.call("echo", {"x": "2"})
    assert c.token == "tok2-secret" and srv.n == 2


def test_key_from_env_at_call_time(monkeypatch):
    srv = FakeServer({"echo": lambda a: {"success": True}})
    assert not wips.available()
    c = wips.WipsClient(http=srv)
    with pytest.raises(wips.WipsError, match=r"\.env"):
        c.call("echo", {})
    monkeypatch.setenv("WIPS_API_KEY", f'"{KEY}"')
    assert wips.available()
    assert c.call("echo", {}) == {"success": True}


def test_bad_key_and_tool_error():
    srv = FakeServer({"bad": lambda a: {"success": False, "error": "NOT_FOUND"}})
    with pytest.raises(wips.WipsError, match="WIPS_API_KEY"):
        wips.WipsClient("wrong", http=srv).call("bad", {})
    with pytest.raises(wips.WipsError, match="NOT_FOUND"):
        wips.WipsClient(KEY, http=srv).call("bad", {})
    with pytest.raises(wips.WipsError, match=r"\.env"):
        wips.WipsClient("", http=srv).call("bad", {})


def test_on_call_payload_has_no_secrets():
    holder = {}
    srv = FakeServer({"ok": lambda a: {"success": True},
                      "leak": lambda a: {"success": False, "error": f"bad {KEY} {holder['c'].token} {holder['c'].session_id}"}})
    rows = []
    c = holder["c"] = wips.WipsClient(KEY, http=srv, on_call=rows.append)
    c.call("ok", {})
    with pytest.raises(wips.WipsError) as info:
        c.call("leak", {})
    assert [r["ok"] for r in rows] == [True, False]
    assert all(set(r) == {"tool", "latency_ms", "ok", "error"} and isinstance(r["latency_ms"], int) for r in rows)
    assert rows[0]["error"] is None and "[redacted]" in rows[1]["error"]
    dumped = json.dumps(rows) + str(info.value)
    for secret in (KEY, c.token, c.session_id):
        assert secret not in dumped


def test_transport_error_becomes_wips_error():
    class Down:
        def post(self, *a, **k):
            raise requests.ConnectionError(f"boom {KEY}")

        def get(self, *a, **k):
            raise requests.ConnectionError("boom")

    rows = []
    with pytest.raises(wips.WipsError, match="연결 실패") as info:
        wips.WipsClient(KEY, http=Down(), on_call=rows.append).call("x", {})
    assert KEY not in str(info.value) and rows[0]["ok"] is False and KEY not in rows[0]["error"]


def test_search_merges_countries_and_biblio():
    def ai(a):
        res = {"KR": [{"skey": "1", "ctry": "KR", "mngNum": "10-1", "adjustScoreDp": "70.1"},
                      {"skey": "2", "ctry": "KR", "mngNum": "10-2", "adjustScoreDp": "80.5"}],
               "JP": [{"skey": "2", "ctry": "KR", "mngNum": "10-2", "adjustScoreDp": "99"},   # 겹치면 처음 것
                      {"skey": "3", "ctry": "JP", "mngNum": "7000", "adjustScoreDp": "75"}]}[a["ctry"]]
        return {"success": True, "data": {"results": res}}

    def engine(a):
        assert a["query"] == "(1 2 3).SKEY." and "TIK" in a["outitem"] and a["pgrow"] == 3
        return {"success": True, "data": {"row_list": [
            {"SKEY": "2", "CC": "KR", "XN_D": "10-2", "KC_D": "B1", "TIK": "냉각판", "ABK": "요약", "XD": "20240614", "AD": "20221207"},
            {"SKEY": "3", "CC": "JP", "XN_D": "7000", "KC_D": "B2", "TIK": "", "TIE": "Cooling", "ABK": "", "ABE": "abs", "XD": "2020"}]}}

    stub = Stub({"get_patent_ai_search": ai, "get_patent_search_onlyengine": engine})
    hits = wips.search(stub, " 냉각판 ", ["KR", "JP"], 99)
    ai_calls = [a for t, a in stub.calls if t == "get_patent_ai_search"]
    assert [a["size"] for a in ai_calls] == [wips.MAX_SIZE] * 2 and ai_calls[1]["targetCtry"] == "JP"
    assert [h["skey"] for h in hits] == ["2", "3", "1"]
    assert hits[0]["published"] == "2024-06-14" and hits[0]["filed"] == "2022-12-07" and wips.number(hits[0]) == "KR 10-2 B1"
    assert hits[1]["title"] == "Cooling" and hits[1]["abstract"] == "abs" and hits[1]["published"] == "2020"
    assert hits[2]["title"] == "" and wips.number(hits[2]) == "KR 10-1"
    assert set(hits[0]) == {"skey", "ctry", "number", "kind", "title", "abstract", "published", "filed", "score"}
    with pytest.raises(wips.WipsError):
        wips.search(stub, "  ", ["KR"])


def _ai_by(table):
    """(나라, 검색 문장) → 결과 목록 또는 예외."""
    def ai(a):
        out = table[(a["ctry"], a["keyword"])]
        if isinstance(out, Exception):
            raise out
        return {"success": True, "data": {"results": out}}
    return ai


def _engine(a):
    return {"success": True, "data": {"row_list": []}}


def test_search_all_merges_queries_and_reports_country_failure():
    table = {("KR", "국문 하나"): [{"skey": "1", "mngNum": "10-1", "adjustScoreDp": "60"}],
             ("KR", "국문 둘"): [{"skey": "1", "mngNum": "10-1", "adjustScoreDp": "90"}, {"skey": "2", "mngNum": "10-2", "adjustScoreDp": "50"}],
             ("US", "english one"): wips.WipsError("WIPS get_patent_ai_search 실패(HTTP 500)."),
             ("US", "english two"): [{"skey": "9", "ctry": "US", "mngNum": "123", "adjustScoreDp": "70"}],
             ("EP", "english one"): [], ("EP", "english two"): []}
    stub = Stub({"get_patent_ai_search": _ai_by(table), "get_patent_search_onlyengine": _engine})
    seen = []
    hits, warns = wips.search_all(stub, ["국문 하나", "국문 둘", " "], ["english one", "english two"], ["KR", "US", "EP"], 10, seen.append)
    assert [h["skey"] for h in hits] == ["1", "9", "2"]
    assert hits[0]["queries"] == ["국1", "국2"] and hits[0]["score"] == 90.0
    assert hits[1]["queries"] == ["영2"]
    assert any("US" in w and "영1" in w and "HTTP 500" in w for w in warns)
    assert not any(w.startswith("EP") for w in warns)          # EP는 영문 문장이 있으니 검색했다
    assert "WIPS 검색 KR · 문장 1/2" in seen


def test_search_all_skips_country_without_queries():
    table = {("KR", "국문"): [{"skey": "1", "mngNum": "10-1", "adjustScoreDp": "60"}]}
    stub = Stub({"get_patent_ai_search": _ai_by(table), "get_patent_search_onlyengine": _engine})
    hits, warns = wips.search_all(stub, ["국문"], [], ["KR", "US"], 10)
    assert [h["skey"] for h in hits] == ["1"] and any("US" in w and "검색하지 않았습니다" in w for w in warns)
    with pytest.raises(wips.WipsError, match="검색 문장이 없습니다"):
        wips.search_all(stub, [], [], ["US"], 10)


def test_search_all_raises_when_every_search_fails():
    err = wips.WipsError("WIPS get_patent_ai_search 실패(HTTP 503).")
    table = {("KR", "국문"): err, ("US", "english"): err}
    stub = Stub({"get_patent_ai_search": _ai_by(table), "get_patent_search_onlyengine": _engine})
    with pytest.raises(wips.WipsError, match="모두 실패"):
        wips.search_all(stub, ["국문"], ["english"], ["KR", "US"], 10)


def test_fetch_builds_text_with_paragraph_numbers():
    desc = ('<BR><FONT COLOR=RED>[기술분야]</FONT><BR><p num="0001">[0001] 본 발명은 냉각판에 관한 것이다.</p>'
            '<p num="0002">[0002] 냉각판은 A&amp;B 유로를 가진다.</p><table><tr><td>x</td><td>y</td></tr></table>')
    stub = Stub({"get_patent_spec_text": lambda a: {"success": True, "data": (
        {"cl_list": [{"cl_num": "1", "cl": "냉각판;<br />유로를 포함하는, 모듈."}]} if a["section"] == "all_claim"
        else {"dtl_desc_list": [{"dtl_desc": desc}]})}})
    hit = {"skey": "9", "ctry": "KR", "number": "10-2024-0084709", "kind": "A", "title": "배터리 모듈",
           "abstract": "냉각 유로를 포함한다.", "published": "2024-06-14", "filed": "2022-12-07", "score": 78.8}
    text, meta = wips.fetch(stub, hit)
    assert text.startswith("KR 10-2024-0084709 A — 배터리 모듈\n\n공개일 2024-06-14 · 출원일 2022-12-07\n\n【요약】\n냉각 유로를 포함한다.")
    assert "【청구항 1】 냉각판;\n유로를 포함하는, 모듈." in text and "A&B 유로" in text and "<" not in text
    assert "【발명의 설명】\n[기술분야]\n[0001] 본 발명은" in text
    assert quotes.find_quote(text, "냉각판은 A&B 유로를 가진다") == {"where": "[0002]", "exact": True}
    assert meta == {"skey": "9", "number": "KR 10-2024-0084709 A", "title": "배터리 모듈", "published": "2024-06-14",
                    "filed": "2022-12-07", "score": 78.8, "warnings": [wips.DRAWING_NOTE],
                    "claims_text": "【요약】\n냉각 유로를 포함한다.\n\n【청구범위】\n【청구항 1】 냉각판;\n유로를 포함하는, 모듈."}
    assert "발명의 설명" not in meta["claims_text"]


def test_fetch_keeps_going_without_description():
    def spec(a):
        if a["section"] == "dtl_desc":
            raise wips.WipsError("WIPS get_patent_spec_text 실패: NOT_FOUND")
        return {"success": True, "data": {"cl_list": []}}

    text, meta = wips.fetch(Stub({"get_patent_spec_text": spec}), {"skey": "1", "ctry": "US", "number": "123", "kind": "B2", "title": "",
                                                                  "abstract": "an abstract", "published": "", "filed": ""})
    assert text == "US 123 B2\n\n【요약】\nan abstract" and meta["claims_text"] == "【요약】\nan abstract"
    assert any("NOT_FOUND" in w for w in meta["warnings"]) and meta["score"] == 0.0


def test_env_overrides_url(monkeypatch):
    monkeypatch.setenv("CLAIM_AGENT_WIPS_URL", "https://example.test/x/mcp")
    monkeypatch.setenv("CLAIM_AGENT_WIPS_TIMEOUT", "7")
    assert wips.server_url() == "https://example.test/x/mcp" and wips.timeout_s() == 7
    assert wips.date("20240614") == "2024-06-14" and wips.date("2024") == "2024"
    assert wips.clean_html("a<br>b&nbsp;c") == "a\nb c"
