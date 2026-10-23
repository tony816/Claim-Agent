"""Layer 2 — invention graph built from the current raw material, and the idea sheet derived from it.

One model call extracts what the material states explicitly. Code then keeps a node or edge only if every cited
quote is found verbatim in a text material (whitespace-insensitive); a drawing citation is kept as
DRAWING_OBSERVED; anything else goes to `rejected` and never into the graph. The model is told not to infer.

The graph algorithms then propose — never decide — the things the architects decide:
  - skeleton groups      : communities of components joined by stated relations (주골격 후보 묶음)
  - cooperation bridges  : stated relations that join two groups (핵심 협동관계 후보)
  - causal chains        : feature → mechanism → effect → problem, closed or with the missing link named
                           (08 과제–특징–원리–효과 후보 힌트; an open chain is a question for the inventor)
  - alternative paths    : one effect reached by two features (대안 가지 · 회피설계 위험 힌트)
  - isolated components  : named but in no relation (단순 도면 묘사 후보)
Every proposal is labelled `INFERRED — 근거 미확인 (설계 계약 입력 아님)`; the spans it cites are the evidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..models.ids import sha256_text
from ..models.request import MaterialBundle
from .store import DRAWING_OBSERVED, EXTRACTED, INFERRED, Edge, Graph, Node, Span, find_span

NODE_TYPES = ("COMPONENT", "STEP", "PARAMETER", "MATERIAL", "CONDITION", "FEATURE", "PROBLEM", "MECHANISM", "EFFECT")
EDGE_TYPES = {
    # structure
    "PART_OF": ("COMPONENT", "COMPONENT"),
    "RELATION": ("COMPONENT", "COMPONENT"),        # attrs.predicate (원문 술어), attrs.relation_kind
    "HAS_PARAMETER": ("*", "PARAMETER"),
    "MADE_OF": ("COMPONENT", "MATERIAL"),
    "PRECEDES": ("STEP", "STEP"),
    "PERFORMED_BY": ("STEP", "COMPONENT"),
    "UNDER_CONDITION": ("*", "CONDITION"),
    # causal
    "INVOLVES": ("FEATURE", "*"),
    "ADDRESSES": ("FEATURE", "PROBLEM"),
    "ACTS_BY": ("FEATURE", "MECHANISM"),
    "YIELDS": ("*", "EFFECT"),                     # from a MECHANISM, or from a FEATURE when no principle is stated
    "RESOLVES": ("EFFECT", "PROBLEM"),
    "HAS_REASON": ("PARAMETER", "*"),              # why the value/range: an EFFECT, MECHANISM or PROBLEM node
}
RELATION_KINDS = ("POSITION", "COUPLING", "CONTACT", "ALIGNMENT", "MOTION", "OTHER")
FEATURE_KINDS = ("POSITION_COUPLING", "SUBASSEMBLY_ACTION", "CONDITIONAL", "MODE", "ORDER", "PURPOSE_POSITION_MATERIAL",
                 "NUMERIC", "MATERIAL_SELECTION", "SHAPE_SELECTION", "METHOD_SELECTION", "INPUT_DATA_SELECTION", "OTHER")
IDEA_LABEL = "INFERRED — 근거 미확인 (설계 계약 입력 아님)"

EXTRACTION_SYSTEM = """당신은 특허 청구항 작업의 원자료 구조화 도구다. 판단·설계·청구항 작성은 하지 않는다.

제공된 현재 발명 원자료에 **명시적으로 적힌 것만** 노드와 관계로 옮긴다.
- 추론·일반 상식·유사 발명 지식으로 관계나 효과를 보태지 않는다. 원자료에 '때문에/따라서/위해/이에 따라'처럼 이어져 있지 않으면 인과(ADDRESSES·ACTS_BY·YIELDS·RESOLVES·HAS_REASON)를 만들지 않는다.
- 모든 노드와 관계에 `evidence`를 하나 이상 단다. 텍스트 원자료는 `quote`에 원문을 **글자 그대로** 15~120자 옮긴다(요약·말바꿈 금지, 코드가 원문에서 글자 일치로 찾는다). 도면에서만 보이는 것은 `material`에 도면 이름, `figure`에 관찰 위치를 적고 `quote`는 비운다.
- 노드 `label`은 원자료의 명칭 그대로 쓴다. 원자료에 명칭이 없으면 그 노드를 만들지 않는다.
- FEATURE는 원자료가 하나의 기술적 특징으로 설명하는 구성·관계·순서·조건·수치를 한 문장으로 묶은 것이다. `feature_kind`를 고르고, INVOLVES로 관련 구성·단계·수치·재질·조건에 잇는다.
- RELATION에는 `predicate`(원문 술어 그대로)와 `relation_kind`를 단다.
- 작용 문장이 "A하면/A하므로 B되어 C한다"처럼 원리와 결과를 함께 말하면, 원리 부분(B)을 MECHANISM, 결과(C)를 EFFECT로 나누고 FEATURE —ACTS_BY→ MECHANISM —YIELDS→ EFFECT로 잇는다. 원리가 따로 적혀 있지 않을 때만 FEATURE —YIELDS→ EFFECT로 바로 잇는다.
- PROBLEM은 원자료가 해결 대상인 불편·문제로 서술한 것만 만든다. 효과 문장을 뒤집어 문제를 지어내지 않는다.
- 원자료에 문제·원리·효과가 없으면 그 노드를 만들지 않는다. 빈 결과도 정상이다.

