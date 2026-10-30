"""선행기술조사 페이지: WIPS만으로 찾고 읽는 조사 에이전트, 필요할 때만 부르는 판례·OA 그래프 도구(Jev 없음), 지침 생성 절, 웹 작업 관리."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claim_agent import web
from claim_agent.config import load_config
from claim_agent.prior_art import guide, survey, wips
from claim_agent.provider.scripted import ScriptedProvider

ROOT = Path(__file__).resolve().parents[1]
DESC = ("[0005] 클램프부(10)는 책상 가장자리에 걸리는 클램프부를 갖는 베이스로 이루어진다.\n\n"
        "[0006] 조임 나사(20)는 하판을 관통하는 조임 나사가 책상 하면을 누른다.\n\n[0007] 모니터 팔은 회전한다.")
OA = {"version": 1, "cases": {"1020240000001": {"title": "케이블 정리 장치"}}, "examples": [
    {"id": "amend:1:1", "kind": "보정", "application_number": "1020240000001", "title": "케이블 정리 장치",
     "office_action": {"date": "2025-01-01", "statutes": ["제29조제2항"]},
     "changes": [{"claim": 1, "added": ["상기 슬롯의 입구 폭은 바닥 폭보다 좁게 형성되는"], "from_claims": [3], "matched_paragraphs": []}]},
    {"id": "effect:1:1", "kind": "효과", "application_number": "1020240000001", "title": "케이블 정리 장치",
     "office_action": {"statutes": ["제29조제2항"]}, "reply": ["입구가 좁아 케이블이 빠지지 않는 효과([0010])"]},
    {"id": "oa:1:1", "kind": "지적", "rule": "E2", "application_number": "1020240000001", "title": "케이블 정리 장치",
     "statute": "특허법 제42조제4항제2호", "quote": "\"대략\"은 범위를 불명확하게 한다", "terms": ["대략"]},
]}


class FakeWips:
    """KR만 결과가 있다. 도구 이름과 인자를 기록한다."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if tool == "get_patent_ai_search":
            if args["ctry"] != "KR":
                return {"data": {"results": []}}
            return {"data": {"results": [{"skey": "KR1", "mngNum": "10-1", "adjustScoreDp": "90"}, {"skey": "KR2", "mngNum": "10-2", "adjustScoreDp": "70"}]}}
        if tool == "get_patent_search_onlyengine":
            return {"data": {"row_list": [
                {"SKEY": "KR1", "CC": "KR", "XN_D": "10-1", "TIK": "케이블 클립 홀더", "ABK": "클램프부와 슬롯을 갖는 홀더", "XD": "20200101", "AD": "20190101"},
                {"SKEY": "KR2", "CC": "KR", "XN_D": "10-2", "TIK": "모니터 거치대", "ABK": "모니터를 거는 팔", "XD": "20250601", "AD": "20240101"}]}}
        if tool == "get_patent_spec_text":
            if args["section"] == "all_claim":
                return {"data": {"cl_list": [{"cl_num": 1, "cl": "클램프부를 갖는 베이스와 슬롯을 갖는 홀더."}]}}
            return {"data": {"dtl_desc_list": [{"dtl_desc": DESC.replace("\n", "<br>")}]}}
        raise AssertionError(tool)


@pytest.fixture
def cfg(tmp_path):
    oa = tmp_path / "examples.json"
    oa.write_text(json.dumps(OA, ensure_ascii=False), encoding="utf-8")
    return load_config(None, ROOT, {"graph.precedent_path": str(oa), "graph.caselaw_dir": str(tmp_path / "no-db")})


@pytest.fixture(autouse=True)
def no_jev(monkeypatch):
    """The survey path must never reach Jev."""
    from claim_agent.prior_art import jev

    def refuse(*a, **k):
        raise AssertionError("Jev must not be used by the prior-art survey")

    monkeypatch.setattr(jev, "JevClient", refuse)
    monkeypatch.setattr(jev, "status", refuse)


def test_guide_file_matches_its_generator():
    real = load_config(None, ROOT)
    if not guide.oa_path(real).is_file():
        pytest.skip("OA 그래프(cs2-spec-writer examples.json)가 이 컴퓨터에 없음")
    assert (ROOT / guide.GUIDE_PATH).read_text(encoding="utf-8") == guide.build(real)


def test_guide_replace_needs_markers():
    with pytest.raises(ValueError, match="표식"):
        guide.replace("본문만 있음", guide.START + "x" + guide.END)


