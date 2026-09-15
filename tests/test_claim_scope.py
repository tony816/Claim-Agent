import pytest

from claim_agent.claim_scope import constrain_target, parse_target
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.routing import classify_request
from tests import scripted_roles as R


@pytest.mark.parametrize("target,expected", [("2", {2}), ("2~4,6", {2,3,4,6}), ("청구항 2–3", {2,3}), ("기술기여가 확인된 후보", None)])
def test_parse_target(target, expected):
    assert parse_target(target) == expected


def test_current_question_overrides_stale_full_set_option(project_root):
    provider = ScriptedProvider({"request-router": [{"mode":"AUTHORING_DRAFT", "reason":"수정", "dependent":True, "dependent_target":"2~8"}]})
    route = classify_request(provider, project_root, "test", "1항을 하측 지지로 바꾸면 2항이 어떻게 수정되어야 하지", [], "scope-test", {"dependent":True,"target":"2~8"})
    assert route.dependent_target == "2"
    assert constrain_target("2항을 참고하여 1항만 수정해줘", False, None) == (False, None)
    assert constrain_target("2항을 참고하여 1항만 수정해줘", True, "2~8") == (False, None)
    assert constrain_target("3항과 비교해서 2항만 수정해줘", True, "2~8") == (True, "2")
    assert constrain_target("전체 청구항을 수정해줘", True, "2~8") == (True, "2~8")


def test_out_of_scope_design_cannot_issue_lock_or_call_drafter(rt, request_dep):
    request_dep.dependent_target = "2"
    provider = ScriptedProvider(R.happy_script())  # Deliberately proposes 2~4.
    engine = rt.engine(provider)
    state = engine.run(engine.start(request_dep, "oversized-design"))
    assert state.halt and "REQUEST_SCOPE_MISMATCH" in state.halt.message
    assert not state.dependent.draft_set_lock
    assert not any(c.role == "claim-drafter" and c.scope == "DEPENDENT_SET" for c in provider.calls)
    refs = [r for r in state.records.values() if r.role == "dependent-claim-strategy-architect"]
    assert len(refs) == 1 and not refs[0].issued


def test_single_requested_claim_completes_all_required_gates(rt, request_dep, monkeypatch):
    request_dep.dependent_target = "2"
    monkeypatch.setattr(R, "DEP_CLAIMS", R.DEP_CLAIMS[:1])
    monkeypatch.setattr(R, "DEP_SET_TEXT", R.DEP_CLAIMS[0][3])
    provider = ScriptedProvider(R.happy_script(n_targets=1))
    engine = rt.engine(provider)
    state = engine.run(engine.start(request_dep, "single-dependent"))
    assert not state.halt and state.dependent.draft_set_lock
    assert [c["claim_no"] for c in state.dependent.current.claims] == [2]
    targets = [c.meta["target_claim_id"] for c in provider.calls if c.scope == "DEPENDENT_SINGLE"]
    assert targets == ["2", "2"]
