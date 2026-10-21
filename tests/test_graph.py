from __future__ import annotations

import json
from pathlib import Path

import pytest

from claim_agent.config import AppConfig
from claim_agent.graph import invention as inv
from claim_agent.graph.pipeline import IDEA_TITLE, PRECEDENT_TITLE, GraphHooks
from claim_agent.graph.precedent import PrecedentGraph, classify, precedent_sheet
from claim_agent.graph.rules import PRIMARY, RuleGraph, pack_provider, split_sections
from claim_agent.graph.store import DRAWING_OBSERVED, EXTRACTED, Graph, find_span
from claim_agent.models.enums import RequestMode, Scope
from claim_agent.models.ids import sha256_text
from claim_agent.models.request import MaterialBundle, MaterialItem
from claim_agent.models.state import CandidateState, RunState
from claim_agent.provider.base import CallResult
from claim_agent.roles.prompt import PromptAssembler
from claim_agent.roles.registry import RoleRegistry
from claim_agent.sources.registry import SourceSet

ROOT = Path(__file__).resolve().parents[1]
MATERIAL = (ROOT / "eval" / "cases" / "sample-clip-holder" / "sources" / "invention.md").read_text(encoding="utf-8")


def _bundle() -> MaterialBundle:
    text = MaterialItem("invention.md", "invention.md", "text", sha256_text(MATERIAL), text=MATERIAL)
    img = MaterialItem("fig2.png", "fig2.png", "image", "imgsha", mime_type="image/png", data=b"x", category="drawing")
    return MaterialBundle([text, img])


def _ev(q: str) -> list[dict]:
    return [{"material": "invention.md", "quote": q}]


RAW = {
    "nodes": [
        {"id": "C1", "type": "COMPONENT", "label": "베이스", "evidence": _ev("베이스: 책상 가장자리에 걸리는 ㄷ자 형태의 클램프부를 갖는다.")},
        {"id": "C2", "type": "COMPONENT", "label": "조임 나사", "evidence": _ev("조임 나사: 하판을 관통하여 상판 쪽으로 진행하며")},
        {"id": "C3", "type": "COMPONENT", "label": "홀더 본체", "evidence": _ev("홀더 본체: 상판의 상면에 결합되고")},
        {"id": "C4", "type": "COMPONENT", "label": "슬롯", "evidence": _ev("각 슬롯은 위쪽으로 개방된 U자 형상의 홈이며")},
        {"id": "C5", "type": "COMPONENT", "label": "탄성 리브", "evidence": _ev("탄성 리브: 홀더 본체의 각 슬롯 입구 양측에 형성되고")},
        {"id": "C6", "type": "COMPONENT", "label": "연결판", "evidence": _ev("상판과 하판을 잇는 연결판으로 이루어진다")},
        {"id": "F1", "type": "FEATURE", "label": "입구 폭이 바닥 폭보다 좁은 슬롯과 탄성 리브", "feature_kind": "POSITION_COUPLING",
         "evidence": _ev("입구 폭이 바닥 폭보다 좁아 케이블이 위에서 눌러 넣어지면")},
        {"id": "F2", "type": "FEATURE", "label": "폭 방향으로 나란한 슬롯", "feature_kind": "POSITION_COUPLING",
         "evidence": _ev("상면에 복수의 슬롯이 폭 방향으로 나란히 형성된다")},
        {"id": "M1", "type": "MECHANISM", "label": "리브가 벌어졌다가 복원", "evidence": _ev("리브가 벌어졌다가 복원되어")},
        {"id": "E1", "type": "EFFECT", "label": "케이블이 빠지지 않음", "evidence": _ev("케이블이 빠지지 않는다")},
        {"id": "P1", "type": "PROBLEM", "label": "케이블 이탈", "evidence": _ev("케이블이 빠지지 않는다")},
        {"id": "X1", "type": "COMPONENT", "label": "자석", "evidence": _ev("자석이 홀더를 붙든다")},  # not in the material
        {"id": "D1", "type": "COMPONENT", "label": "입구 폭 w1", "evidence": [{"material": "fig2.png", "figure": "도 2 단면"}]},
    ],
    "edges": [
        {"src": "C2", "dst": "C1", "type": "RELATION", "predicate": "하판을 관통", "relation_kind": "COUPLING", "evidence": _ev("하판을 관통하여 상판 쪽으로 진행하며")},
        {"src": "C6", "dst": "C1", "type": "PART_OF", "evidence": _ev("상판과 하판을 잇는 연결판으로 이루어진다")},
        {"src": "C3", "dst": "C1", "type": "RELATION", "predicate": "상판의 상면에 결합", "relation_kind": "COUPLING", "evidence": _ev("상판의 상면에 결합되고")},
        {"src": "C4", "dst": "C3", "type": "PART_OF", "evidence": _ev("상면에 복수의 슬롯이 폭 방향으로 나란히 형성된다")},
        {"src": "C5", "dst": "C4", "type": "RELATION", "predicate": "슬롯 내측으로 돌출", "relation_kind": "POSITION", "evidence": _ev("슬롯 내측으로 돌출한다")},
        {"src": "F1", "dst": "C4", "type": "INVOLVES", "evidence": _ev("입구 폭이 바닥 폭보다 좁아")},
        {"src": "F1", "dst": "M1", "type": "ACTS_BY", "evidence": _ev("리브가 벌어졌다가 복원되어 케이블이 빠지지 않는다")},
        {"src": "M1", "dst": "E1", "type": "YIELDS", "evidence": _ev("리브가 벌어졌다가 복원되어 케이블이 빠지지 않는다")},
        {"src": "E1", "dst": "P1", "type": "RESOLVES", "evidence": _ev("케이블이 빠지지 않는다")},
        {"src": "F2", "dst": "C4", "type": "INVOLVES", "evidence": _ev("슬롯이 폭 방향으로 나란히 있으므로")},
        {"src": "F2", "dst": "P1", "type": "ADDRESSES", "evidence": _ev("케이블이 서로 엉키지 않는다")},
        {"src": "C1", "dst": "X1", "type": "RELATION", "evidence": _ev("하판")},           # endpoint rejected
        {"src": "C3", "dst": "C5", "type": "YIELDS", "evidence": _ev("홀더 본체")},         # wrong endpoint types
        {"src": "C3", "dst": "C4", "type": "RELATION", "evidence": _ev("슬롯이 홀더 본체를 감싼다")},  # quote not in material
    ],
}


