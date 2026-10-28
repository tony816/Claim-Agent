import pytest

from claim_agent.claim_scope import apply_ui_target, constrain_target, format_target, parse_target, resolve_claim_target, ui_target_set
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.routing import classify_request
from tests import scripted_roles as R


@pytest.mark.parametrize("target,expected", [("2", {2}), ("2~4,6", {2,3,4,6}), ("청구항 2–3", {2,3}), ("기술기여가 확인된 후보", None),
                                             ("세트 끝(10항~)에 이어지는 히트리드 모듈 관련 신규 종속항 — 기술기여가 확인된 후보", None)])
def test_parse_target(target, expected):
    assert parse_target(target) == expected


def test_parse_target_refuses_digits_that_form_no_list():
    with pytest.raises(ValueError, match="2~4,6 형식"):
        parse_target("2~4;6")


def test_current_question_overrides_stale_full_set_option(project_root):
    provider = ScriptedProvider({"request-router": [{"mode":"AUTHORING_DRAFT", "reason":"수정", "dependent":True, "dependent_target":"2~8"}]})
    route = classify_request(provider, project_root, "test", "1항을 하측 지지로 바꾸면 2항이 어떻게 수정되어야 하지", [], "scope-test", {"dependent":True,"target":"2~8"})
    assert route.dependent_target == "2"
    assert constrain_target("2항을 참고하여 1항만 수정해줘", False, None) == (False, None)
    assert constrain_target("2항을 참고하여 1항만 수정해줘", True, "2~8") == (False, None)
    assert constrain_target("3항과 비교해서 2항만 수정해줘", True, "2~8") == (True, "2")
    assert constrain_target("전체 청구항을 수정해줘", True, "2~8") == (True, "2~8")


# Run web-6be7454f: the router put this on the end of the user's 1~12 set with no number, and the guard turned '1~12항'
# into targets 2~12, so the edit wrote new claims at 2~12 and moved the whole set to 13~23.
MISSED_POINTS = "유지하려고 함. 1~12항에서 놓친 특허 포인트가 있는지 확인하고 추가 종속항들을 작성해줘. 특허 포인트가 없으면 종속항 안 만들어도 괜찮아."


def test_a_span_from_claim_1_names_the_set_new_claims_join_not_targets():
    open_target = "기술기여가 확인된 후보"
    assert constrain_target(MISSED_POINTS, True, open_target, append=True) == (True, open_target)
    assert constrain_target("제1항~제12항을 참고해서 종속항 더 써줘", True, open_target, append=True) == (True, open_target)
    to_12 = ",".join(map(str, range(2, 13)))
    assert constrain_target(MISSED_POINTS, True, "2~12", append=True) == (True, to_12)              # the router's own numbers
    assert constrain_target("1~12항을 정리해줘", True, open_target, append=True) == (True, to_12)    # an edit names its targets
    assert constrain_target("10항을 추가해줘", True, open_target, append=True) == (True, "10")       # no claim 1: a number to write
    assert constrain_target("13항부터 추가 종속항 써줘", True, open_target, append=True) == (True, open_target)
    assert constrain_target("12항 뒤에 종속항을 추가해줘", True, open_target, append=True) == (True, open_target)
    assert constrain_target(MISSED_POINTS, True, open_target) == (True, to_12)                     # not on a user's set


def test_out_of_scope_design_cannot_issue_lock_or_call_drafter(rt, request_dep):
    request_dep.dependent_target = "2"
    script = R.happy_script()
    script["dependent-claim-strategy-architect"] = [R.dep_architect()] * 2   # Deliberately proposes 2~4, also on the repair call.
    provider = ScriptedProvider(script)
    engine = rt.engine(provider)
    state = engine.run(engine.start(request_dep, "oversized-design"))
    assert state.halt and "REQUEST_SCOPE_MISMATCH" in state.halt.message
    assert f"독립항 {state.candidate.draft_claim_lock}은 변경하지 않았습니다" in state.halt.message
    assert not state.dependent.draft_set_lock
    assert not any(c.role == "claim-drafter" and c.scope == "DEPENDENT_SET" for c in provider.calls)
    refs = [r for r in state.records.values() if r.role == "dependent-claim-strategy-architect"]
    assert len(refs) == 2 and not any(r.issued for r in refs)          # one repair call, then the stop


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


