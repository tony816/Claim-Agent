from __future__ import annotations

import json
from pathlib import Path

import pytest

from claim_agent.improve.feedback import build_feedback
from claim_agent.improve.lessons import LessonStore
from claim_agent.models.contracts import contract_for
from claim_agent.models.enums import RequestMode, Scope
from claim_agent.models.envelope import ENVELOPE_JSON_SCHEMA, RoleEnvelope, schema_depth
from claim_agent.models.ids import Identifiers, bump_revision, record_id
from claim_agent.pipeline.blind_guard import BlindContamination, BlindPacket, assert_isolated
from claim_agent.pipeline.claimtext import MultiDependentChain, exact_sha256, flatten, parent_chain, parent_chain_text, parse_claim_set, validate_parent_refs
from claim_agent.pipeline.transitions import decide
from claim_agent.pipeline.verify import cross_check
from claim_agent.provider.base import CallSpec
from claim_agent.roles.whitelist import SOURCE_WHITELIST, sources_for
from claim_agent.sources.corpus import CorpusIndex, is_forbidden_query

from . import scripted_roles as R

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------- claim text
def test_parse_real_claim_set_and_chains():
    text = (ROOT / "eval" / "cases" / "reagent-tube-review-only" / "sources" / "claims.md").read_text(encoding="utf-8")
    claims = parse_claim_set(text)
    assert [c.claim_no for c in claims] == [1, 2, 3, 4, 5]
    assert claims[0].is_independent and claims[3].parent_no == 3 and claims[4].parent_no == 3
    assert validate_parent_refs(claims) == []
    chain = parent_chain(claims, 5)[0]
    assert [c.claim_no for c in chain] == [1, 3]
    assert "【청구항 2】" not in parent_chain_text(chain)


def test_multi_dependent_detection_and_expansion():
    text = "【청구항 1】\nA를 포함하는 장치.\n【청구항 2】\n제1항에 있어서, B를 더 포함하는 장치.\n【청구항 3】\n제1항 또는 제2항에 있어서, C를 더 포함하는 장치."
    claims = parse_claim_set(text)
    assert claims[2].multi_dependent and claims[2].parent_nos == [1, 2]
    with pytest.raises(MultiDependentChain):
        parent_chain(claims, 3)
    chains = parent_chain(claims, 3, expand_multi=True)
    assert [[c.claim_no for c in ch] for ch in chains] == [[1], [1, 2]]


def test_forward_reference_is_reported():
    text = "【청구항 1】\n제2항에 있어서, A.\n【청구항 2】\nB를 포함하는 장치."
    problems = validate_parent_refs(parse_claim_set(text))
    assert problems and "뒤 번호" in problems[0]


def test_flatten_and_hash_sensitivity():
    a = "【청구항 1】\n상기 A;\n상기 B를 포함하는 장치."
    b = a.replace("상기 B", "상기  B")
    assert flatten(a) == "상기 A; 상기 B를 포함하는 장치."
    assert exact_sha256(a) != exact_sha256(b)


def test_ids_and_revision_bumps():
    ids = Identifiers("c1", "r2", "d1", dependent_set_id="c1-dep", dependent_revision="dr3", dependent_design_revision="dd1", target_claim_id="4")
    assert record_id("success", ids) == "sr-c1-r2-01"
    assert record_id("dependent_blind", ids) == "dbs-c1-dep-dr3-4-01"
    assert record_id("dependent_success", ids) == "dsr-c1-dep-dr3-01"
    assert record_id("draft_dependent_set_lock", ids) == "ddsl-c1-dep-dr3-01"
    assert bump_revision("r9") == "r10"


# ----------------------------------------------------------------------------- envelope / schema
def test_envelope_schema_is_flat_and_closed():
    assert schema_depth(ENVELOPE_JSON_SCHEMA) <= 3
    blob = json.dumps(ENVELOPE_JSON_SCHEMA)
    assert "additionalProperties" not in blob and "patternProperties" not in blob
    env = RoleEnvelope.model_validate({"status": "PASS", "gates": {"OA_DRAFT_GATE": "PASS"}, "reason_code": "spec not provided", "report_markdown": "x"})
    assert env.reason_code == "SPEC_NOT_PROVIDED" and env.gate("OA_DRAFT_GATE").value == "PASS"
    bad = RoleEnvelope.model_validate({"status": "PASS", "reason_code": "SOMETHING_NEW", "report_markdown": "x"})
    assert bad.reason_code == "OTHER"


# ----------------------------------------------------------------------------- contracts / transitions
def _spec(role="oa-strategy-reviewer", scope="INDEPENDENT"):
    return CallSpec(role=role, scope=scope, model="m", system_instruction="s", packet_text="candidate_id: c\nrevision: r1\ndesign_revision: d1\n", meta={"record_id": "oa-c-r1-01"})


def test_oa_final_unverified_passes_in_draft_but_not_finalization():
    env = RoleEnvelope.model_validate(R.oa("INDEPENDENT")(_spec()))
    c = contract_for("oa-strategy-reviewer", Scope.INDEPENDENT)
    assert c.passes(env, RequestMode.AUTHORING_DRAFT)
    assert not c.passes(env, RequestMode.FINALIZATION)
    tr = decide(env, c, RequestMode.FINALIZATION, {}, {"DRAFTER": 2})
    assert tr.kind == "HALT"


