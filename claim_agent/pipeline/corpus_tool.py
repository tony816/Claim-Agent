"""Restricted corpus tools exposed to claim-style-adjuster (automatic function calling).

The search reads the style-reference claim graph (graph/claims.py) when it is installed: the whole source the style
corpus was strictly selected from, so a spelling can be counted across 1,014 claims and a dependent claim's parents
followed. Without the graph it reads the markdown corpus as before. Either way only exact surface matches come back.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..graph.claims import ClaimGraph
from ..sources.corpus import MAX_FRAGMENTS, CorpusIndex, is_forbidden_query
from ..sources.registry import SourceSet

CORPUS_FILE = "sources/청구항_문체학습용_분야별검색최적화본.md"
ROUTING_FILE = "sources/청구항_예시검색_라우팅인덱스.md"


@dataclass
class ToolLog:
    calls: list[dict[str, Any]] = field(default_factory=list)
    source: str = f"{ROUTING_FILE}, {CORPUS_FILE}"

    @property
    def used(self) -> bool:
        return any((c.get("name") == "search_style_corpus" and c.get("results")) or c.get("name") == "open_claim_chain" for c in self.calls)

    def render(self) -> str:
        if not self.calls:
            return "조건부 보조 소스: NOT_ACTIVATED"
        lines = ["조건부 보조 소스: USED", "사용 소스: " + self.source]
        for c in self.calls:
            if c["name"] == "open_routing_index":
                lines.append("- open_routing_index(): 라우팅 인덱스 전문 열람")
            elif c["name"] == "open_claim_chain":
                lines.append(f"- open_claim_chain(claim_id={c['claim_id']!r}): " + " · ".join(c.get("labels") or []) + (" — 없는 항" if c.get("refused") else ""))
            else:
                counts = f" (정확 일치 {c['total']}항, 코퍼스 수록 {c['in_corpus']}항)" if "total" in c else ""
                lines.append(f"- search_style_corpus(mode={c['mode']}, query={c['query']!r}) → {len(c['results'])}건{counts}")
                for r in c["results"]:
                    where = r.get("label") or f"{r['ex_id']} 청구항 {r['claim_no']}"
                    lines.append(f"  · {r.get('claim_id') or r['ex_id']} ({where}){' [재검토 대상]' if r['flagged_review'] else ''}: {r['context']}")
        return "\n".join(lines)


def make_tools(sources: SourceSet, log: ToolLog, graph: ClaimGraph | None = None, graph_problem: str = "") -> list[Callable[..., Any]]:
    """`graph` is the style-reference claim graph; None (with `graph_problem` saying why) searches the markdown corpus."""
    index = CorpusIndex.parse(sources.get("CORPUS").text)
    routing_text = sources.get("ROUTING").text
    log.source = f"{graph.describe()}, {ROUTING_FILE}" if graph else f"{ROUTING_FILE}, {CORPUS_FILE}" + (f" (청구항 그래프 미사용 — {graph_problem})" if graph_problem else "")

    def open_routing_index() -> str:
        """라우팅 인덱스(sources/청구항_예시검색_라우팅인덱스.md) 전문을 반환한다. 코퍼스를 검색하기 전에 반드시 먼저 읽는다."""
        log.calls.append({"name": "open_routing_index"})
        return routing_text

    def refuse(mode: str, query: str) -> dict | None:
        if is_forbidden_query(query) or mode not in ("TERM_EXACT", "LOCAL_SYNTAX"):
            log.calls.append({"name": "search_style_corpus", "mode": mode, "query": query, "results": [], "refused": True})
            return {"error": "분야·아키텍처·카테고리 기반 검색 또는 잘못된 mode는 허용되지 않는다.", "results": []}
        return None

    if graph is None:
        def search_style_corpus(mode: str, query: str, max_fragments: int = 2) -> dict:
            """역사 코퍼스에서 정확히 일치하는 표면 표현 조각을 최대 2개 찾는다.

            mode: "TERM_EXACT" (이미 확정된 개념의 표면 명칭·띄어쓰기·형상 술어 확인) 또는
            "LOCAL_SYNTAX" (정확히 특정된 국소 통사 문제의 문장 조각). 분야·아키텍처·카테고리 검색은 금지된다.
            query: 코퍼스에서 찾을 정확한 문자열.
            """
            if (no := refuse(mode, query)) is not None:
                return no
            n = max(1, min(int(max_fragments), MAX_FRAGMENTS))
            frags = index.search_exact_term(query, n) if mode == "TERM_EXACT" else index.search_local_syntax(query, n)
            results = [f.as_dict() for f in frags]
            log.calls.append({"name": "search_style_corpus", "mode": mode, "query": query, "results": results})
            return {"results": results, "note": "예시 없음" if not results else "정확한 조각과 최소 문맥만 사용하고 전체 청구항을 모방하지 않는다."}

        return [open_routing_index, search_style_corpus]

    def search_style_corpus(mode: str, query: str, max_fragments: int = 2) -> dict:
        """스타일 참고용 청구항 그래프(문체 코퍼스와 같은 원문 126건·1,014항)에서 정확히 일치하는 표면 표현을 찾는다.

        mode: "TERM_EXACT" (이미 확정된 개념의 표면 명칭·띄어쓰기·형상 술어 확인 — 표기 후보마다 따로 검색해 total을 비교한다) 또는
        "LOCAL_SYNTAX" (정확히 특정된 국소 통사 문제의 문장 조각). 분야·아키텍처·카테고리 검색은 금지된다.
        query: 청구항에서 찾을 정확한 문자열.
        돌려주는 것: total(그 문자열을 쓰는 항 수), in_corpus(그중 엄격선별 코퍼스 수록 항 수), 조각 최대 2개
        (코퍼스 수록 항 먼저, 재검토 대상은 맨 뒤). corpus가 null인 항은 엄격선별을 거치지 않았다.
        """
        if (no := refuse(mode, query)) is not None:
            return no
        n = max(1, min(int(max_fragments), MAX_FRAGMENTS))
        total, in_corpus, frags = graph.search(query, n)
        results = [{"claim_id": c.claim_id, "label": c.label(), "corpus": c.corpus, "claim_no": c.claim_no,
                    "cites": [graph.claims[p].claim_no for p in c.parents if p in graph.claims], "fragment": query.strip(),
                    "context": ctx, "flagged_review": c.flagged_review} for c, ctx in frags]
        log.calls.append({"name": "search_style_corpus", "mode": mode, "query": query, "results": results, "total": total, "in_corpus": in_corpus})
        return {"total": total, "in_corpus": in_corpus, "results": results,
                "note": "예시 없음" if not results else "정확한 조각과 최소 문맥만 사용하고 전체 청구항을 모방하지 않는다. 같은 문제를 푸는 조각이 있으면 코퍼스 수록 항을 쓴다."}

    def open_claim_chain(claim_id: str) -> dict:
        """search_style_corpus가 돌려준 claim_id의 청구항과 그 부모항 체인(인용관계)을 청구항 번호순으로 반환한다.

        종속항 조각의 `상기 …` 선행기재가 어느 항에서 오는지 확인할 때만 쓴다. 전체 청구항·구성 계층을 모방하지 않는다.
        """
        try:
            chain = graph.chain(claim_id)
        except KeyError:
            log.calls.append({"name": "open_claim_chain", "claim_id": claim_id, "labels": [], "refused": True})
            return {"error": "그래프에 없는 claim_id다. search_style_corpus 결과의 claim_id만 쓴다.", "claims": []}
        log.calls.append({"name": "open_claim_chain", "claim_id": claim_id, "labels": [c.label() for c in chain]})
        return {"claims": [{"claim_id": c.claim_id, "label": c.label(), "text": c.text, "flagged_review": c.flagged_review} for c in chain],
                "note": "선행기재 확인용 최소 문맥이다. 기술 구성·수치·재질·효과를 현재 발명에 옮기지 않는다."}

    return [open_routing_index, search_style_corpus, open_claim_chain]