def test_search_assigns_refs_marks_cutoff_and_keeps_korean_to_kr_jp(cfg):
    tools = survey.SurveyTools(FakeWips(), cfg, ["KR", "US"], cutoff="2025-01-01")
    res = tools.wips_search("클램프부와 슬롯을 갖는 케이블 홀더")
    assert res["countries"] == ["KR"] and res["new"] == 2
    assert [r["ref"] for r in res["results"]] == ["D1", "D2"]
    assert res["results"][1]["after_cutoff"] is True and "after_cutoff" not in res["results"][0]
    again = tools.wips_search("cable clip holder with clamp", countries=["US"])
    assert again["countries"] == ["US"] and again["hits"] == 0
    assert tools.docs["D1"]["queries"] == [1]
    with pytest.raises(ValueError, match="국문"):
        survey.SurveyTools(FakeWips(), cfg, ["US"]).wips_search("국문 문장")


def test_read_finds_paragraphs_and_rate_fills_the_table(cfg):
    tools = survey.SurveyTools(FakeWips(), cfg, ["KR"])
    tools.wips_search("클립 홀더")
    claims = tools.wips_read("D1", "claims")
    assert "클램프부를 갖는 베이스" in claims["text"]
    found = tools.wips_read("D1", "description", find="조임 나사")
    assert found["paragraphs"] and all("조임" in p for p in found["paragraphs"])
    assert tools.run("wips_read", {"ref": "D9"})["error"].startswith("후보 표에 없는")
    assert tools.run("nope", {})["error"].startswith("없는 도구")
    tools.rate([{"ref": "D1", "relevance": "상", "reason": "클램프부가 요약에 있음"}, {"ref": "D2", "relevance": "모름"}])
    rows = tools.candidates()
    assert rows[0]["ref"] == "D1" and rows[0]["relevance"] == "상" and rows[0]["read"] == ["claims", "description"]
    assert rows[1]["relevance"] == ""


def test_graph_tools_answer_from_caselaw_and_oa_records_without_jev(cfg):
    tools = survey.SurveyTools(FakeWips(), cfg, ["KR"])
    sig = tools.caselaw_signals("슬롯 폭을 0.5 내지 2 mm 범위로 한정", "F-NUMERIC")
    assert sig["type_code"] == "F-NUMERIC" and sig["outcomes"] and sig["cases"]
    assert sig["cases"][0]["quote_verified"] is None          # no DB in the test → not checked, never claimed
    assert tools.caselaw_search("결합 동기가 없어 결합이 곤란", "patentability")["cases"]
    oa = tools.oa_search("슬롯의 입구 폭이 바닥 폭보다 좁은", "보정")
    assert oa["보정"][0]["patterns"] and oa["caveat"].startswith("심사 이력 참고")
    every = tools.oa_search("대략 수평")
    assert set(every) == {"caveat", "보정", "효과", "지적"}
    assert [r["tool"] for r in tools.rag] == ["caselaw_signals", "caselaw_search", "oa_search", "oa_search"]


