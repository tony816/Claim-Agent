"""ACE ① Failure Miner: 실패 후보를 하나의 기록 형식으로 모은다.

기존 RCA가 보는 명시적 HALT·비-PASS 말고도 다음을 실패 후보로 수집한다.
  - 끝까지 LOCK까지 갔지만 사용자 피드백이 오류를 확인한 경우 (escaped_to_lock)
  - 예상 게이트보다 늦게 잡힌 오류 (late detection)
  - 반복되는 RETURN_TO_* 패턴
  - adversarial eval에서 기대 탐지 지점을 놓친 경우

LLM은 쓰지 않는다. 모든 판단은 저장된 telemetry·state·eval 결과에서 계산한다.
기술내용(청구항 문언·부품명·수치)은 어떤 필드에도 담지 않는다.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..store.runstore import RunStore
from ..store.telemetry import read_telemetry
from .feedback import PASSY, build_feedback, is_gating_failure
from .rca import build_rca

# 파이프라인 단계 순서. Mean Detection Stage와 "예상보다 늦게 잡혔는가" 판정의 좌표계다.
STAGE_ORDER = [
    "ARCHITECT", "DRAFT", "STYLE", "SUCCESS", "SYNTAX", "OA", "BLIND", "PICTURE", "LOCK",
    "DEP_ARCHITECT", "DEP_DRAFT", "DEP_STYLE", "DEP_SUCCESS", "DEP_SYNTAX", "DEP_OA",
    "DEP_BLIND", "DEP_PICTURE", "DEP_RECON", "DEP_LOCK", "POST_LOCK",
]
STAGE_INDEX = {s: i for i, s in enumerate(STAGE_ORDER)}

FAILURE_TYPES = ("HALT", "ESCAPED_TO_LOCK", "LATE_DETECTION", "REPEATED_RETURN", "EVAL_MISS")
ORIGINS = ("runtime", "eval", "human_feedback")
STATUSES = ("NEW", "REFLECTED", "CURATED", "CLOSED")

# 게이트가 어느 역할·단계의 책임인지. Reflector가 expected_detector를 정할 때 쓴다.
GATE_OWNER: dict[str, tuple[str, str]] = {
    "DESIGN_GATE": ("claim-architect", "ARCHITECT"),
    "DRAFTER_GATE": ("claim-drafter", "DRAFT"),
    "CLAIM_STYLE_GATE": ("claim-style-adjuster", "STYLE"),
    "TERM_EXPRESSION_GATE": ("claim-style-adjuster", "STYLE"),
    "NON_PATENT_TECHNICAL_READER_GATE": ("claim-style-adjuster", "STYLE"),
    "GEOMETRIC_OBJECT_GATE": ("claim-style-adjuster", "STYLE"),
    "OA_DRAFT_GATE": ("oa-strategy-reviewer", "OA"),
    "OA_FINAL_GATE": ("oa-strategy-reviewer", "OA"),
    "DEPENDENT_DESIGN_GATE": ("dependent-claim-strategy-architect", "DEP_ARCHITECT"),
    "DEPENDENT_SOURCE_GATE": ("dependent-claim-strategy-architect", "DEP_ARCHITECT"),
    "CAUSAL_CONTRIBUTION_GATE": ("dependent-claim-strategy-architect", "DEP_ARCHITECT"),
    "CLAIMABILITY_GATE": ("dependent-claim-strategy-architect", "DEP_ARCHITECT"),
    "DEPENDENT_OA_DRAFT_GATE": ("oa-strategy-reviewer", "DEP_OA"),
    "DEPENDENT_OA_FINAL_GATE": ("oa-strategy-reviewer", "DEP_OA"),
    "DEPENDENT_RECONSTRUCTION_GATE": ("picture-claim-reconstruction-reviewer", "DEP_PICTURE"),
}

# 사용자 후속 대화에서 "이미 낸 결과가 틀렸다"는 신호. 단순 추가 요청과 구분한다.
_CORRECTION_HINTS = (
    "틀렸", "잘못", "오류", "빠졌", "누락", "아닙니다", "아니라", "안 맞", "맞지 않", "수정해", "고쳐",
    "이상하", "말이 안", "다시 봐", "빠뜨", "반대로", "거꾸로",
)


@dataclass
class Detector:
    """어디서 잡혔거나 잡혔어야 하는가. 값이 없으면 빈 문자열/None."""

    role: str = ""
    stage: str = ""
    gate: str | None = None

    @property
    def stage_index(self) -> int:
        return STAGE_INDEX.get(self.stage, len(STAGE_ORDER))

    def label(self) -> str:
        return f"{self.stage or '-'} / {self.role or '-'}" + (f" / {self.gate}" if self.gate else "")


@dataclass
class FailureRecord:
    failure_id: str
    source_run_ids: list[str] = field(default_factory=list)
    failure_type: str = "HALT"
    invention_type: str | None = None
    origin: str = "runtime"
    expected_detector: dict[str, Any] = field(default_factory=dict)
    actual_detector: dict[str, Any] = field(default_factory=dict)
    escaped_to_lock: bool = False
    root_cause_summary: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    # ---- 아래는 집계·중복 제거용 부가 필드. 스펙의 최소 항목을 덮어쓰지 않는다.
    signature: str = ""            # dedupe 키: failure_type|expected role|gate|reason
    detection_stage_index: int | None = None   # 실제로 잡힌 단계의 좌표 (Mean Detection Stage)
    expected_stage_index: int | None = None
    repeat_count: int = 1
    status: str = "NEW"
    created_at: str = ""
    updated_at: str = ""
    reflection: dict[str, Any] | None = None
    lesson_ids: list[str] = field(default_factory=list)
    eval_candidate_ids: list[str] = field(default_factory=list)

    @property
    def expected(self) -> Detector:
        return Detector(**{k: v for k, v in self.expected_detector.items() if k in ("role", "stage", "gate")})

    @property
    def actual(self) -> Detector:
        return Detector(**{k: v for k, v in self.actual_detector.items() if k in ("role", "stage", "gate")})

    @property
    def detected_late(self) -> bool:
        if self.detection_stage_index is None or self.expected_stage_index is None:
            return False
        return self.detection_stage_index > self.expected_stage_index

    def to_yaml(self) -> str:
        return yaml.safe_dump(asdict(self), allow_unicode=True, sort_keys=False)

    @classmethod
    def from_yaml(cls, text: str) -> FailureRecord:
        data = yaml.safe_load(text) or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class FailureStore:
    """improve/failures/F-NNNN.yaml. 같은 signature는 새 기록 대신 근거만 는다."""

    def __init__(self, root: Path):
        self.root = root / "failures"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, failure_id: str) -> Path:
        return self.root / f"{failure_id}.yaml"

    def list(self, status: str | None = None) -> list[FailureRecord]:
        out = [FailureRecord.from_yaml(p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("F-*.yaml"))]
        return [f for f in out if status is None or f.status == status]

    def get(self, failure_id: str) -> FailureRecord:
        p = self._path(failure_id)
        if not p.exists():
            raise FileNotFoundError(failure_id)
        return FailureRecord.from_yaml(p.read_text(encoding="utf-8"))

    def save(self, rec: FailureRecord) -> Path:
        rec.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        p = self._path(rec.failure_id)
        p.write_text(rec.to_yaml(), encoding="utf-8")
        return p

    def next_id(self) -> str:
        existing = {f.failure_id for f in self.list()}
        n = len(existing) + 1
        while f"F-{n:04d}" in existing:
            n += 1
        return f"F-{n:04d}"

    def find_by_signature(self, signature: str) -> FailureRecord | None:
        if not signature:
            return None
        return next((f for f in self.list() if f.signature == signature), None)

    def upsert(self, rec: FailureRecord) -> tuple[FailureRecord, bool]:
        """같은 signature가 있으면 근거·run만 합치고 (기록, False)를 준다. 새로 만들면 (기록, True)."""
        existing = self.find_by_signature(rec.signature)
        if existing is None:
            rec.failure_id = rec.failure_id or self.next_id()
            rec.created_at = rec.created_at or time.strftime("%Y-%m-%dT%H:%M:%S")
            self.save(rec)
            return rec, True
        added = [r for r in rec.source_run_ids if r not in existing.source_run_ids]
        if not added and rec.evidence and all(e in existing.evidence for e in rec.evidence):
            return existing, False
        existing.source_run_ids += added
        existing.evidence += [e for e in rec.evidence if e not in existing.evidence]
        existing.repeat_count = len(existing.source_run_ids) or existing.repeat_count
        existing.escaped_to_lock = existing.escaped_to_lock or rec.escaped_to_lock
        if added and existing.status == "CLOSED":
            existing.status = "NEW"          # 닫힌 실패가 재발하면 다시 회고 대상이다
        self.save(existing)
        return existing, False


# --------------------------------------------------------------------------- 수집


def _owner(gate: str | None, fallback_role: str = "", fallback_stage: str = "") -> Detector:
    if gate and gate in GATE_OWNER:
        role, stage = GATE_OWNER[gate]
        return Detector(role, stage, gate)
    return Detector(fallback_role, fallback_stage, gate)


def _signature(failure_type: str, expected: Detector, reason: str) -> str:
    return f"{failure_type}|{expected.role or '-'}|{expected.gate or '-'}|{reason or '-'}"


def _human_correction(state: Any) -> str:
    """resume 피드백 중 '이미 낸 결과가 틀렸다'는 지적만 뽑는다. 문언은 담지 않는다."""
    fb = getattr(state, "feedback", None) or {}
    text = str(fb.get("text") or "")
    if not text:
        return ""
    hit = [h for h in _CORRECTION_HINTS if h in text]
    return hit[0] if hit else ""


def _mine_run(store: RunStore, run_id: str, feedback: Any) -> list[FailureRecord]:
    """한 run에서 실패 후보를 만든다. 정상 종료 run도 escaped_to_lock 검사를 받는다."""
    state = store.load_state(run_id)
    rca = build_rca(store, run_id, feedback)
    rows = read_telemetry(store.telemetry_path(run_id))
    out: list[FailureRecord] = []
    itype = rca.invention_primary
    locked = state.outcome.endswith("LOCK")

    # ---- ① 사람이 뒤에서 잡은 오류: LOCK까지 갔는데 피드백이 오류를 확인했다 -----------
    correction = _human_correction(state)
    if correction:
        gate = next(iter(rca.fault.gates), None) if rca.fault else None
        expected = _owner(gate, (rca.fault.role if rca.fault else ""), (rca.fault.stage if rca.fault else ""))
        if not expected.role:
            expected = Detector("claim-success-reviewer", "SUCCESS", None)
        actual = Detector("user", "POST_LOCK", None)
        out.append(
            FailureRecord(
                failure_id="",
                source_run_ids=[run_id],
                failure_type="ESCAPED_TO_LOCK" if locked else "LATE_DETECTION",
                invention_type=itype,
                origin="human_feedback",
                expected_detector=asdict(expected),
                actual_detector=asdict(actual),
                escaped_to_lock=locked,
                root_cause_summary=(
                    f"사용자가 후속 대화에서 결과의 오류를 지적했다(‘{correction}’ 계열). "
                    f"파이프라인은 {state.outcome}으로 끝나 이 오류를 잡지 못했다."
                ),
                evidence=[{"run_id": run_id, "kind": "user_feedback", "locator": "state.feedback.text", "hint": correction}],
                signature=_signature("ESCAPED_TO_LOCK" if locked else "LATE_DETECTION", expected, "USER_REPORTED"),
                detection_stage_index=STAGE_INDEX["POST_LOCK"],
                expected_stage_index=expected.stage_index,
            )
        )

    # ---- ② 명시적 중지 -----------------------------------------------------------
    if rca.fault is not None and state.halt is not None:
        f = rca.fault
        gate = next(iter(f.gates), None)
        expected = _owner(gate, f.role, f.stage)
        actual = Detector(f.role, f.stage, gate)
        reason = f.reason_code or f.kind
        ftype = "LATE_DETECTION" if actual.stage_index > expected.stage_index else "HALT"
        out.append(
            FailureRecord(
                failure_id="",
                source_run_ids=[run_id],
                failure_type=ftype,
                invention_type=itype,
                origin="runtime",
                expected_detector=asdict(expected),
                actual_detector=asdict(actual),
                escaped_to_lock=False,
                root_cause_summary=(
                    f"{f.stage}({f.role})에서 {f.kind}"
                    + (f" / {f.reason_code}" if f.reason_code else "")
                    + (f"; 비-PASS 게이트 {', '.join(f.gates)}" if f.gates else "")
                    + (f"; 직전 통과 {f.last_pass_stage}" if f.last_pass_stage else "")
                ),
                evidence=[{"run_id": run_id, "record_id": f.record_id, "kind": "halt", "locator": f"records/{f.record_id}.json"}]
                + [{"run_id": run_id, "kind": "failed_check", "locator": c.get("name", "")} for c in f.failed_checks[:6]],
                signature=_signature(ftype, expected, reason),
                detection_stage_index=actual.stage_index,
                expected_stage_index=expected.stage_index,
            )
        )

    # ---- ③ 반복 RETURN_TO_*: 같은 역할이 같은 곳으로 두 번 이상 되돌렸다 -------------
    returns: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        step = str(r.get("next_step", ""))
        if step.startswith("RETURN_TO_"):
            returns.setdefault((r.get("role", ""), step), []).append(r)
    for (role, step), hits in returns.items():
        if len(hits) < 2:
            continue
        gate = next((g for h in hits for g, v in (h.get("gates") or {}).items() if is_gating_failure(g, v, h.get("reason_code"))), None)
        expected = _owner(gate, role, hits[0].get("stage", ""))
        out.append(
            FailureRecord(
                failure_id="",
                source_run_ids=[run_id],
                failure_type="REPEATED_RETURN",
                invention_type=itype,
                origin="runtime",
                expected_detector=asdict(expected),
                actual_detector=asdict(Detector(role, hits[0].get("stage", ""), gate)),
                escaped_to_lock=False,
                root_cause_summary=(
                    f"{role}가 같은 run에서 {step}을 {len(hits)}회 반복했다. "
                    "한 번의 지적으로 닫히지 않는 쟁점이므로 문언 교정이 아니라 상류 계약이 원인일 가능성이 높다."
                ),
                evidence=[{"run_id": run_id, "record_id": h.get("record_id"), "kind": "return", "locator": step} for h in hits[:6]],
                signature=_signature("REPEATED_RETURN", expected, step),
                detection_stage_index=STAGE_INDEX.get(hits[-1].get("stage", ""), None),
                expected_stage_index=expected.stage_index,
                repeat_count=len(hits),
            )
        )

    # ---- ④ 늦은 탐지: 게이트 소유 단계를 지나서야 그 게이트가 실패로 잡혔다 ------------
    for r in rows:
        if r.get("phase") not in (None, "main"):
            continue
        stage, role = r.get("stage", ""), r.get("role", "")
        for gate, val in (r.get("gates") or {}).items():
            if not is_gating_failure(gate, val, r.get("reason_code")):
                continue
            expected = _owner(gate)
            if not expected.role or expected.stage_index >= STAGE_INDEX.get(stage, len(STAGE_ORDER)):
                continue
            out.append(
                FailureRecord(
                    failure_id="",
                    source_run_ids=[run_id],
                    failure_type="LATE_DETECTION",
                    invention_type=itype,
                    origin="runtime",
                    expected_detector=asdict(expected),
                    actual_detector=asdict(Detector(role, stage, gate)),
                    escaped_to_lock=False,
                    root_cause_summary=(
                        f"{gate}는 {expected.stage}({expected.role})의 게이트인데 {stage}({role})에서야 {val}로 잡혔다. "
                        "상류 역할이 같은 항목을 PASS로 넘겼다."
                    ),
                    evidence=[{"run_id": run_id, "record_id": r.get("record_id"), "kind": "late_gate", "locator": f"{gate}={val}"}],
                    signature=_signature("LATE_DETECTION", expected, r.get("reason_code") or val),
                    detection_stage_index=STAGE_INDEX.get(stage),
                    expected_stage_index=expected.stage_index,
                )
            )
    return out


def _mine_patterns(feedback: Any) -> list[FailureRecord]:
    """여러 run에 걸친 반복 패턴을 하나의 실패 기록으로 만든다(feedback.pattern_cards 재사용)."""
    out: list[FailureRecord] = []
    for card in getattr(feedback, "pattern_cards", []):
        if card["count"] < 2:
            continue
        expected = _owner(card["gate"], card["role"])
        out.append(
            FailureRecord(
                failure_id="",
                source_run_ids=list(card["runs"]),
                failure_type="REPEATED_RETURN",
                invention_type=card["invention_type"],
                origin="runtime",
                expected_detector=asdict(expected),
                actual_detector=asdict(Detector(card["role"], expected.stage, card["gate"])),
                escaped_to_lock=False,
                root_cause_summary=(
                    f"{card['invention_type']} 발명에서 {card['role']}의 {card['gate']}가 {card['reason']} 사유로 "
                    f"{card['count']}회 비-PASS였다."
                    + (f" 반복 실패 시험: {', '.join(card['checks'][:3])}." if card.get("checks") else "")
                ),
                evidence=[{"run_id": r, "kind": "pattern", "locator": f"{card['gate']}/{card['reason']}"} for r in card["runs"][:8]],
                signature=_signature("REPEATED_RETURN", expected, card["reason"]),
                repeat_count=card["count"],
                expected_stage_index=expected.stage_index,
            )
        )
    return out


def _mine_eval(results_dir: Path) -> list[FailureRecord]:
    """adversarial eval 결과에서 기대 탐지 지점을 놓친 case를 실패로 올린다.

    `expected_first_detector`를 가진 케이스(= adversarial)만 본다. 일반 golden case의
    실패는 회귀이지 탐지 실패가 아니므로 여기서 다루지 않는다.
    """
    out: list[FailureRecord] = []
    if not results_dir.exists():
        return out
    latest = sorted(results_dir.glob("*.json"))[-1:] if any(results_dir.glob("*.json")) else []
    for path in latest:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for res in payload.get("results", []):
            missed = [c for c in res.get("checks", []) if not c.get("ok") and str(c.get("name", "")).startswith(("first_detector", "must_not_pass", "escaped_to_lock", "expected_return_to"))]
            if not missed:
                continue
            gate = next((str(c["name"]).split(":", 1)[-1] for c in missed if str(c["name"]).startswith("must_not_pass")), None)
            expected = _owner(gate)
            out.append(
                FailureRecord(
                    failure_id="",
                    source_run_ids=[res.get("run_id", "")],
                    failure_type="EVAL_MISS",
                    invention_type=None,
                    origin="eval",
                    expected_detector=asdict(expected),
                    actual_detector=asdict(Detector("", "", gate)),
                    escaped_to_lock=any(str(c.get("name")) == "escaped_to_lock" for c in missed),
                    root_cause_summary=(
                        f"adversarial case {res.get('case_id')}에서 주입한 결함을 기대 지점이 잡지 못했다: "
                        + ", ".join(f"{c['name']}(기대 {c['expected']} / 실제 {c['actual']})" for c in missed[:4])
                    ),
                    evidence=[{"run_id": res.get("run_id", ""), "kind": "eval", "locator": f"{path.name}#{res.get('case_id')}"}],
                    signature=_signature("EVAL_MISS", expected, str(res.get("case_id"))),
                    expected_stage_index=expected.stage_index,
                )
            )
    return out


def mine(store: RunStore, failures: FailureStore, runs: list[str] | None = None, eval_results_dir: Path | None = None, since: str | None = None) -> tuple[list[FailureRecord], list[FailureRecord]]:
    """실패 후보를 수집해 저장한다. 반환값은 (새로 만든 기록, 근거만 는 기록)."""
    feedback = build_feedback(store.runs_dir, since)
    candidates: list[FailureRecord] = []
    for run_id in runs if runs is not None else store.list_runs():
        try:
            candidates += _mine_run(store, run_id, feedback)
        except (FileNotFoundError, OSError, ValueError):
            continue
    candidates += _mine_patterns(feedback)
    if eval_results_dir is not None:
        candidates += _mine_eval(eval_results_dir)

    created, reinforced = [], []
    for cand in candidates:
        rec, is_new = failures.upsert(cand)
        (created if is_new else reinforced).append(rec)
    return created, reinforced


def normalize_summary(text: str) -> str:
    """기록에 남기기 전 공백 정리. 기술내용 필터는 curate 단계의 게이트가 담당한다."""
    return re.sub(r"\s+", " ", text).strip()
