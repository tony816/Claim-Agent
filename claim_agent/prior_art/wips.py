"""WIPSCLIP MCP로 선행 특허를 찾고 전문을 받는다(cs2-spec-writer 6556039 pipeline/wips.py · prior_search.search_all 이식).

cs2와 다른 점: `import config` 대신 환경 변수를 부를 때 읽고, 클라이언트를 전역 하나로 두지 않고 인자로 받으며,
fetch는 Doc 대신 (본문, 메타)를 돌려준다. search_all은 나라·문장 하나의 실패를 경고로 돌리고 모두 실패할 때만 멈춘다.

인증: 서버는 OAuth(authorization_code + PKCE)만 받고 로그인 화면은 'MCP 키' 한 칸이다. 그래서 WIPS_API_KEY로 그 과정을
코드가 대신 밟아 Bearer 토큰을 받는다(클라이언트 등록 → /authorize → /login에 키 → 돌아온 code를 /token에서 교환).
리디렉션 주소는 열지 않고 Location 머리글에서 code만 읽는다. 키·토큰·MCP 세션 id는 프로세스 메모리에만 두고
오류·로그·on_call 기록에 넣지 않는다. MCP는 JSON-RPC over HTTP(응답은 JSON 또는 SSE 한 건)라 requests로 직접 부른다.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter

DEFAULT_URL = "https://mcp.wipsclip.co.kr/mcp"
REDIRECT = "http://127.0.0.1/wips-callback"   # 열지 않는다(code만 읽는다)
CLIENT_NAME = "claim-agent"
PROTOCOL = "2025-06-18"
COUNTRIES = ["KR", "JP", "US", "CN", "EP", "PCT"]
KO_COUNTRIES = {"KR", "JP"}                    # 국문 검색 문장을 쓰는 나라
ENGLISH_ONLY = {"US", "CN", "EP", "PCT"}       # 이 나라는 영문 질의만 받는다
MAX_SIZE = 50
BIBLIO = "SKEY,XN_D,CC,KC_D,TIK,TIE,ABK,ABE,XD,AD"
DRAWING_NOTE = "WIPS에서 받은 글 자료 — 도면은 넣지 않았습니다(필요하면 도면 이미지를 따로 올려 주세요)."


class WipsError(RuntimeError):
    pass


def server_url() -> str:
    return (os.getenv("CLAIM_AGENT_WIPS_URL") or "").strip() or DEFAULT_URL


def timeout_s() -> int:
    try:
        return int(os.getenv("CLAIM_AGENT_WIPS_TIMEOUT") or 120)
    except ValueError:
        return 120


def _key() -> str:
    return (os.getenv("WIPS_API_KEY") or "").strip().strip("\"'")


def available() -> bool:
    return bool(_key())


class _ContextAdapter(HTTPAdapter):
    """requests가 certifi 대신 주어진 SSLContext(Windows 신뢰 저장소)를 쓰게 한다. 전역 ssl은 바꾸지 않는다."""

    def __init__(self, context: ssl.SSLContext):
        self._context = context
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context
        return super().proxy_manager_for(*args, **kwargs)


def _session() -> requests.Session:
    session = requests.Session()
    if sys.platform == "win32":
        import truststore

        # 사내 TLS 검사 프록시의 루트 인증서를 믿도록 OS 저장소로 검증한다(provider.openrouter.tls_verify와 같은 방식).
        session.mount("https://", _ContextAdapter(truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)))
    return session


def _parse(r):
    """JSON 또는 SSE(첫 data 이벤트) 응답. SSE는 charset이 없어 바이트를 UTF-8로 읽는다."""
    if r.headers.get("Content-Type", "").startswith("text/event-stream"):
        for ev in r.content.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in ev.split("\n") if line.startswith("data:"))
            if data:
                return json.loads(data)
        return None
    return r.json() if r.content else None


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


class WipsClient:
    """WIPS MCP 도구 호출. key=None이면 인증할 때마다 WIPS_API_KEY를 읽는다. http: 시험용 가짜 세션."""

    def __init__(self, key: str | None = None, http=None, on_call: Callable[[dict], None] | None = None):
        self._fixed_key = key
        self.http = http or _session()
        self.on_call = on_call
        self.token, self.session_id = "", ""
        self._id = 0
        self._lock = threading.RLock()

    @property
    def key(self) -> str:
        return self._fixed_key if self._fixed_key is not None else _key()

    def _scrub(self, text: str) -> str:
        """오류 문구에 키·토큰·세션 id가 섞여 들어오면 지운다."""
        for secret in (self.key, self.token, self.session_id):
            if secret and len(secret) >= 4:
                text = text.replace(secret, "[redacted]")
        return text

    # ---------- 인증 ----------
    def _authorize(self) -> None:
        key = self.key
        if not key:
            raise WipsError(".env에 WIPS_API_KEY가 없습니다.")
        url, timeout = server_url(), timeout_s()
        base = url.rsplit("/", 1)[0]
        reg = self.http.post(base + "/register", json={
            "client_name": CLIENT_NAME, "redirect_uris": [REDIRECT], "grant_types": ["authorization_code"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}, timeout=timeout)
        if reg.status_code >= 400:
            raise WipsError(f"WIPS 클라이언트 등록 실패(HTTP {reg.status_code}).")
        client_id = reg.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = self.http.get(base + "/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": secrets.token_urlsafe(8), "resource": url},
            timeout=timeout, allow_redirects=False)
        page = self.http.get(base + auth.headers.get("Location", "/login"), timeout=timeout)
        m = re.search(r'name="req" value="([^"]+)"', page.text)
        if not m:
            raise WipsError("WIPS 로그인 화면을 읽지 못했습니다.")
        login = self.http.post(base + "/login", data={"req": html.unescape(m.group(1)), "mcp_key": key},
                               timeout=timeout, allow_redirects=False)
        code = parse_qs(urlparse(login.headers.get("Location", "")).query).get("code", [""])[0]
        if not code:
            raise WipsError("WIPS가 MCP 키를 받지 않았습니다(.env의 WIPS_API_KEY를 확인하세요).")
        tok = self.http.post(base + "/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client_id,
            "code_verifier": verifier}, timeout=timeout)
        if tok.status_code >= 400 or not tok.json().get("access_token"):
            raise WipsError(f"WIPS 토큰 발급 실패(HTTP {tok.status_code}).")
        self.token, self.session_id = tok.json()["access_token"], ""

    # ---------- JSON-RPC ----------
    def _post(self, method: str, params: dict | None = None, notify: bool = False):
        body: dict = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self._id += 1
            body["id"] = self._id
        if params is not None:
            body["params"] = params
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return self.http.post(server_url(), headers=headers, json=body, timeout=timeout_s())

    def _initialize(self):
        return self._post("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                         "clientInfo": {"name": CLIENT_NAME, "version": "1"}})

    def _open(self) -> None:
        if not self.token:
            self._authorize()
        r = self._initialize()
        if r.status_code == 401:
            self._authorize()
            r = self._initialize()
        if r.status_code >= 400:
            raise WipsError(f"WIPS 연결 실패(HTTP {r.status_code}).")
        self.session_id = r.headers.get("mcp-session-id", "")
        self._post("notifications/initialized", notify=True)

    def _call(self, tool: str, args: dict) -> dict:
        with self._lock:
            for attempt in (0, 1):
                if not self.session_id:
                    self._open()
                r = self._post("tools/call", {"name": tool, "arguments": args})
                if r.status_code in (401, 404) and attempt == 0:  # 토큰 만료 또는 세션 만료
                    self.token = "" if r.status_code == 401 else self.token
                    self.session_id = ""
                    continue
                break
        if r.status_code >= 400:
            raise WipsError(f"WIPS {tool} 실패(HTTP {r.status_code}).")
        msg = _parse(r) or {}
        if "error" in msg:
            err = msg["error"]
            raise WipsError(f"WIPS {tool} 실패: {_short(str(err.get('message', err) if isinstance(err, dict) else err), 300)}")
        res = msg.get("result") or {}
        data = res.get("structuredContent")
        if data is None:
            text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            try:
                data = json.loads(text)
            except ValueError:
                data = {"success": not res.get("isError"), "error": text}
        if not isinstance(data, dict):
            data = {"success": not res.get("isError"), "data": data}
        if res.get("isError") or data.get("success") is False:
            raise WipsError(f"WIPS {tool} 실패: {_short(str(data.get('error') or data.get('message') or data), 300)}")
        return data

    def call(self, tool: str, args: dict) -> dict:
        """도구 하나를 불러 결과 data(dict)를 돌려준다. 인증·세션이 끊겼으면 한 번 다시 연다. 실패는 모두 WipsError."""
        start = time.monotonic()
        try:
            data = self._call(tool, args)
        except WipsError as e:
            err = WipsError(self._scrub(str(e)))
        except requests.RequestException as e:
            err = WipsError(f"WIPS {tool} 연결 실패({type(e).__name__}).")
        except (ValueError, KeyError, TypeError) as e:
            err = WipsError(f"WIPS {tool} 응답을 읽지 못했습니다({type(e).__name__}).")
        else:
            self._report(tool, start, None)
            return data
        self._report(tool, start, str(err))
        raise err

    def _report(self, tool: str, start: float, error: str | None) -> None:
        if self.on_call:
            self.on_call({"tool": tool, "latency_ms": int((time.monotonic() - start) * 1000), "ok": error is None, "error": error})


# ---------------- 검색 ----------------
def date(s: str) -> str:
    """'20240614' → '2024-06-14'(이미 다른 꼴이면 그대로)."""
    s = (s or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else s


def number(hit: dict) -> str:
    return " ".join(x for x in (hit.get("ctry", ""), hit.get("number", ""), hit.get("kind", "")) if x)


def search(client, query: str, countries: list[str], size: int = 10) -> list[dict]:
    """AI 유사도 검색(나라마다) 뒤 서지(명칭·요약·공개일)를 한 번에 붙인다. 점수 높은 순.
    반환: [{skey, ctry, number, kind, title, abstract, published, filed, score}]."""
    query = query.strip()
    if not query:
        raise WipsError("검색할 내용을 적으세요.")
    size = max(1, min(int(size), MAX_SIZE))
    hits: dict[str, dict] = {}
    for c in countries or ["KR"]:
        data = client.call("get_patent_ai_search", {"keyword": query, "ctry": c, "targetCtry": c, "size": size})
        for x in (data.get("data") or {}).get("results", []):
            if x.get("skey") and x["skey"] not in hits:
                hits[x["skey"]] = {"skey": x["skey"], "ctry": x.get("ctry", c), "number": x.get("mngNum", ""), "kind": "", "title": "",
                                   "abstract": "", "published": "", "filed": "", "score": float(x.get("adjustScoreDp") or 0)}
    if hits:
        rows = client.call("get_patent_search_onlyengine", {"query": f"({' '.join(hits)}).SKEY.", "outitem": BIBLIO, "pgrow": min(len(hits), 500)})
        for row in (rows.get("data") or {}).get("row_list", []):
            h = hits.get(str(row.get("SKEY", "")))
            if h:
                h.update(ctry=row.get("CC") or h["ctry"], number=row.get("XN_D") or h["number"], kind=row.get("KC_D", ""),
                         title=row.get("TIK") or row.get("TIE", ""), abstract=row.get("ABK") or row.get("ABE", ""),
                         published=date(row.get("XD", "")), filed=date(row.get("AD", "")))
    return sorted(hits.values(), key=lambda h: -h["score"])


def search_all(client, ko: list[str], en: list[str], countries: list[str], size: int, progress=lambda m: None) -> tuple[list[dict], list[str]]:
    """나라마다 그 나라 말의 문장(KR·JP는 국문, 나머지는 영문)으로 검색해 합친다. 같은 문헌은 가장 높은 점수와 찾은 문장
    번호(국1·영2 …)의 합집합으로 남긴다. 나라·문장 하나의 실패는 경고로 돌리고, 해 본 검색이 모두 실패할 때만 WipsError.
    반환: (점수 높은 순 후보, 경고)."""
    hits: dict[str, dict] = {}
    warnings: list[str] = []
    skipped: list[str] = []
    attempted = failed = 0
    for c in countries or ["KR"]:
        ko_side = c in KO_COUNTRIES
        qs = [q.strip() for q in (ko if ko_side else en) or [] if q and q.strip()]
        if not qs:
            skipped.append(c)
            continue
        for qi, q in enumerate(qs, 1):
            label = f"{'국' if ko_side else '영'}{qi}"
            progress(f"WIPS 검색 {c} · 문장 {qi}/{len(qs)}")
            attempted += 1
            try:
                found = search(client, q, [c], size)
            except WipsError as e:
                failed += 1
                warnings.append(f"WIPS 검색 {c} · {label} 실패: {e}")
                continue
            for h in found:
                h["queries"] = [label]
                old = hits.get(h["skey"])
                if old is None:
                    hits[h["skey"]] = h
                else:
                    old["queries"] = sorted(set(old["queries"] + h["queries"]))
                    old["score"] = max(old["score"], h["score"])
    if skipped:
        warnings.append(f"{', '.join(skipped)}: 그 나라 말의 검색 문장이 없어 검색하지 않았습니다.")
    if attempted == 0:
        raise WipsError(f"{', '.join(skipped)}에 쓸 검색 문장이 없습니다.")
    if failed == attempted:
        raise WipsError("WIPS 검색이 모두 실패했습니다: " + _short("; ".join(warnings), 600))
    return sorted(hits.values(), key=lambda h: -h["score"]), warnings


# ---------------- 전문 ----------------
def clean_html(s: str) -> str:
    """WIPS 본문의 HTML(<BR>, <p num=..>, <FONT>, 표)을 문단 줄글로. [0001] 문단 번호는 그대로 둔다."""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</h\d>|</li>", "\n", s or "")
    s = re.sub(r"(?i)</t[dh]>", " ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r" *\n *", "\n", s)).strip()


def _section(client, skey: str, section: str) -> dict:
    return client.call("get_patent_spec_text", {"section": section, "skey": skey}).get("data") or {}


def fetch(client, hit: dict) -> tuple[str, dict]:
    """후보 하나의 전문(요약·전체 청구항·발명의 설명)을 받는다.
    반환: (본문 markdown, {skey, number, title, published, filed, score, warnings, claims_text(요약+청구범위)})."""
    warns: list[str] = []
    head = [f"{number(hit)} — {hit.get('title', '')}".strip(" —")]
    if hit.get("published"):
        head.append(f"공개일 {hit['published']}" + (f" · 출원일 {hit['filed']}" if hit.get("filed") else ""))
    short: list[str] = []
    if hit.get("abstract"):
        short.append("【요약】\n" + clean_html(hit["abstract"]))
    try:
        cl = _section(client, hit["skey"], "all_claim").get("cl_list") or []
        if cl:
            short.append("【청구범위】\n" + "\n".join(f"【청구항 {c.get('cl_num', '')}】 {clean_html(c.get('cl', ''))}" for c in cl))
    except WipsError as e:
        warns.append(f"청구범위를 받지 못했습니다({e}).")
    parts = head + short
    try:
        d = _section(client, hit["skey"], "dtl_desc")
        desc = "\n\n".join(clean_html(x.get("dtl_desc", "")) for x in d.get("dtl_desc_list") or [])
        if desc:
            parts.append("【발명의 설명】\n" + desc)
        else:
            warns.append("발명의 설명 본문이 비어 있습니다(요약·청구범위만으로 대비합니다).")
    except WipsError as e:
        warns.append(f"발명의 설명을 받지 못했습니다({e}). 요약·청구범위만으로 대비합니다.")
    warns.append(DRAWING_NOTE)
    meta = {"skey": hit["skey"], "number": number(hit), "title": hit.get("title", ""), "published": hit.get("published", ""),
            "filed": hit.get("filed", ""), "score": hit.get("score", 0.0), "warnings": warns, "claims_text": "\n\n".join(short)}
    return "\n\n".join(parts), meta
