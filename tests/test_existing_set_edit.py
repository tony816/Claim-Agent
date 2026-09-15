"""EXISTING_SET_EDIT: rewrite one claim of the user's numbered set in place.

Regression for run web-51c7555474e845629bbd4082383687d0: '정리되지 않은 9항을 청구항 형태로 작성해줘' with claims 1~9 in the
project instructions became a merged independent claim (1+5+8+9), then a dependent design numbered 2~6, and stopped
after 21 calls with no claim text.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claim_agent.conversation_pipeline import intake, resolve_edit_scope, revision_path, run_pipeline
from claim_agent.improve.estimate import expected_calls
from claim_agent.improve.evalharness import EvalCase, evaluate, summarize
from claim_agent.models.enums import RequestMode
from claim_agent.models.request import RunRequest
from claim_agent.pipeline.claimtext import parse_claim_set
from claim_agent.pipeline.engine import Decision
from claim_agent.provider.base import ProviderError
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.routing import RouteDecision
from claim_agent.store.report import edited_set_text
from claim_agent.store.telemetry import read_telemetry

from . import scripted_roles as R

CASES = Path(__file__).resolve().parents[1] / "eval" / "cases"
BASELINE = (CASES / "existing-set-edit-9" / "sources" / "claims.md").read_text(encoding="utf-8")
INCIDENT_TEXT = "정리되지 않은 9항을 청구항 형태로 작성해줘. 오목면의 곡률반경이 케이블이 구속부에 중간끼워맞춤됨을 만드는구나라고 해석될 수 있는 형태가 목적."
EDITED_9 = "【청구항 9】\n제8항에 있어서,\n상기 오목면의 곡률반경은 상기 케이블이 상기 구속부에 중간 끼워맞춤으로 구속되도록 정해지는 케이블 클립 홀더."
MERGED_9 = """【청구항 9】
책상 가장자리에 걸리는 클램프부를 갖는 베이스;
상기 클램프부의 하판을 관통하여 끝단이 책상 하면을 누르는 조임 나사; 및
상기 클램프부의 상판의 상면에 결합되고, 바닥이 오목면으로 형성된 복수의 슬롯과 상기 슬롯의 입구 양측의 구속부를 갖는 홀더 본체를 포함하고,
상기 오목면의 곡률반경은 케이블이 상기 구속부에 중간 끼워맞춤으로 구속되도록 정해지는 케이블 클립 홀더."""


@pytest.fixture
def edit_roles(monkeypatch):
    monkeypatch.setattr(R, "DEP_CLAIMS", [(9, 8, "DC-01", EDITED_9)])
    monkeypatch.setattr(R, "DEP_SET_TEXT", EDITED_9)


def combined_review():
    """One envelope carrying the success, syntax and OA verdicts of an edit's combined review call."""
    def review(spec):
        gates = {"CLAIM_STYLE_GATE": "PASS", "TERM_EXPRESSION_GATE": "PASS", "NON_PATENT_TECHNICAL_READER_GATE": "PASS", "GEOMETRIC_OBJECT_GATE": "PASS",
                 "DEPENDENT_OA_DRAFT_GATE": "PASS", "DEPENDENT_OA_FINAL_GATE": "UNVERIFIED"}
        return R._base(spec, "PASS", gates, ["- syntax 진행 가능: YES", "- DRAFT 진행 가능: 역구성 YES"],
                       gate_reasons=[{"gate": "DEPENDENT_OA_FINAL_GATE", "reason_code": "SPEC_NOT_PROVIDED"}])
    return review


def edit_script(**override):
    script = {
        "dependent-claim-strategy-architect": [R.dep_architect()],
        "claim-drafter": [R.drafter("DEPENDENT_SET")],
        "claim-style-adjuster": [R.style("DEPENDENT_SET")],
        "claim-success-reviewer": [combined_review()],       # success + syntax + OA in one call
        "blind-claim-reconstruction-reviewer": [R.blind()],
        "picture-claim-reconstruction-reviewer": [R.picture()],
    }
    script.update(override)
    return script


