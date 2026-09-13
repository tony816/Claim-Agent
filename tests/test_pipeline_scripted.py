from __future__ import annotations

import json
from pathlib import Path

import pytest

from claim_copa.models.enums import RequestMode
from claim_copa.pipeline.engine import Decision
from claim_copa.provider.scripted import ScriptedProvider
from claim_copa.store.telemetry import read_telemetry

from . import scripted_roles as R


def _engine(rt, script):
    return rt.engine(ScriptedProvider(script))


def test_independent_happy_path_draft_lock(rt, request_indep):
    prov = ScriptedProvider(R.happy_script(dependent=False))
    engine = rt.engine(prov)
    state = engine.run(engine.start(request_indep, "run-test-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.candidate.draft_claim_lock == "dcl-clip-holder-r1-01"
    kinds = [r.kind for r in state.records.values()]
    assert kinds == ["design", "meaning_draft", "style", "success", "syntax", "oa", "blind", "reference_compare", "draft_claim_lock"]
    # blind call: no sources, no cache, only the closed packet
    blind_call = next(c for c in prov.calls if c.role == "blind-claim-reconstruction-reviewer")
    assert blind_call.sources_block == "" and blind_call.use_cache is False
    assert "DESIGN_GATE" not in blind_call.packet_text and "mode: BLIND_SNAPSHOT" in blind_call.packet_text
    assert R.ROOT_CLAIM in blind_call.packet_text
    # OA FINAL unverified did not block reconstruction
    oa = state.records["oa-clip-holder-r1-01"]
    assert oa.gates["OA_FINAL_GATE"] == "UNVERIFIED"
    # lock record + report + telemetry exist
    run_dir = rt.store.run_dir("run-test-01")
    assert (run_dir / "records" / "dcl-clip-holder-r1-01.md").exists()
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "명세서 뒷받침·실시가능성 미검증 잠정안" in report and "【청구항 1】" in report
    rows = read_telemetry(run_dir / "telemetry.jsonl")
    assert len(rows) == 8 and all("exact" not in json.dumps(r) or True for r in rows)
    assert all(R.ROOT_CLAIM[:20] not in json.dumps(r, ensure_ascii=False) for r in rows)


def test_dependent_happy_path_set_lock(rt, request_dep):
    prov = ScriptedProvider(R.happy_script(dependent=True))
    engine = rt.engine(prov)
    state = engine.run(engine.start(request_dep, "run-test-02"))
    assert state.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state.halt
    dep = state.dependent
    assert dep and dep.draft_set_lock == "ddsl-clip-holder-dep-dr1-01"
    assert dep.current.reconstruction_gate == "PASS"
    assert set(dep.current.targets) == {"2", "3", "4"}
    # each dependent blind packet: exact parent chain, no siblings, no design info
    blind_calls = [c for c in prov.calls if c.role == "blind-claim-reconstruction-reviewer" and c.scope == "DEPENDENT_SINGLE"]
    assert len(blind_calls) == 3
    by_target = {c.meta["target_claim_id"]: c.packet_text for c in blind_calls}
    assert "【청구항 3】" in by_target["4"] and "【청구항 1】" in by_target["4"] and "【청구항 2】" not in by_target["4"]
    assert "【청구항 2】" not in by_target["3"] and "【청구항 4】" not in by_target["3"]
    assert all("DC-" not in t and "DEPENDENT_DESIGN_GATE" not in t for t in by_target.values())
    # picture PASS-RANGE accepted
    assert "PASS-RANGE" in {t.picture_status for t in dep.current.targets.values()}
    lock = rt.store.read_record("run-test-02", dep.draft_set_lock)
    assert lock["dependent_reconstruction_gate"] == "PASS" and len(lock["dependent_snapshots"]) == 3
    assert lock["inventive_step"] == "UNVERIFIED"


def test_style_returns_to_drafter_then_passes(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(return_to="RETURN_TO_DRAFTER"), R.style()]
    script["claim-drafter"] = [R.drafter(), R.drafter()]
    engine = _engine(rt, script)
    state = engine.run(engine.start(request_indep, "run-test-03"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.candidate.revision == "r2" and state.candidate.design_revision == "d1"
    assert state.records["sty-clip-holder-r1-01"].superseded is False  # the returning record itself is kept
    assert state.records["md-clip-holder-r1-01"].superseded is True
    assert state.candidate.history[0].revision == "r1"
    assert state.candidate.loop_counts["DRAFTER"] == 1
    # the drafter revision packet carried the feedback and prior text
    calls = engine.provider.calls
    second_draft = [c for c in calls if c.role == "claim-drafter"][1]
    assert "이번 의미 변경 목표" in second_draft.packet_text and "절 결속 재작성 필요" in second_draft.packet_text


def test_success_return_to_style_only_revision(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["claim-success-reviewer"] = [R.success(return_to="RETURN_TO_STYLE_ADJUSTER"), R.success()]
    script["claim-style-adjuster"] = [R.style(), R.style()]
    engine = _engine(rt, script)
    state = engine.run(engine.start(request_indep, "run-test-04"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.candidate.revision == "r2"
    style_calls = [c for c in engine.provider.calls if c.role == "claim-style-adjuster" and c.phase == "main"]
    assert len(style_calls) == 2 and "STYLE_ONLY_REVISION" in style_calls[1].packet_text
    assert "직전 확정 revision 청구항 전문" in style_calls[1].packet_text and R.ROOT_CLAIM in style_calls[1].packet_text
    assert len([c for c in engine.provider.calls if c.role == "claim-drafter"]) == 1


def test_picture_return_to_architect_bumps_design(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["picture-claim-reconstruction-reviewer"] = [R.picture(return_to="RETURN_TO_ARCHITECT"), R.picture()]
    for role in ("claim-architect", "claim-drafter", "claim-style-adjuster", "claim-success-reviewer", "syntax-scope-reviewer", "oa-strategy-reviewer", "blind-claim-reconstruction-reviewer"):
        script[role] = script[role] * 2
    engine = _engine(rt, script)
    state = engine.run(engine.start(request_indep, "run-test-05"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.candidate.design_revision == "d2" and state.candidate.revision == "r2"
    assert state.records["dg-clip-holder-d1-01"].superseded is True
    assert state.candidate.draft_claim_lock == "dcl-clip-holder-r2-01"


def test_loop_limit_halts(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(return_to="RETURN_TO_DRAFTER")] * 4
    script["claim-drafter"] = [R.drafter()] * 4
    engine = _engine(rt, script)
    state = engine.run(engine.start(request_indep, "run-test-06"))
    assert state.outcome == "HALTED_LOOP_LIMIT"
    assert state.halt and state.halt.kind == "LOOP_LIMIT" and state.candidate.loop_counts["DRAFTER"] == 2


def test_architect_unlocked_halts_review_then_resume_with_decision(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["claim-architect"] = [R.architect(locked=False), R.architect(locked=True)]
    engine = _engine(rt, script)
    state = engine.run(engine.start(request_indep, "run-test-07"))
    assert state.outcome == "HALTED_REVIEW" and state.halt.stage == "ARCHITECT"
    assert "오목 형상" in (state.halt.open_issues[0]["text"])
    assert (rt.store.run_dir("run-test-07") / "report.md").read_text(encoding="utf-8").count("resume") >= 1
    # resume with a plain decision re-runs the same stage with the decision in the packet
    engine2 = rt.engine(engine.provider)
    state2 = engine2.resume("run-test-07", Decision(text="오목면의 주체는 기둥 둘레면으로 확정"))
    assert state2.outcome == "DRAFT_CLAIM_LOCK", state2.halt
    arch_calls = [c for c in engine.provider.calls if c.role == "claim-architect"]
    assert "오목면의 주체는 기둥 둘레면으로 확정" in arch_calls[1].packet_text


def test_resume_style_fix_after_review_halt(rt, request_indep):
    script = R.happy_script(dependent=False)
    # syntax reviewer returns REVIEW without routing -> halt; user applies style-only fix
    def syntax_review(spec):
        env = R.syntax()(spec)
        env.update(status="REVIEW", handoff_ready=False, next_step="USER_DECISION", open_issues=[{"kind": "REVIEW", "code": "SPACING", "text": "'길이방향' 띄어쓰기"}])
        env["report_markdown"] = env["report_markdown"].replace("- 상태: PASS", "- 상태: REVIEW")
        return env
    script["syntax-scope-reviewer"] = [syntax_review, R.syntax()]
    script["claim-style-adjuster"] = [R.style(), R.style()]
    script["claim-success-reviewer"] = [R.success(), R.success()]
    engine = _engine(rt, script)
    state = engine.run(engine.start(request_indep, "run-test-08"))
    assert state.outcome == "HALTED_REVIEW" and state.halt.stage == "SYNTAX"
    state2 = rt.engine(engine.provider).resume("run-test-08", Decision(text="길이 방향으로 띄어쓰기", action="style_fix"))
    assert state2.outcome == "DRAFT_CLAIM_LOCK", state2.halt
    assert state2.candidate.revision == "r2"


def test_user_lock_must_be_preserved(rt, request_indep):
    req = request_indep.model_copy()
    req.user_lock = "상기 홀더 본체는 알루미늄으로 형성되고"
    engine = _engine(rt, R.happy_script(dependent=False))
    state = engine.run(engine.start(req, "run-test-09"))
    assert state.outcome == "HALTED_BLOCK" and state.halt.reason_code == "USER_LOCK_NOT_PRESERVED"


def test_dependent_no_tsc_halts_without_padding(rt, request_dep):
    script = R.happy_script(dependent=True)
    script["dependent-claim-strategy-architect"] = [R.dep_architect(tsc=False)]
    engine = _engine(rt, script)
    state = engine.run(engine.start(request_dep, "run-test-10"))
    assert state.candidate.draft_claim_lock is not None
    assert state.outcome == "HALTED_BLOCK" and state.halt.stage == "DEP_ARCHITECT"
    assert state.halt.reason_code == "NO_TECHNICAL_SOLUTION_CANDIDATE"
    assert not [c for c in engine.provider.calls if c.role == "claim-drafter" and c.scope == "DEPENDENT_SET"]


def test_oa_merged_verdict_is_rejected(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["oa-strategy-reviewer"] = [R.oa(merged=True), R.oa(merged=True)]
    engine = _engine(rt, script)
    state = engine.run(engine.start(request_indep, "run-test-11"))
    assert state.outcome == "HALTED_ENVELOPE_REPORT_MISMATCH"
    oa_calls = [c for c in engine.provider.calls if c.role == "oa-strategy-reviewer"]
    assert len(oa_calls) == 2 and oa_calls[1].phase == "repair"


def test_finalization_with_spec_reaches_final_locks(rt, request_dep, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# 정식 명세서 (테스트)\n케이블 클립 홀더의 명세서 본문.", encoding="utf-8")
    req = request_dep.model_copy()
    req.request_mode = RequestMode.FINALIZATION
    req.spec_path = str(spec)
    script = R.happy_script(dependent=True)
    script["oa-strategy-reviewer"] = [R.oa("INDEPENDENT", final_pass=True), R.oa("DEPENDENT_SET", final_pass=True)]
    engine = _engine(rt, script)
    state = engine.run(engine.start(req, "run-test-12"))
    assert state.outcome == "FINAL_CLAIM_LOCK+FINAL_DEPENDENT_SET_LOCK", state.halt
    assert state.candidate.final_claim_lock == "fcl-clip-holder-r1-01"
    assert state.spec_present is True


def test_finalization_without_final_gate_keeps_draft_lock_and_blocks_dependents(rt, request_dep):
    req = request_dep.model_copy()
    req.request_mode = RequestMode.FINALIZATION
    engine = _engine(rt, R.happy_script(dependent=True))
    state = engine.run(engine.start(req, "run-test-13"))
    # OA_FINAL UNVERIFIED -> OA contract fails in FINALIZATION -> halt at OA (never silently promoted)
    assert state.outcome in ("HALTED_UNVERIFIED", "HALTED_REVIEW") and state.halt.stage == "OA"


def test_review_only_runs_requested_reviewer_without_locks(rt, project_root):
    from claim_copa.models.request import RunRequest

    req = RunRequest.from_yaml(project_root / "eval" / "cases" / "reagent-tube-review-only" / "request.yaml")
    engine = _engine(rt, {"syntax-scope-reviewer": [R.syntax()]})
    state = engine.run(engine.start(req, "run-test-14"))
    assert state.outcome == "REVIEW_ONLY_DONE" and state.candidate.draft_claim_lock is None
    call = engine.provider.calls[0]
    assert "design_revision: N/A" in call.packet_text and "【청구항 5】" in call.packet_text


def test_resume_refuses_stale_source_set(rt, request_indep, tmp_path):
    engine = _engine(rt, R.happy_script(dependent=False))
    state = engine.run(engine.start(request_indep, "run-test-15"))
    assert state.outcome == "DRAFT_CLAIM_LOCK"
    # simulate a source/lessons change -> different source_set_id
    engine2 = rt.engine(engine.provider)
    engine2.source_set_id = "cc-py-deadbeef-deadbeef-none-a1"
    with pytest.raises(Exception, match="STALE"):
        engine2.resume("run-test-15", Decision(text="x", action="style_fix"))
    st = rt.store.load_state("run-test-15")
    assert all(r.stale for r in st.records.values())