def test_find_span_ignores_line_breaks():
    text = "첫 줄\n입구 폭이 바닥\n폭보다 좁아 케이블이"
    span = find_span(text, "입구 폭이 바닥 폭보다 좁아", "m", "s")
    assert span is not None and (span.line_start, span.line_end) == (2, 3)
    assert find_span(text, "없는 문장", "m", "s") is None


def test_build_keeps_only_verbatim_evidence():
    built = inv.build(RAW, _bundle())
    g = built.graph
    assert "X1" not in g.nodes                       # quote not in the material
    assert g.nodes["D1"].status == DRAWING_OBSERVED  # drawing citation kept, marked
    assert g.nodes["C1"].status == EXTRACTED and g.nodes["C1"].spans[0].line_start > 0
    assert ("C3", "C4", "RELATION") not in g.edges
    reasons = sorted(r["reason"] for r in built.rejected)
    assert reasons == ["endpoint missing or rejected", "endpoint types do not fit edge type", "quote not found in material", "quote not found in material"]


def test_idea_sheet_chains_groups_and_labels():
    g = inv.build(RAW, _bundle()).graph
    ideas = inv.analyze(g)
    chains = {c.feature: c for c in ideas.chains}
    assert chains["F1"].closed and chains["F1"].problem == "P1"
    assert not chains["F2"].closed and "작동·협동 원리(ACTS_BY)" in chains["F2"].missing
    assert any("C1" in grp and "C3" in grp for grp in ideas.groups)
    assert "D1" in ideas.isolated
    sheet = inv.idea_sheet(g, ideas)
    assert inv.IDEA_LABEL in sheet and "exam_pattern" not in sheet
    assert "exam_pattern 힌트: REL_POSITION_COUPLING" in inv.idea_sheet(g, ideas, include_exam_patterns=True)


def test_exam_pattern_hint_numeric_reason():
    c = inv.Chain("F", None, None, None, [], "NUMERIC", False)
    assert inv.exam_pattern_hint(c) == "NUMERIC_NO_REASON"
    c.numeric_reason = True
    assert inv.exam_pattern_hint(c) == "NUMERIC_WITH_REASON"


def test_graph_roundtrip(tmp_path):
    g = inv.build(RAW, _bundle()).graph
    g.save(tmp_path / "g.json")
    h = Graph.load(tmp_path / "g.json")
    assert h.digest() == g.digest()


def test_insert_before_contract():
    t = "앞\n### 출력 계약\n\n뒤"
    assert inv.insert_before_contract(t, "### X\n\n") == "앞\n### X\n\n### 출력 계약\n\n뒤"
    assert inv.insert_before_contract("끝", "### X\n") == "끝### X\n"


# --------------------------------------------------------------------------- layer 1

@pytest.fixture(scope="module")
def rule_graph() -> RuleGraph:
    return RuleGraph(SourceSet.load(ROOT / "sources", ROOT), RoleRegistry.load(ROOT / ".claude" / "agents"))


