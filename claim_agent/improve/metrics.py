"""ACE ⑧ 핵심 지표. 정의를 코드와 보고서에 함께 적어 해석이 흔들리지 않게 한다.

    Failure Detection Rate   파이프라인이 스스로 잡은 실패 / 전체 실패
    First Correct Detector   최초 탐지 지점 = 기대 탐지 지점인 실패 / 전체 실패
    Escaped-to-Lock Rate     LOCK까지 간 run 중 뒤에서 오류가 확인된 비율  ← 최상위 품질 지표
    False Block Rate         같은 revision에서 비-PASS였다가 문언 변경 없이 PASS로 바뀐 게이트 판정 비율
    Regression Rate          직전 eval 결과에서 PASS였다가 최신 결과에서 FAIL이 된 케이스 비율
    Lesson Hit Rate          활성 교훈 중 활성화 이후 같은 failure mode가 재발하지 않은 비율
    Lesson Helpful/Harmful   위 판정을 교훈별로 누적한 수
    Mean Detection Stage     실패가 실제로 잡힌 단계의 평균 좌표 (작을수록 이르다)

LLM은 쓰지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..store.runstore import RunStore
from ..store.telemetry import read_telemetry
from .failures import STAGE_ORDER, FailureRecord, FailureStore
from .feedback import PASSY, is_gating_failure
from .lessons import GATE_ACTIVE, Lesson, LessonStore


@dataclass
class Metrics:
    failures: int = 0
    failure_detection_rate: float | None = None
    first_correct_detector_rate: float | None = None
    escaped_to_lock_rate: float | None = None
    escaped_to_lock_count: int = 0
    locked_runs: int = 0
    false_block_rate: float | None = None
    false_blocks: int = 0
    gate_verdicts: int = 0
    regression_rate: float | None = None
    regressed_cases: list[str] = field(default_factory=list)
    lesson_hit_rate: float | None = None
    lesson_scores: list[dict[str, Any]] = field(default_factory=list)
    mean_detection_stage: float | None = None
    mean_detection_stage_label: str = "-"
    by_type: dict[str, int] = field(default_factory=dict)
    by_role: dict[str, int] = field(default_factory=dict)
    open_lessons: int = 0
    active_lessons: int = 0
    open_cases: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    def render_md(self) -> str:
        def pct(v: float | None) -> str:
            return "—" if v is None else f"{v:.0%}"

        out = ["# ACE 개선 지표", ""]
        out.append("| 지표 | 값 | 정의 |")
        out.append("|---|---|---|")
        out.append(f"| **Escaped-to-Lock Rate** | **{pct(self.escaped_to_lock_rate)}** ({self.escaped_to_lock_count}/{self.locked_runs}) | LOCK까지 간 run 중 뒤에서 오류가 확인된 비율 — 최상위 품질 지표 |")
        out.append(f"| Failure Detection Rate | {pct(self.failure_detection_rate)} | 파이프라인이 스스로 잡은 실패 / 전체 실패 |")
        out.append(f"| First Correct Detector Rate | {pct(self.first_correct_detector_rate)} | 최초 탐지 지점이 기대 지점과 일치한 비율 |")
        out.append(f"| False Block Rate | {pct(self.false_block_rate)} ({self.false_blocks}/{self.gate_verdicts}) | 문언 변경 없이 뒤집힌 비-PASS 판정 비율 |")
        out.append(f"| Regression Rate | {pct(self.regression_rate)} | 직전 eval에서 PASS였다가 최신에서 FAIL이 된 케이스 |")
        out.append(f"| Lesson Hit Rate | {pct(self.lesson_hit_rate)} | 활성 교훈 중 같은 failure mode가 재발하지 않은 비율 |")
        out.append(f"| Mean Detection Stage | {self.mean_detection_stage if self.mean_detection_stage is not None else '—'} ({self.mean_detection_stage_label}) | 실패가 실제로 잡힌 단계의 평균 좌표 (작을수록 이르다) |")
        out.append("")
        out.append(f"- 실패 기록 {self.failures}건 / 승인 대기 교훈 {self.open_lessons}건 / 활성 교훈 {self.active_lessons}건 / 승인 대기 적대 케이스 {self.open_cases}건")
        if self.regressed_cases:
            out.append(f"- 회귀 케이스: {', '.join(self.regressed_cases)}")
        out.append("")
        if self.by_type:
            out.append("## 실패 유형 분포")
            out.append("")
            for k, v in sorted(self.by_type.items(), key=lambda x: -x[1]):
                out.append(f"- {k}: {v}")
            out.append("")
        if self.by_role:
            out.append("## 기대 탐지 역할별 실패")
            out.append("")
            for k, v in sorted(self.by_role.items(), key=lambda x: -x[1]):
                out.append(f"- {k}: {v}")
            out.append("")
        if self.lesson_scores:
            out.append("## 교훈별 helpful / harmful")
            out.append("")
            out.append("| 교훈 | 상태 | helpful | harmful | failure mode |")
            out.append("|---|---|---|---|---|")
            for s in self.lesson_scores:
                out.append(f"| {s['id']} | {s['gate_status']} | {s['helpful']} | {s['harmful']} | `{s['failure_mode_id'] or '-'}` |")
            out.append("")
        out.append("> 선행기술 없이 신규성·진보성을 단정하지 않듯, 이 지표도 표본이 작으면 추세로만 읽는다.")
        return "\n".join(out) + "\n"


def _detected_by_pipeline(rec: FailureRecord) -> bool:
    role = str(rec.actual_detector.get("role") or "")
    return bool(role) and role not in ("user", "engine")


def _first_correct(rec: FailureRecord) -> bool:
    exp, act = rec.expected, rec.actual
    return bool(exp.role) and exp.role == act.role and (not exp.gate or exp.gate == act.gate)


def false_blocks(store: RunStore, run_ids: list[str]) -> tuple[int, int]:
    """(뒤집힌 비-PASS 수, 전체 게이트 판정 수).

    같은 revision·같은 (역할, 게이트)에서 비-PASS가 났다가 문언 revision이 올라가지 않은 채
    PASS로 바뀌면 그 최초 판정은 근거가 약했던 것으로 본다.
    """
    flipped = total = 0
    for run_id in run_ids:
        rows = [r for r in read_telemetry(store.telemetry_path(run_id)) if r.get("phase") in (None, "main")]
        seen: dict[tuple[str, str, str], str] = {}
        for r in rows:
            rev = r.get("revision") or r.get("dependent_revision") or ""
            for gate, val in (r.get("gates") or {}).items():
                total += 1
                key = (r.get("role", ""), gate, rev)
                prior = seen.get(key)
                if prior is not None and is_gating_failure(gate, prior, r.get("reason_code")) and val in PASSY:
                    flipped += 1
                seen[key] = val
    return flipped, total


def regression_rate(results_dir: Path) -> tuple[float | None, list[str]]:
    """최신 eval 결과와 그 직전 결과를 비교한다."""
    files = sorted(results_dir.glob("*.json")) if results_dir.exists() else []
    if len(files) < 2:
        return None, []
    def load(p: Path) -> dict[str, bool]:
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {r["case_id"]: bool(r.get("passed")) for r in payload.get("results", []) if r.get("variant_id", "").startswith("default")}
    prev, latest = load(files[-2]), load(files[-1])
    shared = [c for c in latest if c in prev]
    if not shared:
        return None, []
    regressed = sorted(c for c in shared if prev[c] and not latest[c])
    return len(regressed) / len(shared), regressed


def lesson_effect(lessons: LessonStore, failures: FailureStore, update_scores: bool = False) -> tuple[float | None, list[dict[str, Any]]]:
    """활성 교훈별로 활성화 이후 같은 failure mode가 재발했는지 본다."""
    active = [l for l in lessons.list("approved") if l.gate_status == GATE_ACTIVE]
    if not active:
        return None, []
    recs = failures.list()
    scores: list[dict[str, Any]] = []
    hits = 0
    for l in active:
        since = l.approved_at or l.created_at or ""
        recurred = [r for r in recs if r.reflection and r.reflection.get("failure_mode_id") == l.failure_mode_id
                    and (r.updated_at or r.created_at or "") > since] if l.failure_mode_id else []
        helpful = not recurred
        hits += 1 if helpful else 0
        if update_scores:
            l = lessons.score(l.id, helpful=1 if helpful else 0, harmful=0 if helpful else 1)
        scores.append({"id": l.id, "gate_status": l.gate_status, "helpful": l.helpful, "harmful": l.harmful,
                       "failure_mode_id": l.failure_mode_id, "recurred": [r.failure_id for r in recurred]})
    return hits / len(active), scores


def build_metrics(store: RunStore, failures: FailureStore, lessons: LessonStore, eval_dir: Path,
                  candidates_open: int = 0, update_scores: bool = False) -> Metrics:
    recs = failures.list()
    m = Metrics(failures=len(recs))
    run_ids = store.list_runs()

    if recs:
        m.failure_detection_rate = sum(1 for r in recs if _detected_by_pipeline(r)) / len(recs)
        m.first_correct_detector_rate = sum(1 for r in recs if _first_correct(r)) / len(recs)
        indices = [r.detection_stage_index for r in recs if r.detection_stage_index is not None]
        if indices:
            mean = sum(indices) / len(indices)
            m.mean_detection_stage = round(mean, 2)
            m.mean_detection_stage_label = STAGE_ORDER[min(int(round(mean)), len(STAGE_ORDER) - 1)]
        for r in recs:
            m.by_type[r.failure_type] = m.by_type.get(r.failure_type, 0) + 1
            role = str(r.expected_detector.get("role") or "미지정")
            m.by_role[role] = m.by_role.get(role, 0) + 1

    locked: list[str] = []
    for run_id in run_ids:
        try:
            state = json.loads((store.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(state.get("outcome", "")).endswith("LOCK"):
            locked.append(run_id)
    escaped_runs = {rid for r in recs if r.escaped_to_lock for rid in r.source_run_ids}
    m.locked_runs = len(locked)
    m.escaped_to_lock_count = len([r for r in locked if r in escaped_runs])
    if locked:
        m.escaped_to_lock_rate = m.escaped_to_lock_count / len(locked)

    m.false_blocks, m.gate_verdicts = false_blocks(store, run_ids)
    if m.gate_verdicts:
        m.false_block_rate = m.false_blocks / m.gate_verdicts

    m.regression_rate, m.regressed_cases = regression_rate(eval_dir / "results")
    m.lesson_hit_rate, m.lesson_scores = lesson_effect(lessons, failures, update_scores)

    all_lessons = lessons.list()
    m.active_lessons = sum(1 for l in all_lessons if l.status == "approved" and l.gate_status == GATE_ACTIVE)
    m.open_lessons = sum(1 for l in all_lessons if l.status == "pending")
    m.open_cases = candidates_open
    return m


__all__ = ["Metrics", "build_metrics", "false_blocks", "regression_rate", "lesson_effect", "Lesson"]
