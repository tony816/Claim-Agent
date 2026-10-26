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
from claim_agent.store.report import edited_set_text, render_chat_report
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
                 "FUNCTION_DEDUCTION_GATE": "PASS", "DEPENDENT_OA_DRAFT_GATE": "PASS", "DEPENDENT_OA_FINAL_GATE": "UNVERIFIED"}
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


def _conversation(tmp_path, request_indep, name, text=INCIDENT_TEXT, previous=None, instructions="미완성된 9항 작성하기.\n\n" + BASELINE, user_lock=""):
    folder = tmp_path / name
    folder.mkdir()
    # A follow-up attaches nothing new: the previous run's materials come back through intake(previous).
    files = [] if previous else list(request_indep.invention_sources)
    request = dict(text=text, files=files, history=[], instructions=instructions, user_lock=user_lock)
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


NEW_10 = "【청구항 10】\n제9항에 있어서,\n상기 구속부는 상기 슬롯의 입구 쪽으로 갈수록 서로 가까워지는 케이블 클립 홀더."


def test_a_new_claim_appended_after_the_set_is_an_edit_with_the_whole_set_as_its_chain(rt, request_indep, tmp_path, monkeypatch):
    """Regression for run web-e69bc8a54174415cb943d99bccd59893: '돌출부에 대한 3항을 추가해줘' on a pasted set of 1~2 became a
    new dependent set, re-ran the independent pipeline, and stopped at DEP_STYLE because 제2항 was in neither text."""
    monkeypatch.setattr(R, "DEP_CLAIMS", [(10, 9, "DC-01", NEW_10)])
    monkeypatch.setattr(R, "DEP_SET_TEXT", NEW_10)
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "append", text="10항을 추가해줘")
    route = _route(blocks, target="10")
    provider = ScriptedProvider(edit_script())
    result = run_pipeline(rt, provider, request, route, blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    assert route.authoring_scope == "EXISTING_SET_EDIT" and route.edit_targets == [10]
    assert "claim-architect" not in [c.role for c in provider.calls]
    assert state.baseline_set.chain_nos == list(range(1, 10))
    design = next(c for c in provider.calls if c.role == "dependent-claim-strategy-architect")
    assert "제10항은(는) 기존 세트 끝에 새로 붙는 항" in design.packet_text
    full = edited_set_text(state)
    assert full.count("【청구항") == 10 and full.endswith(NEW_10)
    blind = next(c for c in provider.calls if c.role == "blind-claim-reconstruction-reviewer")
    assert all(f"【청구항 {n}】" in blind.packet_text for n in (1, 5, 8, 9, 10))       # the exact chain 10→9→8→5→1 only
    assert not any(f"【청구항 {n}】" in blind.packet_text for n in (2, 6, 7))


def test_appended_numbers_must_follow_the_set_without_a_gap():
    from claim_agent.claim_scope import find_existing_set
    from claim_agent.pipeline.claimtext import appended_nos

    claims = parse_claim_set(BASELINE)
    assert appended_nos(claims, {10, 11}) == {10, 11} and appended_nos(claims, {11}) == set() and appended_nos(claims, {9}) == set()
    assert find_existing_set({10}, [("a", BASELINE)]) == ("a", BASELINE)
    assert find_existing_set({11}, [("a", BASELINE)]) is None
    assert find_existing_set({1}, [("a", BASELINE)]) is None          # an independent claim is never an edit target


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
    # 제11항 leaves a gap after the set of 1~9 (제10항 would be a valid append).
    edit = request_indep.model_copy(update=dict(dependent=True, dependent_target="11", claim_file=str(claims), authoring_scope="EXISTING_SET_EDIT"))
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


# Run web-c9131a10ecba461e89c443ab5f09b0a4: the set sat in the project USER_LOCK, which neither the router nor the set
# finder read. The request became a new dependent set, the independent architect ran on it and stopped (2.5 min, 127k
# tokens) asking which claims '4·5·6항' were. The set also had blank numbers and a parent claim cut off mid-sentence.
LID_LOCK = """【청구항 1】
반응 용기가 안착되는 안착부;
상기 안착부의 상부에 위치하는 리드 지지부;
상기 리드 지지부에 착탈 가능하게 결합되는 히트리드 모듈을 포함하는 핵산 증폭 장치.

【청구항 2】
제1항에 있어서,
상기 히트리드 모듈에 포고 핀이 포함되는 핵산 증폭 장치.

【청구항 3】
제1항에 있어서,
상기 제2 전력 전달부는,
포고 핀; 및
상기 포고 핀이 수용되는 수형 하우징;
을 포함하고,

【청구항 4】
제3항에 있어서,
상기 제1 전력 전달부는 상기 수형 하우징이 수용되는 수용부가 마련되는 암형 하우징을 포함하는 핵산 증폭 장치.

【청구항 】
제1항에 있어서,
상기 리드 지지부는, 바디와, 상기 바디로부터 돌출되는 복수의 돌출부를 포함하는 핵산 증폭 장치.

【청구항 】
제항에 있어서,
상기 복수의 돌출부는, 서로 인접하는 상기 히트리드 모듈들 사이에 개재되는 핵산 증폭 장치."""
LID_REQUEST = "4항,5항,6항이 첨부 이미지를 온전하게 설명하도록 하고, 바닥부는 그 배치된 위치 구조와 같은 것이 더 구체화될 필요가 있어보임"


def test_a_set_kept_in_the_project_user_lock_is_edited_in_place_with_only_the_named_claim_released(rt, request_indep, tmp_path, edit_roles):
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "lock", instructions="", user_lock=BASELINE)
    route = _route(blocks)
    provider = ScriptedProvider(edit_script())
    result = run_pipeline(rt, provider, request, route, blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    assert route.authoring_scope == "EXISTING_SET_EDIT" and "claim-architect" not in [c.role for c in provider.calls]
    assert state.baseline_set.user_lock_released == [9] and state.request["user_lock"] == BASELINE.strip()
    assert Path(state.baseline_set.source_path).name.startswith("project-user-lock-")
    design = next(c for c in provider.calls if c.role == "dependent-claim-strategy-architect")
    assert "USER_LOCK 해제: 제9항" in design.packet_text
    full = edited_set_text(state)
    assert EDITED_9 in full and all(c.text in full for c in parse_claim_set(BASELINE)[:8])
    assert any("USER_LOCK 중 제9항은 이번 요청이 편집 대상으로 지정해 잠금 해제" in n for n in state.notes)


def test_an_edit_that_changes_a_user_lock_claim_it_did_not_name_is_still_blocked():
    from claim_agent.pipeline.claimtext import user_lock_kept

    full = BASELINE.replace(parse_claim_set(BASELINE)[8].text, EDITED_9)
    assert user_lock_kept(full, BASELINE, {9}) and not user_lock_kept(full, BASELINE, set())
    assert not user_lock_kept(full.replace("조임 나사", "조임 볼트"), BASELINE, {9})
    assert user_lock_kept(full, "책상 가장자리에 걸리는 클램프부", {9})          # a phrase lock is never released by a claim number


def test_a_user_lock_set_with_blank_numbers_stops_before_any_call_and_says_what_to_fill(rt, request_indep, tmp_path):
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "blank", text=LID_REQUEST, instructions="", user_lock=LID_LOCK)
    route = _route(blocks, target="4,5,6")
    provider = ScriptedProvider({})
    result = run_pipeline(rt, provider, request, route, blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert route.authoring_scope == "EXISTING_SET_EDIT" and provider.calls == []
    assert state.halt.reason_code == "BASELINE_SET_UNNUMBERED" and "프로젝트 USER_LOCK 확정 문언" in state.halt.message
    # Blank headers take their place numbers (5, 6) as the user counts them; a blank citation cannot be guessed.
    issues = [i["text"] for i in state.halt.open_issues]
    assert issues[:2] == [
        "제6항(상기 복수의 돌출부는, 서로 인접하는 상기 …): 인용항 번호(제항)가 비어 있음",
        "3번째 항: 종결부 없이 '…하우징; 을 포함하고,'로 끝남",
    ]
    # The request names claims the set already has without saying edit or new, so the same stop asks that too.
    assert issues[2].startswith("요청한 제4항, 제5항, 제6항은 세트에 이미 있는 번호입니다.") and len(issues) == 3
    # Stopped before the set was sealed, it still reports as a set edit (run web-f09a88a6 said 독립항 LOCK 없음).
    chat = render_chat_report(state)
    assert "기존 세트 편집" in chat and "독립항 LOCK 없음" not in chat and "독립항 문언 없음" not in chat and "미실행: ARCHITECT" not in chat


def test_a_new_claim_request_on_a_set_whose_parent_claim_is_cut_off_stops_before_any_call(rt, request_indep, tmp_path):
    numbered = LID_LOCK.replace("【청구항 】\n제항", "【청구항 】\n제5항")
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "unfinished", text="4항,5항,6항을 첨부 이미지로 새로 만들어줘",
                                                   instructions="", user_lock=numbered)
    route = _route(blocks, target="4,5,6")
    provider = ScriptedProvider({})
    result = run_pipeline(rt, provider, request, route, blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.request["edit_intent"] == "NEW"
    assert provider.calls == [] and state.halt.reason_code == "BASELINE_SET_UNFINISHED"
    assert [i["text"][:4] for i in state.halt.open_issues] == ["제3항:"]      # a new 제4~6항 may cite 제1~3항


AMBIGUOUS_9 = "9항이 도면을 온전하게 설명하도록"


def test_an_ambiguous_request_asks_before_any_call_and_new_inserts_the_claim_and_moves_the_rest(rt, request_indep, tmp_path, edit_roles):
    """User decision 2026-10-26: '4항,5항,6항이 첨부 이미지를…' meant new claims. Unclear words → ask; 새로 → insert and move back."""
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "ask", text=AMBIGUOUS_9, instructions="", user_lock=BASELINE)
    provider = ScriptedProvider(edit_script())
    first = run_pipeline(rt, provider, request, _route(blocks), blocks, paths, folder)
    state = rt.store.load_state(first["run_id"])
    assert provider.calls == [] and state.halt.kind == "USER_DECISION" and state.halt.reason_code == "EDIT_OR_NEW_UNCLEAR"
    assert "'수정'" in state.halt.message and "'새로'" in state.halt.message and state.request["edit_intent"] == "UNCLEAR"
    state = rt.engine(provider).resume(state.run_id, Decision(text="새로 만들어줘"))
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    base = state.baseline_set
    assert state.request["edit_intent"] == "NEW" and base.renumbered == {9: 10} and base.edit_targets == [9]
    assert base.chain_nos == [1, 2, 3, 4, 5, 6, 7, 8] and base.user_lock_released == []
    design = next(c for c in provider.calls if c.role == "dependent-claim-strategy-architect")
    assert "번호 재배열 (사용자 결정 — 요청 항을 새 항으로 끼움): 제9항→제10항" in design.packet_text
    assert "제9항은(는) 사용자가 새로 만들라고 한 항" in design.packet_text
    full = edited_set_text(state)
    old_9 = parse_claim_set(BASELINE)[8].body
    assert full.count("【청구항") == 10 and EDITED_9 + "\n\n【청구항 10】\n" + old_9 in full
    assert all(c.text in full for c in parse_claim_set(BASELINE)[:8])
    assert any("기존 항 번호·인용을 옮김(제9항→제10항)" in n for n in state.notes)


