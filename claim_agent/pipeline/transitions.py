"""Routing decisions after each role call (CLAUDE.md invalidation rules)."""
from __future__ import annotations

from dataclasses import dataclass

from ..models.contracts import GateContract
from ..models.enums import ADMISSIBLE_STOP_BASES, ExecStatus, GateValue, NextStep, RequestMode, Status
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

    A role that neither passes nor routes a return may stop only for a USER_LOCK change or a parent-chain change in an
    existing-set edit. A fact the sources lack or a scope choice they leave open no longer stops a run: inventor answers
    cannot be had, so the role decides by the fixed order and records a 설계자 가정 (CLAUDE.md 발명자 확인 불가 하드 규칙).
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
    if basis in ADMISSIBLE_STOP_BASES:
        return None
    said = f"{env.status.value}" + (f" ({env.reason_code})" if env.reason_code else "") + (f", 정지 사유 {basis.value}" if basis else ", 정지 사유 없음")
    decide_it = (" 발명자 확인은 받을 수 없으므로 원자료가 답하지 않거나 어긋나는 사항, 원자료가 정하지 않는 권리범위 선택으로는 멈추지 않는다. "
                 "그 사항은 (1) 그것 없이 주골격·협동관계가 닫히면 한정하지 않고 열어 두며, (2) 열 수 없으면 원자료가 더 직접·더 나중에·더 많이 "
                 "뒷받침하는 쪽(같으면 넓은 쪽)을 고르고, (3) 원자료에 없는 사실이 있어야 서는 한정·기능 구절은 빼고 원자료가 보여 주는 구조·관계만 "
                 "청구한다. 정한 사항마다 `설계자 가정 — 확인 필요`(사항 / 고른 쪽 / 근거 / 다른 쪽이면 바뀌는 권리범위)로 적고 끊긴 고리는 발명자 확인 "
                 "질문으로 남긴다. 둘 다 비게이팅이다.")
    if stage == Stage.ARCHITECT:
        return said + "." + decide_it + " 그렇게 정해 `DESIGN_GATE: LOCKED`로 낸다."
    if stage == Stage.DEP_ARCHITECT:
        return (said + "." + decide_it + " 종속항 설계는 원자료로 닫히지 않는 후보 때문에 멈추지 않는다: 원자료·부모항 체인으로 확인되는 구조만 남겨 "
                "보조 축 FALLBACK_ONLY로 잠그거나(근거·청구 가능성 PASS) 그 후보를 세트에서 빼고 `DEPENDENT_DESIGN_GATE`를 잠근다.")
    return (said + "." + decide_it + " 지적을 고칠 되돌림 단계(표면 표기면 RETURN_TO_STYLE_ADJUSTER, 절 결속·관계 술어·한정 표현이면 "
            "RETURN_TO_DRAFTER, 후보·의미 한정 패키지·주체 구조·형상 계약이나 위 사항의 선택이면 설계자 되돌림)를 `next_step`으로 낸다.")


def gate_is_pass(env: RoleEnvelope, name: str, allow_na: bool = False) -> bool:
    v = env.gate(name)
    if v is None:
        return False
    return v == GateValue.PASS or (allow_na and v == GateValue.NOT_APPLICABLE)
