"""Automatic patent routing and gate enforcement, with offline model responses."""
from __future__ import annotations

import json

import pytest

from claim_agent import chat, routing, runtime
from claim_agent.conversation_pipeline import intake, run_pipeline
from claim_agent.live_events import EventReader, EventWriter
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.routing import RouteDecision, classify_request
from tests import scripted_roles as R


def test_semantic_router_receives_context_and_current_contract(project_root):
    blocks = [{"id": "original", "origin": "user", "text": "청구항 2의 방향이 불명확하다."}]
    provider = ScriptedProvider({"request-router": [{
        "mode": "AUTHORING_DRAFT", "reason": "이전 청구항의 후속 수정 요청",
        "dependent": True, "dependent_target": "2", "claim_source_id": "original",
    }]})
    decision = classify_request(provider, project_root, "test-model", "그렇게 바꿔줘", blocks, "route-test")
    assert decision.dependent and decision.dependent_target == "2"
    call = provider.calls[0]
    assert (project_root / "CLAUDE.md").read_text(encoding="utf-8") in call.sources_block
    assert "그렇게 바꿔줘" in call.packet_text and "original" in call.packet_text
    assert call.tools is None and call.use_cache   # contract block is the cacheable prefix


@pytest.mark.parametrize("text", ["청구항 수정안을 작성해줘", "suggest me revised claim 2", "특허적인 의견을 알려줘"])
def test_plain_chat_route_cannot_accept_explicit_patent_requests(project_root, text):
    provider = ScriptedProvider({"request-router": [{"mode": "CHAT", "reason": "잘못된 분류"}]})
    with pytest.raises(ValueError, match="일반 대화로 분류"):
        classify_request(provider, project_root, "test", text, [], "blocked-route")


@pytest.mark.parametrize("decision", [
    {"mode": "REVIEW_ONLY", "reason": "의견", "reviewers": []},
    {"mode": "AUTHORING_DRAFT", "reason": "작성", "dependent": True},
    {"mode": "REVIEW_ONLY", "reason": "의견", "reviewers": ["claim-drafter"]},
    {"mode": "REVIEW_ONLY", "reason": "의견", "reviewers": ["oa-strategy-reviewer"], "claim_source_id": "invented"},
])
def test_invalid_route_fails_closed(project_root, decision):
    provider = ScriptedProvider({"request-router": [decision]})
    with pytest.raises(ValueError):
        classify_request(provider, project_root, "test", "검토해줘", [], "invalid-route")


def test_harness_question_stays_meta(project_root):
    provider = ScriptedProvider({"request-router": [{"mode": "META", "reason": "프로그램 설정 감사"}]})
    decision = classify_request(provider, project_root, "test", "청구항 수정 시 하네스 코드를 어떻게 고쳐?", [], "meta")
    assert decision.mode == "META"


ADVICE_REQUEST = "1항에서 돌출부제거하고 튜브를 지지하는 것으로 변경하라는 지시가 있는데 그러면 부재들의 기능 구현이 안돼서 어떤 방법을 택해야할지?"


def test_manual_authoring_selection_is_only_a_hint_for_advice(project_root):
    provider = ScriptedProvider({"request-router": [{
        "mode": "REVIEW_ONLY", "reason": "변경 방향의 타당성과 선택 근거를 묻는 질문",
        "reviewers": ["syntax-scope-reviewer", "oa-strategy-reviewer"],
    }]})
    hints = dict(selected_mode="AUTHORING_DRAFT", dependent=True, target="2~8")
    route = classify_request(provider, project_root, "test", ADVICE_REQUEST, [], "advice", hints)
    assert route.mode == "REVIEW_ONLY" and not route.dependent
    packet = json.loads(provider.calls[0].packet_text)
    assert packet["current_request"] == ADVICE_REQUEST and packet["ui_hints"] == hints
    assert "변경 방향의 타당성" in provider.calls[0].system_instruction


def test_opinion_cannot_inherit_dependent_writing_options():
    with pytest.raises(ValueError, match="작성하지 않는 요청"):
        RouteDecision(mode="REVIEW_ONLY", reason="의견", reviewers=["oa-strategy-reviewer"],
                      dependent=True, dependent_target="2~8")


def request_data(request_indep):
    return dict(text="청구항과 종속항 2~4를 작성해줘", material="",
                files=request_indep.invention_sources, history=[], model="offline-test")


