"""Scripted runs through PRIOR_* and PRE_EXAM (docs/선행기술조사_사전심사_설계_2026-10.md) with fake WIPS and Jev."""
from __future__ import annotations

import json

from claim_agent.models.prior_art import LABEL_SEARCHED, PriorSearchOptions
from claim_agent.pipeline.engine import Decision
from claim_agent.pipeline.prior_steps import PriorArtTools
from claim_agent.prior_art.wips import WipsError
from claim_agent.provider.scripted import ScriptedProvider

from . import scripted_roles as R

DOC1 = "[0001] 책상 가장자리에 걸리는 클램프부를 갖는 베이스를 개시한다.\n[0002] 하판을 관통하는 조임 나사가 책상 하면을 누른다.\n[0003] 홀더 본체에 슬롯이 나란히 형성된다."
ELEMENTS = [
    {"element_id": "E1", "kind": "LIMITATION", "text": "클램프부를 갖는 베이스", "in_independent": True, "source_location": "발명설명 [0003]", "basis": "DIRECT"},
    {"element_id": "E2", "kind": "LIMITATION", "text": "하판을 관통하는 조임 나사", "in_independent": True, "source_location": "발명설명 [0004]", "basis": "DIRECT"},
    {"element_id": "E3", "kind": "SKELETON", "text": "입구 폭이 바닥 폭보다 좁은 슬롯", "in_independent": True, "source_location": "발명설명 [0006]", "basis": "DERIVED"},
]
QUOTES = {"E1": "책상 가장자리에 걸리는 클램프부를 갖는 베이스", "E2": "하판을 관통하는 조임 나사가 책상 하면을 누른다", "E3": "홀더 본체에 슬롯이 나란히 형성된다"}


class FakeWips:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append(tool)
        if self.fail:
            raise WipsError("WIPS 클라이언트 등록 실패(HTTP 401).")
        if tool == "get_patent_ai_search":
            if args["ctry"] != "KR":
                return {"data": {"results": []}}
            return {"data": {"results": [{"skey": "KR1", "mngNum": "10-1", "adjustScoreDp": "90"}, {"skey": "KR2", "mngNum": "10-2", "adjustScoreDp": "80"}]}}
        if tool == "get_patent_search_onlyengine":
            return {"data": {"row_list": [
                {"SKEY": "KR1", "CC": "KR", "XN_D": "10-1", "TIK": "케이블 클립 홀더", "ABK": "클램프부와 슬롯을 갖는 홀더", "XD": "20200101", "AD": "20190101"},
                {"SKEY": "KR2", "CC": "KR", "XN_D": "10-2", "TIK": "모니터 거치대", "ABK": "모니터를 거는 팔", "XD": "20210101", "AD": "20200101"},
            ]}}
        if tool == "get_patent_spec_text":
            if args["section"] == "all_claim":
                return {"data": {"cl_list": [{"cl_num": 1, "cl": "클램프부를 갖는 베이스와 슬롯을 갖는 홀더."}]}}
            return {"data": {"dtl_desc_list": [{"dtl_desc": DOC1.replace("\n", "<br>")}]}}
        raise AssertionError(tool)


class FakeJev:
    def __init__(self, screen: dict[str, float] | None = None, trim: dict[str, float] | None = None, fail: bool = False):
        self.screen = screen if screen is not None else {"c0": 0.9, "c1": 0.2}
        self.trim = trim if trim is not None else {"d0": 0.8}
        self.fail = fail
        self.batches: list[dict] = []

    def noul_batch(self, state: dict, questions: dict[str, str]) -> dict:
        self.batches.append({"state": state, "questions": questions})
        if self.fail:
            return {"status": "FAILED", "probabilities": {}, "error": "jev down", "state_chars": 10, "cost_usd": None}
        src = self.screen if "candidates" in state else self.trim
        return {"status": "RUN", "probabilities": {k: src.get(k, 0.0) for k in questions}, "state_chars": len(json.dumps(state, ensure_ascii=False)), "cost_usd": 0.001}

    def noul(self, state: dict, question: str) -> dict:
        return {"status": "FAILED", "probability": None, "error": "jev down", "state_chars": 10}


def architect_with_elements(elements=None):
    base = R.architect()

    def f(spec):
        env = base(spec)
        env["design_elements"] = elements or ELEMENTS
        return env
    return f