def test_decide_routes_returns_and_caps_loops():
    env = RoleEnvelope.model_validate(R.style(return_to="RETURN_TO_DRAFTER")(_spec("claim-style-adjuster")))
    c = contract_for("claim-style-adjuster", Scope.INDEPENDENT)
    assert decide(env, c, RequestMode.AUTHORING_DRAFT, {}, {"DRAFTER": 2}).kind == "RETURN"
    assert decide(env, c, RequestMode.AUTHORING_DRAFT, {"DRAFTER": 2}, {"DRAFTER": 2}).halt_kind == "LOOP_LIMIT"


def test_cross_check_detects_report_mismatch_and_foreign_sources():
    env = RoleEnvelope.model_validate(R.style()(_spec("claim-style-adjuster")))
    env.report_markdown = env.report_markdown.replace("CLAIM_STYLE_GATE: PASS", "CLAIM_STYLE_GATE: REVIEW")
    env.sources_read = ["sources/06_OA_심사리스크_체크리스트.md"]
    ids = Identifiers("c", "r1", "d1")
    res = cross_check(env, contract_for("claim-style-adjuster", Scope.INDEPENDENT), ids, "oa-c-r1-01", None, ["sources/07_용어표현_출처게이트.md"])
    assert any("CLAIM_STYLE_GATE differs" in p for p in res.problems)
    assert any("not pre-loaded" in p for p in res.problems)


# ----------------------------------------------------------------------------- whitelist / blind
def test_whitelist_matches_readme_contract():
    assert sources_for("claim-drafter", Scope.INDEPENDENT) == ["SUCCESS"]
    assert "S04" not in sources_for("claim-drafter", Scope.DEPENDENT_SET)
    assert sources_for("blind-claim-reconstruction-reviewer", Scope.DEPENDENT_SINGLE) == []
    assert "CORPUS" not in {k for keys in SOURCE_WHITELIST.values() for k in keys}
    assert "ROUTING" not in {k for keys in SOURCE_WHITELIST.values() for k in keys}


def test_blind_packet_is_closed():
    bp = BlindPacket("DEPENDENT_SINGLE", "c", "r1", "d1", "dbs-x", dependent_set_id="s", dependent_design_revision="dd1", dependent_revision="dr1", target_claim_id="2", parent_chain_text="【청구항 1】 A.", target_claim_text="【청구항 2】 제1항에 있어서, B.")
    txt = bp.render()
    assert "parent_chain_text:" in txt and "DESIGN_GATE" not in txt
    with pytest.raises(BlindContamination):
        BlindPacket("INDEPENDENT", "c", "r1", "d1", "bs-x").render()
    with pytest.raises(BlindContamination):
        assert_isolated("mode: BLIND_SNAPSHOT\nDESIGN_GATE: LOCKED\n", [])


# ----------------------------------------------------------------------------- corpus
def test_corpus_index_and_restricted_search():
    text = (ROOT / "sources" / "청구항_문체학습용_분야별검색최적화본.md").read_text(encoding="utf-8")
    idx = CorpusIndex.parse(text)
    assert len(idx.claims) == 143
    assert idx.get_claim("EX-08", 3).flagged_review and not idx.get_claim("EX-01", 1).flagged_review
    frags = idx.search_exact_term("길이 방향", max_fragments=5)
    assert 1 <= len(frags) <= 2
    assert is_forbidden_query("TF_생활위생") and not is_forbidden_query("판 형상")


# ----------------------------------------------------------------------------- lessons / feedback
def test_lessons_store_and_digest(tmp_path):
    ls = LessonStore(tmp_path / "lessons")
    assert ls.digest() == "none"
    l = ls.propose("치수축 표현은 '길이 방향'처럼 띄어 쓴다", ["claim-style-adjuster"], "07 정규화", ["run-x"])
    assert l.status == "pending" and ls.injection_text("claim-style-adjuster") == ""
    ls.approve(l.id)
    assert "길이 방향" in ls.injection_text("claim-style-adjuster") and ls.injection_text("claim-drafter") == ""
    assert ls.digest() != "none"
    ls.reject(l.id)
    assert ls.digest() == "none"


def test_feedback_report_from_runs(rt, request_indep):
    from claim_agent.provider.scripted import ScriptedProvider

    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(return_to="RETURN_TO_DRAFTER"), R.style()]
    script["claim-drafter"] = [R.drafter(), R.drafter()]
    engine = rt.engine(ScriptedProvider(script))
    engine.run(engine.start(request_indep, "run-fb-01"))
    engine2 = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    engine2.run(engine2.start(request_indep, "run-fb-02"))
    rep = build_feedback(rt.cfg.path("runs_dir"))
    assert rep.runs == 2 and rep.calls == 18  # 10 (one drafter return loop) + 8
    assert rep.return_flows[("claim-style-adjuster", "RETURN_TO_DRAFTER")] == 1
    md = rep.render_md()
    assert "역할별 비용·지연" in md and "DRAFT_CLAIM_LOCK" in md