def _conversation(tmp_path, request_indep, name, text=INCIDENT_TEXT, previous=None, instructions="미완성된 9항 작성하기.\n\n" + BASELINE):
    folder = tmp_path / name
    folder.mkdir()
    # A follow-up attaches nothing new: the previous run's materials come back through intake(previous).
    files = [] if previous else list(request_indep.invention_sources)
    request = dict(text=text, files=files, history=[], instructions=instructions)
    blocks, paths = intake(request, folder, previous)
    return request, blocks, paths, folder


def _route(blocks, revision_kind="DESIGN", target="9"):
    # What the router returned in the incident: the request block as claim source, and DESIGN.
    return RouteDecision(mode="AUTHORING_DRAFT", reason="9항 작성", dependent=True, dependent_target=target,
                         claim_source_id=blocks[-1]["id"], revision_kind=revision_kind)


def _numbered_design(numbers):
    def design(spec):
        env = R.dep_architect()(spec)
        template = env["candidates"][0]
        env["candidates"] = [dict(template, dc_id=f"DC-{i:02d}", planned_claim_no=n, parent_claim_no=1) for i, n in enumerate(numbers, 1)]
        return env
    return design


def test_the_incident_request_edits_claim_9_in_place_without_the_independent_pipeline(rt, request_indep, tmp_path, edit_roles):
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "incident")
    route = _route(blocks)
    provider = ScriptedProvider(edit_script())
    result = run_pipeline(rt, provider, request, route, blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    assert route.authoring_scope == "EXISTING_SET_EDIT" and route.edit_targets == [9]
    roles = [c.role for c in provider.calls]
    assert "claim-architect" not in roles and "syntax-scope-reviewer" not in roles and "oa-strategy-reviewer" not in roles
    assert len(provider.calls) == 7 == sum(expected_calls(RunRequest.model_validate(state.request)).values())
    review = next(c for c in provider.calls if c.role == "claim-success-reviewer")
    assert all(f"## 역할 파일: {r}" in review.system_instruction for r in ("claim-success-reviewer", "syntax-scope-reviewer", "oa-strategy-reviewer"))
    assert "review_mode: COMBINED_SUCCESS_SYNTAX_OA" in review.packet_text
    records = state.dependent.current.records
    assert records["dependent_success"] == records["dependent_syntax"] == records["dependent_oa"]
    assert state.records[records["dependent_success"]].role == "claim-success-reviewer+syntax-scope-reviewer+oa-strategy-reviewer"
    assert state.candidate.design_revision == "N/A" and state.candidate.current is None and not state.candidate.draft_claim_lock
    assert [c["claim_no"] for c in state.dependent.current.claims] == [9]
    assert state.baseline_set.edit_targets == [9] and state.baseline_set.chain_nos == [1, 5, 8]
    design = next(c for c in provider.calls if c.role == "dependent-claim-strategy-architect")
    assert "dependent_target_claim_nos: 9" in design.packet_text and "정확히 일치" in design.packet_text
    assert "제9항의 부모항 제8항" in design.packet_text and "authoring_scope: EXISTING_SET_EDIT" in design.packet_text
    blind = next(c for c in provider.calls if c.role == "blind-claim-reconstruction-reviewer")
    assert all(f"【청구항 {n}】" in blind.packet_text for n in (1, 5, 8, 9))
    assert not any(f"【청구항 {n}】" in blind.packet_text for n in (2, 6, 7))
    full = edited_set_text(state)
    assert EDITED_9 in full and full.count("【청구항") == 9
    assert all(c.text in full for c in parse_claim_set(BASELINE)[:8])
    assert EDITED_9 in result["answer"] and "독립항 문언 없음" not in result["answer"]
    # The set came from the project instructions: user material, and the same file is the edit target.
    assert sorted(m["category"] for m in state.material_meta if Path(m["path"]).name.startswith("project-instructions-")) == ["claim_file", "invention"]


def test_resolver_needs_a_user_supplied_set_that_holds_the_target(tmp_path, request_indep):
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "resolve")
    route = _route(blocks)
    path, _ = resolve_edit_scope(route, request, blocks, folder)
    assert route.authoring_scope == "EXISTING_SET_EDIT" and "【청구항 9】" in Path(path).read_text(encoding="utf-8")
    fragment = "【청구항 9】\n제8항에 있어서,\n상기 오목면 곡률반경"
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "fragment", instructions=fragment)
    route = _route(blocks)
    assert resolve_edit_scope(route, request, blocks, folder) is None and route.authoring_scope == "NEW_DEPENDENT_SET"
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "model-answer", instructions="")
    blocks.append(dict(id="answer", text=BASELINE, origin="assistant_reference", category="invention", path="", is_request=False))
    route = _route(blocks)
    assert resolve_edit_scope(route, request, blocks, folder) is None      # a model's earlier answer is not the user's set
    assert RouteDecision(mode="REVIEW_ONLY", reason="의견", reviewers=["syntax-scope-reviewer"], authoring_scope="EXISTING_SET_EDIT").authoring_scope == "NEW_INDEPENDENT"


