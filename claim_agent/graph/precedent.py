"""Layer 3 — precedent graph from registered Korean prosecution histories, linked to the invention graph.

Input is cs2-spec-writer's `graph/prosecution_v2/examples.json` (built read-only from `prosecution_graph_v2.zip`;
config `graph.precedent_path`). Its `보정` examples say which phrases were added to a claim after an office action
and from which original claim/paragraph; `효과` examples quote the effect arguments that answered §29② rejections.

Each added phrase is classified into the design doc §14.2 `exam_pattern` by word rules. The classification is not
reviewed by a person, can be off by a case or two, and the sample is 30 registered cases from one year — survivors
only. So the output is a reference sheet, never a gate, never prior art for the current invention, and by the user's
decision (design doc §13 #10) it goes only to the dependent-claim side, never to independent-claim generation.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.ids import sha256_text
from .invention import Chain, exam_pattern_hint
from .store import INFERRED, Edge, Graph, Node, Span

CAVEAT = ("경험 통계 — 등록된 한국 특허 30건(1년치, 등록 건만 본 생존 편향)의 보정 문구를 낱말 규칙으로 분류한 것이다. "
          "사람 검토 전이며 한두 건 오차가 있을 수 있다. 판정 기준·게이트가 아니고, 현재 발명의 원자료나 선행기술도 아니다.")

# Word rules → exam_pattern. Order matters only for display; a phrase may get several.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("NUMERIC", re.compile(r"\d+(?:\.\d+)?\s*(?:%|nm|㎚|mm|㎜|cm|μm|㎛|m|°|도|℃|초|분|시간|배|g|kg|V|A|W|Hz|rpm)|이상|이하|초과|미만|내지|범위|수식|≤|≥|<|>")),
    ("OPERATION_ORDER", re.compile(r"먼저|이후|후에|다음에|순차|순서|선행|뒤에|한 뒤|한 후")),
    ("CONDITIONAL_BRANCH", re.compile(r"경우|일 때|인 때|조건|기준값|임계|판단하여|이면|만족하면|초과하면|미만이면")),
    ("MODE_OPERATION", re.compile(r"모드")),
    ("REL_POSITION_COUPLING", re.compile(r"\s상에|상부|하부|사이|인접|결합|체결|연결|관통|내측|외측|내부|외부|위치|배치|대향|접촉|삽입|수용|돌출|형성되|장착|고정|지지|둘러|맞닿")),
    ("SUBASSEMBLY_ACTION", re.compile(r"함께|연동|협동|동시에|구동하여|작동하여|회전시키|이동시키")),
    ("PURPOSE", re.compile(r"위해|위하여|하도록|되도록")),
    ("MATERIAL", re.compile(r"재질|소재|수지|금속|합금|세라믹|고분자|필름|섬유|유리|실리콘|카본|탄소")),
]


def classify(phrase: str) -> list[str]:
    hits = [name for name, rx in _RULES if rx.search(phrase)]
    if "MATERIAL" in hits and "PURPOSE" in hits and "REL_POSITION_COUPLING" in hits:
        hits = [h for h in hits if h not in ("MATERIAL", "PURPOSE")] + ["PURPOSE_POSITION_MATERIAL"]
    elif "MATERIAL" in hits and len(hits) == 1:
        hits = ["SELECTION_MATERIAL"]
    hits = [h for h in hits if h not in ("MATERIAL", "PURPOSE")]
    return hits or ["OTHER"]


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


@dataclass
class AddedLimitation:
    id: str
    case: str
    title: str
    claim: int
    phrase: str
    patterns: list[str]
    statutes: list[str]
    from_claims: list[int]
    paragraphs: list[int]
    reason_in_paragraph: bool   # a matched original paragraph explains the range/choice (위해·때문·비교 …)


_REASON = re.compile(r"위해서는|위하여|때문|비교|대비|이에 따라|따라서|그 결과|이유")


class PrecedentGraph:
    def __init__(self, data: dict[str, Any], source_label: str):
        self.data = data
        self.source = source_label
        self.sha = sha256_text(json.dumps(data, ensure_ascii=False, sort_keys=True))
        self.limitations: list[AddedLimitation] = []
        self.effects: list[dict[str, Any]] = []
        self.graph = self._build()

    @classmethod
    def load(cls, path: Path) -> PrecedentGraph:
        if not path.is_file():
            raise FileNotFoundError(f"심사 이력 사례 파일이 없습니다: {path}")
        return cls(json.loads(path.read_text(encoding="utf-8")), str(path))

    def _build(self) -> Graph:
        g = Graph("precedent", {"source": self.source, "sha256": self.sha, "built_at": self.data.get("built_at"), "caveat": CAVEAT})
        for app, meta in (self.data.get("cases") or {}).items():
            g.add_node(Node(f"CASE:{app}", "CASE", meta.get("title", app), attrs={k: meta.get(k) for k in ("registration_number", "registration_date")}))
        for ex in self.data.get("examples", []):
            app = ex.get("application_number", "")
            case = f"CASE:{app}"
            if case not in g.nodes:
                g.add_node(Node(case, "CASE", ex.get("title", app)))
            oa = ex.get("office_action") or {}
            statutes = list(oa.get("statutes") or ([ex["statute"]] if ex.get("statute") else []))
            span = Span(self.source, self.sha, 0, 0, ex["id"])
            if ex["kind"] == "보정":
                oid = f"OA:{app}:{oa.get('date', '')}"
                g.add_node(Node(oid, "OFFICE_ACTION", oa.get("name", "의견제출통지서"), attrs={"statutes": statutes, "date": oa.get("date")}))
                g.add_edge(Edge(case, oid, "RECEIVED"))
                paras = {m.get("no"): m.get("text", "") for m in ex.get("matched") or []}
                for ch in ex.get("changes") or []:
                    for k, phrase in enumerate(ch.get("added") or []):
                        lid = f"ADD:{ex['id']}:{ch.get('claim')}:{k}"
                        mp = list(ch.get("matched_paragraphs") or [])
                        reason = any(_REASON.search(paras.get(p, "") or "") for p in mp)
                        lim = AddedLimitation(lid, app, ex.get("title", ""), int(ch.get("claim") or 0), phrase, classify(phrase), statutes,
                                              list(ch.get("from_claims") or []), mp, reason)
                        self.limitations.append(lim)
                        g.add_node(Node(lid, "ADDED_LIMITATION", phrase[:80], INFERRED,
                                        {"claim": lim.claim, "patterns": lim.patterns, "from_claims": lim.from_claims, "paragraphs": mp}, [span]))
                        g.add_edge(Edge(oid, lid, "ANSWERED_BY"))
                        for p in lim.patterns:
                            pid = f"PATTERN:{p}"
                            g.add_node(Node(pid, "EXAM_PATTERN", p, INFERRED))
                            g.add_edge(Edge(lid, pid, "CLASSIFIED_AS", INFERRED))
            elif ex["kind"] == "효과":
                self.effects.append(ex)
                eid = f"EFFECT:{ex['id']}"
                g.add_node(Node(eid, "EFFECT_ARGUMENT", (ex.get("reply") or [""])[0][:80], attrs={"statutes": statutes}, spans=[span]))
                g.add_edge(Edge(case, eid, "ARGUED"))
            elif ex["kind"] == "지적":
                did = f"DEFECT:{ex['id']}"
                g.add_node(Node(did, "DEFECT", ex.get("rule", ""), attrs={"terms": ex.get("terms"), "statute": ex.get("statute")}, spans=[span]))
                g.add_edge(Edge(case, did, "CITED_FOR"))
        return g

    # ---- queries

    def pattern_stats(self) -> dict[str, dict[str, int]]:
        """Per pattern: added phrases in claim 1 of an answer to a §29② (inventive step) rejection, and in all changes."""
        out: dict[str, dict[str, int]] = {}
        for lim in self.limitations:
            for p in lim.patterns:
                s = out.setdefault(p, Counter())  # type: ignore[assignment]
                s["all"] += 1
                if lim.claim == 1 and any("29조제2항" in st for st in lim.statutes):
                    s["claim1_after_inventive_step"] += 1
                if lim.from_claims:
                    s["from_original_dependent"] += 1
        return {k: dict(v) for k, v in sorted(out.items(), key=lambda kv: -kv[1]["all"])}

    def similar(self, pattern: str, text: str, k: int = 2) -> list[tuple[float, AddedLimitation]]:
        pool = [lim for lim in self.limitations if pattern in lim.patterns or (pattern.startswith("NUMERIC") and "NUMERIC" in lim.patterns)]
        scored = sorted(((similarity(text, lim.phrase), lim) for lim in pool), key=lambda t: (-t[0], t[1].id))
        return scored[:k]


def precedent_sheet(pg: PrecedentGraph, inv: Graph, chains: list[Chain], k: int = 2) -> str:
    """Reference lines per causal chain of the invention graph, for the dependent-claim strategy architect only."""
    stats = pg.pattern_stats()
    L = [f"**{CAVEAT}**\n\n", f"출처: {pg.source} (sha256 {pg.sha[:12]}, 보정 문구 {len(pg.limitations)}개, 효과 주장 {len(pg.effects)}개)\n\n"]
    L.append("| exam_pattern | 보정 문구 수 | 진보성 거절 뒤 청구항 1에 더해진 수 | 최초 종속항에서 온 수 |\n|---|---|---|---|\n")
    for p, s in stats.items():
        L.append(f"| {p} | {s.get('all', 0)} | {s.get('claim1_after_inventive_step', 0)} | {s.get('from_original_dependent', 0)} |\n")
    L.append("\n#### 현재 발명 사슬별 참고 사례 (분류 힌트가 같은 보정 문구, 글자 유사도순)\n")
    if not chains:
        L.append("- 없음 — 발명 그래프에 기술적 특징 사슬이 없다.\n")
    for c in chains:
        hint = exam_pattern_hint(c)
        f = inv.nodes[c.feature]
        L.append(f"- {f.label}({c.feature}) · 힌트 {hint}\n")
        if hint == "NUMERIC_NO_REASON":
            L.append("  - 원자료에 범위 안팎의 이유·비교가 그래프상 연결되지 않았다 — 반복 실험 최적화로 넘어갈 위험(경험 기준 E7 c). 원자료 재확인 대상.\n")
        for score, lim in pg.similar(hint, f.label, k):
            src = f"최초 종속항 {', '.join(map(str, lim.from_claims))}" if lim.from_claims else "최초 종속항 대응 없음"
            near = "" if score >= 0.1 else " (글자 유사도 낮음 — 같은 유형의 다른 분야 예시)"
            L.append(f"  - [{lim.case}] {lim.title[:30]} · 청구항 {lim.claim}에 더함: \"{lim.phrase[:90]}\" · {src} · 유사도 {score:.2f}{near}\n")
    return "".join(L)
