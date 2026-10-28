"""Conversation moderator (CLAUDE.md 대화 모더레이터): front answers questions, back delivers results, code keeps the seals."""
from __future__ import annotations

import pytest

from claim_agent import chat, moderator, routing, runtime
from claim_agent.live_events import EventWriter
from claim_agent.models.state import Halt
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.routing import RouteDecision
from claim_agent.store.report import render_chat_report, render_report
from tests import scripted_roles as R

QUESTION = '본체부 부분에서 "상기 복수의 돌출부에 의해 서로 구획되고" 대신 본체부의 특징을 적을 만한게 있을지?'
USER_CLAIM = "【청구항 5】\n제1항에 있어서,\n상기 리드 지지부는 본체부; 및 복수의 돌출부;를 포함하는,\n핵산 증폭 장치."


def test_consult_is_a_route_without_writing_scope():
    assert RouteDecision(mode="CONSULT", reason="선택지 질문").mode == "CONSULT"
    with pytest.raises(ValueError, match="작성하지 않는 요청"):
        RouteDecision(mode="CONSULT", reason="질문", dependent=True, dependent_target="6")
    with pytest.raises(ValueError, match="재개"):
        RouteDecision(mode="CONSULT", reason="질문", resumes_halt=True)


def test_router_prompt_separates_questions_from_wording_requests():
    assert "CONSULT" in routing.ROUTER_SYSTEM and "적을 만한 게 있을지" in routing.ROUTER_SYSTEM.replace("할 만한", "적을 만한")
    assert "resumes_halt" in routing.ROUTER_SYSTEM


def test_guard_labels_wording_that_no_seal_holds():
    answer = "후보는 이렇습니다.\n\n" + USER_CLAIM + "\n\n【청구항 6】\n제5항에 있어서,\n상기 본체부는 평판형인,\n핵산 증폭 장치.\n\n## 최종안"
    out = moderator.guard(answer, moderator.known_claims(USER_CLAIM))
    assert "【청구항 5】\n" in out                                   # the user's own claim, quoted verbatim, stays as it is
    assert "【청구항 6】 " + moderator.UNSEALED_LABEL in out
    assert "## 후보(미검수)" in out and "## 최종안" not in out
    assert moderator.guard(out, set()).count(moderator.UNSEALED_LABEL) == 2   # idempotent: no double label


def test_consult_question_is_answered_without_any_claim_role(rt, tmp_path, monkeypatch):
    route = {"mode": "CONSULT", "reason": "본체부 특징 후보를 묻는 질문"}
    answer = "있습니다. 슬라이드 3의 슬롯 배열을 쓸 수 있습니다.\n\n【청구항 6】\n제5항에 있어서, 상기 본체부는 슬롯을 포함하는,\n핵산 증폭 장치.\n\nB로 6항을 다시 쓸까요?"
    provider = ScriptedProvider({moderator.MODERATOR: [route, answer]})
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **k: pytest.fail("질문에 파이프라인을 돌림"))
    request = dict(text=QUESTION, material=USER_CLAIM, files=[], history=[], model="conversation-model")
    result = chat.routed_turn(provider, rt.cfg, None, request, tmp_path, EventWriter(tmp_path / "events.jsonl"))
    assert result["effective_mode"] == "CONSULT" and "run_id" not in result
    routing_call, consult_call = provider.calls
    assert routing_call.role == consult_call.role == moderator.MODERATOR
    assert routing_call.model == rt.cfg.model_for(moderator.MODERATOR) != "conversation-model"   # Opus reads the intent
    assert consult_call.stage == "CONSULT" and QUESTION in consult_call.packet_text and USER_CLAIM in consult_call.packet_text
    assert "【청구항 6】 " + moderator.UNSEALED_LABEL in result["answer"] and moderator.CONSULT_NOTE in result["answer"]
    assert result["history"][-1]["parts"][0]["text"].endswith("B로 6항을 다시 쓸까요?")   # no footnote in the model's history


def test_consult_falls_back_to_reviewers_when_the_moderator_is_off(rt, tmp_path, monkeypatch):
    monkeypatch.setattr(rt.cfg.conversation, "moderator", "off")
    provider = ScriptedProvider({"request-router": [{"mode": "CONSULT", "reason": "질문"}],
                                 "syntax-scope-reviewer": [R.syntax()], "oa-strategy-reviewer": [R.oa()]})
    monkeypatch.setattr(runtime, "build_runtime", lambda *a, **kw: rt)
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **kw: provider)
    request = dict(text=QUESTION, material=USER_CLAIM, files=[], history=[], model="conversation-model")
    result = chat.routed_turn(provider, rt.cfg, None, request, tmp_path, EventWriter(tmp_path / "events.jsonl"))
    assert result["effective_mode"] == "REVIEW_ONLY" and provider.calls[0].model == "conversation-model"