def test_claim_intent_reads_only_explicit_words():
    from claim_agent.claim_scope import claim_intent

    assert claim_intent(LID_REQUEST) is None and claim_intent(AMBIGUOUS_9) is None
    assert claim_intent("4항,5항,6항을 새로 만들어줘") == "NEW" and claim_intent("10항을 추가해줘") == "NEW"
    assert claim_intent(INCIDENT_TEXT) == "EDIT"                     # 정리되지 않은 9항…; 중간끼워맞춤 is not 끼워 넣기
    assert claim_intent("9항은 수정하고 10항은 추가해줘") is None       # both: ask


def test_inserting_claims_moves_the_set_back_and_its_citations_follow():
    from claim_agent.pipeline.claimtext import ClaimParseError, fill_blank_headers, insert_claims, renumber_text

    claims = parse_claim_set(BASELINE)
    moved, mapping = insert_claims(claims, {4, 5, 6})
    by_no = {c.claim_no: c for c in moved}
    assert mapping == {n: n + 3 for n in range(4, 10)} and sorted(by_no) == [1, 2, 3, 7, 8, 9, 10, 11, 12]
    for old in claims:
        new = by_no[mapping.get(old.claim_no, old.claim_no)]
        assert new.parent_nos == [mapping.get(p, p) for p in old.parent_nos]
        assert all(f"제{mapping.get(p, p)}항" in new.body[:20] for p in old.parent_nos)
    assert insert_claims(claims, {10, 11}) == (claims, {})                    # an append moves nothing
    ranged = BASELINE.replace("【청구항 9】\n제8항에 있어서", "【청구항 9】\n제3항 내지 제5항 중 어느 한 항에 있어서")
    with pytest.raises(ClaimParseError, match="범위 인용"):
        insert_claims(parse_claim_set(ranged), {4})
    assert "【청구항 7】\n제3항에 있어서" in renumber_text(LID_LOCK.replace("【청구항 4】", "【청구항 】"), {4: 7})
    shuffled = "【청구항 1】\nA.\n\n【청구항 3】\n제1항에 있어서, B.\n\n【청구항 】\n제1항에 있어서, C."
    assert fill_blank_headers(shuffled) == shuffled                           # numbers out of place: not guessed


