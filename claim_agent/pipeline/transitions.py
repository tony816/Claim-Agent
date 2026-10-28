"""Routing decisions after each role call (CLAUDE.md invalidation rules)."""
from __future__ import annotations

from dataclasses import dataclass

from ..models.contracts import GateContract
from ..models.enums import ExecStatus, GateValue, NextStep, RequestMode, Status, StopBasis
from ..models.envelope import RoleEnvelope
from ..models.state import Stage

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
    for _name, val in env.gates.present().items():
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


# Stages whose stop goes back to the user only for the reasons CLAUDE.md lists (검수 실패 자동 수정 루프 하드 규칙).
# Prior-art steps, the pre-exam, blind snapshots and REVIEW_ONLY keep their own stop rules.
AUTHORING_STAGES = {
    Stage.ARCHITECT, Stage.DRAFT, Stage.STYLE, Stage.SUCCESS, Stage.SYNTAX, Stage.OA, Stage.PICTURE,
    Stage.DEP_ARCHITECT, Stage.DEP_DRAFT, Stage.DEP_STYLE, Stage.DEP_SUCCESS, Stage.DEP_SYNTAX, Stage.DEP_OA, Stage.DEP_RECON,
}
# Reason codes for missing or mismatched inputs, a missing root, or no candidate at all: a re-asked role cannot close them.
INPUT_STOP_REASONS = {
    "INPUT_MISSING", "INPUT_MISSING_OR_REVISION_MISMATCH", "INPUT_REVISION_MISMATCH", "DEPENDENT_REVISION_MISMATCH",
    "UPSTREAM_GATE_NOT_PASS", "DEPENDENT_UPSTREAM_GATE_NOT_PASS", "UPSTREAM_STYLE_GATE_NOT_PASS", "UPSTREAM_OA_GATE_NOT_PASS",
    "BLIND_INPUT_CONTAMINATED", "BLIND_NOT_COMPLETE", "REFERENCE_MISSING", "LOCK_MISSING_OR_STALE", "ROOT_LOCK_MISSING_OR_STALE",
    "NO_TECHNICAL_SOLUTION_CANDIDATE", "AUXILIARY_SOURCE_MISSING", "REQUEST_MODE_MISSING_OR_INVALID", "CONFIG_VERSION_MISMATCH",
    "NO_SOURCE_DIFFERENTIATOR", "DESIGN_ELEMENTS_MISSING",
}


def inadmissible_stop(env: RoleEnvelope, contract: GateContract, mode: RequestMode, stage: Stage) -> str | None:
    """Why this envelope would stop the run for the user without a reason CLAUDE.md allows, or None.

    A role that neither passes nor routes a return may stop only for a USER_LOCK change, a parent-chain change in an
    existing-set edit, a fact the sources lack, or a scope choice the locked contract leaves open. The dependent architect
    may not stop for a missing fact: an unclosed candidate drops to the fallback axis or leaves the set, and the broken
    link becomes an inventor question (CLAUDE.md 제12단계).
    """
    if stage not in AUTHORING_STAGES or contract.passes(env, mode):
        return None
    if env.next_step in LOOP_KEY or _gate_return(env) is not None:
        return None
    if env.execution_status == ExecStatus.GATE_NOT_RUN or (env.reason_code or "") in INPUT_STOP_REASONS:
        return None
    if env.status in (Status.PASS, Status.PASS_RANGE) and env.next_step != NextStep.USER_DECISION:
        return None            # a PASS that breaks the contract is an envelope fault, handled by decide()
    basis = env.stop_basis
    if basis is not None and not (basis == StopBasis.SOURCE_FACT_ABSENT and stage == Stage.DEP_ARCHITECT):
        return None
    said = f"{env.status.value}" + (f" ({env.reason_code})" if env.reason_code else "") + (f", 정지 사유 {basis.value}" if basis else ", 정지 사유 없음")
    if stage == Stage.DEP_ARCHITECT:
        return (said + ". 종속항 설계는 원자료로 닫히지 않는 후보 때문에 멈추지 않는다: 원자료·부모항 체인으로 확인되는 구조만 남겨 "
                "보조 축 FALLBACK_ONLY로 잠그거나(근거·청구 가능성 PASS) 그 후보를 세트에서 빼고, 끊긴 고리는 발명자 확인 질문으로 남긴다.")
    return (said + ". 허용 사유가 아니면 지적을 고칠 되돌림 단계(표면 표기면 RETURN_TO_STYLE_ADJUSTER, 절 결속·관계 술어·한정 표현이면 "
            "RETURN_TO_DRAFTER, 후보·의미 한정 패키지·주체 구조·형상 계약이면 설계자 되돌림)를 `next_step`으로 낸다.")


def gate_is_pass(env: RoleEnvelope, name: str, allow_na: bool = False) -> bool:
    v = env.gate(name)
    if v is None:
        return False
    return v == GateValue.PASS or (allow_na and v == GateValue.NOT_APPLICABLE)