def test_pipeline_result_gets_an_answer_above_the_verbatim_report(rt, request_indep, tmp_path, monkeypatch):
    pipeline = ScriptedProvider(R.happy_script(dependent=False))
    talk = ScriptedProvider({moderator.MODERATOR: ["요청하신 독립항을 작성했고 잠정 LOCK까지 통과했습니다.\n\n## 최종안"]})
    monkeypatch.setattr(routing, "classify_request", lambda *a: RouteDecision(mode="AUTHORING_DRAFT", reason="작성 요청"))
    monkeypatch.setattr(runtime, "build_runtime", lambda *a, **kw: rt)
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **kw: pipeline)
    request = dict(text="청구항 작성해줘", material="", files=request_indep.invention_sources, history=[], model="m")
    result = chat.routed_turn(talk, rt.cfg, None, request, tmp_path, EventWriter(tmp_path / "events.jsonl"))
    state = rt.store.load_state(result["run_id"])
    report = render_chat_report(state)
    assert result["answer"].startswith("## 답변\n\n요청하신 독립항을 작성했고")
    assert result["answer"].endswith(report) and "## 후보(미검수)" in result["answer"]   # the report itself is untouched
    deliver = talk.calls[0]
    assert deliver.stage == "DELIVER" and "청구항 작성해줘" in deliver.packet_text and report in deliver.packet_text
    # Run web-c90e0c66: with no drawings in hand the moderator called sourced limitations `근거 미확인` on its own.
    assert not deliver.images and "스스로 판단하지 않는다" in deliver.system_instruction
    assert (rt.cfg.path("runs_dir") / result["run_id"] / "report-chat.md").read_text(encoding="utf-8") == result["answer"]


def test_failed_delivery_never_loses_the_result(rt, request_indep, tmp_path, monkeypatch):
    pipeline = ScriptedProvider(R.happy_script(dependent=False))
    monkeypatch.setattr(routing, "classify_request", lambda *a: RouteDecision(mode="AUTHORING_DRAFT", reason="작성 요청"))
    monkeypatch.setattr(runtime, "build_runtime", lambda *a, **kw: rt)
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **kw: pipeline)
    request = dict(text="청구항 작성해줘", material="", files=request_indep.invention_sources, history=[], model="m")
    result = chat.routed_turn(ScriptedProvider({}), rt.cfg, None, request, tmp_path, EventWriter(tmp_path / "events.jsonl"))
    assert result["answer"].startswith(render_chat_report(rt.store.load_state(result["run_id"])))
    assert "모더레이터 답변 생성 실패" in result["answer"] and result["outcome"] == "DRAFT_CLAIM_LOCK"


def _halted_run(rt, request_indep):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    state = engine.run(engine.start(request_indep, "halted-run"))
    state.candidate.draft_claim_lock = None
    state.halt = Halt(stage="PICTURE", role="picture-claim-reconstruction-reviewer", kind="REVIEW", message="역구성 REVIEW")
    rt.store.save_state(state)
    return state


def test_message_on_a_halted_run_is_handed_back_as_its_decision(rt, request_indep, tmp_path, monkeypatch):
    _halted_run(rt, request_indep)
    provider = ScriptedProvider({moderator.MODERATOR: [{"mode": "AUTHORING_DRAFT", "reason": "중지 사유에 대한 결정", "resumes_halt": True}]})
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **k: pytest.fail("재개는 같은 run의 재개 경로가 맡는다"))
    request = dict(text="그 방향으로 진행해", files=[], history=[], model="m", run_id="halted-run",
                   ui_hints={"halted": {"stage": "PICTURE", "kind": "REVIEW"}})
    result = chat.routed_turn(provider, rt.cfg, None, request, tmp_path, EventWriter(tmp_path / "events.jsonl"))
    assert result["resume_halted"] and result["run_id"] == "halted-run" and result["answer"] == ""


def test_a_request_for_other_claims_on_a_halted_edit_starts_a_new_run(rt, request_indep, tmp_path, monkeypatch):
    """Run web-6be7454f: sent again after the target fix, the request resumed the run sealed with new claims at 2~12."""
    from claim_agent import conversation_pipeline
    from claim_agent.models.state import BaselineSet

    state = _halted_run(rt, request_indep)
    state.baseline_set = BaselineSet(record_id="bl-1", source_path="", sha256="x", claims=[], edit_targets=list(range(2, 13)),
                                     chain_nos=[1], chain_text="")
    rt.store.save_state(state)
    route = {"mode": "AUTHORING_DRAFT", "reason": "세트 끝에 추가", "resumes_halt": True, "dependent": True,
             "dependent_target": "기술기여가 확인된 후보", "authoring_scope": "EXISTING_SET_EDIT"}
    class NewRun(Exception):
        pass

    started = []

    def new_run(rt_, provider, request, route, *a, **k):
        started.append(route)
        raise NewRun

    monkeypatch.setattr(runtime, "make_provider", lambda *a, **k: None)
    monkeypatch.setattr(conversation_pipeline, "run_pipeline", new_run)
    request = dict(text="1~12항에서 놓친 특허 포인트가 있는지 확인하고 추가 종속항들을 작성해줘.", files=[], history=[], model="m",
                   run_id="halted-run", ui_hints={"halted": {"stage": "DEP_ARCHITECT", "kind": "REVIEW"}})
    with pytest.raises(NewRun):
        chat.routed_turn(ScriptedProvider({moderator.MODERATOR: [route]}), rt.cfg, None, request, tmp_path, EventWriter(tmp_path / "events.jsonl"))
    assert len(started) == 1 and not started[0].resumes_halt
    proceed = dict(request, text="그 방향으로 진행해")
    result = chat.routed_turn(ScriptedProvider({moderator.MODERATOR: [route]}), rt.cfg, None, proceed, tmp_path, EventWriter(tmp_path / "events.jsonl"))
    assert result["resume_halted"] and len(started) == 1                 # no claim words: still the halted run's decision