def test_chat_entry_runs_full_independent_and_dependent_harness(rt, request_indep, tmp_path, monkeypatch):
    folder = tmp_path / "automatic-draft"
    folder.mkdir()
    route = RouteDecision(mode="AUTHORING_DRAFT", reason="청구항 출력물 요청", dependent=True, dependent_target="2~4")
    provider = ScriptedProvider(R.happy_script())
    monkeypatch.setattr(routing, "classify_request", lambda *args: route)
    monkeypatch.setattr(runtime, "build_runtime", lambda *a, **kw: rt)
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(chat, "generate_turn", lambda *a: pytest.fail("청구항 요청이 일반 답변으로 우회함"))
    events = EventWriter(folder / "events.jsonl")
    result = chat.routed_turn(object(), rt.cfg, None, request_data(request_indep), folder, events)
    assert result["effective_mode"] == "AUTHORING_DRAFT"
    assert result["outcome"] == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK"
    assert result["status"] == "complete"
    roles = [c.role for c in provider.calls if c.phase == "main"]
    expected = ["claim-architect", "claim-drafter", "claim-style-adjuster", "claim-success-reviewer",
                "syntax-scope-reviewer", "oa-strategy-reviewer", "blind-claim-reconstruction-reviewer",
                "picture-claim-reconstruction-reviewer"]
    assert roles[:8] == expected
    assert roles[8:14] == ["dependent-claim-strategy-architect", *expected[1:6]]
    assert roles[14:] == expected[6:] * 3
    for call in provider.calls:
        if call.role == "blind-claim-reconstruction-reviewer":
            assert call.sources_block == "" and not call.images and not call.use_cache
            assert "현재 발명 원자료" not in call.packet_text
    state = rt.store.load_state(result["run_id"])
    assert state.candidate.draft_claim_lock and state.dependent.draft_set_lock
    assert not state.candidate.final_claim_lock
    assert any(e["kind"] == "route" for e in EventReader(events.path).read())
    assert json.loads((folder / "route.json").read_text(encoding="utf-8"))["contract_sha256"]


def test_failed_design_never_calls_drafter_or_plain_chat(rt, request_indep, tmp_path):
    folder = tmp_path / "blocked-design"
    folder.mkdir()
    request = request_data(request_indep)
    blocks, paths = intake(request, folder)
    provider = ScriptedProvider({"claim-architect": [R.architect(locked=False)]})
    result = run_pipeline(rt, provider, request, RouteDecision(mode="AUTHORING_DRAFT", reason="수정"), blocks, paths, folder)
    assert result["status"] == "review" and result["finish_reason"] == "PIPELINE_HALTED"
    assert [c.role for c in provider.calls] == ["claim-architect"]
    assert not rt.store.load_state(result["run_id"]).candidate.draft_claim_lock
    assert "## 중지 단계의 검토 내용" in result["answer"]
    assert rt.store.load_state(result["run_id"]).halt.report_markdown in result["answer"]


def test_opinion_uses_reviewer_report_without_authoring_or_locks(rt, tmp_path):
    folder = tmp_path / "opinion"
    folder.mkdir()
    request = dict(text="신규성과 진보성의 차이에 대한 특허 의견", files=[], history=[])
    blocks, paths = intake(request, folder)
    provider = ScriptedProvider({"oa-strategy-reviewer": [R.oa()]})
    route = RouteDecision(mode="REVIEW_ONLY", reason="특허 의견 요청", reviewers=["oa-strategy-reviewer"])
    result = run_pipeline(rt, provider, request, route, blocks, paths, folder)
    assert [c.role for c in provider.calls] == ["oa-strategy-reviewer"]
    assert "미제공 — 일반 의견 요청" in provider.calls[0].packet_text
    state = rt.store.load_state(result["run_id"])
    assert state.candidate.current.exact_text is None
    assert state.candidate.design_revision == "N/A"
    assert not state.candidate.draft_claim_lock and not state.candidate.final_claim_lock
    assert "## 검토 의견" in result["answer"]
    assert state.review_reports["oa-strategy-reviewer"] in result["answer"]
    assert "독립항 문언 없음 (설계 단계에서 중지)" not in result["answer"]


def test_intake_does_not_promote_model_answers_to_invention_sources(tmp_path):
    request = dict(text="이 자료로 검토", material="사용자 설명", files=[], history=[
        {"role": "user", "parts": [{"text": "발명 사실"}]},
        {"role": "model", "parts": [{"text": "모델이 추측한 효과"}]},
    ], reference="이전 생성 보고서")
    blocks, paths = intake(request, tmp_path)
    texts = [open(p, encoding="utf-8").read() for p in paths["invention_sources"]]
    assert "발명 사실" in texts and "사용자 설명" in texts
    assert "모델이 추측한 효과" not in texts and "이전 생성 보고서" not in texts
    assert len([b for b in blocks if b["origin"] == "assistant_reference"]) == 2