def test_format_target_collapses_consecutive_numbers():
    assert format_target({2, 3, 4, 6, 8, 9}) == "2~4,6,8~9" and parse_target("2~4,6,8~9") == {2, 3, 4, 6, 8, 9}
    assert format_target({3}) == "3"
    with pytest.raises(ValueError):
        format_target(set())


@pytest.mark.parametrize("data,expected", [
    ({"target_mode": "auto"}, (False, None, "auto")),
    ({"target_mode": "independent"}, (False, None, "independent")),
    ({"target_mode": "single", "target_claim": "3"}, (True, "3", "single")),
    ({"target_mode": "single", "target_claim": 1}, (False, None, "single")),
    ({"target_mode": "range", "target_segments": [{"from": 2, "to": 4}, {"from": 6}, 9, {"from": "7", "to": "8"}]}, (True, "2~4,6~9", "range")),
    ({"target_mode": "range", "target_segments": "청구항 2~3, 5"}, (True, "2~3,5", "range")),
    ({"dependent": True, "target": "2~8"}, (True, "2~8", "range")),
    ({"dependent": False, "target": "2~8"}, (False, None, "auto")),
    ({}, (False, None, "auto")),
])
def test_resolve_claim_target(data, expected):
    assert resolve_claim_target(data) == expected


@pytest.mark.parametrize("data", [
    {"target_mode": "all"},
    {"target_mode": "single", "target_claim": "둘"},
    {"target_mode": "single", "target_claim": 0},
    {"target_mode": "range", "target_segments": [{"from": 4, "to": 2}]},
    {"target_mode": "range", "target_segments": [{"from": 1, "to": 3}]},
    {"target_mode": "range", "target_segments": []},
    {"target_mode": "range", "target_segments": 5},
])
def test_resolve_claim_target_rejects_bad_input(data):
    with pytest.raises(ValueError):
        resolve_claim_target(data)


def test_ui_target_set_only_for_explicit_picks():
    assert ui_target_set({"target_mode": "single", "dependent": True, "target": "3"}) == {3}
    assert ui_target_set({"target_mode": "range", "dependent": True, "target": "2~3"}) == {2, 3}
    assert ui_target_set({"dependent": True, "target": "2~8"}) is None          # legacy default hint, not a pick
    assert ui_target_set({"target_mode": "single", "dependent": False, "target": ""}) is None
    assert ui_target_set(None) is None


def test_independent_pick_drops_dependent_work_unless_the_text_names_claims():
    independent, auto = {"target_mode": "independent", "dependent": False, "target": ""}, {"target_mode": "auto", "dependent": False, "target": ""}
    assert apply_ui_target("AUTHORING_DRAFT", True, "2~8", "청구항 세트 작성해줘", independent) == (False, None)
    assert apply_ui_target("AUTHORING_DRAFT", True, "3", "3항도 고쳐줘", independent) == (True, "3")
    assert apply_ui_target("AUTHORING_DRAFT", True, "2~8", "청구항 세트 작성해줘", auto) == (True, "2~8")   # 미지정: router decides
    assert apply_ui_target("REVIEW_ONLY", False, None, "검토해줘", independent) == (False, None)


def test_apply_ui_target_narrows_but_the_request_text_wins():
    single = {"target_mode": "single", "dependent": True, "target": "3"}
    assert apply_ui_target("AUTHORING_DRAFT", True, "2~8", "종속항을 더 넓게 써줘", single) == (True, "3")
    assert apply_ui_target("AUTHORING_DRAFT", False, None, "이 항을 더 넓게 써줘", single) == (True, "3")
    assert apply_ui_target("AUTHORING_DRAFT", False, None, "1항만 고쳐줘", single) == (False, None)
    assert apply_ui_target("AUTHORING_DRAFT", True, "3~4", "3항과 4항을 고쳐줘", {"target_mode": "range", "dependent": True, "target": "2~3"}) == (True, "3")
    assert apply_ui_target("REVIEW_ONLY", False, None, "3항 의견", single) == (False, None)
    assert apply_ui_target("AUTHORING_DRAFT", True, "2~8", "종속항 써줘", {"dependent": True, "target": "2~8"}) == (True, "2~8")
    with pytest.raises(ValueError, match="요청 문장의 항 번호"):
        apply_ui_target("AUTHORING_DRAFT", True, "5", "5항 고쳐줘", single)