def test_a_halted_run_that_read_named_parents_as_targets_is_not_resumed(rt, request_indep, tmp_path, monkeypatch):
    """Run web-54630bf2: routed before log #18, its route kept 14·15 as targets of a new design however often it resumed."""
    from claim_agent import conversation_pipeline

    ask = "이에 관한 14OR15 항의 종속항을 추가해줘."
    state = _halted_run(rt, request_indep)
    state.request.update(request_text="## 현재 요청\n\n" + ask, dependent=True, dependent_target="14,15", named_parents=[])
    rt.store.save_state(state)
    class NewRun(Exception):
        pass

    started = []

    def new_run(rt_, provider, request, route, *a, **k):
        started.append(route)
        raise NewRun

    monkeypatch.setattr(runtime, "make_provider", lambda *a, **k: None)
    monkeypatch.setattr(conversation_pipeline, "run_pipeline", new_run)
    route = {"mode": "AUTHORING_DRAFT", "reason": "중지 사유에 대한 결정", "resumes_halt": True}
    request = dict(text="하기 재개", files=[], history=[], model="m", run_id="halted-run", ui_hints={"halted": {"stage": "ARCHITECT", "kind": "REVIEW"}})
    with pytest.raises(NewRun):
        chat.routed_turn(ScriptedProvider({moderator.MODERATOR: [route]}), rt.cfg, None, request, tmp_path, EventWriter(tmp_path / "events.jsonl"))
    assert len(started) == 1 and not started[0].resumes_halt


def test_question_on_a_halted_run_is_answered_and_the_run_keeps_waiting(rt, request_indep, tmp_path, monkeypatch):
    _halted_run(rt, request_indep)
    provider = ScriptedProvider({moderator.MODERATOR: [{"mode": "CONSULT", "reason": "멈춘 이유 질문"}, "역구성에서 한 가지가 달리 읽혀 멈췄습니다."]})
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **k: pytest.fail("질문에 파이프라인을 돌림"))
    request = dict(text="왜 멈췄어?", files=[], history=[], model="m", run_id="halted-run",
                   ui_hints={"halted": {"stage": "PICTURE", "kind": "REVIEW"}})
    result = chat.routed_turn(provider, rt.cfg, None, request, tmp_path, EventWriter(tmp_path / "events.jsonl"))
    assert result["effective_mode"] == "CONSULT" and not result.get("resume_halted")
    assert "중지: PICTURE REVIEW" in provider.calls[1].packet_text
    assert rt.store.load_state("halted-run").halt is not None


def test_the_moderator_sees_the_findings_a_halt_carries(rt, request_indep):
    """Regression for run replay2-e69bc8-165948: given only the halt message, the answer guessed a reason no reviewer gave."""
    state = _halted_run(rt, request_indep)
    long_text = "제3항: 제1 전력 전달부의 장착 부위 미결정 " + "가" * 1000
    state.halt.open_issues = [{"kind": "BLOCK", "code": "OPENNESS_NOT_IN_CONTRACT", "text": long_text, "return_to": None}] + \
                             [{"kind": "REVIEW", "code": f"C{n}", "text": f"지적 {n}"} for n in range(7)]
    summary = moderator.seal_summary(state)
    assert "중지 지적" in summary and "[OPENNESS_NOT_IN_CONTRACT] 제3항: 제1 전력 전달부의 장착 부위 미결정" in summary
    assert "가" * (moderator.ISSUE_CHARS + 10) not in summary and "- 외 2건" in summary


def test_unlocked_wording_never_fills_the_final_slot(rt, request_indep):
    state = _halted_run(rt, request_indep)
    for text in (render_chat_report(state), render_report(state)):
        final = text.split("## 최종안", 1)[1]
        assert final.lstrip().startswith("없음 — PICTURE REVIEW 판단 대기")
        assert "### 판단 대기 revision(LOCK 없음 · 사용 금지): 독립항" in final and "LOCK 없음; 미확정 문언" not in final
    assert render_report(state, claims_only=True).startswith("<!-- 판단 대기 revision")
