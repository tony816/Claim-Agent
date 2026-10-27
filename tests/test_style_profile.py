"""The claim form the style stage writes to: the user's set when a run adds to one, the corpus otherwise.

Regression for runs up to 2026-10-27: the style adjuster kept the drafter's `…하는,⏎핵산 증폭 장치.` and `상기 X는,⏎`
form (94% and 67% of dependent claims) though no claim of the corpus is written that way.
"""
from __future__ import annotations

from pathlib import Path

from claim_agent.conversation_pipeline import run_pipeline
from claim_agent.pipeline.style_profile import BASELINE_SET, CORPUS, drift, render_target, subject_on_own_line, target_profile, title_on_own_line
from claim_agent.provider.scripted import ScriptedProvider

from . import scripted_roles as R
from .test_existing_set_edit import BASELINE, EDITED_9, _conversation, _route, edit_script

CORPUS_TEXT = (Path(__file__).resolve().parents[1] / "sources" / "청구항_문체학습용_분야별검색최적화본.md").read_text(encoding="utf-8")
SET_FORM = """【청구항 1】
반응 용기가 안착되는 안착부; 및
상기 안착부의 상부에 위치하는 리드 지지부;
를 포함하는,
핵산 증폭 장치.

【청구항 2】
제1항에 있어서,
상기 리드 지지부는,
상기 안착부를 덮는 커버를 포함하는,
핵산 증폭 장치.

【청구항 3】
제2항에 있어서,
상기 커버는,
금속으로 형성되는,
핵산 증폭 장치."""


def _bodies(text):
    from claim_agent.pipeline.claimtext import parse_claim_set
    return [c.body for c in parse_claim_set(text)]


def test_the_corpus_writes_the_name_on_the_predicate_line_and_the_subject_inline():
    p = target_profile(CORPUS_TEXT)
    assert (p.source, p.title_on_own_line, p.subject_on_own_line) == (CORPUS, False, False)
    assert drift(R.ROOT_CLAIM + "\n\n" + R.DEP_SET_TEXT, p) == []


def test_a_set_written_with_the_name_on_its_own_line_is_followed():
    p = target_profile(CORPUS_TEXT, _bodies(SET_FORM))
    assert (p.source, p.title_on_own_line, p.subject_on_own_line, p.fallback) == (BASELINE_SET, True, True, ())
    assert drift(SET_FORM, p) == []
    issues = drift(R.DEP_SET_TEXT, p)
    assert len(issues) == 6 and "제2항 종결 형식: 기존 세트 기준은" in issues[0] and "제2항 주어 분절: 기존 세트 기준은" in issues[1]
    assert "기존 세트" in render_target(p, CORPUS_TEXT) and "코퍼스 본보기" not in render_target(p, CORPUS_TEXT)


def test_what_the_set_does_not_decide_comes_from_the_corpus():
    only_root = SET_FORM.split("\n\n【청구항 2】")[0]
    p = target_profile(CORPUS_TEXT, _bodies(only_root))
    assert (p.title_on_own_line, p.subject_on_own_line, p.fallback) == (True, False, ("주어 분절",))


def test_drift_skips_claims_outside_the_targets_and_user_lock_wording():
    p = target_profile(CORPUS_TEXT)
    assert drift(SET_FORM, p, only_nos={3}) == ["제3항 종결 형식: 코퍼스 기준은 명칭을 마지막 술어와 같은 줄에 쉼표 없이 붙인다(`…하는 [명칭].`)",
                                               "제3항 주어 분절: 코퍼스 기준은 `상기 X는` 뒤에 쉼표·줄바꿈 없이 한정을 이어 쓴다(`상기 X는 …하는 [명칭].`)"]
    assert drift(SET_FORM, p, user_lock=SET_FORM) == []


def test_the_features_do_not_take_the_preamble_or_a_mid_claim_comma_for_the_form():
    assert not title_on_own_line("제1항에 있어서,\n상기 커버는 금속으로 형성되는 핵산 증폭 장치.")
    assert not subject_on_own_line("제1항에 있어서,\n상기 제1 전달부 및 상기 제2 전달부 중 어느 하나에 핀이 포함되고,\n상기 핀은 탄성을 갖는,\n핵산 증폭 장치.")
    assert not subject_on_own_line("제1항에 있어서,\n상기 베이스의 하면에는 패드가 부착되는,\n케이블 클립 홀더.")   # a predicate line ending in 는,
    assert subject_on_own_line("제1항에 있어서,\n상기 브릿지는,\n금속으로 형성되는,\n장치.")
    assert subject_on_own_line("제1항에 있어서,\n상기 커버는,\n금속으로 형성되는 장치.")
    assert not title_on_own_line("제1항에 있어서,\n상기 커버는,\n금속으로 형성되는 장치.")   # the last line is the limitation


def test_new_writing_in_the_drafters_form_goes_back_to_the_style_adjuster(rt, request_indep):
    drifted = R.ROOT_CLAIM.replace("형성되는 케이블 클립 홀더.", "형성되는,\n케이블 클립 홀더.")
    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(text=drifted), R.style()]
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "style-profile-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.candidate.revision == "r2" and state.candidate.loop_counts["STYLE_ONLY"] == 1
    calls = [c for c in engine.provider.calls if c.role == "claim-style-adjuster" and c.phase == "main"]
    assert "문체 기준" in calls[0].packet_text and "기준: 코퍼스" in calls[0].packet_text and "코퍼스 본보기" in calls[0].packet_text
    assert "STYLE_ONLY_REVISION" in calls[1].packet_text and "STYLE_PROFILE_DRIFT" in calls[1].packet_text and "제1항 종결 형식" in calls[1].packet_text
    assert len([c for c in engine.provider.calls if c.role == "claim-drafter"]) == 1


def test_a_form_that_never_follows_stops_at_the_style_only_limit(rt, request_indep):
    drifted = R.ROOT_CLAIM.replace("형성되는 케이블 클립 홀더.", "형성되는,\n케이블 클립 홀더.")
    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(text=drifted)] * 3
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "style-profile-02"))
    assert state.halt and state.halt.kind == "LOOP_LIMIT" and state.halt.reason_code == "STYLE_PROFILE_DRIFT"
    assert not state.candidate.draft_claim_lock


def _set_form(text: str) -> str:
    return text.replace(" 케이블 클립 홀더.", ",\n케이블 클립 홀더.")


def test_an_edit_follows_the_users_set_form_not_the_corpus(rt, request_indep, tmp_path, monkeypatch):
    set_9 = _set_form(EDITED_9)
    monkeypatch.setattr(R, "DEP_CLAIMS", [(9, 8, "DC-01", set_9)])
    monkeypatch.setattr(R, "DEP_SET_TEXT", set_9)
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "set-form", instructions="미완성된 9항 작성하기.\n\n" + _set_form(BASELINE))
    provider = ScriptedProvider(edit_script(**{"claim-style-adjuster": [R.style("DEPENDENT_SET", text=EDITED_9), R.style("DEPENDENT_SET")]}))
    result = run_pipeline(rt, provider, request, _route(blocks), blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    calls = [c for c in provider.calls if c.role == "claim-style-adjuster" and c.phase == "main"]
    assert len(calls) == 2 and "기준: 기존 세트" in calls[0].packet_text and "코퍼스 본보기" not in calls[0].packet_text
    assert "제9항 종결 형식: 기존 세트 기준은" in calls[1].packet_text
    assert state.dependent.current.claims[0]["text"] == set_9
