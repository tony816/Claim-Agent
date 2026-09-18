"""Feedback on a delivered claim gets an answer about the feedback, not only the redrawn claim set.

Regression for web-dd908d8d (feedback box on run web-3f02c4ec): the user pasted a revised set (a reworded claim 8 and
claim 9 re-cited to claim 5) and asked "아래로 했는데 어떨지?". The resume rebuilt claim 9 on the old set for 4.5 minutes,
dropped the other changes silently and answered with the set alone.
"""
from __future__ import annotations

from claim_agent import web
from claim_agent.conversation_pipeline import run_pipeline
from claim_agent.pipeline.claimtext import parse_claim_set, proposal_outside_targets
from claim_agent.pipeline.engine import FEEDBACK_INSTRUCTION, Decision
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.store.report import render_chat_report
from claim_agent.tui_support import resume_command  # noqa: F401  (web resume builds this command)
from tests.test_existing_set_edit import BASELINE, EDITED_9, _conversation, _route, edit_roles, edit_script  # noqa: F401
from tests.test_web import wait_for  # noqa: F401

from . import scripted_roles as R

REWORDED_8 = "【청구항 8】\n제5항에 있어서,\n상기 구속부는 동일한 구속 영역을 형성하는 다른 구속부와 함께 케이블과 중간 끼워맞춤을 이루는 케이블 클립 홀더."
RECITED_9 = "【청구항 9】\n제5항에 있어서,\n상기 구속부는 둘레면 중 케이블과 접촉되는 부분이 오목면으로 형성되는 케이블 클립 홀더."


def _edit_run(rt, request_indep, tmp_path, provider=None):
    request, blocks, paths, folder = _conversation(tmp_path, request_indep, "edit")
    result = run_pipeline(rt, provider or ScriptedProvider(edit_script()), request, _route(blocks), blocks, paths, folder)
    return rt.store.load_state(result["run_id"])


def test_proposal_changes_outside_the_edit_are_listed():
    base = [dict(claim_no=c.claim_no, parent_nos=c.parent_nos, text=c.text) for c in parse_claim_set(BASELINE)]
    changes = proposal_outside_targets(base, [9], "아래로 했는데 어떨지?\n\n---\n\n" + REWORDED_8 + "\n\n" + RECITED_9)
    assert changes == ["제8항: 문언 변경", "제9항(편집 대상): 인용 제8항 → 제5항"]
    unchanged_8 = next(c.text for c in parse_claim_set(BASELINE) if c.claim_no == 8)
    assert proposal_outside_targets(base, [9], unchanged_8 + "\n\n" + EDITED_9) == []      # only the target, same citation


def test_feedback_box_answers_at_once_when_the_paste_changes_the_read_only_set(rt, request_indep, tmp_path, monkeypatch, edit_roles):  # noqa: F811  (pytest fixture, not a redefinition)
    state = _edit_run(rt, request_indep, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    config = tmp_path / "config.json"
    config.write_text(rt.cfg.model_dump_json(), encoding="utf-8")
    workspace = web.Workspace(rt.cfg.project_root, config)
    workspace.folder = tmp_path / "web-ui"
    workspace.sessions = {}
    spawned = []
    monkeypatch.setattr(workspace, "execute", lambda sid, job, command: spawned.append(command))
    try:
        sid = workspace.create()["id"]
        workspace.sessions[sid]["run_id"] = state.run_id
        workspace.resume(sid, {"kind": "style", "text": "아래로 했는데 어떨지?\n\n---\n\n" + REWORDED_8 + "\n\n" + RECITED_9})
        answer = workspace.snapshot(sid)["messages"][-1]
        assert spawned == [] and answer["status"] == "review" and answer["role"] == "assistant"
        assert "파이프라인을 실행하지 않았습니다" in answer["text"] and "- 제8항: 문언 변경" in answer["text"]
        assert "제9항(편집 대상): 인용 제8항 → 제5항" in answer["text"] and "【청구항 1】` 머리가 없습니다" in answer["text"]
    finally:
        workspace.close()


def test_resumed_answer_leads_with_what_became_of_the_feedback(rt, request_indep, tmp_path, edit_roles):  # noqa: F811  (pytest fixture, not a redefinition)
    def design_with_account(spec):
        env = R.dep_architect()(spec)
        env["report_markdown"] += "\n\n### 사용자 피드백 처리\n\n- 질문한 제9항 문안: 인과사슬이 닫혀 그대로 채택\n- 이탈 허용 조건: 원자료 근거가 없어 미반영\n\n## 다음 절\n"
        return env

    script = edit_script()
    script = {role: steps * 2 for role, steps in script.items()}
    script["dependent-claim-strategy-architect"] = [R.dep_architect(), design_with_account]
    provider = ScriptedProvider(script)
    state = _edit_run(rt, request_indep, tmp_path, provider)
    assert state.feedback is None and not render_chat_report(state).startswith("## 피드백 반영")
    engine = rt.engine(provider)
    state = engine.resume(state.run_id, Decision(text="아래로 했는데 어떨지?\n\n" + EDITED_9, action="redesign"))
    assert state.outcome == "BASELINE_SET+DRAFT_DEPENDENT_SET_LOCK", state.halt
    chat = render_chat_report(state)
    assert chat.startswith("## 피드백 반영") and chat.index("## 피드백 반영") < chat.index("## 최종안")
    assert "- 적용 경로: 재설계" in chat and "- 제안 제9항: 제안 문언 그대로 채택" in chat
    assert "DEP_ARCHITECT 역할의 피드백 처리: - 질문한 제9항 문안" in chat and "원자료 근거가 없어 미반영" in chat and "다음 절" not in chat
    resumed = [c for c in provider.calls if c.role == "dependent-claim-strategy-architect"][-1]
    assert FEEDBACK_INSTRUCTION.strip() in resumed.packet_text
    blind = [c for c in provider.calls if c.role == "blind-claim-reconstruction-reviewer"][-1]
    assert "사용자 피드백 처리" not in blind.packet_text                           # blind stays sealed from the discussion