def test_split_sections_tracks_parents_and_skips_code():
    secs = split_sections("K", "머리\n# A\n본문\n```\n# 코드\n```\n## B\nOA_DRAFT_GATE 조건 4·9\n# C\n")
    assert [s.heading for s in secs] == ["(머리말)", "A", "B", "C"]
    b = secs[2]
    assert b.parent == secs[1].id and {"OA_DRAFT_GATE", "조건 4", "조건 9"} <= b.terms


def test_pack_keeps_primary_files_whole(rule_graph):
    for role, keys in PRIMARY.items():
        for (r, scope) in [(r, s) for (r, s) in __import__("claim_agent.roles.whitelist", fromlist=["x"]).SOURCE_WHITELIST if r == role]:
            pack = rule_graph.pack(r, scope)
            for k in keys & set(pack.file_keys):
                assert pack.file_text(k) == rule_graph.sources.get(k).text, (r, scope, k)


def test_pack_covers_role_terms(rule_graph):
    for role, scope in [("syntax-scope-reviewer", Scope.DEPENDENT_SET), ("dependent-claim-strategy-architect", Scope.DEPENDENT_SET)]:
        pack = rule_graph.pack(role, scope)
        assert pack.coverage()["missing_terms"] == []
        assert pack.text_chars() <= pack.full_chars()


def test_pack_provider_exempts_and_assembler_marks_pack(rule_graph):
    get = pack_provider(rule_graph.sources, rule_graph.roles, ["claim-success-reviewer"])
    assert get("claim-success-reviewer", Scope.DEPENDENT_SET) is None
    assert get("claim-architect", Scope.INDEPENDENT) is None          # nothing to save → whole files
    pack = get("syntax-scope-reviewer", Scope.DEPENDENT_SET)
    assert pack is not None and pack.text_chars() < pack.full_chars()
    asm = PromptAssembler(rule_graph.sources, {}, get)
    block, keys, _ = asm.sources_block(rule_graph.roles.get("syntax-scope-reviewer"), Scope.DEPENDENT_SET)
    assert pack.pack_id in block and "SOURCE_SECTION_MISSING" in block and "<!-- §" in block
    plain, _, _ = PromptAssembler(rule_graph.sources, {}).sources_block(rule_graph.roles.get("syntax-scope-reviewer"), Scope.DEPENDENT_SET)
    assert len(block) < len(plain)


def test_impact_edges(rule_graph):
    srcs = {e.src for e in rule_graph.graph.in_edges("OA_DRAFT_GATE")}
    assert "ROLE:oa-strategy-reviewer" in srcs and any(s.startswith("S06#") for s in srcs)


# --------------------------------------------------------------------------- layer 3

PRECEDENT = {
    "built_at": "t",
    "cases": {"1": {"title": "클립"}, "2": {"title": "마스크"}},
    "examples": [
        {"id": "amend:1:a", "kind": "보정", "application_number": "1", "title": "클립", "office_action": {"date": "d", "name": "의견제출통지서", "statutes": ["제29조제2항"]},
         "changes": [{"claim": 1, "added": ["상기 리브는 상기 슬롯 내측으로 돌출되어 결합되는"], "from_claims": [3], "matched_paragraphs": [12]}],
         "matched": [{"no": 12, "text": "리브는 케이블을 잡기 위하여 돌출된다"}]},
        {"id": "amend:2:a", "kind": "보정", "application_number": "2", "title": "마스크", "office_action": {"date": "d", "statutes": ["제29조제2항"]},
         "changes": [{"claim": 1, "added": ["25nm 를 초과하고 50nm 미만인"], "from_claims": [], "matched_paragraphs": []}]},
        {"id": "effect:1", "kind": "효과", "application_number": "1", "reply": ["회전 방지 메커니즘"], "office_action": {"statutes": ["제29조제2항"]}},
        {"id": "oa:1:1", "kind": "지적", "application_number": "1", "rule": "E2", "statute": "특허법 제42조제4항제2호", "terms": ["질감"]},
    ],
}


def test_classify_rules():
    assert classify("상기 리브는 상기 슬롯 내측으로 돌출되어 결합되는") == ["REL_POSITION_COUPLING"]
    assert classify("25nm 를 초과하고 50nm 미만인") == ["NUMERIC"]
    assert "REL_POSITION_COUPLING" not in classify("제2 영상에 알고리즘을 적용")   # '영상에' is not '…상에'
    assert classify("불소계 고분자수지필름") == ["SELECTION_MATERIAL"]


def test_precedent_graph_and_sheet():
    pg = PrecedentGraph(PRECEDENT, "examples.json")
    st = pg.pattern_stats()
    assert st["REL_POSITION_COUPLING"] == {"all": 1, "claim1_after_inventive_step": 1, "from_original_dependent": 1}
    assert pg.limitations[0].reason_in_paragraph
    g = inv.build(RAW, _bundle()).graph
    sheet = precedent_sheet(pg, g, inv.causal_chains(g))
    assert "생존 편향" in sheet and "[1] 클립" in sheet