def searcher_query():
    def f(spec):
        return R._base(spec, "PASS", {}, ["- 국문 검색 문장 1개"], queries_ko=["클램프부와 조임 나사, 입구가 좁은 슬롯을 갖는 케이블 클립 홀더"],
                       queries_en=["cable clip holder with clamp, screw and narrow-mouth slots"], provisional_elements=["클램프부", "조임 나사", "슬롯"], problem="케이블 고정")
    return f


def searcher_screen():
    def f(spec):
        return R._base(spec, "PASS", {}, ["- 후보별 관련도"], screened=[{"skey": "KR1", "relevance": "높음", "reason": "클램프부와 슬롯"},
                                                                       {"skey": "KR2", "relevance": "낮음", "reason": "모니터"}])
    return f


def compare(statuses: dict[str, str], quotes: dict[str, str] | None = None):
    quotes = QUOTES if quotes is None else quotes

    def f(spec):
        ids = [e["element_id"] for e in ELEMENTS] + [k for k in statuses if k not in QUOTES]
        rows = [{"element_id": k, "status": statuses.get(k, "NOT_DISCLOSED"), "quote": quotes.get(k, "") if statuses.get(k, "NOT_DISCLOSED") != "NOT_DISCLOSED" else "",
                 "where": "", "note": ""} for k in ids]
        return R._base(spec, "PASS", {}, ["- 요소별 대비"], preexam_rows=rows, doc_published="2020-01-01", doc_field="케이블", doc_summary="요지")
    return f


def judge(conclusion: str, gate: str = "PASS", remaining=None, risk: str = "LOW"):
    def f(spec):
        rd = remaining if remaining is not None else [{"element_id": "E3", "source_location": "발명설명 [0006]", "note": "입구 폭", "exam_pattern": "REL_POSITION_COUPLING",
                                                        "design_change_risk": "LOW", "effect_link": "RELATION", "why_not_simple": "NONE", "numeric_basis": "NONE"}]
        return R._base(spec, "PASS", {"PREEXAM_NOVELTY_GATE": gate}, ["- 문헌별 신규성"], novelty=[{"doc_id": "D1", "conclusion": conclusion, "missing": "", "reason": "r"}],
                       inventive_primary_doc="D1", inventive_risk=risk, inventive_reason="설계변경 위험 서술", remaining_differences=rd,
                       next_step="RETURN_TO_ARCHITECT" if gate == "BLOCK" else "PROCEED")
    return f


def _request(request_indep, **opts):
    r = request_indep.model_copy()
    r.prior_search = PriorSearchOptions.model_validate({"enabled": True, "countries": ["KR", "US"], "base_date": "2026-10-01", **opts})
    return r


def _script(**extra):
    s = R.happy_script(dependent=False)
    s["claim-architect"] = [architect_with_elements()]
    s["prior-art-searcher"] = [searcher_query()]
    s.update(extra)
    return s


def _engine(rt, script, wips=None, jev=None):
    prov = ScriptedProvider(script)
    engine = rt.engine(prov)
    engine.prior_art_tools = PriorArtTools(wips or FakeWips(), jev or FakeJev())
    return engine, prov


