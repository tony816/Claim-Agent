"""Free work mode (CLAUDE.md 자유 작업 모드): an ungated general agent, and gate roles run on one message's wording
before the agent fixes it from their reports and the user's feedback."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claim_agent import free_work, runtime, web
from claim_agent.pipeline.claimtext import parse_claim_set
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.routing import RouteDecision
from tests import scripted_roles as R
from tests.test_web import wait_for

DRAFT = ("본체와 돌출부의 관계를 독립항에 묶었습니다.\n\n"
         "【청구항 1】\n본체; 및\n상기 본체에서 돌출된 복수의 돌출부;를 포함하는,\n클립 홀더.\n\n"
         "【청구항 2】\n제1항에 있어서,\n상기 돌출부는 탄성 재질인,\n클립 홀더.\n\n"
         "※ 돌출부 개수는 근거 미확인이라 열어 두었습니다.")
USER_FACT = "발명: 본체에 복수의 돌출부가 있는 클립 홀더"
FIXED = ("- 바꾼 곳: 제2항의 '탄성 재질'을 돌출부 전체로 한정했습니다.\n\n"
         "【청구항 1】\n본체; 및\n상기 본체에서 돌출된 복수의 돌출부;를 포함하는,\n클립 홀더.\n\n"
         "【청구항 2】\n제1항에 있어서,\n상기 복수의 돌출부 각각은 탄성 재질로 이루어지는,\n클립 홀더.")


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


def _review_request(role: str, note: str = "", claims: str = DRAFT, history: list[dict] | None = None, user_lock: str = "") -> dict:
    history = history if history is not None else [{"role": "user", "parts": [{"text": USER_FACT}]}, {"role": "model", "parts": [{"text": DRAFT}]}]
    return dict(text=free_work.review_request_text([role], note), files=[], attachments=[], history=history, model="m", instructions="", user_lock=user_lock,
                review=dict(roles=[role], claim_text=free_work.review_target(claims), note=note))


def _offline(rt, monkeypatch, provider):
    monkeypatch.setattr(runtime, "build_runtime", lambda *a, **k: rt)
    monkeypatch.setattr(runtime, "make_provider", lambda *a, **k: provider)


def _blind_calls(provider, *marks: str) -> list:
    """The blind calls, one per mark found in its packet (they may run in parallel, in any order)."""
    blind = [c for c in provider.calls if c.role == "blind-claim-reconstruction-reviewer"]
    assert len(blind) == len(marks)
    return [next(c for c in blind if mark in c.packet_text) for mark in marks]


def _agent_calls(provider) -> list:
    return [c for c in provider.calls if c.role == free_work.FREE_ROLE]


def test_review_without_a_request_answers_with_the_review_itself(rt, tmp_path, monkeypatch):
    provider = ScriptedProvider({"oa-strategy-reviewer": [R.oa()]})       # no general agent turn is scripted
    _offline(rt, monkeypatch, provider)
    folder = tmp_path / "request"
    folder.mkdir()
    request = _review_request("oa-strategy-reviewer")
    result = free_work.review_turn(provider, rt.cfg, None, request, folder)
    (review,) = provider.calls
    target = free_work.review_target(DRAFT)
    assert review.role == "oa-strategy-reviewer" and target in review.packet_text and "사용자 요청:" not in review.packet_text
    assert result["revised"] is False and result["effective_mode"] == "REVIEW_ONLY"
    assert result["answer"].startswith(free_work.REVIEW_NOTE) and "## 검토 의견" in result["answer"] and "### oa-strategy-reviewer" in result["answer"]
    assert (rt.store.run_dir(result["run_id"]) / "report-chat.md").read_text(encoding="utf-8") == result["answer"]
    asked, reports = result["history"][-2:]
    assert result["history"][:-2] == request["history"]
    assert asked["parts"][0]["text"] == "[선택 검수 · OA 검수] 아래 문언을 검수해 줘.\n\n" + target and reports["parts"][0]["text"] == result["answer"]


def test_a_fix_request_steers_the_review_then_the_agent_fixes_the_wording(rt, tmp_path, monkeypatch):
    provider = ScriptedProvider({"syntax-scope-reviewer": [R.syntax()], free_work.FREE_ROLE: [FIXED]})
    _offline(rt, monkeypatch, provider)
    folder = tmp_path / "request"
    folder.mkdir()
    request = _review_request("syntax-scope-reviewer", "돌출부 지칭 위주로 보고 지적대로 고쳐 줘")
    result = free_work.review_turn(provider, rt.cfg, None, request, folder)
    review, fix = provider.calls
    target = free_work.review_target(DRAFT)
    assert review.role == "syntax-scope-reviewer" and "design_revision: N/A" in review.packet_text and "review_scope: DEPENDENT_SET" in review.packet_text
    assert target in review.packet_text and USER_FACT in review.packet_text
    assert "사용자 요청: 돌출부 지칭 위주로 보고 지적대로 고쳐 줘" in review.packet_text   # the request typed beside the tool steers the reviewer
    assert "묶었습니다" not in review.packet_text                        # the general agent's prose is neither target nor material
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "REVIEW_ONLY_DONE" and state.candidate.draft_claim_lock is None
    assert [m["category"] for m in state.material_meta].count("invention") == 1   # the tool's request text is not material
    # The general agent carries the request out with the reviewer's report right above its turn.
    assert fix.role == free_work.FREE_ROLE and fix.system_instruction.startswith(free_work.FREE_SYSTEM)
    assert "사용자 요청: 돌출부 지칭 위주로 보고 지적대로 고쳐 줘" in fix.packet_text and fix.packet_text.endswith(target)
    assert fix.history[:2] == request["history"] and fix.history[-1]["parts"][0]["text"].startswith(free_work.REVIEW_NOTE)
    assert "### syntax-scope-reviewer" in fix.history[-1]["parts"][0]["text"]
    # The request asked for a fix: the answer is the fixed claims with what changed, not the report.
    assert result["effective_mode"] == "REVIEW_ONLY" and result["revised"] is True
    assert result["answer"].startswith(free_work.REVISED_LABEL.format(labels="통사·범위 검수")) and FIXED in result["answer"]
    assert "## 검토 의견" not in result["answer"] and "※ 작업 소요" in result["answer"]
    assert (rt.store.run_dir(result["run_id"]) / "report-chat.md").read_text(encoding="utf-8") == result["answer"]
    asked, reports, fix_asked, fixed = result["history"][-4:]
    assert asked["parts"][0]["text"].startswith("[선택 검수 · 통사·범위 검수] 돌출부 지칭 위주로 보고 지적대로 고쳐 줘") and asked["parts"][0]["text"].endswith(target)
    assert reports["parts"][0]["text"] == fix.history[-1]["parts"][0]["text"]
    assert fix_asked["parts"][0]["text"] == fix.packet_text and fixed["parts"][0]["text"] == FIXED   # the bare fix, without labels


def test_a_question_beside_the_review_is_answered_without_rewriting_a_claim(rt, tmp_path, monkeypatch):
    answer = "명확성 위주로 보면 제2항의 `상기 돌출부`가 복수 중 어느 것인지 정해지지 않은 점이 첫 지적입니다 (OA 검수 보고서)."
    provider = ScriptedProvider({"oa-strategy-reviewer": [R.oa()], free_work.FREE_ROLE: [answer]})
    _offline(rt, monkeypatch, provider)
    folder = tmp_path / "request"
    folder.mkdir()
    result = free_work.review_turn(provider, rt.cfg, None, _review_request("oa-strategy-reviewer", "명확성 위주로 봐 줘"), folder)
    review, follow_up = provider.calls
    assert "사용자 요청: 명확성 위주로 봐 줘" in review.packet_text and "사용자 요청: 명확성 위주로 봐 줘" in follow_up.packet_text
    assert result["revised"] is False                                   # no claim block came back, so nothing was rewritten
    assert result["answer"].startswith(free_work.ANSWERED_LABEL.format(labels="OA 검수")) and answer in result["answer"]


STYLED = ("【청구항 1】\n본체; 및\n상기 본체에서 돌출되는 복수의 돌출부;를 포함하는,\n클립 홀더.\n\n"
          "【청구항 2】\n제1항에 있어서,\n상기 돌출부는 탄성 재질인,\n클립 홀더.")


def test_style_restyles_first_and_the_other_tools_review_its_wording(rt, tmp_path, monkeypatch):
    provider = ScriptedProvider({"claim-style-adjuster": [R.style("DEPENDENT_SET", text=STYLED)], "syntax-scope-reviewer": [R.syntax()]})
    _offline(rt, monkeypatch, provider)
    folder = tmp_path / "request"
    folder.mkdir()
    request = _review_request("syntax-scope-reviewer")
    request["review"]["roles"] = ["syntax-scope-reviewer", "claim-style-adjuster"]       # style runs first whatever the order
    result = free_work.review_turn(provider, rt.cfg, None, request, folder)
    style, syntax = [c for c in provider.calls if c.phase == "main"]     # style may first take its corpus tool phase
    target = free_work.review_target(DRAFT)
    assert style.role == "claim-style-adjuster" and "style_change_mode: REVIEW_ONLY_INPUT" in style.packet_text and target in style.packet_text
    assert not style.images and "DESIGN_GATE: LOCKED" not in style.packet_text
    assert syntax.role == "syntax-scope-reviewer" and STYLED in syntax.packet_text and "revision: r2" in syntax.packet_text
    assert target not in syntax.packet_text
    state = rt.store.load_state(result["run_id"])
    assert state.review_styled_text == STYLED and state.candidate.draft_claim_lock is None and state.outcome == "REVIEW_ONLY_DONE"
    # The restyled wording is the answer's draft: shown among the reports and kept as the next review target.
    assert result["revised"] is True and result["claim_text"] == STYLED
    assert free_work.STYLE_NOTE in result["answer"] and "### 스타일 조정 문언" in result["answer"] and STYLED in result["answer"]


def test_style_that_changes_nothing_leaves_the_target_as_it_was(rt, tmp_path, monkeypatch):
    target = free_work.review_target(DRAFT)
    provider = ScriptedProvider({"claim-style-adjuster": [R.style("DEPENDENT_SET", text=target)]})
    _offline(rt, monkeypatch, provider)
    folder = tmp_path / "request"
    folder.mkdir()
    result = free_work.review_turn(provider, rt.cfg, None, _review_request("claim-style-adjuster"), folder)
    assert result["revised"] is False and result["claim_text"] is None
    assert free_work.STYLE_UNCHANGED_NOTE in result["answer"] and "### 스타일 조정 문언" not in result["answer"]


def test_blind_reads_each_claim_with_its_parent_chain_and_skips_picture(rt, tmp_path, monkeypatch):
    provider = ScriptedProvider({"blind-claim-reconstruction-reviewer": [R.blind(), R.blind()], free_work.FREE_ROLE: [FIXED]})
    _offline(rt, monkeypatch, provider)
    folder = tmp_path / "request"
    folder.mkdir()
    result = free_work.review_turn(provider, rt.cfg, None, _review_request("blind-claim-reconstruction-reviewer", "형상 위주로 보고 고쳐 줘"), folder)
    first, second = _blind_calls(provider, "claim_scope: INDEPENDENT", "claim_scope: DEPENDENT_SINGLE")
    (fix,) = _agent_calls(provider)
    for call in (first, second):
        assert call.role == "blind-claim-reconstruction-reviewer" and "mode: BLIND_SNAPSHOT" in call.packet_text
        assert USER_FACT not in call.packet_text and "검수" not in call.packet_text and not call.history
        assert "형상 위주로" not in call.packet_text                     # the blind reader gets the wording alone, never the request
    assert "claim_scope: INDEPENDENT" in first.packet_text and "【청구항 1】" in first.packet_text and "【청구항 2】" not in first.packet_text
    assert "claim_scope: DEPENDENT_SINGLE" in second.packet_text and "target_claim_id: 2" in second.packet_text
    chain, claim = second.packet_text.split("parent_chain_text:\n")[1].split("\ntarget_claim_text:\n")
    assert chain.startswith("【청구항 1】") and "【청구항 2】" not in chain and claim.startswith("【청구항 2】")
    assert "형상 위주로" in fix.packet_text                               # the request goes to the follow-up instead
    assert free_work.BLIND_NOTE in result["answer"] and free_work.BLIND_NOTE_TEXT in result["answer"]
    state = rt.store.load_state(result["run_id"])
    assert state.outcome == "REVIEW_ONLY_DONE" and len(state.candidate.current.records) == 2


def test_blind_finds_parents_in_the_conversation_and_user_lock_and_never_guesses(rt, tmp_path, monkeypatch):
    # The user's case: 제5·6항 cite 제4항 (pasted in the conversation), which cites 제3·1항 (USER_LOCK, where drafts
    # without a number yet follow 제4항 and must not leak into its text).
    user_lock = ("【청구항 1】\n안착부; 및\n리드 지지부;를 포함하는,\n핵산 증폭 장치.\n\n【청구항 3】\n제1항에 있어서,\n상기 리드 지지부는 포고 핀을 포함하는,\n핵산 증폭 장치.\n\n"
                 "【청구항 4】\n제3항에 있어서,\n예전 문언인,\n핵산 증폭 장치.\n\n【청구항 】\n제항에 있어서,\n번호 없는 초안인,\n핵산 증폭 장치.")
    claim_4 = "【청구항 4】\n제3항에 있어서,\n상기 리드 지지부는 암형 하우징을 포함하는,\n핵산 증폭 장치."
    claims = ("【청구항 5】\n제4항에 있어서,\n상기 암형 하우징은 일 방향을 따라 연장되는 바닥부를 포함하는,\n핵산 증폭 장치.\n\n"
              "【청구항 6】\n제5항에 있어서,\n상기 바닥부에 홀이 형성되는,\n핵산 증폭 장치.\n\n"
              "【청구항 7】\n제7항에 있어서,\n상기 홀은 관통하는,\n핵산 증폭 장치.\n\n"
              "【청구항 9】\n제8항에 있어서,\n상기 홀은 원형인,\n핵산 증폭 장치.")
    history = [{"role": "user", "parts": [{"text": "4항을 인용하는 5항을 써 줘\n---\n\n" + claim_4}]}, {"role": "model", "parts": [{"text": "초안입니다.\n\n" + claims}]}]
    provider = ScriptedProvider({"blind-claim-reconstruction-reviewer": [R.blind(), R.blind()]})   # review only: no agent turn
    _offline(rt, monkeypatch, provider)
    folder = tmp_path / "request"
    folder.mkdir()
    result = free_work.review_turn(provider, rt.cfg, None, _review_request("blind-claim-reconstruction-reviewer", claims=claims, history=history, user_lock=user_lock), folder)
    five, six = _blind_calls(provider, "target_claim_id: 5", "target_claim_id: 6")
    assert not _agent_calls(provider) and result["revised"] is False
    chains = [call.packet_text.split("parent_chain_text:\n")[1].split("\ntarget_claim_text:\n")[0] for call in (five, six)]
    expected = [c.text for c in parse_claim_set(user_lock)[:2]] + [claim_4]
    assert chains[0] == "\n\n".join(expected)                             # the conversation's 제4항 wins over USER_LOCK's
    assert chains[1] == "\n\n".join([*expected, parse_claim_set(claims)[0].text])
    assert "번호 없는 초안" not in chains[0] and "예전 문언" not in chains[0]
    report = result["answer"]
    assert "#### 제9항" in report and "부모 제8항 없음" in report             # no chain, no snapshot, said so
    assert "#### 제7항" in report and "자기 자신" in report                  # a self-citing draft has no chain (and no loop)


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
    assert requests[1]["review"] == dict(roles=["oa-strategy-reviewer"], claim_text=free_work.review_target(DRAFT), message_id=answer["id"], note="")
    asked, placeholder = workspace.sessions[sid]["messages"][-2:]
    assert asked["mode"] == placeholder["mode"] == "REVIEW" and placeholder["execution_mode"] == "REVIEW_ONLY"
    assert asked["review"]["asked"] is False and placeholder["review"]["revise"] is False   # an empty input: the review only
    # Several tools picked in the panel, plus the request typed before Enter: one run, in the panel's order.
    workspace.review(sid, {"message": answer["id"], "roles": ["oa-strategy-reviewer", "syntax-scope-reviewer"], "text": " 명확성 위주로 "})
    wait_for(lambda: len(requests) == 3)
    assert requests[2]["review"]["roles"] == ["syntax-scope-reviewer", "oa-strategy-reviewer"] and requests[2]["review"]["note"] == "명확성 위주로"
    assert requests[2]["text"].endswith("사용자 요청: 명확성 위주로")
    assert workspace.sessions[sid]["messages"][-2]["text"].startswith("[선택 검수 · 통사·범위 검수, OA 검수] 명확성 위주로")
    fixed = workspace.sessions[sid]["messages"][-1]
    assert fixed["review"]["asked"] is True
    fixed.update(text=FIXED, status="complete")
    fixed["review"]["revise"] = True                                     # what execute records when the answer is fixed wording
    workspace.review(sid, {"message": fixed["id"], "role": "oa-strategy-reviewer"})        # a fixed answer is the next target
    wait_for(lambda: len(requests) == 4)
    assert requests[3]["review"]["claim_text"] == free_work.review_target(FIXED)
    fixed["review"] = dict(roles=["oa-strategy-reviewer"], source=answer["id"])          # an older review answer only quotes
    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        workspace.review(sid, {"message": fixed["id"], "role": "oa-strategy-reviewer"})
    with pytest.raises(ValueError, match="검수 역할"):
        workspace.review(sid, {"message": answer["id"], "roles": []})
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
        # Enter on an empty input: the review itself.
        workspace.review(sid, {"message": draft["id"], "role": "syntax-scope-reviewer"})
        wait_for(lambda: not workspace.snapshot(sid)["running"], timeout=30)
        review = workspace.snapshot(sid)["messages"][-1]
        assert review["status"] == "complete" and review["execution_mode"] == "REVIEW_ONLY", review["text"]
        assert review["text"].startswith(free_work.REVIEW_NOTE) and "## 검토 의견" in review["text"] and review["pipeline_run_id"]
        assert review["review"]["revise"] is False
        # Enter with a request: reviewed in that direction, then carried out (here a fix, so the answer is the wording).
        workspace.review(sid, {"message": draft["id"], "role": "syntax-scope-reviewer", "text": "지적대로 고쳐 줘"})
        wait_for(lambda: not workspace.snapshot(sid)["running"], timeout=30)
        fixed = workspace.snapshot(sid)["messages"][-1]
        assert fixed["status"] == "complete" and fixed["text"].startswith(free_work.REVISED_LABEL.format(labels="통사·범위 검수")), fixed["text"]
        assert DRAFT in fixed["text"] and "## 검토 의견" not in fixed["text"]   # the fixed wording; the report stays in the full report
        assert fixed["review"]["revise"] is True                         # so it can be the next review target
        assert workspace.sessions[sid]["run_id"] is None                   # a review never becomes the conversation's run
        history = json.loads((workspace.directory(sid) / "history.json").read_text(encoding="utf-8"))["history"]
        # draft asked/answered, review asked/answered, then review asked/answered and the request asked/answered
        assert [h["role"] for h in history] == ["user", "model"] * 4 and history[-1]["parts"][0]["text"] == DRAFT
        assert history[3]["parts"][0]["text"] == review["text"] and history[5]["parts"][0]["text"].startswith(free_work.REVIEW_NOTE)
    finally:
        workspace.close()


def test_feedback_phrases_default_save_validate_and_reset(workspace):
    from claim_agent import phrases

    first = workspace.phrases()
    assert first["custom"] is False and first["phrases"] == phrases.defaults() and len(first["phrases"]) == len(phrases.DEFAULT_PHRASES)
    kept = first["phrases"][:2] + [dict(group=" ", text="  돌출부를 '걸림 돌기'로 통일해 줘. "), dict(id="bad id!", group="용어", text="상기를 빼 줘.")]
    saved = workspace.save_phrases(kept)
    assert saved["custom"] is True and [p["text"] for p in saved["phrases"]][2:] == ["돌출부를 '걸림 돌기'로 통일해 줘.", "상기를 빼 줘."]
    assert saved["phrases"][2]["group"] == phrases.DEFAULT_GROUP and saved["phrases"][3]["id"] != "bad id!"
    assert workspace.phrases() == saved                                  # kept on this computer, read back as saved
    for bad in ([dict(text="   ")], [dict(text="x" * (phrases.MAX_TEXT + 1))], [dict(text="a")] * (phrases.MAX_PHRASES + 1), "not a list"):
        with pytest.raises(ValueError):
            workspace.save_phrases(bad)
    assert workspace.phrases() == saved                                  # a refused save leaves the list as it was
    (workspace.folder / phrases.FILE).write_text("{broken", encoding="utf-8")
    assert workspace.phrases()["custom"] is False                       # an unreadable file falls back to the defaults
    assert workspace.reset_phrases() == dict(phrases=phrases.defaults(), custom=False)
