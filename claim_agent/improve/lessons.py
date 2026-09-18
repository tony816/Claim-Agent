"""Human-approved lessons memory (개선 루프 ③ 지속 학습).

Lessons are procedural / expression cautions only; they are never technical
evidence. Only approved lessons are injected, and their hash is part of
source_set_id so every record is traceable to the lesson set it ran under.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..models.ids import sha256_text

STATES = ("pending", "approved", "rejected")

# Regression gate states (ACE ⑤). The directory still decides injection — only `approved` is injected —
# so a candidate a human approved sits in `pending` until the regression eval passes.
GATE_PENDING = "PENDING"
GATE_CANDIDATE = "CANDIDATE_APPROVED"        # human said yes; not injected yet
GATE_ACTIVE = "ACTIVE_APPROVED"              # regression passed; injected
GATE_FAILED_EVAL = "APPROVED_BUT_FAILED_EVAL"
GATE_HELD = "HELD"
GATE_STATES = (GATE_PENDING, GATE_CANDIDATE, GATE_ACTIVE, GATE_FAILED_EVAL, GATE_HELD)


@dataclass
class Lesson:
    id: str
    version: int = 1
    status: str = "pending"
    target_roles: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=lambda: ["INDEPENDENT", "DEPENDENT_SET"])
    invention_types: list[str] = field(default_factory=list)
    text_ko: str = ""
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)
    eval_evidence: list[str] = field(default_factory=list)
    key: str = ""            # stable dedupe signature, e.g. "claim-style-adjuster|CLAIM_STYLE_GATE|RETURN_TO_DRAFTER"
    created_at: str = ""
    approved_by: str | None = None
    approved_at: str | None = None
    note: str | None = None
    llm_drafted: bool = False
    # ---- ACE fields. Lessons written before ACE have source="manual" and requires_eval=False,
    # so `approve()` keeps its old meaning for them.
    source: str = "manual"                   # manual | rca | feedback | llm | ace
    failure_mode_id: str = ""
    failure_ids: list[str] = field(default_factory=list)
    requires_eval: bool = False              # ACE-generated: never injected before a passing regression run
    gate_status: str = ""                    # see GATE_* above; empty = derived from status
    helpful: int = 0                         # ACE grow-and-refine counters
    harmful: int = 0
    approval: dict[str, Any] = field(default_factory=dict)   # approved_by/at, original_proposal, approved_content, approval_note
    eval_record: dict[str, Any] = field(default_factory=dict)  # last regression gate verdict
    reflection: dict[str, Any] = field(default_factory=dict)   # Reflector summary shown in the inbox

    def __post_init__(self) -> None:
        if not self.gate_status:
            self.gate_status = GATE_ACTIVE if self.status == "approved" else GATE_PENDING

    @property
    def injected(self) -> bool:
        return self.status == "approved"

    def to_yaml(self) -> str:
        return yaml.safe_dump(asdict(self), allow_unicode=True, sort_keys=False)

    @classmethod
    def from_yaml(cls, text: str) -> Lesson:
        data = yaml.safe_load(text) or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class LessonStore:
    def __init__(self, root: Path):
        self.root = root
        for s in STATES:
            (root / s).mkdir(parents=True, exist_ok=True)

    def _path(self, lesson_id: str, status: str) -> Path:
        return self.root / status / f"{lesson_id}.yaml"

    def list(self, status: str | None = None) -> list[Lesson]:
        out: list[Lesson] = []
        for s in STATES if status is None else [status]:
            for p in sorted((self.root / s).glob("*.yaml")):
                out.append(Lesson.from_yaml(p.read_text(encoding="utf-8")))
        return out

    def get(self, lesson_id: str) -> tuple[Lesson, str]:
        for s in STATES:
            p = self._path(lesson_id, s)
            if p.exists():
                return Lesson.from_yaml(p.read_text(encoding="utf-8")), s
        raise FileNotFoundError(lesson_id)

    def next_id(self) -> str:
        n = len(self.list()) + 1
        while any(l.id == f"L-{n:04d}" for l in self.list()):
            n += 1
        return f"L-{n:04d}"

    def save(self, lesson: Lesson) -> Path:
        for s in STATES:
            p = self._path(lesson.id, s)
            if p.exists() and s != lesson.status:
                p.unlink()
        p = self._path(lesson.id, lesson.status)
        p.write_text(lesson.to_yaml(), encoding="utf-8")
        return p

    def propose(self, text_ko: str, target_roles: list[str], rationale: str, evidence: list[str], llm_drafted: bool = False, key: str = "", **extra: Any) -> Lesson:
        lesson = Lesson(id=self.next_id(), text_ko=text_ko, target_roles=target_roles, rationale=rationale, evidence=evidence, key=key, created_at=time.strftime("%Y-%m-%dT%H:%M:%S"), llm_drafted=llm_drafted,
                        **{k: v for k, v in extra.items() if k in Lesson.__dataclass_fields__})
        self.save(lesson)
        return lesson

    def find_by_key(self, key: str) -> Lesson | None:
        """Any lesson (pending/approved/rejected) already covering this failure signature."""
        if not key:
            return None
        return next((l for l in self.list() if l.key == key), None)

    def add_evidence(self, lesson: Lesson, evidence: list[str]) -> Lesson:
        """Reinforce an existing proposal instead of creating a duplicate."""
        added = [e for e in evidence if e not in lesson.evidence]
        if added:
            lesson.evidence = lesson.evidence + added
            self.save(lesson)
        return lesson

    def approve(self, lesson_id: str, by: str = "user", note: str | None = None) -> Lesson:
        """Approve for injection.

        A lesson marked `requires_eval` (everything the ACE curator produces) cannot reach the
        injected `approved` state on a human yes alone: it stops at CANDIDATE_APPROVED until
        `activate()` seals a passing regression run.
        """
        lesson, _ = self.get(lesson_id)
        if lesson.requires_eval and lesson.gate_status != GATE_ACTIVE:
            return self.approve_candidate(lesson_id, by=by, note=note)
        lesson.status = "approved"
        lesson.gate_status = GATE_ACTIVE
        lesson.version += 1 if lesson.approved_at else 0
        lesson.approved_by = by
        lesson.approved_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        lesson.note = note
        self.save(lesson)
        return lesson

    def reject(self, lesson_id: str, note: str | None = None, by: str = "user") -> Lesson:
        lesson, _ = self.get(lesson_id)
        lesson.status = "rejected"
        lesson.gate_status = GATE_PENDING
        lesson.note = note
        lesson.approval = {**lesson.approval, "rejected_by": by, "rejected_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "approval_note": note}
        self.save(lesson)
        return lesson

    # ------------------------------------------------------- ACE regression gate (운영 안전 규칙 ②③)
    def approve_candidate(self, lesson_id: str, by: str = "user", note: str | None = None,
                          text_ko: str | None = None, target_roles: list[str] | None = None) -> Lesson:
        """Human approval. The lesson stays out of the injected set until the regression gate passes.

        `text_ko`/`target_roles` implement '수정 후 승인': the original proposal is preserved in
        `approval.original_proposal` so the audit trail shows what the model proposed and what the
        human actually approved.
        """
        lesson, _ = self.get(lesson_id)
        original = {"text_ko": lesson.text_ko, "target_roles": list(lesson.target_roles)}
        edited = False
        if text_ko is not None and text_ko.strip() and text_ko.strip() != lesson.text_ko.strip():
            lesson.text_ko, edited = text_ko.strip(), True
        if target_roles is not None and list(target_roles) != list(lesson.target_roles):
            lesson.target_roles, edited = list(target_roles), True
        lesson.status = "pending"
        lesson.gate_status = GATE_CANDIDATE
        lesson.approved_by = by
        lesson.approved_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        lesson.note = note
        lesson.approval = {
            "approved_by": by,
            "approved_at": lesson.approved_at,
            "original_proposal": original,
            "approved_content": {"text_ko": lesson.text_ko, "target_roles": list(lesson.target_roles)},
            "approval_note": note,
            "edited": edited,
        }
        self.save(lesson)
        return lesson

    def hold(self, lesson_id: str, note: str | None = None, by: str = "user") -> Lesson:
        lesson, _ = self.get(lesson_id)
        lesson.status = "pending"
        lesson.gate_status = GATE_HELD
        lesson.note = note
        lesson.approval = {**lesson.approval, "held_by": by, "held_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "approval_note": note}
        self.save(lesson)
        return lesson

    def activate(self, lesson_id: str, eval_record: dict[str, Any]) -> Lesson:
        """Regression gate passed: move the human-approved candidate into the injected set."""
        lesson, _ = self.get(lesson_id)
        if lesson.gate_status not in (GATE_CANDIDATE, GATE_FAILED_EVAL):
            raise ValueError(f"{lesson_id}: 사람이 승인한 candidate만 활성화할 수 있다 (현재 {lesson.gate_status})")
        lesson.status = "approved"
        lesson.gate_status = GATE_ACTIVE
        lesson.eval_record = eval_record
        lesson.eval_evidence = list(dict.fromkeys(lesson.eval_evidence + list(eval_record.get("runs", []))))
        self.save(lesson)
        return lesson

    def fail_eval(self, lesson_id: str, eval_record: dict[str, Any]) -> Lesson:
        """Regression gate failed: keep the human approval on record but never inject it."""
        lesson, _ = self.get(lesson_id)
        lesson.status = "pending"
        lesson.gate_status = GATE_FAILED_EVAL
        lesson.eval_record = eval_record
        self.save(lesson)
        return lesson

    def score(self, lesson_id: str, helpful: int = 0, harmful: int = 0) -> Lesson:
        """ACE grow-and-refine counters. Scoring never changes the gate state on its own."""
        lesson, _ = self.get(lesson_id)
        lesson.helpful += helpful
        lesson.harmful += harmful
        self.save(lesson)
        return lesson

    def by_failure_mode(self) -> dict[str, str]:
        """{failure_mode_id or key: lesson_id} — the Reflector's duplicate check."""
        out: dict[str, str] = {}
        for l in self.list():
            for token in (l.failure_mode_id, l.key):
                if token and token not in out:
                    out[token] = l.id
        return out

    # ------------------------------------------------------------ injection
    def approved_for_role(self, role: str, include: list[str] | None = None) -> list[Lesson]:
        out = []
        for l in self.list("approved"):
            if include is not None and l.id not in include:
                continue
            if not l.target_roles or role in l.target_roles:
                out.append(l)
        return out

    def injection_text(self, role: str, include: list[str] | None = None) -> str:
        lessons = self.approved_for_role(role, include)
        if not lessons:
            return ""
        return "\n".join(f"- ({l.id} v{l.version}) {l.text_ko.strip()}" for l in lessons)

    def texts_by_role(self, roles: list[str], enabled: bool = True, include: list[str] | None = None) -> dict[str, str]:
        if not enabled:
            return {}
        return {r: t for r in roles if (t := self.injection_text(r, include))}

    def digest(self, enabled: bool = True, include: list[str] | None = None) -> str:
        if not enabled:
            return "none"
        items = [l for l in self.list("approved") if include is None or l.id in include]
        if not items:
            return "none"
        return sha256_text("\n".join(f"{l.id}:{l.version}:{l.text_ko}" for l in sorted(items, key=lambda x: x.id)))


