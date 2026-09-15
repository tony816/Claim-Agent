"""Routing decisions after each role call (CLAUDE.md invalidation rules)."""
from __future__ import annotations

from dataclasses import dataclass

from ..models.contracts import GateContract
from ..models.enums import ExecStatus, GateValue, NextStep, RequestMode, Status
from ..models.envelope import RoleEnvelope

LOOP_KEY = {
    NextStep.RETURN_TO_STYLE_ADJUSTER: "STYLE_ONLY",
    NextStep.RETURN_TO_DRAFTER: "DRAFTER",
    NextStep.RETURN_TO_ARCHITECT: "ARCHITECT",
    NextStep.RETURN_TO_DEPENDENT_ARCHITECT: "DEPENDENT_ARCHITECT",
}


@dataclass
class Transition:
    kind: str                      # PROCEED | RETURN | HALT
    return_to: NextStep | None = None
    halt_kind: str | None = None   # REVIEW | BLOCK | UNVERIFIED | LOOP_LIMIT | USER_DECISION
    reason: str = ""


def _gate_return(env: RoleEnvelope) -> NextStep | None:
    """A RETURN_TO_* value carried in a gate (e.g. CLAIM_STYLE_GATE: RETURN_TO_DRAFTER)."""
    for name, val in env.gates.present().items():
        if val.startswith("RETURN_TO_"):
            try:
                return NextStep(val)
            except ValueError:
                continue
    return None


def decide(env: RoleEnvelope, contract: GateContract, mode: RequestMode, loop_counts: dict[str, int], max_loops: dict[str, int], allow_returns: bool = True) -> Transition:
    if contract.passes(env, mode):
        return Transition("PROCEED")

    ret = env.next_step if env.next_step in LOOP_KEY else _gate_return(env)
    if ret is not None and allow_returns:
        key = LOOP_KEY[ret]
        if loop_counts.get(key, 0) >= max_loops.get(key, 1):
            return Transition("HALT", ret, "LOOP_LIMIT", f"{key} 루프 한도 초과 ({loop_counts.get(key, 0)})")
        return Transition("RETURN", ret, None, env.reason_code or "")

    if env.execution_status in (ExecStatus.GATE_NOT_RUN, ExecStatus.UNVERIFIED) or env.status == Status.UNVERIFIED:
        return Transition("HALT", None, "UNVERIFIED", env.reason_code or "UNVERIFIED")
    if env.next_step == NextStep.USER_DECISION or env.status == Status.REVIEW:
        return Transition("HALT", None, "REVIEW", env.reason_code or "REVIEW")
    if env.status == Status.BLOCK:
        return Transition("HALT", None, "BLOCK", env.reason_code or "BLOCK")
    # status PASS but contract failed (e.g. missing gate, handoff_ready false, OA_FINAL absent)
    missing = contract.missing_gates(env)
    if missing:
        return Transition("HALT", None, "ENVELOPE_INVALID", f"필수 게이트 누락: {missing}")
    return Transition("HALT", None, "REVIEW", "계약 술어 미충족 (handoff_ready/게이트 값 확인 필요)")


def gate_is_pass(env: RoleEnvelope, name: str, allow_na: bool = False) -> bool:
    v = env.gate(name)
    if v is None:
        return False
    return v == GateValue.PASS or (allow_na and v == GateValue.NOT_APPLICABLE)