def test_a_design_numbered_2_to_6_gets_one_repair_call_then_stops_before_drafting(rt, request_indep, tmp_path, edit_roles):
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "numbers")
    provider = ScriptedProvider(edit_script(**{"dependent-claim-strategy-architect": [_numbered_design([2, 3, 4, 5, 6])] * 2}))
    result = run_pipeline(rt, provider, request, _route(blocks), blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert [c.role for c in provider.calls] == ["dependent-claim-strategy-architect"] * 2
    first, repair = provider.calls
    assert "번호 계약 위반 복구" not in first.packet_text and "번호 계약 위반 복구" in repair.packet_text
    assert state.halt and "REQUEST_SCOPE_MISMATCH" in state.halt.message and "변경하지 않았습니다" in state.halt.message
    assert not state.dependent.draft_set_lock


def test_a_repaired_design_proceeds_to_the_edit(rt, request_indep, tmp_path, edit_roles):
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "repaired")
    provider = ScriptedProvider(edit_script(**{"dependent-claim-strategy-architect": [_numbered_design([2, 3, 4, 5, 6]), R.dep_architect()]}))
    result = run_pipeline(rt, provider, request, _route(blocks), blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    designs = [r for r in state.records.values() if r.kind == "dependent_design"]
    assert [(r.issued, r.superseded) for r in designs] == [(False, True), (True, False)]
    assert state.dependent.design_record_id == designs[1].record_id


def test_a_draft_that_absorbs_the_parent_chain_into_an_independent_claim_is_stopped(rt, request_indep, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "DEP_CLAIMS", [(9, None, "DC-01", MERGED_9)])
    monkeypatch.setattr(R, "DEP_SET_TEXT", MERGED_9)
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "merge")
    provider = ScriptedProvider(edit_script())
    result = run_pipeline(rt, provider, request, _route(blocks), blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.halt and state.halt.stage == "DEP_DRAFT" and "인용관계" in state.halt.message
    assert [c.role for c in provider.calls] == ["dependent-claim-strategy-architect", "claim-drafter"]


def test_a_reviewer_asking_to_redesign_the_parent_chain_stops_instead_of_merging(rt, request_indep, tmp_path, edit_roles):
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "parent-change")
    provider = ScriptedProvider(edit_script(**{"claim-success-reviewer": [R.success(return_to="RETURN_TO_ARCHITECT")]}))
    result = run_pipeline(rt, provider, request, _route(blocks), blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.halt and state.halt.reason_code == "EDIT_SCOPE_PARENT_CHANGE_REQUIRED" and state.halt.kind == "REVIEW"
    assert "claim-architect" not in [c.role for c in provider.calls] and state.candidate.design_revision == "N/A"


def test_follow_ups_on_the_same_edit_stay_on_the_dependent_path(rt, request_indep, tmp_path, edit_roles):
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "edit-1")
    first = run_pipeline(rt, ScriptedProvider(edit_script()), request, _route(blocks), blocks, paths, folder)
    previous = rt.store.load_state(first["run_id"])

    def path_for(name, text, revision_kind, target="9"):
        req, blk, pth, fld = _conversation(tmp_path, request_indep, name, text=text, previous=previous)
        route = _route(blk, revision_kind, target)
        baseline = resolve_edit_scope(route, req, blk, fld, previous)
        return revision_path(route, req, blk, pth, previous, baseline[1] if baseline else None)

    assert path_for("style", "9항 조사만 다듬어줘", "STYLE_ONLY") == ("style", "DEPENDENT")
    assert path_for("design", "9항을 다시 설계해줘", "DESIGN") == ("redesign", "DEPENDENT")
    assert path_for("other", "8항을 정리해줘", "DESIGN", target="8") == ("new", None)
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "edit-2", text="9항을 다시 써줘", previous=previous)
    provider = ScriptedProvider(edit_script())
    result = run_pipeline(rt, provider, request, _route(blocks), blocks, paths, folder, previous)
    state = rt.store.load_state(previous.run_id)
    assert result["run_id"] == previous.run_id and result["revision_path"] == "redesign/DEPENDENT"
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    assert state.dependent.dependent_design_revision == "dd2" and state.candidate.design_revision == "N/A"
    assert provider.calls[0].role == "dependent-claim-strategy-architect" and "claim-architect" not in [c.role for c in provider.calls]