def test_run_survey_loops_tools_then_verifies_quotes(cfg, tmp_path):
    seen: list[str] = []

    def step(reply):
        def answer(spec):
            seen.append(spec.packet_text)
            assert "14. 선행기술조사 특허성 판단 지침" in spec.system_instruction
            return reply
        return answer

    final = {"report": "## 조사 요약\n- 신규성 위협 없음(검색 집합 기준)", "evidence": [
        {"ref": "D1", "element": "E1", "quote": "책상 가장자리에 걸리는 클램프부를 갖는 베이스"},
        {"ref": "D1", "element": "E2", "quote": "책상을 붙잡는 집게"},
        {"ref": "D2", "element": "E3", "quote": "모니터를 거는 팔"}]}
    provider = ScriptedProvider({survey.ROLE: [
        step({"memo": "발명 전체로 찾는다", "calls": [{"tool": "wips_search", "args": {"query": "클램프부와 조임 나사를 갖는 케이블 홀더"}}], "final": None}),
        step({"memo": "D1을 읽고 수치 차이의 판례 신호를 본다", "calls": [
            {"tool": "wips_read", "args": {"ref": "D1", "part": "description"}},
            {"tool": "caselaw_signals", "args": {"feature": "슬롯 폭 수치 범위", "type_code": "F-NUMERIC"}},
            {"tool": "oa_search", "args": {"text": "슬롯의 입구 폭", "kind": "보정"}}], "final": None}),
        step({"memo": "보고서", "calls": [], "final": final})]})
    request = {"text": "케이블 클립 홀더: 클램프부, 조임 나사, 슬롯", "countries": ["KR", "US"], "cutoff": "2025-01-01", "depth": "quick"}
    result = survey.run_survey(provider, cfg, request, tmp_path / "out", client=FakeWips())
    report = result["report"]
    assert report.startswith(survey.LABEL)
    assert "| D1 KR 10-1 | E1 |" in report and "일치 [0005]" in report
    assert "불일치 — 원문에서 찾지 못함" in report and "전문 미확인 — 대조 못 함" in report
    assert "기준일(2025-01-01) 이후 공개라 선행기술에서 뺄 문헌: D2" in report
    assert "그래프 참조: caselaw_signals(F-NUMERIC)" in report and "oa_search(보정)" in report
    assert "후보 표 (코드 집계)" in seen[1] and "| D1 | KR 10-1 |" in seen[1]
    assert '"tool": "wips_search"' not in seen[0] and "결과:" in seen[2]
    lines = [json.loads(x) for x in (tmp_path / "out" / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["tool"] for x in lines if x["kind"] == "call"] == ["wips_search", "wips_read", "caselaw_signals", "oa_search"]
    assert lines[-1]["kind"] == "done"
    assert json.loads((tmp_path / "out" / "candidates.json").read_text(encoding="utf-8"))[0]["ref"] == "D1"
    assert not provider.remaining()


def test_run_survey_forces_a_report_when_steps_run_out(cfg, tmp_path):
    keep_searching = {"memo": "더 찾는다", "calls": [{"tool": "wips_search", "args": {"query": "클립 홀더"}}], "final": None}
    closing = {"memo": "끝", "calls": [], "final": {"report": "## 조사 요약\n- 단계를 다 씀", "evidence": []}}
    steps = survey.DEPTHS["quick"]
    provider = ScriptedProvider({survey.ROLE: [keep_searching] * steps + [lambda spec: closing if "단계를 다 썼다" in spec.packet_text else {}]})
    result = survey.run_survey(provider, cfg, {"text": "클립 홀더", "countries": ["KR"], "depth": "quick"}, tmp_path / "o", client=FakeWips())
    assert "단계를 다 씀" in result["report"] and "대조할 인용이 없습니다" in result["report"]
    assert len(provider.calls) == steps + 1


def test_a_report_before_any_search_is_refused_and_calls_are_logged(cfg, tmp_path):
    early = {"memo": "검색 없이 끝낸다", "calls": [], "final": {"report": "## 조사 요약\n- 검색 안 함", "evidence": []}}
    search = {"memo": "찾는다", "calls": [{"tool": "wips_search", "args": {"query": "클립 홀더"}}], "final": None}
    done = {"memo": "끝", "calls": [], "final": {"report": "## 조사 요약\n- 검색함", "evidence": []}}
    provider = ScriptedProvider({survey.ROLE: [early, lambda spec: search if "검색 전 보고서는 받지 않는다" in spec.packet_text else {}, done]})
    result = survey.run_survey(provider, cfg, {"text": "클립 홀더", "countries": ["KR"], "depth": "quick"}, tmp_path / "o", client=FakeWips())
    assert "검색함" in result["report"] and "검색 안 함" not in result["report"]
    logs = sorted((tmp_path / "o" / "calls").glob("*.txt"))
    assert len(logs) == 3 and "# 받은 글" in logs[0].read_text(encoding="utf-8")


def test_a_failed_model_call_is_retried_once(cfg):
    from claim_agent.provider.base import CallResult

    class Flaky:
        def __init__(self, fails):
            self.fails, self.n = fails, 0

        def generate(self, spec):
            self.n += 1
            if self.n <= self.fails:
                raise RuntimeError(f"cli exit {self.n}")
            return CallResult(text='{"memo": "ok", "calls": [], "final": null}', model=spec.model)

    once = Flaky(1)
    assert survey.ask(once, cfg, "sys", "packet")["memo"] == "ok" and once.n == 2
    with pytest.raises(RuntimeError, match="cli exit 1"):
        survey.ask(Flaky(2), cfg, "sys", "packet")


def test_non_json_reply_is_asked_again_then_taken_as_the_report(cfg):
    class Plain:
        def __init__(self):
            self.n = 0

        def generate(self, spec):
            from claim_agent.provider.base import CallResult

            self.n += 1
            return CallResult(text="## 조사 요약\n그냥 글", model=spec.model)

    p = Plain()
    action = survey.ask(p, cfg, "sys", "packet")
    assert p.n == 2 and action["final"]["report"].startswith("## 조사 요약")


# ---------------- 웹 ----------------
@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    ws = web.Workspace(tmp_path)
    yield ws
    ws.close()


def test_survey_start_checks_inputs_and_wips_key(workspace, monkeypatch):
    monkeypatch.delenv("WIPS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="발명 내용"):
        workspace.surveys.start({"countries": ["KR"]})
    with pytest.raises(ValueError, match="나라"):
        workspace.surveys.start({"text": "발명", "countries": ["XX"]})
    with pytest.raises(ValueError, match="기준일"):
        workspace.surveys.start({"text": "발명", "countries": ["KR"], "cutoff": "2025/01/01"})
    with pytest.raises(ValueError, match="WIPS_API_KEY"):
        workspace.surveys.start({"text": "발명", "countries": ["KR"]})
    with pytest.raises(ValueError, match="글 자료만"):
        workspace.surveys.upload("drawing.png", b"\x89PNG")
    assert workspace.surveys.list()["wips"] is False


def test_survey_runs_as_a_child_process_and_the_page_reads_its_files(workspace, monkeypatch):
    monkeypatch.setenv("WIPS_API_KEY", "offline-key")
    monkeypatch.setattr("claim_agent.model_settings.connection", lambda cfg, kind: {"connected": True, "message": ""})
    commands = []

    def fake_execute(sid, job, command):
        commands.append(command)
        folder = workspace.surveys.folder / sid
        (folder / "progress.jsonl").write_text(json.dumps({"kind": "call", "tool": "wips_search", "ok": True, "summary": "KR · 2건"}) + "\n{broken",
                                               encoding="utf-8")
        (folder / "candidates.json").write_text(json.dumps([{"ref": "D1", "title": "홀더"}]), encoding="utf-8")
        (folder / "result.json").write_text(json.dumps({"status": "complete", "report": "## 조사 요약\n키 offline-web-secret"}), encoding="utf-8")
        with workspace.lock:
            workspace.surveys.items[sid]["status"] = "complete"
            job["done"] = True

    monkeypatch.setattr(workspace.surveys, "_execute", fake_execute)
    item = workspace.surveys.upload("invention.md", "# 발명\n클립 홀더".encode())
    sid = workspace.surveys.start({"text": "", "files": [item["id"]], "countries": ["KR", "US"], "cutoff": "2025-01-01", "depth": "deep"})["id"]
    workspace.surveys.jobs[sid]["thread"].join(5)
    command = commands[0]
    assert command[:4] == [sys.executable, "-u", "-m", "claim_agent.prior_art.survey"]
    request = json.loads(Path(command[command.index("--request") + 1]).read_text(encoding="utf-8"))
    assert request["files"][0]["name"] == "invention.md" and request["depth"] == "deep" and request["countries"] == ["KR", "US"]
    snap = workspace.surveys.snapshot(sid)
    assert snap["running"] is False and snap["survey"]["title"] == "invention.md"
    assert [p["tool"] for p in snap["progress"]] == ["wips_search"]                 # a half-written line is skipped
    assert snap["candidates"][0]["ref"] == "D1" and "[API KEY]" in snap["report"] and "offline-web-secret" not in snap["report"]
    body, name = workspace.surveys.export(sid)
    assert name.startswith("선행기술조사_") and body.decode().startswith("## 조사 요약")
    assert [s["id"] for s in workspace.surveys.list()["surveys"]] == [sid]
    workspace.surveys.delete(sid)
    assert not (workspace.surveys.folder / sid).exists()


def test_pages_are_served_with_the_shared_markdown_script():
    for name in ("prior_art.html", "prior_art.js", "md.js"):
        assert name in {v[0] for v in web.PAGES.values()} and (web.ASSETS / name).is_file()
    index = (web.ASSETS / "index.html").read_text(encoding="utf-8")
    assert index.index("/md.js") < index.index("/app.js") and 'href="/prior-art"' in index
    assert "function markdown" not in (web.ASSETS / "app.js").read_text(encoding="utf-8")
