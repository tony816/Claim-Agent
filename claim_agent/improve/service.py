"""ACE 루프의 단일 진입점. CLI와 웹 Approval Inbox가 같은 객체를 쓴다.

여기서 하는 일은 수집·분석·제안·승인 기록·게이트 실행뿐이다. 역할 파일(`.claude/agents/*.md`),
`CLAUDE.md`, 파이프라인 소스는 열지도 고치지도 않는다.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import AppConfig, load_config
from ..roles.registry import ROLE_NAMES
from ..store.runstore import RunStore
from .audit import AuditLog
from .curate import EvalCandidate, EvalCandidateStore, curate
from .curate import render_md as curate_md
from .failures import FailureStore, mine
from .lessons import GATE_ACTIVE, GATE_CANDIDATE, GATE_FAILED_EVAL, GATE_HELD, Lesson, LessonStore
from .metrics import build_metrics
from .reflect import reflect, reflect_with_llm
from .reflect import render_md as reflect_md

LESSON_ACTIONS = ("approve", "reject", "edit_approve", "hold")
CASE_ACTIONS = ("approve", "reject", "edit_approve")

GATE_LABEL = {
    "PENDING": "제안됨 (승인 대기)",
    GATE_CANDIDATE: "승인됨 — regression eval 대기 (주입 안 됨)",
    GATE_ACTIVE: "활성 — 후속 run에 주입됨",
    GATE_FAILED_EVAL: "승인했으나 eval 실패 — 주입 안 됨",
    GATE_HELD: "보류",
}


def gate_label(lesson: Lesson) -> str:
    """‘판정 불가’를 ‘실패’로 보고하지 않는다. 둘 다 주입되지 않는 것은 같다."""
    if lesson.gate_status == GATE_FAILED_EVAL and lesson.eval_record.get("verdict") == "UNVERIFIED":
        return "승인했으나 회귀 평가 판정 불가 — 주입 안 됨"
    return GATE_LABEL.get(lesson.gate_status, lesson.gate_status)


class ImproveService:
    def __init__(self, project_root: Path, config_path: Path | None = None, cfg: AppConfig | None = None):
        self.project_root = project_root.resolve()
        self.config_path = config_path
        self.cfg = cfg or load_config(config_path, self.project_root)
        self.store = RunStore(self.cfg.path("runs_dir"))
        self.improve_dir = self.cfg.path("improve_dir")
        self.improve_dir.mkdir(parents=True, exist_ok=True)
        self.failures = FailureStore(self.improve_dir)
        self.lessons = LessonStore(self.cfg.path("lessons_dir"))
        self.candidates = EvalCandidateStore(self.cfg.path("eval_dir"))
        self.audit = AuditLog(self.improve_dir)

    # ------------------------------------------------------------------ ①②③ 한 번에
    def mine_and_curate(self, runs: list[str] | None = None, since: str | None = None,
                        provider: Any = None, actor: str = "system") -> dict[str, Any]:
        """Failure Miner → Reflector → Curator. 산출물은 전부 pending이다."""
        eval_results = self.cfg.path("eval_dir") / "results"
        created, reinforced = mine(self.store, self.failures, runs, eval_results, since)
        self.audit.write("MINE", actor=actor, created=[f.failure_id for f in created],
                         reinforced=[f.failure_id for f in reinforced], since=since)

        known = self.lessons.by_failure_mode()
        reflections = []
        for rec in self.failures.list("NEW"):
            refl = reflect(rec, known, self.cfg.improve.min_repeat)
            if provider is not None and self.cfg.improve.reflect_llm:
                refl = reflect_with_llm(provider, self.cfg.default_model, rec, refl)
            rec.reflection = refl.as_dict()
            rec.status = "REFLECTED"
            self.failures.save(rec)
            reflections.append(refl)
        self.audit.write("REFLECT", actor=actor, failures=[r.failure_id for r in reflections],
                         blocked_by_tech_gate=[r.failure_id for r in reflections if not r.clean])

        lessons_made, cases_made = curate(reflections, self.failures, self.lessons, self.candidates, self.audit)
        self.audit.write("CURATE", actor=actor, lessons=[l.id for l in lessons_made], cases=[c.case_id for c in cases_made])
        return {
            "created": [f.failure_id for f in created],
            "reinforced": [f.failure_id for f in reinforced],
            "reflections": reflections,
            "lessons": lessons_made,
            "cases": cases_made,
            "report": reflect_md(reflections, self.failures) + "\n" + curate_md(lessons_made, cases_made),
        }

    # ------------------------------------------------------------------ ④ Approval Inbox
    def lesson_card(self, lesson: Lesson) -> dict[str, Any]:
        """Inbox 카드 한 장. 제안 문구·대상 역할·failure mode·근거 run·회고 요약·중복·영향 범위."""
        similar = [l.id for l in self.lessons.list()
                   if l.id != lesson.id and lesson.failure_mode_id and l.failure_mode_id == lesson.failure_mode_id]
        roles = lesson.target_roles or list(ROLE_NAMES)
        return {
            "id": lesson.id,
            "version": lesson.version,
            "text_ko": lesson.text_ko,
            "target_roles": lesson.target_roles,
            "failure_mode_id": lesson.failure_mode_id,
            "failure_ids": lesson.failure_ids,
            "evidence": lesson.evidence,
            "reflection": lesson.reflection,
            "rationale": lesson.rationale,
            "similar": similar + lesson.reflection.get("duplicate_of", []),
            "impact": {"roles": roles, "role_count": len(roles),
                       "scopes": lesson.scopes, "invention_types": lesson.invention_types},
            "source": lesson.source,
            "llm_drafted": lesson.llm_drafted,
            "requires_eval": lesson.requires_eval,
            "status": lesson.status,
            "gate_status": lesson.gate_status,
            "gate_label": gate_label(lesson),
            "helpful": lesson.helpful,
            "harmful": lesson.harmful,
            "approval": lesson.approval,
            "eval_record": lesson.eval_record,
            "injected": lesson.injected,
        }

    def case_card(self, cand: EvalCandidate) -> dict[str, Any]:
        failure = None
        if cand.failure_id:
            try:
                rec = self.failures.get(cand.failure_id)
                failure = {"failure_id": rec.failure_id, "failure_type": rec.failure_type,
                           "root_cause_summary": rec.root_cause_summary, "source_run_ids": rec.source_run_ids}
            except FileNotFoundError:
                failure = None
        return {**asdict(cand), "failure": failure,
                "expected_first_detector_label": " / ".join(
                    str(cand.expected_first_detector.get(k) or "-") for k in ("stage", "role", "gate"))}

    def inbox(self) -> dict[str, Any]:
        lessons = [self.lesson_card(l) for l in self.lessons.list()
                   if l.status == "pending" or l.gate_status in (GATE_CANDIDATE, GATE_FAILED_EVAL)]
        active = [self.lesson_card(l) for l in self.lessons.list("approved")]
        cases = [self.case_card(c) for c in self.candidates.list()]
        open_lessons = [l for l in lessons if l["gate_status"] == "PENDING"]
        return {
            "lessons": lessons,
            "active_lessons": active,
            "cases": cases,
            "failures": [asdict(f) for f in self.failures.list()],
            "counts": {
                "pending_lessons": len(open_lessons),
                "awaiting_eval": sum(1 for l in lessons if l["gate_status"] == GATE_CANDIDATE),
                "failed_eval": sum(1 for l in lessons if l["gate_status"] == GATE_FAILED_EVAL),
                "active_lessons": len(active),
                "pending_cases": sum(1 for c in cases if c["status"] == "pending"),
                "open_failures": sum(1 for f in self.failures.list() if f.status != "CLOSED"),
            },
            "audit": self.audit.read(limit=50),
        }

    def decide_lesson(self, lesson_id: str, action: str, by: str = "user", note: str | None = None,
                      text_ko: str | None = None, target_roles: list[str] | None = None) -> dict[str, Any]:
        """승인 / 거절 / 수정 후 승인 / 보류. 승인은 주입이 아니라 regression 대기다."""
        if action not in LESSON_ACTIONS:
            raise ValueError(f"알 수 없는 동작: {action}")
        if action == "reject":
            lesson = self.lessons.reject(lesson_id, note=note, by=by)
            self.audit.write("LESSON_REJECTED", actor=by, lesson_id=lesson_id, approval_note=note)
        elif action == "hold":
            lesson = self.lessons.hold(lesson_id, note=note, by=by)
            self.audit.write("LESSON_HELD", actor=by, lesson_id=lesson_id, approval_note=note)
        else:
            edited = action == "edit_approve"
            lesson = self.lessons.approve_candidate(
                lesson_id, by=by, note=note,
                text_ko=text_ko if edited else None,
                target_roles=target_roles if edited else None)
            self.audit.write("LESSON_EDIT_APPROVED" if edited else "LESSON_APPROVED", actor=by, lesson_id=lesson_id,
                             approved_by=by, approved_at=lesson.approved_at,
                             original_proposal=lesson.approval.get("original_proposal"),
                             approved_content=lesson.approval.get("approved_content"), approval_note=note)
        return self.lesson_card(lesson)

    def decide_case(self, case_id: str, action: str, by: str = "user", note: str | None = None,
                    patch: dict[str, Any] | None = None) -> dict[str, Any]:
        if action not in CASE_ACTIONS:
            raise ValueError(f"알 수 없는 동작: {action}")
        if action == "reject":
            cand = self.candidates.reject(case_id, by=by, note=note)
            self.audit.write("CASE_REJECTED", actor=by, case_id=case_id, approval_note=note)
            return self.case_card(cand)
        cand, target = self.candidates.approve(case_id, by=by, note=note, patch=patch if action == "edit_approve" else None)
        self.audit.write("CASE_EDIT_APPROVED" if action == "edit_approve" else "CASE_APPROVED", actor=by, case_id=case_id,
                         approved_by=by, approved_at=cand.approval.get("approved_at"),
                         original_proposal=cand.approval.get("original_proposal"),
                         approved_content=cand.approval.get("approved_content"), approval_note=note,
                         promoted_to=str(target.relative_to(self.project_root)) if target.is_relative_to(self.project_root) else str(target))
        return self.case_card(cand)

    # ------------------------------------------------------------------ ⑤ Regression Gate
    def regress(self, lesson_id: str, mode: str | None = None, by: str = "user") -> dict[str, Any]:
        from .regression import run_gate, write_result

        mode = mode or self.cfg.improve.regression_mode
        result, lesson = run_gate(self.project_root, self.config_path, self.lessons, lesson_id,
                                  self.cfg.path("eval_dir"), list(ROLE_NAMES), mode,
                                  self.cfg.improve.regression_cases or None)
        path = write_result(self.improve_dir / "regression", result)
        self.audit.write("REGRESSION_RUN", actor=by, lesson_id=lesson_id, mode=mode, verdict=result.verdict,
                         report=str(path.name))
        self.audit.write("LESSON_ACTIVATED" if result.verdict == "PASS" else "LESSON_EVAL_FAILED",
                         actor="regression-gate", lesson_id=lesson_id, verdict=result.verdict)
        return {"result": result, "lesson": self.lesson_card(lesson), "report_path": path}

    # ------------------------------------------------------------------ ⑧ 지표
    def metrics(self, update_scores: bool = False) -> Any:
        return build_metrics(self.store, self.failures, self.lessons, self.cfg.path("eval_dir"),
                             candidates_open=sum(1 for c in self.candidates.list() if c.status == "pending"),
                             update_scores=update_scores)