def test_automatic_followup_preserves_id_and_rechecks_new_revision(rt, request_indep, tmp_path):
    first = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    previous = first.run(first.start(request_indep, "original-auto-run"))
    old_lock = previous.candidate.draft_claim_lock
    folder = tmp_path / "followup-auto-run"
    folder.mkdir()
    request = dict(text="같은 발명의 연결 관계를 고쳐줘", files=[], history=[])
    blocks, paths = intake(request, folder, previous)
    route = RouteDecision(mode="AUTHORING_DRAFT", reason="이전 청구항 수정")
    provider = ScriptedProvider(R.happy_script(dependent=False))
    result = run_pipeline(rt, provider, request, route, blocks, paths, folder, previous)
    current = rt.store.load_state(previous.run_id)
    assert result["run_id"] == previous.run_id
    assert current.candidate.revision == "r2" and current.candidate.design_revision == "d2"
    assert current.candidate.draft_claim_lock != old_lock
    assert len([c for c in provider.calls if c.phase == "main"]) == 8
    assert current.request["user_lock"] == previous.request["user_lock"]


def test_router_failure_never_generates_an_answer(rt, tmp_path, monkeypatch):
    def fail(*args):
        raise ValueError("분류 실패")
    monkeypatch.setattr(routing, "classify_request", fail)
    monkeypatch.setattr(chat, "generate_turn", lambda *a: pytest.fail("분류 실패 후 일반 대화로 우회함"))
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **k: pytest.fail("분류 실패 후 에이전트 호출"))
    with pytest.raises(ValueError, match="분류 실패"):
        chat.routed_turn(object(), rt.cfg, None, dict(text="수정해줘", files=[], history=[], model="test"), tmp_path, EventWriter(tmp_path / "events.jsonl"))


def test_role_contract_change_restarts_and_stamps_new_source_set(rt, request_indep):
    from claim_agent.pipeline.engine import Decision
    from claim_agent.provider.base import ProviderError
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    previous = engine.run(engine.start(request_indep, "contract-update"))
    old_records = set(previous.records)
    updated = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    updated.source_set_id = rt.source_set_id + "-updated"
    with pytest.raises(ProviderError, match="ARCHITECT"):
        updated.resume(previous.run_id, Decision(restart_from="STYLE"))
    current = updated.resume(previous.run_id, Decision(restart_from="ARCHITECT"))
    assert current.source_set_id == updated.source_set_id
    assert current.outcome == "DRAFT_CLAIM_LOCK"
    assert all(current.records[rid].stale for rid in old_records)
    assert all(ref.source_set_id == updated.source_set_id for rid, ref in current.records.items() if rid not in old_records)


def test_project_instructions_reach_router_chat_and_pipeline_but_not_materials(project_root, rt, request_indep, tmp_path, monkeypatch):
    from claim_agent.conversation_pipeline import effective_request_text

    provider = ScriptedProvider({"request-router": [{"mode": "META", "reason": "설정 질문"}]})
    classify_request(provider, project_root, "test", "설정을 알려줘", [], "proj-route", None, "청구항만 출력한다.")
    packet = json.loads(provider.calls[0].packet_text)
    assert packet["project_instructions"] == "청구항만 출력한다." and "project_instructions" in provider.calls[0].system_instruction
    spec = chat._chat_spec("m", [], {"role": "user", "parts": [{"text": "안녕"}]}, "  존댓말로 답한다.  ")
    assert spec.system_instruction.startswith(chat.SYSTEM) and "존댓말로 답한다." in spec.system_instruction and "기술적 사실로 취급" in spec.system_instruction
    assert chat._chat_spec("m", [], {"role": "user", "parts": [{"text": "안녕"}]}, "").system_instruction == chat.SYSTEM
    request = {**request_data(request_indep), "instructions": "종속항은 2~4항까지.", "user_lock": "제1항 문언 유지",
               "attachments": [dict(path=p, category="invention", name="x", project_file_id="f") for p in request_indep.invention_sources]}
    assert effective_request_text(request).startswith("## 프로젝트 지침") and effective_request_text(request).endswith(request["text"])
    assert effective_request_text({"text": "그대로"}) == "그대로"
    folder = tmp_path / "proj-draft"
    folder.mkdir()
    blocks, paths = intake(request, folder)
    assert not any("종속항은 2~4항까지" in b["text"] for b in blocks)          # instructions are never a material block
    roles = ScriptedProvider({"claim-architect": [R.architect(locked=False)]})
    result = run_pipeline(rt, roles, request, RouteDecision(mode="AUTHORING_DRAFT", reason="작성"), blocks, paths, folder)
    state = rt.store.load_state(result["run_id"])
    assert state.request["user_lock"] == "제1항 문언 유지"
    assert "## 프로젝트 지침" in roles.calls[0].packet_text and "종속항은 2~4항까지." in roles.calls[0].packet_text
    assert "<<<MATERIAL" in roles.calls[0].packet_text and "종속항은 2~4항까지" not in roles.calls[0].packet_text.split("## RUN_HEADER")[0]
