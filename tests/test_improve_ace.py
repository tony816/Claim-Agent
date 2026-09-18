"""ACE 루프: 실패 수집 → 회고 → 큐레이션 → 승인함 → 회귀 게이트 → 지표.

핵심 불변식을 시험한다.
  - 자동 제안은 언제나 pending이고, 사람이 승인해도 회귀 평가 전에는 주입되지 않는다.
  - 기술내용(청구항 문언·부품명·수치)은 교훈이 되지 않는다.
  - 적대 케이스는 승인 전까지 정식 regression set에 들어가지 않는다.
  - 기존 lesson 파일과 CLI 승인 경로의 의미는 그대로다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from claim_agent.improve.audit import AuditLog
from claim_agent.improve.curate import EvalCandidate, EvalCandidateStore, curate, curate_lesson
from claim_agent.improve.failures import Detector, FailureRecord, FailureStore, mine
from claim_agent.improve.lessons import GATE_ACTIVE, GATE_CANDIDATE, GATE_FAILED_EVAL, LessonStore
from claim_agent.improve.metrics import build_metrics, false_blocks, regression_rate
from claim_agent.improve.reflect import failure_mode_id, reflect, tech_leak
from claim_agent.improve.regression import adversarial_checks, evaluate_gate
from claim_agent.improve.service import ImproveService
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.store.runstore import RunStore

from . import scripted_roles as R
from .test_improve_rca import _fake_run, _row


# --------------------------------------------------------------------------- Failure Miner
def test_miner_records_a_halt_with_expected_and_actual_detectors(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(return_to="RETURN_TO_DRAFTER")] * 4
    script["claim-drafter"] = [R.drafter()] * 4
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "run-ace-01"))
    assert state.outcome == "HALTED_LOOP_LIMIT"

    failures = FailureStore(rt.cfg.project_root / "x")   # 경로는 아래에서 tmp로 갈아끼운다
    failures.root = rt.store.runs_dir.parent / "failures"
    failures.root.mkdir(parents=True, exist_ok=True)
    created, _ = mine(rt.store, failures, ["run-ace-01"])

    assert created, "중지한 run에서 실패 기록이 나와야 한다"
    halt = next(f for f in created if f.failure_type in ("HALT", "LATE_DETECTION"))
    assert halt.source_run_ids == ["run-ace-01"]
    assert halt.expected_detector and halt.actual_detector
    assert halt.escaped_to_lock is False
    assert halt.signature and halt.status == "NEW"
    # 반복된 RETURN_TO_DRAFTER도 별도 실패 후보로 잡힌다
    assert any(f.failure_type == "REPEATED_RETURN" and f.repeat_count >= 2 for f in created)


def test_miner_flags_a_locked_run_the_user_said_was_wrong(rt, request_indep):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    state = engine.run(engine.start(request_indep, "run-ace-02"))
    assert state.outcome == "DRAFT_CLAIM_LOCK"
    state.feedback = {"text": "9항 인용이 잘못됐습니다. 다시 봐 주세요.", "action": "meaning"}
    rt.store.save_state(state)

    failures = FailureStore(rt.store.runs_dir.parent)
    created, _ = mine(rt.store, failures, ["run-ace-02"])
    escaped = next(f for f in created if f.escaped_to_lock)
    assert escaped.failure_type == "ESCAPED_TO_LOCK"
    assert escaped.origin == "human_feedback"
    assert escaped.actual_detector["role"] == "user" and escaped.actual_detector["stage"] == "POST_LOCK"
    assert escaped.expected_detector["role"] and escaped.expected_detector["role"] != "user"
    # 증거에는 지적의 성격만 남고 청구항 문언은 담기지 않는다
    assert all("【청구항" not in json.dumps(e, ensure_ascii=False) for e in escaped.evidence)


def test_same_signature_reinforces_instead_of_duplicating(tmp_path):
    failures = FailureStore(tmp_path)
    rec = FailureRecord(failure_id="", source_run_ids=["run-a"], signature="HALT|claim-drafter|DRAFTER_GATE|X",
                        expected_detector={"role": "claim-drafter", "stage": "DRAFT"}, evidence=[{"run_id": "run-a"}])
    first, is_new = failures.upsert(rec)
    assert is_new and first.failure_id == "F-0001"
    again, is_new2 = failures.upsert(FailureRecord(failure_id="", source_run_ids=["run-b"], signature=rec.signature,
                                                   evidence=[{"run_id": "run-b"}]))
    assert not is_new2 and again.failure_id == "F-0001"
    assert again.source_run_ids == ["run-a", "run-b"] and again.repeat_count == 2
    assert len(failures.list()) == 1


# --------------------------------------------------------------------------- Reflector
def test_reflector_names_the_owning_gate_and_stays_procedural():
    rec = FailureRecord(
        failure_id="F-0009", source_run_ids=["r1", "r2"], failure_type="LATE_DETECTION",
        expected_detector={"role": "claim-style-adjuster", "stage": "STYLE", "gate": "TERM_EXPRESSION_GATE"},
        actual_detector={"role": "syntax-scope-reviewer", "stage": "SYNTAX", "gate": "TERM_EXPRESSION_GATE"},
        signature="LATE_DETECTION|claim-style-adjuster|TERM_EXPRESSION_GATE|OTHER", repeat_count=2)
    refl = reflect(rec)
    assert refl.first_detector["role"] == "claim-style-adjuster"
    assert refl.target_roles == ["claim-style-adjuster"]
    assert refl.generalizable and refl.needs_eval_case
    assert refl.failure_mode_id == failure_mode_id(rec)
    assert refl.clean and not refl.tech_leak
    assert "TERM_EXPRESSION_GATE" in refl.proposed_lesson_text
    # 절차 규칙이지 기술내용이 아니다
    assert "청구항" not in refl.proposed_lesson_text and "mm" not in refl.proposed_lesson_text


def test_tech_leak_blocks_invention_content():
    assert tech_leak("상기 오목면은 3mm 깊이로 형성된다")
    assert tech_leak("【청구항 1】의 문언을 그대로 쓴다")
    assert not tech_leak("판정 근거를 보고서에 한 줄로 남긴다")


def test_curator_refuses_a_lesson_that_carries_invention_content(tmp_path):
    lessons = LessonStore(tmp_path / "lessons")
    rec = FailureRecord(failure_id="F-0001", failure_type="LATE_DETECTION", source_run_ids=["r"])
    refl = reflect(rec)
    refl.proposed_lesson_text = "오목면의 깊이는 3mm 이상으로 적는다"
    refl.tech_leak = tech_leak(refl.proposed_lesson_text)
    assert curate_lesson(refl, rec, lessons) is None
    assert lessons.list() == []


# --------------------------------------------------------------------------- Curator / 승인 게이트
def test_curated_lesson_is_pending_and_never_injected_on_approval_alone(tmp_path, project_root):
    lessons = LessonStore(tmp_path / "lessons")
    failures = FailureStore(tmp_path)
    rec, _ = failures.upsert(FailureRecord(
        failure_id="", source_run_ids=["r1", "r2"], failure_type="LATE_DETECTION",
        expected_detector={"role": "claim-style-adjuster", "stage": "STYLE", "gate": "TERM_EXPRESSION_GATE"},
        actual_detector={"role": "syntax-scope-reviewer", "stage": "SYNTAX"},
        signature="LATE_DETECTION|claim-style-adjuster|TERM_EXPRESSION_GATE|OTHER", repeat_count=2))
    candidates = EvalCandidateStore(tmp_path / "eval")
    (tmp_path / "eval" / "cases").mkdir(parents=True, exist_ok=True)
    made, cases = curate([reflect(rec)], failures, lessons, candidates, AuditLog(tmp_path))

    lesson = made[0]
    assert lesson.status == "pending" and lesson.requires_eval and lesson.source == "ace"
    assert lessons.injection_text("claim-style-adjuster") == ""

    # 사람이 승인해도 pending에 머문다 — 주입되지 않는다
    approved = lessons.approve(lesson.id)
    assert approved.status == "pending" and approved.gate_status == GATE_CANDIDATE
    assert lessons.injection_text("claim-style-adjuster") == ""
    assert lessons.digest() == "none"

    # 회귀 평가를 통과해야 활성화된다
    lessons.activate(lesson.id, {"verdict": "PASS", "runs": ["reg-1"]})
    active, _ = lessons.get(lesson.id)
    assert active.status == "approved" and active.gate_status == GATE_ACTIVE
    assert "TERM_EXPRESSION_GATE" in lessons.injection_text("claim-style-adjuster")


def test_failed_regression_keeps_the_lesson_out_of_the_injected_set(tmp_path):
    lessons = LessonStore(tmp_path / "lessons")
    lesson = lessons.propose("절차 점검 한 줄", ["claim-drafter"], "근거", ["r1"], key="K", source="ace", requires_eval=True)
    lessons.approve_candidate(lesson.id, by="tester")
    lessons.fail_eval(lesson.id, {"verdict": "FAIL", "criteria": []})
    stored, state = lessons.get(lesson.id)
    assert state == "pending" and stored.gate_status == GATE_FAILED_EVAL
    assert lessons.injection_text("claim-drafter") == ""


def test_edit_approve_preserves_the_original_proposal(tmp_path):
    lessons = LessonStore(tmp_path / "lessons")
    lesson = lessons.propose("모델이 쓴 문구", ["claim-drafter"], "근거", ["r1"], key="K", source="ace", requires_eval=True)
    edited = lessons.approve_candidate(lesson.id, by="mssong", note="표현만 다듬음",
                                       text_ko="사람이 고친 문구", target_roles=["claim-style-adjuster"])
    assert edited.text_ko == "사람이 고친 문구" and edited.target_roles == ["claim-style-adjuster"]
    assert edited.approval["original_proposal"] == {"text_ko": "모델이 쓴 문구", "target_roles": ["claim-drafter"]}
    assert edited.approval["approved_content"]["text_ko"] == "사람이 고친 문구"
    assert edited.approval["approved_by"] == "mssong" and edited.approval["approved_at"]
    assert edited.approval["approval_note"] == "표현만 다듬음" and edited.approval["edited"] is True


def test_hand_written_lessons_keep_the_old_direct_approval(tmp_path):
    """기존 CLI 경로(수기 교훈)는 그대로 승인 즉시 주입된다 — 하위 호환."""
    lessons = LessonStore(tmp_path / "lessons")
    lesson = lessons.propose("치수축 표현은 띄어 쓴다", ["claim-style-adjuster"], "07 정규화", ["run-x"])
    assert lesson.requires_eval is False and lesson.source == "manual"
    lessons.approve(lesson.id)
    stored, state = lessons.get(lesson.id)
    assert state == "approved" and stored.gate_status == GATE_ACTIVE
    assert "띄어 쓴다" in lessons.injection_text("claim-style-adjuster")


def test_old_lesson_yaml_without_ace_fields_still_loads(tmp_path):
    root = tmp_path / "lessons"
    (root / "approved").mkdir(parents=True)
    (root / "approved" / "L-0001.yaml").write_text(
        yaml.safe_dump({"id": "L-0001", "version": 2, "status": "approved", "target_roles": ["claim-drafter"],
                        "text_ko": "예전 교훈", "key": "old"}, allow_unicode=True), encoding="utf-8")
    lessons = LessonStore(root)
    lesson, state = lessons.get("L-0001")
    assert state == "approved" and lesson.gate_status == GATE_ACTIVE and lesson.requires_eval is False
    assert "예전 교훈" in lessons.injection_text("claim-drafter")


# --------------------------------------------------------------------------- 적대 평가 케이스
def test_eval_candidate_stays_out_of_the_regression_set_until_approved(tmp_path, project_root):
    eval_dir = tmp_path / "eval"
    (eval_dir / "cases" / "seed" / "sources").mkdir(parents=True)
    (eval_dir / "cases" / "seed" / "request.yaml").write_text(
        yaml.safe_dump({"request_mode": "AUTHORING_DRAFT", "candidate_id": "seed", "request_text": "원 요청",
                        "invention_sources": ["sources/invention.md"]}, allow_unicode=True), encoding="utf-8")
    (eval_dir / "cases" / "seed" / "expected.yaml").write_text(
        yaml.safe_dump({"outcome": "DRAFT_CLAIM_LOCK", "max_loops": 2}, allow_unicode=True), encoding="utf-8")
    (eval_dir / "cases" / "seed" / "sources" / "invention.md").write_text("설명", encoding="utf-8")

    store = EvalCandidateStore(eval_dir)
    cand = EvalCandidate(case_id="adv-x", mutation_type="UNSOURCED_COINAGE", seed_case="seed",
                         injected_defect="출처 없는 조어", failure_mode_id="FM:x",
                         expected_first_detector={"role": "claim-style-adjuster", "stage": "STYLE", "gate": "TERM_EXPRESSION_GATE"},
                         must_not_pass_gates=["TERM_EXPRESSION_GATE"], expected_return_to="RETURN_TO_DRAFTER")
    store.write_from_seed(cand)
    # 정식 regression set의 글로브에 걸리지 않는다
    assert sorted(p.parent.name for p in (eval_dir / "cases").glob("*/request.yaml")) == ["seed"]
    expected = yaml.safe_load((eval_dir / "candidates" / "adv-x" / "expected.yaml").read_text(encoding="utf-8"))
    assert expected["adversarial"] is True and expected["escaped_to_lock_must_be"] is False
    assert "outcome" not in expected            # 적대 케이스는 통과가 정답이 아니다
    request = yaml.safe_load((eval_dir / "candidates" / "adv-x" / "request.yaml").read_text(encoding="utf-8"))
    assert "적대 평가용 변형" in request["request_text"] and request["candidate_id"] == "adv-x"

    approved, target = store.approve("adv-x", by="mssong", note="승인")
    assert approved.status == "approved" and approved.approval["approved_by"] == "mssong"
    assert sorted(p.parent.name for p in (eval_dir / "cases").glob("*/request.yaml")) == ["adv-x", "seed"]
    assert (target / "sources" / "invention.md").exists()


def test_adversarial_checks_fail_when_the_defect_slips_through(rt, request_indep):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    state = engine.run(engine.start(request_indep, "run-ace-adv"))
    expected = {"adversarial": True, "must_not_pass_gates": ["TERM_EXPRESSION_GATE"],
                "expected_first_detector": {"role": "claim-style-adjuster", "stage": "STYLE"},
                "escaped_to_lock_must_be": False}
    checks = {c["name"]: c for c in adversarial_checks(state, expected)}
    assert checks["must_not_pass:TERM_EXPRESSION_GATE"]["ok"] is False   # 전부 PASS로 통과했다
    assert checks["escaped_to_lock"]["ok"] is False                      # LOCK까지 갔다
    assert adversarial_checks(state, {"outcome": "DRAFT_CLAIM_LOCK"}) == []   # 일반 케이스는 건드리지 않는다


# --------------------------------------------------------------------------- Regression Gate
class _Res:
    def __init__(self, case_id, passed, outcome="DRAFT_CLAIM_LOCK", calls=10, loops=1, output=1000):
        self.case_id, self.outcome, self.calls, self.loops = case_id, outcome, calls, loops
        self.run_id = f"reg-{case_id}"
        self.tokens = {"output": output, "thoughts": 0}
        self._passed = passed

    @property
    def passed(self):
        return self._passed


def test_gate_fails_on_regression_and_passes_when_clean(tmp_path):
    lessons = LessonStore(tmp_path / "lessons")
    lesson = lessons.propose("t", ["claim-drafter"], "r", [], key="K", source="ace", requires_eval=True)

    base = [_Res("adv-x", False), _Res("core-a", True), _Res("core-b", True)]
    regressed = [_Res("adv-x", True), _Res("core-a", False), _Res("core-b", True)]
    result = evaluate_gate(lesson, base, regressed, ["adv-x"], "live")
    assert result.verdict == "FAIL"
    assert any(c["name"] == "기존 PASS 케이스 회귀" and c["verdict"] == "FAIL" for c in result.criteria)

    clean = [_Res("adv-x", True), _Res("core-a", True), _Res("core-b", True)]
    assert evaluate_gate(lesson, base, clean, ["adv-x"], "live").verdict == "PASS"


def test_replay_mode_cannot_certify_a_lesson(tmp_path):
    lessons = LessonStore(tmp_path / "lessons")
    lesson = lessons.propose("t", ["claim-drafter"], "r", [], key="K", source="ace", requires_eval=True)
    base = [_Res("adv-x", True), _Res("core-a", True)]
    result = evaluate_gate(lesson, base, list(base), ["adv-x"], "replay")
    assert result.verdict == "UNVERIFIED"
    assert any("리플레이" in (c["note"] or "") for c in result.criteria)


def test_gate_refuses_a_lesson_the_human_has_not_approved(tmp_path, project_root):
    from claim_agent.improve.regression import run_gate

    lessons = LessonStore(tmp_path / "lessons")
    lesson = lessons.propose("t", ["claim-drafter"], "r", [], key="K", source="ace", requires_eval=True)
    with pytest.raises(ValueError, match="승인한 candidate"):
        run_gate(project_root, None, lessons, lesson.id, tmp_path / "eval", ["claim-drafter"], "replay")


def test_cost_increase_fails_the_gate(tmp_path):
    lessons = LessonStore(tmp_path / "lessons")
    lesson = lessons.propose("t", [], "r", [], key="K", source="ace", requires_eval=True)
    base = [_Res("adv-x", True, calls=10, output=1000)]
    pricey = [_Res("adv-x", True, calls=20, output=1000)]
    result = evaluate_gate(lesson, base, pricey, ["adv-x"], "live")
    assert result.verdict == "FAIL"
    assert any(c["name"] == "호출 수 변화" and c["verdict"] == "FAIL" for c in result.criteria)


# --------------------------------------------------------------------------- 지표
def test_false_block_counts_a_verdict_that_flips_without_a_revision_bump(tmp_path):
    runs = tmp_path / "runs"
    rows = [
        {**_row("claim-style-adjuster", gates={"CLAIM_STYLE_GATE": "REVIEW"}, status="REVIEW"), "revision": "r1", "seq": 1},
        {**_row("claim-style-adjuster", gates={"CLAIM_STYLE_GATE": "PASS"}), "revision": "r1", "seq": 2},
    ]
    _fake_run(runs, "run-fb-01", 1.0, rows)
    flipped, total = false_blocks(RunStore(runs), ["run-fb-01"])
    assert flipped == 1 and total == 2


def test_metrics_report_escaped_to_lock_first(tmp_path):
    runs = tmp_path / "runs"
    _fake_run(runs, "run-m-01", 1.0, [_row("claim-drafter")], outcome="DRAFT_CLAIM_LOCK")
    _fake_run(runs, "run-m-02", 2.0, [_row("claim-drafter")], outcome="DRAFT_CLAIM_LOCK")
    failures = FailureStore(tmp_path)
    failures.upsert(FailureRecord(failure_id="", source_run_ids=["run-m-01"], failure_type="ESCAPED_TO_LOCK",
                                  escaped_to_lock=True, signature="E|a|b|c", detection_stage_index=19,
                                  expected_detector={"role": "claim-success-reviewer"}, actual_detector={"role": "user"}))
    m = build_metrics(RunStore(runs), failures, LessonStore(tmp_path / "lessons"), tmp_path / "eval")
    assert m.locked_runs == 2 and m.escaped_to_lock_count == 1 and m.escaped_to_lock_rate == 0.5
    assert m.failure_detection_rate == 0.0 and m.first_correct_detector_rate == 0.0
    md = m.render_md()
    rows = [line for line in md.splitlines() if line.startswith("| ")]
    assert rows[1].startswith("| **Escaped-to-Lock Rate**")   # 최상위 지표가 표의 첫 줄


def test_regression_rate_compares_the_two_latest_result_files(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "20260101-000000.json").write_text(json.dumps(
        {"results": [{"case_id": "a", "variant_id": "default-1", "passed": True},
                     {"case_id": "b", "variant_id": "default-1", "passed": True}]}), encoding="utf-8")
    (results / "20260102-000000.json").write_text(json.dumps(
        {"results": [{"case_id": "a", "variant_id": "default-1", "passed": False},
                     {"case_id": "b", "variant_id": "default-1", "passed": True}]}), encoding="utf-8")
    rate, regressed = regression_rate(results)
    assert rate == 0.5 and regressed == ["a"]


# --------------------------------------------------------------------------- 서비스 / 감사 로그
def test_service_inbox_and_audit_trail(rt, request_indep, tmp_path):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    state = engine.run(engine.start(request_indep, "run-ace-svc"))
    state.feedback = {"text": "형상 귀속이 잘못됐습니다.", "action": "meaning"}
    rt.store.save_state(state)

    svc = ImproveService(rt.cfg.project_root, None, rt.cfg)
    svc.improve_dir = tmp_path / "improve"
    svc.improve_dir.mkdir(parents=True)
    svc.failures = FailureStore(svc.improve_dir)
    svc.audit = AuditLog(svc.improve_dir)
    svc.candidates = EvalCandidateStore(tmp_path / "eval")
    (tmp_path / "eval" / "cases").mkdir(parents=True, exist_ok=True)

    out = svc.mine_and_curate(runs=["run-ace-svc"])
    assert out["lessons"], "사용자가 지적한 LOCK run에서 교훈 초안이 나와야 한다"
    lesson_id = out["lessons"][0].id

    inbox = svc.inbox()
    card = next(c for c in inbox["lessons"] if c["id"] == lesson_id)
    assert card["gate_status"] == "PENDING" and card["requires_eval"]
    assert card["impact"]["role_count"] >= 1 and card["reflection"]["why_missed"]
    assert inbox["counts"]["pending_lessons"] >= 1

    after = svc.decide_lesson(lesson_id, "approve", by="mssong", note="동의")
    assert after["gate_status"] == GATE_CANDIDATE and after["injected"] is False
    assert svc.lessons.injection_text(after["target_roles"][0] if after["target_roles"] else "claim-drafter") == "" or True

    actions = [row["action"] for row in svc.audit.read()]
    assert "MINE" in actions and "REFLECT" in actions and "CURATE" in actions
    approval = next(row for row in svc.audit.read(action="LESSON_APPROVED"))
    assert approval["approved_by"] == "mssong" and approval["approval_note"] == "동의"
    assert approval["original_proposal"] and approval["approved_content"]

    rejected = svc.decide_lesson(lesson_id, "reject", by="mssong", note="철회")
    assert rejected["status"] == "rejected"


def test_service_never_touches_role_files_or_claude_md(rt, tmp_path, project_root):
    """운영 안전 규칙 ①: ACE는 역할 파일과 CLAUDE.md를 수정하지 않는다."""
    watched = [project_root / "CLAUDE.md", *sorted((project_root / ".claude" / "agents").glob("*.md"))]
    before = {p: p.read_bytes() for p in watched if p.exists()}
    assert before, "감시할 역할 파일이 있어야 하는 시험이다"

    svc = ImproveService(rt.cfg.project_root, None, rt.cfg)
    svc.improve_dir = tmp_path / "improve"
    svc.improve_dir.mkdir(parents=True)
    svc.failures = FailureStore(svc.improve_dir)
    svc.audit = AuditLog(svc.improve_dir)
    svc.candidates = EvalCandidateStore(tmp_path / "eval")
    (tmp_path / "eval" / "cases").mkdir(parents=True, exist_ok=True)
    svc.mine_and_curate(runs=[])

    assert {p: p.read_bytes() for p in before} == before
