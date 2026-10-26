"""Free work mode (CLAUDE.md 자유 작업 모드): an ungated general agent, and gate roles run on one message by a click."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claim_agent import free_work, runtime, web
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.routing import RouteDecision
from tests import scripted_roles as R
from tests.test_web import wait_for

DRAFT = ("본체와 돌출부의 관계를 독립항에 묶었습니다.\n\n"
         "【청구항 1】\n본체; 및\n상기 본체에서 돌출된 복수의 돌출부;를 포함하는,\n클립 홀더.\n\n"
         "【청구항 2】\n제1항에 있어서,\n상기 돌출부는 탄성 재질인,\n클립 홀더.\n\n"
         "※ 돌출부 개수는 근거 미확인이라 열어 두었습니다.")
USER_FACT = "발명: 본체에 복수의 돌출부가 있는 클립 홀더"


def test_review_target_takes_the_claim_block_only():
    target = free_work.review_target(DRAFT)
    assert target.startswith("【청구항 1】") and target.endswith("클립 홀더.") and "【청구항 2】" in target
    assert "묶었습니다" not in target and "※" not in target
    assert free_work.review_target("설명\n\n**【청구항 1】**\n본체를 포함하는,\n장치.") == "【청구항 1】\n본체를 포함하는,\n장치."
    assert free_work.review_target("설명\n```\n【청구항 1】\n본체를 포함하는 장치.\n```\n뒤 설명") == "【청구항 1】\n본체를 포함하는 장치."
    assert free_work.review_target("청구항은 아직 쓰지 않았습니다.") is None
    assert free_work.review_scope(target) == "DEPENDENT_SET"
    assert free_work.review_scope("【청구항 1】\n본체를 포함하는 장치.") == "INDEPENDENT"


def test_the_router_can_name_the_blind_reviewer_for_an_opinion():
    route = RouteDecision(mode="REVIEW_ONLY", reason="문언만 읽고 복원", reviewers=["blind-claim-reconstruction-reviewer"])
    assert route.reviewers == ["blind-claim-reconstruction-reviewer"]


def test_free_turn_answers_without_the_router_and_labels_the_draft(rt, tmp_path):
    source = tmp_path / "발명.txt"
    source.write_text("프로젝트 발명 자료", encoding="utf-8")
    provider = ScriptedProvider({free_work.FREE_ROLE: [DRAFT]})      # any routing or claim role call would find no script
    request = dict(text="청구항 초안 써줘", files=[], history=[], model="free-model", instructions="용어는 돌출부로 쓴다", work_mode=free_work.FREE,
                   project_attachments=[dict(path=str(source), name="발명.txt", category="invention")])
    result = free_work.free_turn(provider, rt.cfg, request)
    (call,) = provider.calls
    assert call.role == free_work.FREE_ROLE and call.model == "free-model" and call.system_instruction.startswith(free_work.FREE_SYSTEM)
    assert "프로젝트 발명 자료" in call.packet_text and "용어는 돌출부로 쓴다" in call.system_instruction
    assert result["effective_mode"] == "FREE" and result["answer"].startswith(free_work.UNREVIEWED_LABEL)
    assert result["history"][-1]["parts"][0]["text"] == DRAFT               # the agent's next turn sees its bare answer
    assert "프로젝트 발명 자료" not in json.dumps(result["history"], ensure_ascii=False)   # preset files are resent, not stored


def _review_request(role: str) -> dict:
    history = [{"role": "user", "parts": [{"text": USER_FACT}]}, {"role": "model", "parts": [{"text": DRAFT}]}]
    return dict(text=free_work.review_request_text([role]), files=[], attachments=[], history=history, model="m", instructions="", user_lock="",
                review=dict(roles=[role], claim_text=free_work.review_target(DRAFT)))


def test_review_click_runs_only_the_chosen_role_on_the_claim_block(rt, tmp_path, monkeypatch):
    provider = ScriptedProvider({"syntax-scope-reviewer": [R.syntax()]})
    monkeypatch.setattr(runtime, "build_runtime", lambda *a, **k: rt)
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **k: provider)
    folder = tmp_path / "request"
    folder.mkdir()
    result = free_work.review_turn(rt.cfg, None, _review_request("syntax-scope-reviewer"), folder)
    (call,) = provider.calls
    target = free_work.review_target(DRAFT)
    assert call.role == "syntax-scope-reviewer" and "design_revision: N/A" in call.packet_text and "review_scope: DEPENDENT_SET" in call.packet_text
    assert target in call.packet_text and USER_FACT in call.packet_text
    assert "묶었습니다" not in call.packet_text                          # the general agent's prose is neither target nor material
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "REVIEW_ONLY_DONE" and state.candidate.draft_claim_lock is None
    assert [m["category"] for m in state.material_meta].count("invention") == 1   # the click's request text is not material
    assert result["effective_mode"] == "REVIEW_ONLY" and result["answer"].startswith(free_work.REVIEW_NOTE)
    asked, answered = result["history"][-2:]
    assert asked["parts"][0]["text"].endswith(target) and answered["parts"][0]["text"] == result["answer"]


def test_blind_click_reads_the_independent_claim_alone_and_skips_picture(rt, tmp_path, monkeypatch):
    provider = ScriptedProvider({"blind-claim-reconstruction-reviewer": [R.blind()]})
    monkeypatch.setattr(runtime, "build_runtime", lambda *a, **k: rt)
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **k: provider)
    folder = tmp_path / "request"
    folder.mkdir()
    result = free_work.review_turn(rt.cfg, None, _review_request("blind-claim-reconstruction-reviewer"), folder)
    (call,) = provider.calls
    assert "mode: BLIND_SNAPSHOT" in call.packet_text and "【청구항 1】" in call.packet_text
    assert "【청구항 2】" not in call.packet_text and USER_FACT not in call.packet_text and "검수" not in call.packet_text
    assert free_work.BLIND_NOTE in result["answer"] and rt.store.load_state(result["run_id"]).outcome == "REVIEW_ONLY_DONE"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    workspace = web.Workspace(tmp_path)
    yield workspace
    workspace.close()


def test_free_conversation_sends_plainly_and_reviews_one_message(workspace, monkeypatch):
    requests = []

    def capture(sid, job, command):
        requests.append(json.loads(Path(command[command.index("--request") + 1]).read_text(encoding="utf-8")))
        job["done"] = True

    monkeypatch.setattr(workspace, "execute", capture)
    sid = workspace.create(None, free_work.FREE)["id"]
    workspace.start(sid, {"text": "초안 써줘", "target_mode": "range", "target_segments": "bogus"})   # composer options do not apply
    wait_for(lambda: len(requests) == 1)
    assert requests[0]["work_mode"] == free_work.FREE and "ui_hints" not in requests[0] and "material" not in requests[0]
    question, answer = workspace.sessions[sid]["messages"]
    assert question["mode"] == answer["mode"] == "FREE"
    answer.update(text=DRAFT, status="complete")
    with pytest.raises(ValueError, match="청구항"):
        workspace.review(sid, {"message": question["id"], "role": "syntax-scope-reviewer"})
    with pytest.raises(ValueError, match="검수 역할"):
        workspace.review(sid, {"message": answer["id"], "role": "claim-drafter"})
    workspace.review(sid, {"message": answer["id"], "role": "oa-strategy-reviewer"})
    wait_for(lambda: len(requests) == 2)
    assert requests[1]["review"] == dict(roles=["oa-strategy-reviewer"], claim_text=free_work.review_target(DRAFT), message_id=answer["id"])
    asked, placeholder = workspace.sessions[sid]["messages"][-2:]
    assert asked["mode"] == placeholder["mode"] == "REVIEW" and placeholder["execution_mode"] == "REVIEW_ONLY"
    assert workspace.set_work_mode(sid, free_work.GATED)["work_mode"] == free_work.GATED
    with pytest.raises(ValueError, match="자유 작업"):
        workspace.review(sid, {"message": answer["id"], "role": "oa-strategy-reviewer"})
    with pytest.raises(ValueError, match="작업 방식"):
        workspace.set_work_mode(sid, "turbo")
    assert workspace.create()["work_mode"] == free_work.GATED


def test_free_draft_then_review_through_the_chat_worker(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    config = tmp_path / "free-config.json"
    config.write_text(rt.cfg.model_dump_json(), encoding="utf-8")
    original = subprocess.Popen

    def scripted(command, **kwargs):
        if "claim_agent.chat" in command:
            program = ("from claim_agent import chat, runtime; from claim_agent.provider.scripted import ScriptedProvider; from tests import scripted_roles as R; "
                       "from tests.test_free_work import DRAFT; "
                       "chat.live_provider=lambda *a, **k: ScriptedProvider({'자유 작업': [DRAFT]}); "
                       "runtime.make_provider=lambda *a, **k: ScriptedProvider({'syntax-scope-reviewer': [R.syntax()]}); raise SystemExit(chat.main())")
            command = [sys.executable, "-u", "-c", program, *command[command.index("claim_agent.chat") + 1:]]
        return original(command, **kwargs)

    monkeypatch.setattr(web.subprocess, "Popen", scripted)
    workspace = web.Workspace(rt.cfg.project_root, config)
    workspace.folder = tmp_path / "free-web-ui"
    workspace.sessions = {}
    try:
        sid = workspace.create(None, free_work.FREE)["id"]
        workspace.start(sid, {"text": USER_FACT + " — 청구항 초안 써줘"})
        wait_for(lambda: not workspace.snapshot(sid)["running"], timeout=30)
        draft = workspace.snapshot(sid)["messages"][-1]
        assert draft["status"] == "complete" and draft["execution_mode"] == "FREE", draft["text"]
        assert draft["text"].startswith(free_work.UNREVIEWED_LABEL)
        workspace.review(sid, {"message": draft["id"], "role": "syntax-scope-reviewer"})
        wait_for(lambda: not workspace.snapshot(sid)["running"], timeout=30)
        review = workspace.snapshot(sid)["messages"][-1]
        assert review["status"] == "complete" and review["execution_mode"] == "REVIEW_ONLY", review["text"]
        assert review["text"].startswith(free_work.REVIEW_NOTE) and review["pipeline_run_id"]
        assert workspace.sessions[sid]["run_id"] is None                   # a review never becomes the conversation's run
        history = json.loads((workspace.directory(sid) / "history.json").read_text(encoding="utf-8"))["history"]
        assert [h["role"] for h in history] == ["user", "model", "user", "model"] and history[-1]["parts"][0]["text"] == review["text"]
    finally:
        workspace.close()