JSON 객체 하나만 반환한다."""

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nodes": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "type": {"type": "string", "enum": list(NODE_TYPES)}, "label": {"type": "string"},
            "feature_kind": {"type": "string", "enum": list(FEATURE_KINDS)},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {
                "material": {"type": "string"}, "quote": {"type": "string"}, "figure": {"type": "string"}}, "required": ["material"]}},
        }, "required": ["id", "type", "label", "evidence"]}},
        "edges": {"type": "array", "items": {"type": "object", "properties": {
            "src": {"type": "string"}, "dst": {"type": "string"}, "type": {"type": "string", "enum": list(EDGE_TYPES)},
            "predicate": {"type": "string"}, "relation_kind": {"type": "string", "enum": list(RELATION_KINDS)},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {
                "material": {"type": "string"}, "quote": {"type": "string"}, "figure": {"type": "string"}}, "required": ["material"]}},
        }, "required": ["src", "dst", "type", "evidence"]}},
    },
    "required": ["nodes", "edges"],
}

CATEGORIES = ("invention", "drawing", "spec")


def extraction_packet(bundle: MaterialBundle) -> str:
    parts = ["### 현재 발명 원자료 (전문)\n\n"]
    for it in bundle.by_category(*CATEGORIES):
        if it.kind == "text":
            parts.append(f"<<<MATERIAL {it.name}>>>\n{(it.text or '').rstrip()}\n<<<END MATERIAL>>>\n\n")
        else:
            parts.append(f"<<<MATERIAL {it.name}>>> (이미지 파트로 첨부)\n\n")
    parts.append("### 출력\n\n스키마에 맞는 JSON 하나. 노드 id는 `C1`, `F1`, `P1`처럼 짧게 붙인다.\n")
    return "".join(parts)


@dataclass
class BuildResult:
    graph: Graph
    rejected: list[dict[str, Any]] = field(default_factory=list)


def _resolve(evidence: list[dict[str, Any]], bundle: MaterialBundle) -> tuple[list[Span], str | None]:
    """Spans for every citation, the provenance status, or None when a text quote is not found."""
    spans: list[Span] = []
    status = EXTRACTED
    items = {it.name: it for it in bundle.by_category(*CATEGORIES)}
    for ev in evidence or []:
        it = items.get(ev.get("material", ""))
        quote = (ev.get("quote") or "").strip()
        if it is None:
            # The model named no known material: try every text material for the quote.
            hits = [find_span(i.text or "", quote, i.name, i.sha256) for i in items.values() if i.kind == "text" and quote]
            hit = next((h for h in hits if h), None)
            if not hit:
                return [], None
            spans.append(hit)
        elif it.kind == "image":
            spans.append(Span(it.name, it.content_sha256, 0, 0, ev.get("figure") or ""))
            status = DRAWING_OBSERVED
        else:
            hit = find_span(it.text or "", quote, it.name, it.sha256)
            if not hit:
                return [], None
            spans.append(hit)
    if not spans:
        return [], None
    if any(s.line_start for s in spans):
        status = EXTRACTED
    return spans, status


def build(raw: dict[str, Any], bundle: MaterialBundle) -> BuildResult:
    g = Graph("invention", {"materials_digest": bundle.digest()})
    rejected: list[dict[str, Any]] = []
    for n in raw.get("nodes", []):
        if n.get("type") not in NODE_TYPES:
            rejected.append({"item": n, "reason": "unknown node type"})
            continue
        spans, status = _resolve(n.get("evidence", []), bundle)
        if status is None:
            rejected.append({"item": n, "reason": "quote not found in material"})
            continue
        attrs = {"feature_kind": n["feature_kind"]} if n.get("feature_kind") else {}
        g.add_node(Node(n["id"], n["type"], n["label"].strip(), status, attrs, spans))
    for e in raw.get("edges", []):
        if e.get("type") not in EDGE_TYPES:
            rejected.append({"item": e, "reason": "unknown edge type"})
            continue
        if e.get("src") not in g.nodes or e.get("dst") not in g.nodes:
            rejected.append({"item": e, "reason": "endpoint missing or rejected"})
            continue
        want_src, want_dst = EDGE_TYPES[e["type"]]
        if (want_src != "*" and g.nodes[e["src"]].type != want_src) or (want_dst != "*" and g.nodes[e["dst"]].type != want_dst):
            rejected.append({"item": e, "reason": "endpoint types do not fit edge type"})
            continue
        spans, status = _resolve(e.get("evidence", []), bundle)
        if status is None:
            rejected.append({"item": e, "reason": "quote not found in material"})
            continue
        attrs = {k: e[k] for k in ("predicate", "relation_kind") if e.get(k)}
        g.add_edge(Edge(e["src"], e["dst"], e["type"], status, attrs, spans))
    g.meta["rejected"] = len(rejected)
    return BuildResult(g, rejected)


# --------------------------------------------------------------------------- analysis

STRUCT_EDGES = {"PART_OF", "RELATION"}


def _ev(spans: list[Span]) -> str:
    return "; ".join(dict.fromkeys(s.label() for s in spans)) or "근거 없음"


def _name(g: Graph, nid: str) -> str:
    n = g.nodes[nid]
    return f"{n.label}({nid})"


@dataclass
class Chain:
    feature: str
    problem: str | None
    mechanism: str | None
    effect: str | None
    missing: list[str]
    feature_kind: str
    numeric_reason: bool | None    # None when the feature has no PARAMETER

    @property
    def closed(self) -> bool:
        return not self.missing


def causal_chains(g: Graph) -> list[Chain]:
    out = []
    for f in sorted(g.of_type("FEATURE"), key=lambda n: n.id):
        problems = [e.dst for e in g.out_edges(f.id, "ADDRESSES")]
        mechs = [e.dst for e in g.out_edges(f.id, "ACTS_BY")]
        effects = [e.dst for m in mechs for e in g.out_edges(m, "YIELDS")] + [e.dst for e in g.out_edges(f.id, "YIELDS")]
        resolved = {e.dst for ef in effects for e in g.out_edges(ef, "RESOLVES")}
        problem = next((p for p in problems if p in resolved), problems[0] if problems else (sorted(resolved)[0] if resolved else None))
        missing = []
        if not problem:
            missing.append("과제(ADDRESSES/RESOLVES)")
        if not mechs:
            missing.append("작동·협동 원리(ACTS_BY)")
        if not effects:
            missing.append("효과(YIELDS)")
        params = [e.dst for e in g.out_edges(f.id, "INVOLVES") if g.nodes[e.dst].type == "PARAMETER"]
        numeric_reason = None if not params else all(g.out_edges(p, "HAS_REASON") for p in params)
        out.append(Chain(f.id, problem, mechs[0] if mechs else None, effects[0] if effects else None, missing,
                         f.attrs.get("feature_kind", "OTHER"), numeric_reason))
    return out


# Design doc §14.2 exam_pattern, read off the graph structure (a hint; the dependent architect classifies).
_PATTERN_BY_KIND = {
    "POSITION_COUPLING": "REL_POSITION_COUPLING", "SUBASSEMBLY_ACTION": "SUBASSEMBLY_ACTION", "CONDITIONAL": "CONDITIONAL_BRANCH",
    "MODE": "MODE_OPERATION", "ORDER": "OPERATION_ORDER", "PURPOSE_POSITION_MATERIAL": "PURPOSE_POSITION_MATERIAL",
    "MATERIAL_SELECTION": "SELECTION_MATERIAL", "SHAPE_SELECTION": "SELECTION_SHAPE", "METHOD_SELECTION": "SELECTION_METHOD",
    "INPUT_DATA_SELECTION": "SELECTION_INPUT_DATA",
}


def exam_pattern_hint(c: Chain) -> str:
    if c.feature_kind == "NUMERIC" or c.numeric_reason is not None:
        return "NUMERIC_WITH_REASON" if c.numeric_reason else "NUMERIC_NO_REASON"
    return _PATTERN_BY_KIND.get(c.feature_kind, "OTHER")


@dataclass
class Ideas:
    groups: list[list[str]]
    bridges: list[Edge]
    chains: list[Chain]
    alternatives: list[tuple[str, list[str]]]   # effect -> features
    isolated: list[str]


def analyze(g: Graph) -> Ideas:
    comps = {"COMPONENT"}
    groups = [grp for grp in g.communities(comps, STRUCT_EDGES) if len(grp) > 1]
    where = {nid: i for i, grp in enumerate(groups) for nid in grp}
    bridges = [e for e in g.edges.values() if e.type == "RELATION" and where.get(e.src) is not None and where.get(e.dst) is not None
               and where[e.src] != where[e.dst]]
    chains = causal_chains(g)
    by_effect: dict[str, list[str]] = {}
    for c in chains:
        if c.effect:
            by_effect.setdefault(c.effect, []).append(c.feature)
    alternatives = sorted((ef, fs) for ef, fs in by_effect.items() if len(set(fs)) > 1)
    isolated = sorted(n.id for n in g.of_type("COMPONENT")
                      if not g.neighbors(n.id, STRUCT_EDGES) and not g.in_edges(n.id, "INVOLVES"))
    return Ideas(groups, bridges, chains, alternatives, isolated)


def idea_sheet(g: Graph, ideas: Ideas | None = None, *, include_exam_patterns: bool = False) -> str:
    """Markdown for the architects. `include_exam_patterns` only for the dependent side (design doc §13 #10)."""
    ideas = ideas or analyze(g)
    L = [f"**{IDEA_LABEL}.** 아래는 원자료를 구조화한 그래프에서 알고리즘이 뽑은 후보다. 각 줄의 원자료 위치만 근거이고, "
         "묶음·연결·순서 판단은 근거가 아니다. 채택 여부와 기술적 의미는 원자료를 다시 읽고 정한다. 후보를 채우려고 원자료에 없는 관계를 만들지 않는다.\n"]
    st = g.stats()
    L.append(f"그래프: 노드 {st['nodes']} · 관계 {st['edges']} · 원문 불일치로 버린 항목 {g.meta.get('rejected', 0)} · graph_digest {g.digest()[:12]}\n")
    L.append("#### 주골격 후보 묶음 (서술된 구성 관계의 커뮤니티)\n")
    if not ideas.groups:
        L.append("- 없음 — 구성 사이 관계가 둘 이상 서술되지 않았다.\n")
    for i, grp in enumerate(ideas.groups, 1):
        hub = max(grp, key=lambda n: (g.degree(n, STRUCT_EDGES), n))
        L.append(f"- G{i}: {', '.join(_name(g, n) for n in grp)} — 중심 {_name(g, hub)}\n")
    L.append("\n#### 핵심 협동관계 후보 (묶음 사이를 잇는 서술된 관계)\n")
    if not ideas.bridges:
        L.append("- 없음\n")
    for e in ideas.bridges:
        L.append(f"- {_name(g, e.src)} —[{e.attrs.get('predicate', e.type)}]→ {_name(g, e.dst)} · {_ev(e.spans)}\n")
    L.append("\n#### 과제–특징–원리–효과 사슬\n")
    if not ideas.chains:
        L.append("- 없음 — 원자료에 기술적 특징(FEATURE)으로 묶인 서술이 없다.\n")
    for c in ideas.chains:
        f = g.nodes[c.feature]
        head = f"- {'닫힘' if c.closed else '열림'} {_name(g, c.feature)}"
        seq = " → ".join(_name(g, x) for x in (c.mechanism, c.effect, c.problem) if x)
        line = head + (f" → {seq}" if seq else "")
        if c.missing:
            line += f" · 빠진 고리: {', '.join(c.missing)} (발명자 확인 질문 후보)"
        if include_exam_patterns:
            line += f" · exam_pattern 힌트: {exam_pattern_hint(c)}"
        L.append(line + f" · {_ev(f.spans)}\n")
    L.append("\n#### 같은 효과에 이르는 다른 특징 (대안 가지 · 회피설계 위험 힌트)\n")
    if not ideas.alternatives:
        L.append("- 없음\n")
    for ef, fs in ideas.alternatives:
        L.append(f"- {_name(g, ef)} ← {', '.join(_name(g, x) for x in sorted(set(fs)))}\n")
    L.append("\n#### 관계 없이 이름만 나온 구성 (단순 도면 묘사 후보)\n")
    L.append(("- " + ", ".join(_name(g, n) for n in ideas.isolated) + "\n") if ideas.isolated else "- 없음\n")
    return "".join(L)


def sheet_section(title: str, body: str) -> str:
    return f"### {title}\n\n{body.rstrip()}\n\n"


def insert_before_contract(packet_text: str, section: str) -> str:
    """Place a section before the packet's output contract (or at the end when there is none)."""
    marker = "### 출력 계약"
    at = packet_text.rfind(marker)
    return packet_text[:at] + section + packet_text[at:] if at >= 0 else packet_text + section


def cache_key(bundle: MaterialBundle, model: str) -> str:
    return sha256_text(json.dumps({"materials": bundle.digest(), "model": model, "schema": EXTRACTION_SCHEMA, "system": EXTRACTION_SYSTEM},
                                  ensure_ascii=False, sort_keys=True))[:16]


__all__ = ["BuildResult", "Chain", "Ideas", "analyze", "build", "cache_key", "causal_chains", "exam_pattern_hint", "extraction_packet",
           "idea_sheet", "insert_before_contract", "sheet_section", "EXTRACTION_SCHEMA", "EXTRACTION_SYSTEM", "INFERRED", "IDEA_LABEL"]
