"""Restricted corpus tools exposed to claim-style-adjuster (automatic function calling)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..sources.corpus import MAX_FRAGMENTS, CorpusIndex, is_forbidden_query
from ..sources.registry import SourceSet


@dataclass
class ToolLog:
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def used(self) -> bool:
        return any(c.get("name") == "search_style_corpus" and c.get("results") for c in self.calls)

    def render(self) -> str:
        if not self.calls:
            return "조건부 보조 소스: NOT_ACTIVATED"
        lines = ["조건부 보조 소스: USED", "사용 파일: sources/청구항_예시검색_라우팅인덱스.md, sources/청구항_문체학습용_분야별검색최적화본.md"]
        for c in self.calls:
            if c["name"] == "open_routing_index":
                lines.append("- open_routing_index(): 라우팅 인덱스 전문 열람")
            else:
                lines.append(f"- search_style_corpus(mode={c['mode']}, query={c['query']!r}) → {len(c['results'])}건")
                for r in c["results"]:
                    lines.append(f"  · {r['ex_id']} 청구항 {r['claim_no']}{' [재검토 대상]' if r['flagged_review'] else ''}: {r['context']}")
        return "\n".join(lines)


def make_tools(sources: SourceSet, log: ToolLog) -> list[Callable[..., Any]]:
    index = CorpusIndex.parse(sources.get("CORPUS").text)
    routing_text = sources.get("ROUTING").text

    def open_routing_index() -> str:
        """라우팅 인덱스(sources/청구항_예시검색_라우팅인덱스.md) 전문을 반환한다. 코퍼스를 검색하기 전에 반드시 먼저 읽는다."""
        log.calls.append({"name": "open_routing_index"})
        return routing_text

    def search_style_corpus(mode: str, query: str, max_fragments: int = 2) -> dict:
        """역사 코퍼스에서 정확히 일치하는 표면 표현 조각을 최대 2개 찾는다.

        mode: "TERM_EXACT" (이미 확정된 개념의 표면 명칭·띄어쓰기·형상 술어 확인) 또는
        "LOCAL_SYNTAX" (정확히 특정된 국소 통사 문제의 문장 조각). 분야·아키텍처·카테고리 검색은 금지된다.
        query: 코퍼스에서 찾을 정확한 문자열.
        """
        if is_forbidden_query(query) or mode not in ("TERM_EXACT", "LOCAL_SYNTAX"):
            entry = {"name": "search_style_corpus", "mode": mode, "query": query, "results": [], "refused": True}
            log.calls.append(entry)
            return {"error": "분야·아키텍처·카테고리 기반 검색 또는 잘못된 mode는 허용되지 않는다.", "results": []}
        n = max(1, min(int(max_fragments), MAX_FRAGMENTS))
        frags = index.search_exact_term(query, n) if mode == "TERM_EXACT" else index.search_local_syntax(query, n)
        results = [f.as_dict() for f in frags]
        log.calls.append({"name": "search_style_corpus", "mode": mode, "query": query, "results": results})
        return {"results": results, "note": "예시 없음" if not results else "정확한 조각과 최소 문맥만 사용하고 전체 청구항을 모방하지 않는다."}

    return [open_routing_index, search_style_corpus]