def test_search_screen_trim_seal_preexam_pass_to_lock(rt, request_indep):
    script = _script(**{"prior-art-pre-examiner": [compare({"E1": "DISCLOSED", "E2": "PARTIALLY_DISCLOSED", "E3": "NOT_DISCLOSED"}), judge("NOT_NEGATED")]})
    engine, prov = _engine(rt, script)
    state = engine.run(engine.start(_request(request_indep), "run-pa-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    pa = state.prior_art
    assert pa.screening == "JEV" and pa.selected == ["KR1"] and [d.skey for d in pa.kept()] == ["KR1"]
    assert state.prior_art_label() == LABEL_SEARCHED and pa.set_id and pa.trim == "JEV"
    arch = next(c for c in prov.calls if c.role == "claim-architect")
    assert "PRIOR_ART_SET: SEARCHED — NOT_EXHAUSTIVE" in arch.packet_text and "[prior_art]" in arch.packet_text
    # 독립항 생성 역할에는 S10과 그 분류 값이 가지 않는다
    for c in prov.calls:
        if c.role in ("claim-architect", "claim-drafter", "claim-style-adjuster"):
            assert "<<<FILE sources/10_" not in c.sources_block and "exam_pattern" not in c.packet_text
    judge_call = next(c for c in prov.calls if c.role == "prior-art-pre-examiner" and c.scope == "PREEXAM_JUDGE")
    compare_call = next(c for c in prov.calls if c.role == "prior-art-pre-examiner" and c.scope == "PREEXAM_COMPARE")
    assert "<<<FILE sources/10_" in judge_call.sources_block and "<<<FILE sources/10_" not in compare_call.sources_block
    assert "확인됨" in judge_call.packet_text   # quotes re-found in the document
    lock = rt.store.read_record("run-pa-01", state.candidate.draft_claim_lock)
    assert lock["preexam_novelty_gate"] == "PASS" and lock["prior_art_set_id"] == pa.set_id and lock["PRIOR_ART_SET"] == LABEL_SEARCHED
    assert any("검색 집합 기준" in u for u in lock["unverified"])
    handoff = json.loads((rt.store.run_dir("run-pa-01") / "handoff" / "spec_materials.json").read_text(encoding="utf-8"))
    rows = {r["limitation"].split()[0]: r for r in handoff["independent"]}
    assert rows["E1"]["떼어 쓸 근거 문장"] == "갖춤" and rows["E3"]["떼어 쓸 근거 문장"] == "부족" and rows["E3"]["관계에서 나오는 효과"] == "RELATION"
    tele = [json.loads(x) for x in (rt.store.run_dir("run-pa-01") / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["phase"] for r in tele} >= {"wips", "jev"}
    assert all("클램프부와 조임 나사" not in json.dumps(r, ensure_ascii=False) for r in tele)   # no query text in telemetry
    assert state.usage.get("cost_usd", 0) >= 0.002 and pa.wips_calls >= 3


def test_novelty_threat_returns_to_architect_without_exam_fields(rt, request_indep):
    e4 = ELEMENTS + [{"element_id": "E4", "kind": "LIMITATION", "text": "슬롯 입구의 탄성 리브", "in_independent": True, "source_location": "발명설명 [0008]", "basis": "DIRECT"}]
    script = _script(**{
        "claim-architect": [architect_with_elements(), architect_with_elements(e4)],
        "prior-art-pre-examiner": [compare({"E1": "DISCLOSED", "E2": "DISCLOSED", "E3": "DISCLOSED"}), judge("NEGATED_RISK", "BLOCK"),
                                   compare({"E1": "DISCLOSED", "E2": "DISCLOSED", "E3": "DISCLOSED", "E4": "NOT_DISCLOSED"}), judge("NOT_NEGATED")],
    })
    engine, prov = _engine(rt, script)
    state = engine.run(engine.start(_request(request_indep), "run-pa-02"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.candidate.design_revision == "d2" and state.candidate.loop_counts["PREEXAM"] == 1
    second = [c for c in prov.calls if c.role == "claim-architect"][1]
    assert "사전 심사 되돌림" in second.packet_text and QUOTES["E1"] in second.packet_text and "발명설명 [0006]" in second.packet_text
    assert "REL_POSITION_COUPLING" not in second.packet_text and "설계변경 위험" not in second.packet_text and "RELATION" not in second.packet_text


def test_anticipated_but_model_disagrees_stops_for_review(rt, request_indep):
    script = _script(**{"prior-art-pre-examiner": [compare({"E1": "DISCLOSED", "E2": "DISCLOSED", "E3": "DISCLOSED"}), judge("NOT_NEGATED")]})
    engine, _ = _engine(rt, script)
    state = engine.run(engine.start(_request(request_indep), "run-pa-03"))
    assert state.outcome == "HALTED_REVIEW" and state.halt.reason_code == "PREEXAM_MODEL_CODE_MISMATCH"


def test_unfound_quote_is_unconfirmed_and_stops(rt, request_indep):
    quotes = dict(QUOTES, E3="문헌에 없는 지어낸 인용 문구입니다")
    script = _script(**{"prior-art-pre-examiner": [compare({"E1": "DISCLOSED", "E2": "DISCLOSED", "E3": "DISCLOSED"}, quotes), judge("NEGATED_RISK", "BLOCK")]})
    engine, _ = _engine(rt, script)
    state = engine.run(engine.start(_request(request_indep), "run-pa-04"))
    assert state.outcome == "HALTED_UNVERIFIED" and state.halt.reason_code == "PREEXAM_EVIDENCE_UNCONFIRMED"


def test_no_differentiator_stops_instead_of_looping(rt, request_indep):
    script = _script(**{"prior-art-pre-examiner": [compare({"E1": "DISCLOSED", "E2": "DISCLOSED", "E3": "DISCLOSED"}), judge("NEGATED_RISK", "BLOCK", remaining=[])]})
    engine, _ = _engine(rt, script)
    state = engine.run(engine.start(_request(request_indep), "run-pa-05"))
    assert state.outcome == "HALTED_REVIEW" and state.halt.reason_code == "NO_SOURCE_DIFFERENTIATOR"


def test_wips_unavailable_then_accept_continues_without_set(rt, request_indep):
    engine, prov = _engine(rt, _script(), wips=FakeWips(fail=True))
    state = engine.run(engine.start(_request(request_indep), "run-pa-06"))
    assert state.outcome == "HALTED_UNVERIFIED" and state.halt.reason_code == "PRIOR_SEARCH_UNAVAILABLE"
    state = engine.resume("run-pa-06", Decision(action="accept_unverified"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.prior_art_label() == "NONE" and not any(c.role == "prior-art-pre-examiner" for c in prov.calls)


def test_confirm_stops_for_selection_and_resume_fetches_choice(rt, request_indep):
    script = _script(**{"prior-art-pre-examiner": [compare({"E1": "DISCLOSED", "E2": "NOT_DISCLOSED", "E3": "NOT_DISCLOSED"}), judge("NOT_NEGATED")]})
    engine, _ = _engine(rt, script, jev=FakeJev(trim={"d0": 0.9}))
    state = engine.run(engine.start(_request(request_indep, confirm=True), "run-pa-07"))
    assert state.outcome == "HALTED_USER_DECISION" and state.halt.reason_code == "PRIOR_ART_SELECTION"
    assert {o["code"] for o in state.halt.open_issues} == {"KR1", "KR2"}
    state = engine.resume("run-pa-07", Decision(action="select_prior_art", prior_art_selection=["KR2"], base_date="2026-09-30"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert [d.skey for d in state.prior_art.kept()] == ["KR2"] and state.prior_art.base_date == "2026-09-30"


def test_jev_failure_falls_back_to_model_screen_and_skips_trim(rt, request_indep):
    script = _script(**{"prior-art-searcher": [searcher_query(), searcher_screen()],
                        "prior-art-pre-examiner": [compare({"E1": "DISCLOSED", "E2": "NOT_DISCLOSED", "E3": "NOT_DISCLOSED"}), judge("NOT_NEGATED")]})
    engine, _ = _engine(rt, script, jev=FakeJev(fail=True))
    state = engine.run(engine.start(_request(request_indep), "run-pa-08"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.prior_art.screening == "MODEL_FALLBACK" and state.prior_art.trim == "SKIPPED" and state.prior_art.selected == ["KR1"]


def test_trim_drops_low_documents_from_the_sealed_set(rt, request_indep):
    engine, prov = _engine(rt, _script(), jev=FakeJev(screen={"c0": 0.9, "c1": 0.8}, trim={"d0": 0.1, "d1": 0.2}))
    state = engine.run(engine.start(_request(request_indep), "run-pa-09"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.prior_art.kept() == [] and len(state.prior_art.trimmed) == 2
    assert not any(c.role == "prior-art-pre-examiner" for c in prov.calls)   # 0건 봉인: 사전 심사 생략
    arch = next(c for c in prov.calls if c.role == "claim-architect")
    assert "[prior_art]" not in arch.packet_text and "PRIOR_ART_SET: SEARCHED — NOT_EXHAUSTIVE" in arch.packet_text


def test_search_off_by_default_keeps_the_old_flow(rt, request_indep):
    prov = ScriptedProvider(R.happy_script(dependent=False))
    engine = rt.engine(prov)
    state = engine.run(engine.start(request_indep, "run-pa-10"))
    assert state.outcome == "DRAFT_CLAIM_LOCK" and state.prior_art is None
    assert [r.kind for r in state.records.values()][:2] == ["design", "meaning_draft"]


def test_dependent_set_gets_preexam_record_s10_and_handoff(rt, request_dep):
    script = R.happy_script(dependent=True)
    script["claim-architect"] = [architect_with_elements()]
    script["prior-art-searcher"] = [searcher_query()]
    script["prior-art-pre-examiner"] = [compare({"E1": "DISCLOSED", "E2": "NOT_DISCLOSED", "E3": "NOT_DISCLOSED"}), judge("NOT_NEGATED")]
    base = R.dep_architect()

    def dep(spec):
        env = base(spec)
        for c in env["candidates"]:
            c.update(exam_pattern="REL_POSITION_COUPLING", design_change_risk="LOW", exam_pattern_basis="발명설명 [0007]")
        env["candidates"][2].update(exam_pattern="NUMERIC_NO_REASON", design_change_risk="HIGH")
        return env
    script["dependent-claim-strategy-architect"] = [dep]
    engine, prov = _engine(rt, script)
    req = request_dep.model_copy()
    req.prior_search = PriorSearchOptions(enabled=True, countries=["KR"], base_date="2026-10-01")
    state = engine.run(engine.start(req, "run-pa-11"))
    assert state.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state.halt
    dep_call = next(c for c in prov.calls if c.role == "dependent-claim-strategy-architect")
    assert "사전 심사 기록 전문" in dep_call.packet_text and "<<<FILE sources/10_" in dep_call.sources_block
    handoff = json.loads((rt.store.run_dir("run-pa-11") / "handoff" / "spec_materials.json").read_text(encoding="utf-8"))
    rows = handoff["dependent"]
    assert [r["exam_pattern"] for r in rows] == ["REL_POSITION_COUPLING", "REL_POSITION_COUPLING", "NUMERIC_NO_REASON"]
    assert rows[2]["수치·비교"] == "부족" and handoff["independent"]
    lock = rt.store.read_record("run-pa-11", state.dependent.draft_set_lock)
    assert lock["PRIOR_ART_SET"] == LABEL_SEARCHED and lock["preexam_novelty_gate"] == "PASS"
    report = (rt.store.run_dir("run-pa-11") / "report.md").read_text(encoding="utf-8")
    assert "## 선행기술 조사·사전 심사" in report and "검색 집합 기준" in report


def test_selection_decision_text_and_cli_flags(tmp_path):
    from claim_agent.cli import _prior_search_args
    from claim_agent.tui_support import RESUME_KINDS, parse_prior_art_decision, prior_art_decision, resume_command

    assert parse_prior_art_decision(prior_art_decision(["KR1", "US9"], "2026-09-30")) == (["KR1", "US9"], "2026-09-30")
    assert parse_prior_art_decision(prior_art_decision([])) == ([], None)
    cmd = resume_command(tmp_path, "run-1", tmp_path / "d.txt", "select_prior_art", "m")
    assert "--select-prior-art-from-decision" in cmd and "select_prior_art" in RESUME_KINDS

    class A:
        prior_search, prior_confirm, base_date = True, True, "2026-11-11"
    opts = _prior_search_args(A, PriorSearchOptions.model_validate({"countries": ["KR"]}))
    assert opts.enabled and opts.confirm and opts.base_date == "2026-11-11" and opts.countries == ["KR"]
    assert opts.model_dump(exclude_unset=True).keys() == {"enabled", "confirm", "base_date", "countries"}


def test_web_status_lists_candidates_without_text(rt, request_indep):
    from claim_agent.web import Workspace

    script = _script(**{"prior-art-pre-examiner": [compare({"E1": "DISCLOSED", "E2": "NOT_DISCLOSED", "E3": "NOT_DISCLOSED"}), judge("NOT_NEGATED")]})
    engine, _ = _engine(rt, script)
    state = engine.run(engine.start(_request(request_indep), "run-pa-12"))
    status = Workspace.prior_art_status(state.prior_art.model_dump(mode="json"))
    assert [c["skey"] for c in status["candidates"]] == ["KR1", "KR2"] and status["candidates"][0]["sealed"] is True
    assert "abstract" not in json.dumps(status, ensure_ascii=False)


def test_each_role_gets_only_its_own_envelope_fields():
    """Gemini rejected the union schema (live run 2026-10-21); every role must get the shared fields plus its own."""
    from claim_agent.models.envelope import ENVELOPE_JSON_SCHEMA, envelope_schema_for

    def props(role):
        return set(envelope_schema_for(role)["properties"])

    def cand(role):
        return set(envelope_schema_for(role)["properties"]["candidates"]["items"]["properties"])

    extras = {"design_elements", "queries_ko", "screened", "preexam_rows", "novelty", "remaining_differences"}
    assert not props("claim-style-adjuster") & extras and not props("claim-drafter") & extras
    assert "design_elements" in props("claim-architect") and "preexam_rows" not in props("claim-architect")
    assert {"queries_ko", "screened"} <= props("prior-art-searcher") and {"preexam_rows", "remaining_differences"} <= props("prior-art-pre-examiner")
    assert "exam_pattern" in cand("dependent-claim-strategy-architect") and "exam_pattern" not in cand("claim-architect")
    assert "preexam_rows" in ENVELOPE_JSON_SCHEMA["properties"]   # the union stays the reference schema
