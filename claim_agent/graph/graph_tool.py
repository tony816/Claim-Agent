"""작업 도구 패널의 그래프 조회: Neo4j GraphRAG MCP 커넥터(neo4j_mcp)로 올라간 KG 데이터셋을 찾아본다.

- `kg_datasets` 데이터셋 목록: 이름·설명·문서·청크·개체 수와 많이 나온 개체 유형
- `kg_search` 원문 검색: 입력창의 말로 원문 청크를 의미+낱말 검색하고 그 청크의 개체·관계를 함께 보인다
- `kg_entity` 개체 보기: 이름에 입력한 말이 든 개체와 연결된 관계·출처

입력창 첫머리에 `@데이터셋` (예: `@kr-oa-300 진보성 거절 뒤 더한 한정`)을 쓰면 그 데이터셋만 본다.
모델·Jev를 부르지 않는다. 답은 판례·OA 도구처럼 사용자만 보는 참고이며 대화 기록·원자료·검수 대상에 넣지 않는다
(web.conversational). 그래프는 LLM이 자동 추출한 개체·관계라 발명 원자료나 게이트 근거가 아니다.
"""
from __future__ import annotations

import re

from .neo4j_mcp import GraphMcpClient, GraphMcpError

TOOLS = {"kg_datasets": "데이터셋 목록", "kg_search": "원문 검색", "kg_entity": "개체 보기"}
MAX_TEXT = 2000
TOP_K = 8
CHUNK_CHARS = 700
MAX_RELS = 40
PRIVATE = ("이 답은 사용자만 보는 참고입니다. 대화 기록에 넣지 않으므로 일반 에이전트·검수 역할·청구항 작성 역할은 읽지 않습니다. "
           "그래프의 개체·관계는 문서에서 LLM이 자동 추출한 것이라 원문 청크로 확인해야 하며, 발명 원자료·게이트 근거가 아닙니다. "
           "청구항에 반영하려면 사용자가 그 내용을 자기 말로 요청하고, 청구하는 구성은 원자료에 있어야 합니다.")
_DATASET = re.compile(r"^@([\w.\-]+)\s*")


def split_dataset(text: str) -> tuple[str, str]:
    """'@kr-oa-300 질의' → ('kr-oa-300', '질의'). 접두가 없으면 ('', text)."""
    text = text.strip()
    m = _DATASET.match(text)
    return (m.group(1), text[m.end():].strip()) if m else ("", text)


def run(tool: str, *, text: str = "", client=None) -> str:
    if tool not in TOOLS:
        raise ValueError("지원하지 않는 그래프 도구입니다.")
    dataset, query = split_dataset(text)
    if tool != "kg_datasets" and not query:
        raise ValueError(f"{TOOLS[tool]}는 입력창에 {'찾을 내용' if tool == 'kg_search' else '개체 이름'}을 적고 Enter를 누르세요"
                         " (앞에 @데이터셋을 쓰면 그 데이터셋만 봅니다).")
    if len(query) > MAX_TEXT:
        raise ValueError(f"그래프 조회에는 {MAX_TEXT}자까지 넣을 수 있습니다.")
    client = client or GraphMcpClient()
    try:
        if tool == "kg_datasets":
            body = _datasets(client.call("kg_datasets", {}))
        elif tool == "kg_search":
            body = _search(client.call("kg_search", {"query": query, "dataset": dataset or None, "top_k": TOP_K}))
        else:
            body = _entity(client.call("kg_entity", {"name": query, "dataset": dataset or None}))
    except GraphMcpError as exc:
        raise ValueError(str(exc)) from None
    scope = f" · 데이터셋: {dataset}" if dataset else (" · 전체 데이터셋" if tool != "kg_datasets" else "")
    head = f"**그래프 참고 (미검토 — 게이트·근거 아님)** · {TOOLS[tool]}{scope}" + (f" · “{_cut(query, 60)}”" if query else "")
    return "\n\n".join([head, PRIVATE, body])


def _cut(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def _error(data: dict) -> str | None:
    return f"없음 — {data['error']}" if isinstance(data, dict) and data.get("error") else None


def _datasets(data: dict) -> str:
    rows = data.get("datasets") or []
    if not rows:
        return _error(data) or "올라간 데이터셋이 없습니다."
    out = ["| 데이터셋 | 설명 | 문서 | 청크 | 개체 | 많은 개체 유형 |", "|---|---|---|---|---|---|"]
    for r in rows:
        types = ", ".join(f"{t.get('type')} {t.get('count')}" for t in (r.get("entity_types") or [])[:5])
        kind = " (쪽 단위)" if r.get("pages") else ""
        out.append(f"| `{r.get('name')}`{kind} | {_cut(r.get('description'), 60)} | {r.get('documents', '')} | {r.get('chunks', '')} | "
                   f"{r.get('entities', '')} | {types} |")
    return "\n".join(out) + "\n\n원문 검색·개체 보기에서 입력창 앞에 `@데이터셋`을 쓰면 그 데이터셋만 봅니다."


def _search(data: dict) -> str:
    chunks = data.get("chunks") or []
    if not chunks:
        return _error(data) or "맞는 원문이 없습니다. 다른 낱말이나 데이터셋으로 찾아보세요."
    out = []
    for i, c in enumerate(chunks, 1):
        where = c.get("source") or ""
        if c.get("page") is not None:
            where = f"{c.get('book') or where} p.{c.get('page')}"
        score = c.get("score")
        meta = " · ".join(x for x in (f"`{c.get('dataset')}`", where, f"점수 {score:.3f}" if isinstance(score, (int, float)) else "",
                                      "낱말 일치" if c.get("keyword_hit") else "") if x)
        text = str(c.get("text") or "").strip()
        text = text if len(text) <= CHUNK_CHARS else text[:CHUNK_CHARS] + "…"
        quoted = "\n".join("> " + line for line in text.splitlines() if line.strip())
        ents = ", ".join((c.get("entities") or [])[:12])
        out.append(f"**{i}.** {meta}\n\n{quoted}" + (f"\n\n개체: {ents}" if ents else ""))
    rels = data.get("relationships") or []
    if rels:
        out.append("**관계** (자동 추출)\n" + "\n".join(f"- {r.get('source')} —{r.get('relation')}→ {r.get('target')}" for r in rels[:MAX_RELS])
                   + (f"\n- … 외 {len(rels) - MAX_RELS}개" if len(rels) > MAX_RELS else ""))
    return "\n\n".join(out)


def _entity(data: dict) -> str:
    ents = data.get("entities") or []
    if not ents:
        return _error(data) or "맞는 개체가 없습니다. 원문 검색으로 찾아보세요."
    out = []
    for e in ents:
        head = f"**{e.get('name')}** [{e.get('type')}] · `{e.get('dataset')}` · 연결 {e.get('degree', '')}"
        props = "; ".join(f"{k}: {_cut(v, 120)}" for k, v in (e.get("properties") or {}).items())
        rels = e.get("relationships") or []
        lines = [f"- {'→' if r.get('direction') == 'out' else '←'} {r.get('relation')} {r.get('other')} [{r.get('other_type')}]" for r in rels[:MAX_RELS]]
        if len(rels) > MAX_RELS:
            lines.append(f"- … 외 {len(rels) - MAX_RELS}개")
        src = ", ".join((e.get("sources") or [])[:10])
        out.append("\n".join([head] + ([f"속성: {props}"] if props else []) + lines + ([f"출처: {src}"] if src else [])))
    return "\n\n".join(out)
