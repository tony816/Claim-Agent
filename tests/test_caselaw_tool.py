"""작업 도구의 판례 도구: 판례 기록으로 답하고(모델 없음, Jev만), 그 답은 대화에 보이되 모델이 읽는 맥락에는 들어가지 않는다."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from claim_agent import free_work, web
from claim_agent.graph import caselaw as cl
from claim_agent.graph import caselaw_tool as ct
from tests.test_web import wait_for

ROOT = Path(__file__).resolve().parents[1]
CLAIMS = ("【청구항 1】\n가열부를 포함하는 장치에 있어서,\n상기 가열부를 제어하기 위한 제어수단; 및\n온도를 300℃ 이상으로 유지하는 히터를 포함하는 장치.\n\n"
          "【청구항 2】\n제1항에 있어서, 사용자가 버튼을 누르면 서버가 신호를 보내는 장치.")
VAGUE = "청구항 1의 '대략 수평으로'는 기준면과 허용 각도를 원자료가 정하지 않음"


class FakeJev:
    """Cases whose 사건 names one of `keep` get 0.9, the rest 0.1. Records every call."""

    def __init__(self, *keep: str, status: str = "RUN"):
        self.keep, self.status, self.calls = keep, status, []

    def noul_batch(self, state, questions):
        self.calls.append((state, questions))
        if self.status != "RUN":
            return {"status": self.status, "error": "offline", "probabilities": {}}
        probs = {k: 0.9 if any(n in state["cases"][k]["사건"] for n in self.keep) else 0.1 for k in questions}
        return {"status": "RUN", "probabilities": probs, "cost_usd": 0.0003}


@pytest.fixture
def paths(tmp_path):
    # No DB here: the tool still answers from the record files and says the quote check was skipped.
    return cl.Paths(tmp_path / "no-db", ROOT / "docs" / "caselaw-lessons.json")


def test_confirm_pairs_keeps_the_confirm_layout_for_one_finding_and_names_each_finding_otherwise():
    lessons = cl.load_lessons(ROOT / "docs" / "caselaw-lessons.json")["records"][:3]
    jev = FakeJev()
    cl.confirm_pairs([("지적", r, "질문?") for r in lessons], jev)
    state, questions = jev.calls[-1]
    assert state["finding"] == "지적" and set(state["cases"]) == set(questions) == {"case_0", "case_1", "case_2"}
    assert questions["case_1"] == "cases.case_1에 대해 답한다. 질문?"
    cl.confirm_pairs([("가", lessons[0], "q1"), ("나", lessons[1], "q2"), ("가", lessons[2], "q1")], jev)
    state, questions = jev.calls[-1]
    assert state["findings"] == {"f0": "가", "f1": "나"} and "finding" not in state
    assert questions["case_1"] == "cases.case_1에 대해 답한다. finding은 findings.f1이다. q2"
    assert questions["case_2"].startswith("cases.case_2에 대해 답한다. finding은 findings.f0이다.")


def test_a_vague_degree_finding_reaches_the_degree_cases_bigram_overlap_alone_misses(paths):
    lessons = cl.load_lessons(paths.lessons)["records"]
    assert "2021허193" not in [r["case_number"] for _, r in cl.find(lessons, VAGUE, k=8)]     # why the hint exists
    assert cl.hinted_tags(VAGUE)[:2] == ["C4", "C3"]
    assert "2021허193" in [r["case_number"] for r in ct._pool(lessons, VAGUE, cl.hinted_tags(VAGUE), "tag")]
    jev = FakeJev("2021허193")
    answer = ct.run("find", root=ROOT, paths=paths, text=VAGUE, client=jev)
    assert len(jev.calls) == 1                                        # every set's candidates in one Jev request
    assert "**특허법원 2021허193**" in answer and "Jev 확인 p=0.90" in answer and "Jev 미확인" not in answer
    assert answer.count("- **") == 1                                   # below the threshold is left out
    assert answer.startswith("**판례 참고 (미검토 — 게이트·근거 아님)** · 판례 찾기") and ct.PRIVATE in answer
    assert "판례 DB 없음 — 인용 원문 대조 생략" in answer and "인용(PDF" not in answer


def test_enforce_flags_the_claim_wording_and_keeps_only_the_cases_jev_confirms(paths):
    data = cl.load_records(ROOT / cl.ENFORCE_FILE)
    flagged = cl.enforcement_report(data, CLAIMS, k=3)
    assert {f["type_code"] for f in flagged} >= {"W02", "W04", "W05", "W09"}
    keep = flagged[0]["cases"][0]["case_number"]
    answer = ct.run("enforce", root=ROOT, paths=paths, claims=CLAIMS, target="에이전트 답변 #2", client=FakeJev(keep))
    assert "· 권리행사 점검 · 대상: 에이전트 답변 #2" in answer
    assert f"### 청구항 1 · {flagged[0]['type_code']}" in answer and "### 청구항 2 · W09" in answer
    assert f"**{flagged[0]['cases'][0]['court']} {keep}**" in answer and "Jev가 관련 있다고 본 판례 없음" in answer
    quiet = ct.run("enforce", root=ROOT, paths=paths, claims="【청구항 1】\n몸체와 덮개를 포함하는 용기.", client=FakeJev())
    assert "해당하는 문언이 없습니다" in quiet and "다른 권리행사 위험이 없다는 뜻은 아닙니다" in quiet


def test_without_jev_the_candidates_are_shown_unconfirmed_not_dropped(paths, monkeypatch):
    from claim_agent.prior_art import jev

    monkeypatch.setattr(jev, "status", lambda: (False, ".env에 OPENROUTER_API_KEY가 없습니다."))
    answer = ct.run("find", root=ROOT, paths=paths, text="제어수단은 기능만 적혀 있고 그 기능을 이루는 구조가 청구항에 없음")
    assert "Jev 미확인 — .env에 OPENROUTER_API_KEY가 없습니다." in answer and "· Jev 미확인 —" in answer
    failed = ct.run("features", root=ROOT, paths=paths, text="가열 온도 300~400℃ 범위::F-NUMERIC", client=FakeJev(status="FAILED"))
    assert "Jev 미확인 — FAILED: offline" in failed and "· Jev 미확인 —" in failed


def test_features_take_a_given_type_guess_the_rest_and_ask_source_questions(paths):
    answer = ct.run("features", root=ROOT, paths=paths, text="- 가열 온도 300~400℃ 범위::F-NUMERIC\n센서 신호로 모터를 자동 정지\n\n", client=FakeJev())
    assert "### F1 가열 온도 300~400℃ 범위" in answer and "- 유형: F-NUMERIC 수치 범위" in answer
    assert "### F2 센서 신호로 모터를 자동 정지" in answer and "(유형 추정: 낱말 규칙" in answer
    assert "- 원자료 확인:" in answer and "등록 가능성 통계가 아닙니다" in answer and "### F3" not in answer


def test_the_tool_refuses_what_it_cannot_answer(paths, tmp_path):
    with pytest.raises(ValueError, match="지원하지 않는"):
        ct.run("draft", root=ROOT, paths=paths, text="x")
    with pytest.raises(ValueError, match="대상으로 고르세요"):
        ct.run("enforce", root=ROOT, paths=paths)
    with pytest.raises(ValueError, match="입력창에"):
        ct.run("find", root=ROOT, paths=paths, text="  ")
    with pytest.raises(ValueError, match="4000자"):
        ct.run("features", root=ROOT, paths=paths, text="가" * 4001)
    with pytest.raises(ValueError, match="판례 기록을 읽지 못했습니다"):
        ct.run("enforce", root=tmp_path, paths=paths, claims=CLAIMS, client=FakeJev())


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    original = ct.run
    calls = []

    def offline(tool, **kw):
        calls.append(dict(tool=tool, **{k: kw[k] for k in ("text", "claims", "target")}))
        return original(tool, **{**kw, "root": ROOT, "paths": cl.Paths(tmp_path / "no-db", ROOT / "docs" / "caselaw-lessons.json"), "client": FakeJev()})

    monkeypatch.setattr(ct, "run", offline)
    workspace = web.Workspace(tmp_path)
    workspace.caselaw_calls = calls
    yield workspace
    workspace.close()


def test_caselaw_turns_show_in_the_conversation_but_never_reach_a_model(workspace, monkeypatch):
    requests = []

    def capture(sid, job, command):
        requests.append(json.loads(Path(command[command.index("--request") + 1]).read_text(encoding="utf-8")))
        job["done"] = True

    monkeypatch.setattr(workspace, "execute", capture)
    sid = workspace.create()["id"]                                       # 게이트 자동: the tool works in both modes
    folder = workspace.directory(sid)
    workspace.sessions[sid]["messages"] = [dict(id="u", role="user", text="발명 설명"),
                                           dict(id="a", role="assistant", text="초안\n\n" + CLAIMS, mode="AUTHORING_DRAFT", status="complete")]
    with pytest.raises(ValueError, match="지원하지 않는"):
        workspace.caselaw(sid, {"tool": "drafter"})
    with pytest.raises(ValueError, match="청구항"):
        workspace.caselaw(sid, {"tool": "enforce", "message": "u"})
    workspace.caselaw(sid, {"tool": "enforce", "message": "a", "text": "이 말은 쓰지 않는다"})
    assert workspace.caselaw_calls[-1] == dict(tool="enforce", text="이 말은 쓰지 않는다", claims=free_work.review_target("초안\n\n" + CLAIMS),
                                               target="에이전트 답변 #2")
    asked, answer = workspace.sessions[sid]["messages"][-2:]
    assert asked["mode"] == answer["mode"] == web.CASELAW and asked["text"] == "[판례 도구 · 권리행사 점검] 에이전트 답변 #2의 청구항"
    assert answer["status"] == "complete" and answer["caselaw"] == dict(tool="enforce", source="a") and "### 청구항 1" in answer["text"]
    workspace.caselaw(sid, {"tool": "find", "text": " 【청구항 3】 상기 각도는 대략 수평이다 "})
    assert workspace.sessions[sid]["messages"][-2]["text"] == "[판례 도구 · 판례 찾기] 【청구항 3】 상기 각도는 대략 수평이다"
    assert not (folder / "history.json").exists()                       # no job ran, so the model's history is untouched
    # A 판례 도구 turn is no claim draft, even when the finding quotes a claim head.
    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        workspace.claim_source(workspace.sessions[sid], workspace.sessions[sid]["messages"][-2]["id"])
    workspace.start(sid, {"text": "이걸로 수정해줘"})
    wait_for(lambda: requests)
    assert requests[0]["material"] == "발명 설명" and requests[0]["prior_requests"] == ["발명 설명"]
    assert "판례" not in requests[0]["reference"] and requests[0]["reference"] == "초안\n\n" + CLAIMS


def test_caselaw_waits_for_a_running_answer_and_works_in_free_mode(workspace):
    sid = workspace.create(None, free_work.FREE)["id"]
    workspace.jobs[sid] = dict(done=False)
    with pytest.raises(ValueError, match="완료하거나 중지한 뒤"):
        workspace.caselaw(sid, {"tool": "find", "text": "기능만 있고 구조가 없음"})
    del workspace.jobs[sid]
    workspace.caselaw(sid, {"tool": "features", "text": "센서 신호로 모터를 자동 정지"})
    assert [m["mode"] for m in workspace.sessions[sid]["messages"]] == [web.CASELAW, web.CASELAW]
    assert workspace.sessions[sid]["title"].startswith("[판례 도구 · 특허성 신호]")