DEPENDENT_FRAGMENT = """【청구항 9】
제8항에 있어서,
상기 구속부는 상기 캡 수용 공간의 둘레를 따라 형성되는 오목면을 포함하는 홀더."""

CLAIM_SET_WITH_ROOT = """【청구항 1】
기둥과 상기 기둥에 결합되는 캡을 포함하는 홀더.

【청구항 8】
제1항에 있어서,
상기 캡은 캡 수용 공간을 구획하는 홀더.

【청구항 9】
제8항에 있어서,
상기 구속부는 상기 캡 수용 공간의 둘레를 따라 형성되는 오목면을 포함하는 홀더."""


def _dependent_request(request_indep, tmp_path, text, target="9"):
    path = tmp_path / "claims.md"
    path.write_text(text, encoding="utf-8")
    req = request_indep.model_copy()
    req.dependent, req.dependent_target, req.claim_file = True, target, str(path)
    return req


def test_dependent_fragment_never_enters_the_independent_pipeline(rt, request_indep, tmp_path):
    """The 9분/13회 호출 failure: a dependent fragment ran the INDEPENDENT stages and every gate rejected it."""
    provider = ScriptedProvider(R.happy_script())
    engine = rt.engine(provider)
    request = _dependent_request(request_indep, tmp_path, DEPENDENT_FRAGMENT)
    state = engine.run(engine.start(request, "dependent-fragment"))
    assert provider.calls == []                                  # stopped before the first model call
    assert state.halt.reason_code == "INDEPENDENT_SCOPE_DEPENDENT_CLAIM_TEXT"
    assert state.halt.kind == "BLOCK" and state.halt.role == "engine"
    assert "제8항" in state.halt.message and "부모항" in state.halt.message
    assert not state.candidate.draft_claim_lock and state.dependent is None
    assert any(i["code"] == "INDEPENDENT_SCOPE_DEPENDENT_CLAIM_TEXT" for i in state.halt.open_issues)


def test_dependent_request_with_an_unresolvable_parent_chain_stops_first(rt, request_indep, tmp_path):
    broken = CLAIM_SET_WITH_ROOT.replace("""【청구항 8】
제1항에 있어서,
상기 캡은 캡 수용 공간을 구획하는 홀더.

""", "")
    provider = ScriptedProvider(R.happy_script())
    state = rt.engine(provider).run(rt.engine(provider).start(_dependent_request(request_indep, tmp_path, broken), "broken-chain"))
    assert provider.calls == [] and state.halt.reason_code == "DEPENDENT_PARENT_CHAIN_MISSING"
    assert "제9항" in state.halt.open_issues[0]["text"]


def test_a_claim_set_with_its_root_and_plain_material_still_run(rt, request_indep, request_dep, tmp_path):
    """The guard must not catch the normal paths: a full set, or authoring claim 1 and its dependents together."""
    engine = rt.engine(ScriptedProvider(R.happy_script()))
    with_root = engine.start(_dependent_request(request_indep, tmp_path, CLAIM_SET_WITH_ROOT), "with-root")
    engine.scope_guard(with_root, engine._bundle)
    from_material = engine.start(request_dep, "from-material")
    engine.scope_guard(from_material, engine._bundle)             # no claim file at all


# Run web-54630bf2: 4·5 are what the new claims draw on and 14·15 their parents. Read as targets, 14·15 were missing
# from the user's 1~12 set and the request fell through to a new independent claim behind a prior-art search.
DRAW_ON = "4항 5항의 구성을 제어가 중점인 14~15항 실시예의 새로운 종속항 작성에 활용 가능해보여. 이에 관한 14OR15 항의 종속항을 추가해줘."


def test_parents_and_claims_drawn_on_are_no_targets():
    from claim_agent.claim_scope import cited_claims, named_parents

    assert named_parents(DRAW_ON) == {14, 15} and cited_claims(DRAW_ON) == {4, 5, 14, 15}
    assert constrain_target(DRAW_ON, True, "4,5,14,15", append=True) == (True, "기술기여가 확인된 후보")
    assert constrain_target("제3항에 종속하는 새 항 13항을 추가해줘", True, "3,13") == (True, "13")
    assert named_parents("제3항 또는 제4항을 인용하는 종속항 추가") == {3, 4}
    assert constrain_target("4항의 구성을 바꿔줘", True, "4") == (True, "4")          # an edit of claim 4 itself