# --------------------------------------------------------------------------- engine hooks

class FakeProvider:
    def __init__(self, raw: dict | None = None, fail: bool = False):
        self.raw, self.fail, self.calls = raw, fail, 0

    def generate(self, spec):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return CallResult(text=json.dumps(self.raw, ensure_ascii=False), parsed=self.raw, usage={"prompt_tokens": 10})


def _state() -> RunState:
    return RunState(run_id="r1", request={}, request_mode=RequestMode.AUTHORING_DRAFT, source_set_id="s", candidate=CandidateState(candidate_id="c"))


def _hooks(tmp_path, provider, **graph) -> tuple[GraphHooks, list]:
    cfg = AppConfig(project_root=tmp_path)
    for k, v in graph.items():
        setattr(cfg.graph, k, v)
    accounted: list = []
    return GraphHooks(cfg, provider, lambda rid: tmp_path / rid, lambda *a: accounted.append(a)), accounted


def test_hooks_off_leave_packet_unchanged(tmp_path):
    hooks, _ = _hooks(tmp_path, FakeProvider(RAW))
    assert not hooks.enabled
    assert hooks.architect_packet(_state(), _bundle(), "x### 출력 계약") == "x### 출력 계약"


def test_hooks_architect_gets_ideas_once_and_no_precedent(tmp_path):
    (tmp_path / "ex.json").write_text(json.dumps(PRECEDENT, ensure_ascii=False), encoding="utf-8")
    prov = FakeProvider(RAW)
    hooks, accounted = _hooks(tmp_path, prov, invention="on", precedent="on", precedent_path=str(tmp_path / "ex.json"))
    st = _state()
    a = hooks.architect_packet(st, _bundle(), "본문\n### 출력 계약\n")
    assert IDEA_TITLE in a and PRECEDENT_TITLE not in a and "exam_pattern" not in a
    assert a.index(IDEA_TITLE) < a.index("### 출력 계약")
    d = hooks.dependent_packet(st, _bundle(), "본문\n### 출력 계약\n")
    assert IDEA_TITLE in d and PRECEDENT_TITLE in d and "exam_pattern 힌트" in d
    assert prov.calls == 1 and len(accounted) == 1          # second use reads the saved graph
    assert any(n.startswith("발명 그래프") for n in st.notes)


def test_hooks_failure_is_advisory(tmp_path):
    hooks, _ = _hooks(tmp_path, FakeProvider(fail=True), invention="on")
    st = _state()
    assert hooks.architect_packet(st, _bundle(), "본문") == "본문"
    assert any("추출 호출 실패" in n for n in st.notes)


# --------------------------------------------------------------------------- full pipeline (scripted)

def test_pipeline_puts_sheets_in_architect_packets(tmp_path, request_dep):
    from claim_agent.provider.scripted import ScriptedProvider
    from claim_agent.runtime import build_runtime

    from . import scripted_roles as R

    class WithGraph(ScriptedProvider):
        def generate(self, spec):
            if spec.phase == "graph":
                self.calls.append(spec)
                return CallResult(text=json.dumps(RAW, ensure_ascii=False), parsed=RAW, usage={"prompt_tokens": 10})
            return super().generate(spec)

    (tmp_path / "ex.json").write_text(json.dumps(PRECEDENT, ensure_ascii=False), encoding="utf-8")
    rt = build_runtime(ROOT, None, None, {"paths.runs_dir": str(tmp_path / "runs"), "paths.lessons_dir": str(tmp_path / "lessons"), "cache.enabled": False,
                                          "pipeline.max_concurrency": 1, "graph.invention": "on", "graph.precedent": "on",
                                          "graph.precedent_path": str(tmp_path / "ex.json")})
    prov = WithGraph(R.happy_script(dependent=True))
    engine = rt.engine(prov)
    state = engine.run(engine.start(request_dep, "graph-run"))
    assert state.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state.halt
    assert sum(c.phase == "graph" for c in prov.calls) == 1
    arch = next(c for c in prov.calls if c.role == "claim-architect" and c.phase == "main")
    dep = next(c for c in prov.calls if c.role == "dependent-claim-strategy-architect" and c.phase == "main")
    assert IDEA_TITLE in arch.packet_text and PRECEDENT_TITLE not in arch.packet_text and "exam_pattern" not in arch.packet_text
    assert IDEA_TITLE in dep.packet_text and PRECEDENT_TITLE in dep.packet_text
    others = [c for c in prov.calls if c.role not in ("claim-architect", "dependent-claim-strategy-architect")]
    assert all(IDEA_TITLE not in c.packet_text and PRECEDENT_TITLE not in c.packet_text for c in others)