def propose_from_run(store_dir: Path, run_state: dict, lessons: LessonStore) -> list[Lesson]:
    """Draft pending lessons from a halted run's open issues (template-based, no LLM)."""
    out: list[Lesson] = []
    halt = run_state.get("halt") or {}
    role = halt.get("role", "")
    run_id = run_state.get("run_id")
    for issue in halt.get("open_issues", []):
        text = issue.get("text", "").strip()
        if not text:
            continue
        key = f"{role}|{halt.get('stage')}|{issue.get('code') or text[:40]}"
        evidence = [f"{run_id}:{halt.get('record_id')}"]
        existing = lessons.find_by_key(key)
        if existing is not None:
            out.append(lessons.add_evidence(existing, evidence))
            continue
        out.append(
            lessons.propose(
                text_ko=f"[{halt.get('stage')}] {issue.get('code','')} — 다음 상황을 미리 점검한다: {text}",
                target_roles=[role] if role and role != "engine" else [],
                rationale=f"run {run_id} 중지 사유({halt.get('kind')})에서 자동 제안; 사람 승인 전에는 주입되지 않음",
                evidence=evidence,
                key=key,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 역전파: 텍스트 피드백 → 절차·표현 교훈 초안
# ---------------------------------------------------------------------------

def propose_from_feedback(report: Any, lessons: LessonStore, min_count: int = 2) -> list[Lesson]:
    """반복 패턴을 역할별 교훈 초안으로 되돌린다(규칙 기반, LLM 없음).

    같은 (역할, 게이트, 사유) 조합이 이미 제안되어 있으면 새 교훈을 만들지 않고
    근거 run만 덧붙인다. 모든 결과는 pending이며 승인 전에는 주입되지 않는다.
    """
    out: list[Lesson] = []
    for card in getattr(report, "pattern_cards", []):
        if card["count"] < min_count:
            continue
        role, gate, reason = card["role"], card["gate"], card["reason"]
        key = f"{role}|{gate}|{reason}"
        evidence = list(card["runs"])
        existing = lessons.find_by_key(key)
        if existing is not None:
            out.append(lessons.add_evidence(existing, evidence))
            continue
        checks = card.get("checks") or []
        detail = f" 특히 다음 시험에서 반복 실패했다: {', '.join(checks[:3])}." if checks else ""
        text = (
            f"{gate}를 판정하기 전에 {reason} 조건을 먼저 점검한다. "
            f"같은 사유로 {card['count']}회 비-PASS가 났으므로, 해당 항목의 근거와 표현을 보고서에 명시적으로 남긴다.{detail}"
        )
        out.append(
            lessons.propose(
                text_ko=text,
                target_roles=[role] if role and role != "engine" else [],
                rationale=f"피드백 리포트의 패턴 카드({card['invention_type']} / {role} / {gate} / {reason}, {card['count']}회)에서 역전파; 사람 승인 전에는 주입되지 않음",
                evidence=evidence,
                key=key,
            )
        )
    return out


LLM_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text_ko": {"type": "string", "description": "역할 프롬프트에 덧붙일 한 문단의 절차·표현 주의사항. 청구항 문언·발명 내용을 인용하지 않는다."},
        "target_roles": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["text_ko", "rationale"],
}

_OUTPUT_HINT = "\n\n위 정보만으로 JSON 하나를 출력한다.\n"

_LLM_INSTRUCTION = """당신은 한국어 특허 청구항 작성 파이프라인의 회고 분석가다.

아래는 한 실행이 중지된 지점의 게이트 판정, 실패한 세부 시험, 미해결 쟁점, 최소 수정 방향이다. 이 정보에서 **다음 실행에 도움이 될 절차·표현 주의사항 한 가지**를 한국어 한 문단으로 뽑아라.

규칙:
- 발명의 기술내용, 청구항 문언, 부품 이름, 수치를 인용하거나 일반화하지 않는다. 기술내용 근거가 아니라 절차와 표현에 관한 주의사항만 쓴다.
- 특정 사건이 아니라 같은 역할이 다음에도 확인해야 할 점검 항목으로 쓴다.
- 역할 파일과 CLAUDE.md의 규칙을 바꾸거나 완화하는 내용을 쓰지 않는다.
- 근거가 약하면 text_ko를 비워 둔다."""


def extract_llm_materials(record: dict[str, Any], halt: dict[str, Any]) -> str:
    """Build the LLM input: gates, failed checks, open issues and the fix direction only.

    The full report_markdown is deliberately not sent: it carries the claim text.
    """
    lines = [f"역할: {halt.get('role')}", f"단계: {halt.get('stage')}", f"중지 종류: {halt.get('kind')} {halt.get('reason_code') or ''}"]
    gates = {k: v for k, v in (record.get("gates") or {}).items() if v not in ("PASS", "PASS-RANGE", "NOT_APPLICABLE", "LOCKED")}
    if gates:
        lines.append("비-PASS 게이트: " + ", ".join(f"{k}: {v}" for k, v in gates.items()))
    for gr in record.get("gate_reasons") or []:
        lines.append(f"게이트 사유: {gr.get('gate')} — {gr.get('reason_code')}")
    failed = [c for c in (record.get("checks") or []) if c.get("status") not in ("PASS", "PASS-RANGE", "NOT_APPLICABLE")]
    for c in failed[:10]:
        lines.append(f"실패 시험: [{c.get('status')}] {c.get('name')}" + (f" — {c.get('note')}" if c.get("note") else ""))
    for i in (halt.get("open_issues") or record.get("open_issues") or [])[:10]:
        lines.append(f"미해결 쟁점: [{i.get('kind')}] {i.get('code','')} {i.get('text','')}")
    report = record.get("report_markdown") or ""
    for header in ("최소 수정 방향", "최소 수정 목표", "돌아갈 단계"):
        m = re.search(rf"^.*{re.escape(header)}.*$", report, re.MULTILINE)
        if m:
            lines.append(m.group(0).strip()[:300])
    return "\n".join(lines)


def propose_with_llm(provider: Any, model: str, materials: str, lessons: LessonStore, role: str, evidence: list[str], key: str, max_output_tokens: int = 2048) -> Lesson | None:
    """Opt-in (`--llm`): let the model draft one lesson from the failure summary.

    The result is always pending and marked llm_drafted; nothing is injected
    before a human approves it.
    """
    from ..provider.base import CallSpec, GenParams, parse_json_text

    spec = CallSpec(
        role="lesson-drafter", scope="META", model=model, system_instruction=_LLM_INSTRUCTION,
        packet_text=materials + _OUTPUT_HINT, sources_block="",
        json_schema=LLM_DRAFT_SCHEMA, gen=GenParams(0.3, "LOW", max_output_tokens), use_cache=False, phase="main", stage="LESSON_DRAFT",
    )
    result = provider.generate(spec)
    data = result.parsed or parse_json_text(result.text) or {}
    text = (data.get("text_ko") or "").strip()
    if not text:
        return None
    existing = lessons.find_by_key(key)
    if existing is not None:
        return lessons.add_evidence(existing, evidence)
    roles = [r for r in (data.get("target_roles") or [role]) if r and r != "engine"]
    return lessons.propose(
        text_ko=text,
        target_roles=roles,
        rationale=(data.get("rationale") or "LLM 초안") + " (LLM 작성 초안; 사람 승인 전에는 주입되지 않음)",
        evidence=evidence,
        llm_drafted=True,
        key=key,
    )
