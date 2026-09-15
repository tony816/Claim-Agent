"""개선 루프 ①-b: 자동 이슈 탐지에 이은 근본 원인 분석(RCA).

책의 RCA 4단계를 그대로 구현한다.
  워크플로 추적 → 결함 국소화 → 패턴 인식 → 영향 평가
무엇이 실패했는지에서 멈추지 않고 왜 실패했는지와 어디로 되돌아가야 하는지를 남긴다.
LLM은 사용하지 않는다. 모든 판단은 저장된 기록에서 계산한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..store.runstore import RunStore
from ..store.telemetry import read_telemetry
from .feedback import PASSY, FeedbackReport, build_feedback, is_gating_failure

SEVERITY = {
    "BLOCK": 3,
    "LOOP_LIMIT": 3,
    "ENVELOPE_INVALID": 3,
    "ENVELOPE_REPORT_MISMATCH": 3,
    "ERROR": 3,
    "REVIEW": 2,
    "USER_DECISION": 2,
    "UNVERIFIED": 1,
}

# 중지 종류별 되돌아갈 단계 힌트 (CLAUDE.md 무효화 규칙)
RETURN_HINT = {
    "RETURN_TO_STYLE_ADJUSTER": "같은 design_revision의 새 revision에서 style 단계부터 (`resume --apply-style-fix`)",
    "RETURN_TO_DRAFTER": "같은 design_revision의 새 revision에서 drafter PRE_STYLE부터 (`resume --apply-meaning-fix`)",
    "RETURN_TO_ARCHITECT": "새 design_revision으로 architect부터 (`resume --redesign`)",
    "RETURN_TO_DEPENDENT_ARCHITECT": "새 dependent_design_revision으로 종속항 설계부터 (`resume --redesign`)",
}


@dataclass
class TraceStep:
    seq: int
    stage: str
    role: str
    record_id: str | None
    status: str
    execution_status: str
    gates: dict[str, str]
    next_step: str
    reason_code: str | None
    non_pass_checks: list[str]
    revision: str
    design_revision: str
    dependent_revision: str | None
    target_claim_id: str | None
    tokens: int
    latency_ms: int
    superseded: bool = False
    note: str = ""

    @property
    def non_pass_gates(self) -> dict[str, str]:
        return {k: v for k, v in self.gates.items() if v not in PASSY}

    @property
    def gating_failures(self) -> dict[str, str]:
        """비게이팅 UNVERIFIED(명세서·선행기술 미제공)를 뺀 실제 실패 게이트."""
        return {k: v for k, v in self.gates.items() if is_gating_failure(k, v, self.reason_code)}


@dataclass
class Fault:
    stage: str
    role: str
    record_id: str | None
    kind: str                      # halt kind, or the first non-PASS status
    reason_code: str | None
    gates: dict[str, str] = field(default_factory=dict)
    gate_reasons: list[dict[str, Any]] = field(default_factory=list)
    failed_checks: list[dict[str, Any]] = field(default_factory=list)
    open_issues: list[dict[str, Any]] = field(default_factory=list)
    last_pass_stage: str | None = None
    return_to: str | None = None

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.kind, 2)


@dataclass
class RcaReport:
    run_id: str
    outcome: str
    request_mode: str
    invention_primary: str | None
    trace: list[TraceStep] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    fault: Fault | None = None
    pattern_kind: str = "고립 사건"
    pattern_count: int = 1
    pattern_runs: list[str] = field(default_factory=list)
    pattern_checks: list[str] = field(default_factory=list)
    total_tokens: int = 0
    loop_waste_tokens: int = 0
    loop_waste_calls: int = 0
    priority: int = 0
    next_actions: list[str] = field(default_factory=list)

    def render_md(self) -> str:
        out = [f"# RCA — {self.run_id}", ""]
        out.append(f"- 결과: **{self.outcome}** / request_mode: {self.request_mode} / 발명 유형: {self.invention_primary or '미기재'}")
        out.append(f"- 우선순위 점수: **{self.priority}** (심각도 {self.fault.severity if self.fault else 0} × 반복 {self.pattern_count})")
        out.append(f"- 소비 토큰 {self.total_tokens:,} / 재작업으로 버려진 토큰 {self.loop_waste_tokens:,} ({self.loop_waste_calls}회 호출)")
        out.append("")

        out.append("## 1. 워크플로 추적")
        out.append("")
        out.append("| seq | 단계 | 역할 | record_id | 상태 | 비-PASS 게이트 | next_step | 토큰 | 지연(ms) | 비고 |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for s in self.trace:
            gates = ", ".join(f"{k}: {v}" for k, v in s.non_pass_gates.items()) or "-"
            note = " ".join(x for x in [("superseded" if s.superseded else ""), s.note] if x) or "-"
            out.append(f"| {s.seq} | {s.stage} | {s.role} | `{s.record_id or '-'}` | {s.status} | {gates} | {s.next_step} | {s.tokens:,} | {s.latency_ms:,} | {note} |")
        out.append("")
        if self.transitions:
            out.append("전이 기록:")
            out.append("")
            for t in self.transitions:
                out.append(f"- {t}")
            out.append("")

        out.append("## 2. 결함 국소화")
        out.append("")
        if self.fault is None:
            out.append("중지·비-PASS 지점 없음. 이 run은 실패하지 않았다.")
            out.append("")
        else:
            f = self.fault
            out.append(f"- 첫 실패 지점: **{f.stage}** / {f.role} / `{f.record_id or '-'}` — {f.kind}{' — ' + f.reason_code if f.reason_code else ''}")
            out.append(f"- 직전 통과 단계: {f.last_pass_stage or '없음(첫 단계에서 실패)'}")
            if f.gates:
                out.append(f"- 비-PASS 게이트: {', '.join(f'{k}: {v}' for k, v in f.gates.items())}")
            for gr in f.gate_reasons:
                out.append(f"  - {gr.get('gate')}: {gr.get('reason_code')}")
            if f.failed_checks:
                out.append("- 실패한 세부 시험:")
                for c in f.failed_checks[:12]:
                    note = f" — {c.get('note')}" if c.get("note") else ""
                    out.append(f"  - [{c.get('status')}] {c.get('name')}{note}")
            if f.open_issues:
                out.append("- 미해결 쟁점:")
                for i in f.open_issues[:12]:
                    ret = f" → {i.get('return_to')}" if i.get("return_to") else ""
                    out.append(f"  - [{i.get('kind')}] {i.get('code','')} {i.get('text','')}{ret}")
            out.append("")

        out.append("## 3. 패턴 인식")
        out.append("")
        out.append(f"- 판정: **{self.pattern_kind}**" + (f" — 같은 (발명 유형, 역할, 게이트, 사유) 조합이 {self.pattern_count}회" if self.pattern_count > 1 else ""))
        if self.pattern_runs:
            out.append(f"- 같은 패턴의 run: {', '.join(self.pattern_runs[:8])}")
        if self.pattern_checks:
            out.append(f"- 반복해서 실패한 시험: {', '.join(self.pattern_checks[:6])}")
        out.append("")

        out.append("## 4. 영향 평가와 다음 행동")
        out.append("")
        for a in self.next_actions:
            out.append(f"- {a}")
        out.append("")
        out.append("> RCA는 책임 소재가 아니라 개선 기회를 가리킨다. 제안은 사람이 검토·승인해야 하며, 승인 전에는 어떤 교훈도 역할 프롬프트에 주입되지 않는다.")
        return "\n".join(out) + "\n"


def _record(store: RunStore, run_id: str, record_id: str | None) -> dict[str, Any]:
    if not record_id:
        return {}
    try:
        return store.read_record(run_id, record_id)
    except (FileNotFoundError, OSError):
        return {}


def build_rca(store: RunStore, run_id: str, feedback: FeedbackReport | None = None) -> RcaReport:
    state = store.load_state(run_id)
    rows = [r for r in read_telemetry(store.telemetry_path(run_id)) if r.get("phase") in ("main", "repair")]
    rows.sort(key=lambda r: (r.get("seq", 0), 0 if r.get("phase") == "main" else 1))

    rep = RcaReport(
        run_id=run_id,
        outcome=state.outcome,
        request_mode=state.request_mode.value,
        invention_primary=next((r.get("invention_primary") for r in rows if r.get("invention_primary")), None),
    )

    # ---------------------------------------------------------------- 1. 워크플로 추적
    for r in rows:
        rid = r.get("record_id")
        ref = state.records.get(rid) if rid else None
        tokens = r.get("prompt_tokens", 0) + r.get("output_tokens", 0) + r.get("thoughts_tokens", 0)
        rep.total_tokens += tokens
        step = TraceStep(
            seq=r.get("seq", 0), stage=r.get("stage", ""), role=r.get("role", ""), record_id=rid,
            status=r.get("status", ""), execution_status=r.get("execution_status", ""), gates=r.get("gates") or {},
            next_step=r.get("next_step", ""), reason_code=r.get("reason_code"), non_pass_checks=r.get("non_pass_checks") or [],
            revision=r.get("revision", ""), design_revision=r.get("design_revision", ""),
            dependent_revision=r.get("dependent_revision"), target_claim_id=r.get("target_claim_id"),
            tokens=tokens, latency_ms=r.get("latency_ms", 0), superseded=bool(ref.superseded) if ref else False,
            note="복구 호출" if r.get("phase") == "repair" else ("" if not r.get("repair_used") else "복구 1회"),
        )
        if step.superseded:
            rep.loop_waste_tokens += tokens
            rep.loop_waste_calls += 1
        rep.trace.append(step)

    for note in state.notes:
        if note.startswith("RETURN_TO_") or note.startswith("설계 변경 목표") or note.startswith("종속항 설계 변경 목표") or note.startswith("사용자 결정"):
            rep.transitions.append(note.splitlines()[0][:200])
    for rs in state.candidate.history:
        rep.transitions.append(f"revision {rs.revision} → 폐기 (style_change_mode: {rs.style_change_mode.value})")
    if state.candidate.current and state.candidate.history:
        rep.transitions.append(f"현재 revision: {state.candidate.current.revision} / design_revision: {state.candidate.current.design_revision}")

    # ---------------------------------------------------------------- 2. 결함 국소화
    halt = state.halt
    fault_step: TraceStep | None = None
    if halt is not None:
        rid = halt.record_id
        fault_step = next((s for s in reversed(rep.trace) if s.record_id == rid), None) or (rep.trace[-1] if rep.trace else None)
        rec = _record(store, run_id, rid)
        rep.fault = Fault(
            stage=halt.stage, role=halt.role, record_id=rid, kind=halt.kind, reason_code=halt.reason_code,
            gates={k: v for k, v in (rec.get("gates") or {}).items() if is_gating_failure(k, v, halt.reason_code)},
            gate_reasons=rec.get("gate_reasons") or [],
            failed_checks=[c for c in (rec.get("checks") or []) if c.get("status") not in PASSY],
            open_issues=halt.open_issues or rec.get("open_issues") or [],
            return_to=halt.return_to,
        )
    else:
        fault_step = next((s for s in rep.trace if s.status not in PASSY or s.gating_failures), None)
        if fault_step is not None:
            rec = _record(store, run_id, fault_step.record_id)
            rep.fault = Fault(
                stage=fault_step.stage, role=fault_step.role, record_id=fault_step.record_id,
                kind=fault_step.status if fault_step.status not in PASSY else "GATE_NOT_PASS",
                reason_code=fault_step.reason_code, gates=fault_step.gating_failures,
                gate_reasons=rec.get("gate_reasons") or [],
                failed_checks=[c for c in (rec.get("checks") or []) if c.get("status") not in PASSY],
                open_issues=rec.get("open_issues") or [],
                return_to=fault_step.next_step if fault_step.next_step.startswith("RETURN_TO_") else None,
            )
    if rep.fault is not None and fault_step is not None:
        idx = rep.trace.index(fault_step) if fault_step in rep.trace else 0
        prior = [s for s in rep.trace[:idx] if s.status in PASSY]
        rep.fault.last_pass_stage = f"{prior[-1].stage} ({prior[-1].role}, `{prior[-1].record_id}`)" if prior else None

    # ---------------------------------------------------------------- 3. 패턴 인식
    if rep.fault is not None:
        fb = feedback or build_feedback(store.runs_dir)
        gate = next(iter(rep.fault.gates), None)
        reason = rep.fault.reason_code or (gate and rep.fault.gates.get(gate)) or rep.fault.kind
        best = None
        for card in fb.pattern_cards:
            if card["role"] != rep.fault.role:
                continue
            if gate and card["gate"] != gate:
                continue
            if best is None or card["count"] > best["count"]:
                best = card
        if best is not None and best["count"] > 1:
            rep.pattern_kind = "반복 추세"
            rep.pattern_count = best["count"]
            rep.pattern_runs = best["runs"]
            rep.pattern_checks = best["checks"]
        else:
            same = [h for h in fb.halts if h.get("role") == rep.fault.role and h.get("kind") == rep.fault.kind]
            if len(same) > 1:
                rep.pattern_kind = "반복 추세"
                rep.pattern_count = len(same)
                rep.pattern_runs = [h["run_id"] for h in same]
            rep.pattern_checks = rep.pattern_checks or (fault_step.non_pass_checks if fault_step else [])

    # ---------------------------------------------------------------- 4. 영향 평가
    if rep.fault is not None:
        rep.priority = rep.fault.severity * rep.pattern_count
        ret = rep.fault.return_to
        if rep.fault.kind == "LOOP_LIMIT":
            rep.next_actions.append("루프 한도 초과: 같은 지적이 반복되었으므로 문언 수정이 아니라 설계 계약(주골격·계층·형상 객체)을 먼저 확정한다 (`resume --redesign`).")
        elif ret and ret in RETURN_HINT:
            rep.next_actions.append(f"되돌아갈 단계: {RETURN_HINT[ret]}")
        elif rep.fault.kind in ("ENVELOPE_INVALID", "ENVELOPE_REPORT_MISMATCH"):
            rep.next_actions.append("모델 출력 계약 위반: 역할별 max_output_tokens를 올리거나 thinking_level을 낮춰 보고서 길이를 줄인다. 재현되면 `eval run --variant`로 비교한다.")
        elif rep.fault.kind == "UNVERIFIED" and rep.fault.reason_code in ("SPEC_NOT_PROVIDED", "PRIOR_ART_NOT_PROVIDED"):
            rep.next_actions.append("비게이팅 미검증: 자료를 붙이거나(`resume --add-source`) 잠정안으로 수용한다(`resume --accept-unverified`).")
        else:
            rep.next_actions.append("사용자 판단 필요: report.md의 쟁점을 확인하고 `resume --decide`로 결정을 전달한다.")
        if rep.pattern_kind == "반복 추세":
            rep.next_actions.append(f"반복 {rep.pattern_count}회: `claim-agent lessons propose --from-feedback`으로 교훈 초안을 만들고 승인 여부를 판단한다.")
        if rep.loop_waste_tokens > 0:
            rep.next_actions.append(f"재작업 비용 {rep.loop_waste_tokens:,} 토큰({rep.loop_waste_calls}회)이 이미 버려졌다. 같은 패턴이 또 보이면 루프 한도를 낮추는 편이 싸다.")
    else:
        rep.priority = 0
        rep.next_actions.append("조치 불필요. 이 run은 LOCK까지 도달했다.")
    return rep


def write_rca(store: RunStore, run_id: str, out: Path | None = None) -> Path:
    rep = build_rca(store, run_id)
    path = out or (store.run_dir(run_id) / "rca.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rep.render_md(), encoding="utf-8")
    return path