# Run web-b013b034: the router kept '히트리드 모듈에 대한 종속항들 작성해줘' on the USER_LOCK set with a description for a
# target ('세트 끝(10항~)에 …'). Its '10' made the target parser raise, and the web showed only "응답을 받지 못했습니다".
OPEN_APPEND = "세트 끝(10항~)에 이어지는 히트리드 모듈 관련 신규 종속항 — 기술기여가 확인된 후보"


def _append_route(blocks):
    return RouteDecision(mode="AUTHORING_DRAFT", reason="세트 끝에 종속항 추가", dependent=True, dependent_target=OPEN_APPEND,
                         authoring_scope="EXISTING_SET_EDIT")


NEW_7 = "【청구항 7】\n제5항에 있어서,\n상기 복수의 돌출부는 상기 바디의 일측을 따라 서로 이격되어 배치되는 핵산 증폭 장치."


def test_new_claims_on_a_set_with_blanks_skip_the_unfinished_claims_as_parents(rt, request_indep, tmp_path, monkeypatch):
    """User decision 2026-10-26 (run web-f09a88a6): new claims after the end of the set go on; a claim cut off, with a blank
    citation, or citing such a claim is left out of their chain and kept as written, instead of stopping the edit."""
    monkeypatch.setattr(R, "DEP_CLAIMS", [(7, 5, "DC-01", NEW_7)])
    monkeypatch.setattr(R, "DEP_SET_TEXT", NEW_7)
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "open-blank", text="히트리드 모듈에 대한 종속항들 작성해줘",
                                                   instructions="", user_lock=LID_LOCK)
    route = _append_route(blocks)
    provider = ScriptedProvider(edit_script())
    result = run_pipeline(rt, provider, request, route, blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    assert route.authoring_scope == "EXISTING_SET_EDIT" and route.edit_targets == []
    base = state.baseline_set
    assert base.edit_targets == [7] and base.chain_nos == [1, 2, 5] and sorted(base.excluded) == [3, 4, 6]
    design = next(c for c in provider.calls if c.role == "dependent-claim-strategy-architect")
    assert "제1항, 제2항, 제5항" in design.packet_text and "제3항(종결부 없이 끝남)" in design.packet_text
    full = edited_set_text(state)
    assert full.endswith(NEW_7) and "【청구항 6】\n제항에 있어서" in full and "을 포함하고,\n\n【청구항 4】" in full


def test_a_new_claim_citing_a_claim_left_out_of_the_chain_is_refused(rt, request_indep, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "DEP_CLAIMS", [(7, 4, "DC-01", NEW_7.replace("제5항", "제4항"))])
    monkeypatch.setattr(R, "DEP_SET_TEXT", NEW_7.replace("제5항", "제4항"))
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "cites-out", text="히트리드 모듈에 대한 종속항을 7항으로 추가해줘",
                                                   instructions="", user_lock=LID_LOCK)
    provider = ScriptedProvider(edit_script(**{"dependent-claim-strategy-architect": [R.dep_architect()] * 2}))
    result = run_pipeline(rt, provider, request, _route(blocks, target="7"), blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert [c.role for c in provider.calls] == ["dependent-claim-strategy-architect"] * 2
    assert "REQUEST_SCOPE_MISMATCH" in state.halt.message and "DC-01 제7항의 부모 제4항" in state.halt.message


NEW_11 = "【청구항 11】\n제10항에 있어서,\n상기 구속부의 두께는 상기 슬롯의 깊이 방향을 따라 일정한 케이블 클립 홀더."


def test_dependents_asked_for_without_a_number_are_counted_and_numbered_by_the_design(rt, request_indep, tmp_path, monkeypatch):
    """User decision 2026-10-26 (run web-53aa9366): no number is asked for. The design writes as many claims as it finds
    technical contributions, numbered on from the set's end, and its numbers become the edit targets."""
    monkeypatch.setattr(R, "DEP_CLAIMS", [(10, 9, "DC-01", NEW_10), (11, 10, "DC-02", NEW_11)])
    monkeypatch.setattr(R, "DEP_SET_TEXT", NEW_10 + "\n\n" + NEW_11)
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "open-count", text="구속부에 대한 종속항들 작성해줘",
                                                   instructions="", user_lock=BASELINE)
    provider = ScriptedProvider(edit_script(**{"blind-claim-reconstruction-reviewer": [R.blind()] * 2,
                                               "picture-claim-reconstruction-reviewer": [R.picture()] * 2}))
    result = run_pipeline(rt, provider, request, _append_route(blocks), blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    base = state.baseline_set
    assert base.numbered_by_design and base.edit_targets == [10, 11] and state.request["dependent_target"] == "10~11"
    assert base.chain_nos == list(range(1, 10)) and "claim-architect" not in [c.role for c in provider.calls]
    design = next(c for c in provider.calls if c.role == "dependent-claim-strategy-architect")
    assert "제10항부터 빈 번호 없이" in design.packet_text and "dependent_target_claim_nos" not in design.packet_text
    assert "세트 끝에 붙일 새 종속항(번호는 종속항 설계가 정함)" in design.packet_text
    assert any("제10항, 제11항으로 정함" in n for n in state.notes)
    full = edited_set_text(state)
    assert full.count("【청구항") == 11 and full.endswith(NEW_10 + "\n\n" + NEW_11)


def test_a_design_numbering_an_open_append_with_a_gap_gets_one_repair_call_then_stops(rt, request_indep, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "DEP_CLAIMS", [(11, 9, "DC-01", NEW_10.replace("10】", "11】"))])
    monkeypatch.setattr(R, "DEP_SET_TEXT", NEW_10.replace("10】", "11】"))
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "open-gap", text="구속부에 대한 종속항들 작성해줘",
                                                   instructions="", user_lock=BASELINE)
    provider = ScriptedProvider(edit_script(**{"dependent-claim-strategy-architect": [R.dep_architect()] * 2}))
    result = run_pipeline(rt, provider, request, _append_route(blocks), blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert [c.role for c in provider.calls] == ["dependent-claim-strategy-architect"] * 2
    assert "REQUEST_SCOPE_MISMATCH" in state.halt.message and "제10항부터 빈 번호 없이" in state.halt.message
    assert state.baseline_set.edit_targets == [] and not state.dependent.draft_set_lock


def test_an_open_append_needs_a_set_that_already_holds_a_dependent_claim():
    from claim_agent.claim_scope import find_existing_set

    assert find_existing_set(set(), [("a", BASELINE)]) == ("a", BASELINE)
    assert find_existing_set(set(), [("a", parse_claim_set(BASELINE)[0].text)]) is None     # claim 1 alone: a new dependent set


def test_a_fixed_user_lock_sent_again_starts_a_new_run_on_the_fixed_set(rt, request_indep, tmp_path, edit_roles):
    blank = BASELINE.replace("【청구항 9】\n제8항", "【청구항 9】\n제항")
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "before-fix", instructions="", user_lock=blank)
    first = run_pipeline(rt, ScriptedProvider({}), request, _route(blocks), blocks, paths, folder)
    previous = rt.store.load_state(first["run_id"])
    assert previous.halt.reason_code == "BASELINE_SET_UNNUMBERED"
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "after-fix", previous=previous, instructions="", user_lock=BASELINE)
    result = run_pipeline(rt, ScriptedProvider(edit_script()), request, _route(blocks), blocks, paths, folder, previous)
    state = rt.store.load_state(result["run_id"])
    assert result["revision_path"] == "new" and state.request["user_lock"] == BASELINE.strip()
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt


def test_the_router_sees_the_project_user_lock(project_root):
    from claim_agent.routing import classify_request

    provider = ScriptedProvider({"request-router": [{"mode": "AUTHORING_DRAFT", "reason": "편집", "dependent": True, "dependent_target": "9"}]})
    classify_request(provider, project_root, "test", "9항을 정리해줘", [], "lock-route", user_lock=BASELINE)
    assert '"project_user_lock": "【청구항 1】' in provider.calls[0].packet_text
