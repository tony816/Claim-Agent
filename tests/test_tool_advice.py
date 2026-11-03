"""자유 작업의 도구 추천과 평가 폼: the recommendation after each free-mode answer (agent, or the code's rules when the call
fails), the tools the user then ran, and the evaluation records that measure the app."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from claim_agent import evaluations, free_work, tool_advice, web
from claim_agent.graph import caselaw_tool
from claim_agent.provider.scripted import ScriptedProvider
from tests.test_free_work import DRAFT
from tests.test_web import wait_for

CLAIMS = free_work.review_target(DRAFT)
FUNCTIONAL = ("【청구항 1】\n본체; 및\n상기 본체에 고정되도록 형성된 돌출부;를 포함하는,\n클립 홀더.\n\n"
              "【청구항 2】\n제1항에 있어서,\n상기 돌출부는 상부에 홈을 갖는,\n클립 홀더.")
PHRASES = [dict(id="p1", group="확인", text="원자료에 근거가 없는 부분을 표시하고 발명자에게 물을 질문을 정리해 줘."),
           dict(id="p2", group="검수 반영", text="위 검수 지적을 반영해서 청구항을 고쳐 줘.")]


def _request(**over) -> dict:
    return dict(answer=DRAFT, asked="초안 써줘", kind="FREE", claims=CLAIMS, target="m1", ran=[], materials=dict(files=0, drawings=0),
                phrases=PHRASES) | over


def test_the_catalog_is_the_panels_tools():
    assert set(tool_advice.TOOLS) == set(free_work.REVIEW_ROLES) | set(caselaw_tool.TOOLS)
    assert set(evaluations.FORM_TOOLS) == set(tool_advice.TOOLS) | {tool_advice.PHRASE}


def test_signals_and_rules_recommend_from_the_answer_alone():
    sig = tool_advice.signals(FUNCTIONAL, "근거 미확인 하나, 근거 미확인 둘")
    assert sig["claims"] == 2 and sig["independent"] == 1 and sig["dependent"] == 1 and sig["unconfirmed"] == 2
    assert sig["functional"] == ["되도록"] and "상부" in sig["shape"] and "홈" in sig["shape"]
    picked = tool_advice.rules(_request(claims=FUNCTIONAL), sig)
    assert [t["tool"] for t in picked["tools"]] == ["oa-strategy-reviewer", "syntax-scope-reviewer"]   # no material: no 역피처
    assert picked["phrases"] == [dict(phrase_id="p1", text=PHRASES[0]["text"], reason="답에 '근거 미확인'이 2곳 있습니다.")]
    with_drawings = tool_advice.rules(_request(claims=FUNCTIONAL, materials=dict(files=1, drawings=1)), sig)
    assert with_drawings["tools"][-1]["tool"] == "blind-claim-reconstruction-reviewer"
    nothing = tool_advice.rules(_request(claims=""), tool_advice.signals("", "청구항 없이 설명만"))
    assert nothing["tools"] == [] and "청구항 블록이 없어" in nothing["summary"]


def test_advise_keeps_what_can_run_and_falls_back_to_rules(rt):
    reply = dict(summary="'복수의 돌출부'의 분배가 불분명합니다.", tools=[
        dict(tool="syntax-scope-reviewer", reason="'복수의 돌출부'와 '상기 돌출부'", focus="돌출부 지칭 위주로"),
        dict(tool="syntax-scope-reviewer", reason="중복"),
        dict(tool="claim-drafter", reason="없는 도구"),
        dict(tool="find", reason="판례", focus=""),                       # needs the finding to look for
        dict(tool="enforce", reason="권리행사", focus="쓰지 않는 말"),
        dict(tool="oa-strategy-reviewer", reason="네 번째는 넘친다")],
        phrases=[dict(phrase_id="p2", text="모델이 바꿔 쓴 문장", reason="반영"), dict(phrase_id="nope", text="  돌출부 개수를 열어 둔 이유를 적어 줘. ")])
    provider = ScriptedProvider({tool_advice.ROLE: [reply]})
    result = tool_advice.advise(provider, rt.cfg, _request())
    (call,) = provider.calls
    assert call.role == tool_advice.ROLE and call.json_schema and "## 저장된 피드백 멘트\n- p1:" in call.packet_text
    assert "청구항 2개 (독립 1, 종속 1)" in call.packet_text and "클립 홀더." in call.packet_text
    assert result["source"] == "agent" and result["error"] == "" and result["usage"]["calls"] == 1
    assert [t["tool"] for t in result["tools"]] == ["syntax-scope-reviewer", "enforce", "oa-strategy-reviewer"]
    assert result["tools"][0]["focus"] == "돌출부 지칭 위주로" and result["tools"][1]["focus"] == ""
    # A saved phrase is its saved text; an unknown id is a new phrase.
    assert result["phrases"] == [dict(phrase_id="p2", text=PHRASES[1]["text"], reason="반영"),
                                 dict(phrase_id="", text="돌출부 개수를 열어 둔 이유를 적어 줘.", reason="")]
    # No claims: nothing that needs a claim block survives.
    provider = ScriptedProvider({tool_advice.ROLE: [reply]})
    assert [t["tool"] for t in tool_advice.advise(provider, rt.cfg, _request(claims=""))["tools"]] == []
    # A broken answer or a failed call: the code's own recommendation, with the reason kept.
    for item in ({"tools": "not a list"}, lambda spec: (_ for _ in ()).throw(RuntimeError("quota"))):
        result = tool_advice.advise(ScriptedProvider({tool_advice.ROLE: [item]}), rt.cfg, _request(claims=FUNCTIONAL))
        assert result["source"] == "rules" and result["error"] and result["tools"][0]["tool"] == "oa-strategy-reviewer"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    workspace = web.Workspace(tmp_path)
    yield workspace
    workspace.close()


def _scripted_advice(monkeypatch, reply: dict | None):
    """The advice child runs the real module on a scripted provider (or exits without an answer when `reply` is None)."""
    original = subprocess.Popen

    def popen(command, **kwargs):
        if "claim_agent.tool_advice" in command:
            args = command[command.index("claim_agent.tool_advice") + 1:]
            if reply is None:
                program = "raise SystemExit(3)"
            else:
                program = ("import json, sys; from claim_agent import runtime, tool_advice; from claim_agent.provider.scripted import ScriptedProvider; "
                           f"runtime.live_provider=lambda *a, **k: ScriptedProvider({{tool_advice.ROLE: [json.loads({json.dumps(json.dumps(reply))})]}}); "
                           "raise SystemExit(tool_advice.main())")
            command = [sys.executable, "-u", "-c", program, *args]
        return original(command, **kwargs)

    monkeypatch.setattr(web.subprocess, "Popen", popen)


def _free_answer(workspace) -> tuple[str, dict]:
    sid = workspace.create(None, free_work.FREE)["id"]
    session = workspace.sessions[sid]
    session["messages"] += [dict(id="u1", role="user", text="초안 써줘", mode="FREE", files=[]),
                            dict(id="a1", role="assistant", text=free_work.UNREVIEWED_LABEL + "\n\n" + DRAFT, status="complete", mode="FREE")]
    return sid, session["messages"][1]


def test_a_free_answer_gets_advice_then_records_the_tools_used_and_the_evaluation(workspace, monkeypatch):
    reply = dict(summary="돌출부 지칭을 보세요.", tools=[dict(tool="syntax-scope-reviewer", reason="'상기 돌출부'"), dict(tool="enforce", reason="권리행사")],
                 phrases=[dict(phrase_id="default-1", text="", reason="반영")])
    _scripted_advice(monkeypatch, reply)
    sid, answer = _free_answer(workspace)
    workspace.advise(sid, answer["id"])
    assert answer["advice"]["status"] == "running" and answer["advice"]["target"] == "a1"
    wait_for(lambda: answer["advice"]["status"] != "running", timeout=30)
    advice = answer["advice"]
    assert advice["status"] == "complete" and advice["source"] == "agent", advice
    assert [t["tool"] for t in advice["tools"]] == ["syntax-scope-reviewer", "enforce"] and advice["target"] == "a1"
    assert advice["phrases"][0]["text"] == workspace.phrases()["phrases"][0]["text"]       # the saved phrase's own text
    request = json.loads(next((workspace.root / ".tui" / "requests").glob("advice-*/advice-request.json")).read_text(encoding="utf-8"))
    assert request["asked"] == "초안 써줘" and request["claims"] == CLAIMS and request["kind"] == "FREE"
    assert answer["text"] == free_work.UNREVIEWED_LABEL + "\n\n" + DRAFT          # the advice rides beside the answer, never in it

    # The user picks the recommended review (via the card) with a phrase in the text: both are recorded on this answer.
    requests = []
    monkeypatch.setattr(workspace, "execute", lambda sid, job, command: (requests.append(json.loads((job["folder"] / "chat.json").read_text(encoding="utf-8"))), job.update(done=True)))
    workspace.review(sid, {"message": "a1", "roles": ["syntax-scope-reviewer", "oa-strategy-reviewer"], "text": "지적 반영",
                           "advice": "a1", "via": ["syntax-scope-reviewer"], "phrases": [dict(id="default-1", text="지적 반영", via="advice")]})
    wait_for(lambda: workspace.jobs[sid]["done"])
    assert "advice" not in json.dumps(requests[0], ensure_ascii=False)          # the reviewers never read the recommendation
    used = [(u["tool"], u["via"]) for u in answer["used_tools"]]
    assert used == [("syntax-scope-reviewer", "advice"), ("oa-strategy-reviewer", "panel"), ("phrase", "advice")]
    # 판례 찾기 has no target: it goes to the newest answer that has a recommendation.
    monkeypatch.setattr(caselaw_tool, "run", lambda *a, **k: "판례 참고")
    workspace.caselaw(sid, {"tool": "find", "text": "'복수의'가 불명확"})
    assert answer["used_tools"][-1]["tool"] == "find" and answer["used_tools"][-1]["via"] == "panel"

    # The evaluation form: refused when empty or out of range, saved on the message and as one log line per save.
    for bad, match in (({}, "하나 이상"), (dict(score=6), "1~5"), (dict(score=True), "1~5"), (dict(tools=[dict(tool="claim-drafter")]), "도구"),
                       (dict(tags=["nice"]), "문제 유형"), (dict(usable="maybe"), "사용 가능성"), (dict(feedback="x" * 4001), "4000")):
        with pytest.raises(ValueError, match=match):
            workspace.evaluate(sid, {"message": "a1", **bad})
    with pytest.raises(ValueError, match="평가할 답변"):
        workspace.evaluate(sid, {"message": "u1", "score": 3})
    form = dict(score=4, usable="edit", fit="partial", tags=["unclear"], feedback=" 돌출부 지칭이 흔들림 ",
                tools=[dict(tool="syntax-scope-reviewer", rating="good"), dict(tool="oa-strategy-reviewer", rating="bad", note="중복 지적"),
                       dict(tool="phrase", rating="ok")])
    saved = workspace.evaluate(sid, {"message": "a1", **form})
    assert saved["revision"] == 1 and saved["feedback"] == "돌출부 지칭이 흔들림" and answer["evaluation"] == saved
    assert workspace.evaluate(sid, {"message": "a1", **form, "score": 5})["revision"] == 2
    lines = [json.loads(x) for x in (workspace.folder / evaluations.FILE).read_text(encoding="utf-8").splitlines()]
    assert [x["revision"] for x in lines] == [1, 2]
    assert lines[-1]["advice"]["tools"] == ["syntax-scope-reviewer", "enforce"] and lines[-1]["advice"]["source"] == "agent"
    assert [u["tool"] for u in lines[-1]["used"]] == ["syntax-scope-reviewer", "oa-strategy-reviewer", "phrase", "find"]
    assert lines[-1]["model"] == workspace.cfg.default_model and lines[-1]["kind"] == "FREE"

    summary = workspace.evaluation_summary()
    assert summary["count"] == 1 and summary["advised"] == 1 and summary["coverage"] == 1.0 and summary["score_avg"] == 5.0
    # Recommended {syntax, enforce}, ran {syntax, oa}: half of each.
    assert summary["precision"] == 0.5 and summary["recall"] == 0.5
    assert summary["tools"]["syntax-scope-reviewer"] == dict(recommended=1, used=1, rated=1, help=1.0, good=1, ok=0, bad=0)
    assert summary["tools"]["enforce"]["recommended"] == 1 and summary["tools"]["enforce"]["used"] == 0
    assert summary["tags"]["unclear"] == 1 and summary["fit"]["partial"] == 1 and summary["usable_share"]["edit"] == 1.0
    assert summary["by_source"]["agent"]["partial"] == 1 and summary["recent"][0]["feedback"] == "돌출부 지칭이 흔들림"
    assert evaluations.export(workspace.folder).count(b"\n") == 2


def test_a_failed_advice_child_still_leaves_the_codes_recommendation(workspace, monkeypatch):
    _scripted_advice(monkeypatch, None)
    sid, answer = _free_answer(workspace)
    workspace.advise(sid, answer["id"])
    wait_for(lambda: answer["advice"]["status"] != "running", timeout=30)
    assert answer["advice"]["source"] == "rules" and "결과를 내지 못했습니다" in answer["advice"]["error"]
    assert [t["tool"] for t in answer["advice"]["tools"]] == ["syntax-scope-reviewer"]      # 종속항 1개
    # A review report's recommendation points at the claims the report read, not at the report.
    session = workspace.sessions[sid]
    session["messages"] += [dict(id="u2", role="user", text="[선택 검수 · OA 검수]", mode="REVIEW", files=[], review=dict(roles=["oa-strategy-reviewer"], source="a1", asked=False)),
                            dict(id="a2", role="assistant", text=free_work.REVIEW_NOTE + "\n\n## 검토 의견", status="complete", mode="REVIEW",
                                 review=dict(roles=["oa-strategy-reviewer"], source="a1", asked=False, revise=False))]
    request = workspace.advice_request(session, session["messages"][-1])
    assert request["target"] == "a1" and request["claims"] == CLAIMS and request["ran"] == ["OA 검수 → 메시지 #2"] and request["kind"] == "REVIEW"


def test_a_server_restart_marks_a_running_advice_stopped(workspace):
    sid, answer = _free_answer(workspace)
    answer["advice"] = dict(status="running", target="a1")
    workspace.persist(workspace.sessions[sid])
    again = web.Workspace(workspace.root)
    try:
        assert again.sessions[sid]["messages"][1]["advice"]["status"] == "stopped"
    finally:
        again.close()