def test_an_edit_request_after_an_ordinary_run_starts_a_new_run_instead_of_restarting_the_architect(rt, request_indep, tmp_path, edit_roles):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    previous = engine.run(engine.start(request_indep, "ordinary"))
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "after-ordinary", previous=previous)
    provider = ScriptedProvider(edit_script())
    result = run_pipeline(rt, provider, request, _route(blocks), blocks, paths, folder, previous)
    assert result["revision_path"] == "new" and result["run_id"] == folder.name
    assert "claim-architect" not in [c.role for c in provider.calls]
    assert rt.store.load_state("ordinary").candidate.design_revision == "d1"


def test_engine_refuses_an_edit_it_cannot_anchor_before_any_call(rt, request_indep, tmp_path):
    claims = tmp_path / "claims.md"
    claims.write_text(BASELINE, encoding="utf-8")
    edit = request_indep.model_copy(update=dict(dependent=True, dependent_target="10", claim_file=str(claims), authoring_scope="EXISTING_SET_EDIT"))
    finalize = edit.model_copy(update=dict(dependent_target="9", request_mode=RequestMode.FINALIZATION))
    for req, code in ((edit, "EDIT_TARGET_INVALID"), (finalize, "EDIT_SCOPE_DRAFT_ONLY")):
        provider = ScriptedProvider({})
        engine = rt.engine(provider)
        state = engine.run(engine.start(req, f"guard-{code.lower()}"))
        assert provider.calls == [] and state.halt.kind == "BLOCK" and state.halt.reason_code == code


def test_a_text_fix_on_a_run_stopped_before_any_dependent_text_says_what_to_do(rt, request_dep):
    """The web resume '9항의 스타일만 수정해주면 돼' on the incident run failed with a bare 'error:' (an assert)."""
    script = R.happy_script(dependent=True)
    script["dependent-claim-strategy-architect"] = [R.dep_architect(tsc=False)]
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_dep, "no-dep-text"))
    assert state.halt.stage == "DEP_ARCHITECT"
    with pytest.raises(ProviderError, match="종속항 문언이 아직 없어"):
        rt.engine(engine.provider).resume("no-dep-text", Decision(text="9항의 스타일만 수정", action="style_fix"))


def test_text_review_stages_get_no_drawings_but_are_told_why():
    from claim_agent.pipeline.packets import Packet, text_only
    from claim_agent.provider.base import ImagePart

    text = ("### 현재 발명 원자료 (전문)\n\n<<<MATERIAL [invention] a.md (sha256=1)>>>\n본문\n\n둘째 단락\n<<<END MATERIAL>>>\n\n"
            "<<<MATERIAL [drawing] 도1.png (sha256=2)>>> (이미지 파트로 첨부)\n\n## RUN_HEADER\n")
    slim = text_only(Packet(text, [ImagePart("image/png", b"x", "도1", "2")]))
    assert slim.images == [] and "도1.png" not in slim.text and "본문\n\n둘째 단락" in slim.text
    assert "도면 이미지 1장을 전달하지 않는다" in slim.text
    assert text_only(Packet("t")).text == "t"


@pytest.mark.parametrize("case_id", ["existing-set-edit-9", "dep-target-single-9", "no-merge-guard"])
def test_eval_cases_hold_on_a_clean_scripted_run(rt, case_id, edit_roles):
    case = EvalCase.load(CASES / case_id)
    engine = rt.engine(ScriptedProvider(edit_script()))
    state = engine.run(engine.start(case.request, f"case-{case_id}"))
    tokens, calls = summarize(state, read_telemetry(rt.store.telemetry_path(state.run_id)))
    checks = evaluate(state, case.expected, edited_set_text(state), tokens, calls)
    assert checks and all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]
