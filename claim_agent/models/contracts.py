"""Gate contracts: per (role, scope, request_mode) PASS predicates.

These encode the progression rules of CLAUDE.md and the role files so that the
orchestrator never relies on the LLM's summary of its own gates.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .enums import CandidateClass, ExecStatus, FinalizedStatus, GateValue, RequestMode, Scope, Status
from .envelope import RoleEnvelope

PASSY = {GateValue.PASS}
PASSY_NA = {GateValue.PASS, GateValue.NOT_APPLICABLE}
PASSY_RANGE = {GateValue.PASS, GateValue.PASS_RANGE}


def _g(env: RoleEnvelope, name: str, allowed: set[GateValue]) -> bool:
    v = env.gate(name)
    return v is not None and v in allowed


@dataclass
class GateContract:
    role: str
    required_gates: list[str]
    predicate: Callable[[RoleEnvelope, RequestMode], bool]
    report_tokens: list[str] = field(default_factory=list)  # gate names that must appear in report_markdown

    def passes(self, env: RoleEnvelope, mode: RequestMode) -> bool:
        if env.execution_status not in (ExecStatus.RUN, ExecStatus.BLIND_COMPLETE):
            return False
        for g in self.required_gates:
            if env.gate(g) is None:
                return False
        return self.predicate(env, mode)

    def missing_gates(self, env: RoleEnvelope) -> list[str]:
        return [g for g in self.required_gates if env.gate(g) is None]


def _architect(env: RoleEnvelope, mode: RequestMode) -> bool:
    return env.status == Status.PASS and _g(env, "DESIGN_GATE", {GateValue.LOCKED})


def _drafter(env: RoleEnvelope, mode: RequestMode) -> bool:
    return (
        env.status == Status.PASS
        and _g(env, "DRAFTER_GATE", PASSY)
        and env.handoff_ready
        and _g(env, "PRE_STYLE_NON_PATENT_TECHNICAL_READER_CHECK", PASSY)
        and _g(env, "PRE_STYLE_GEOMETRIC_OBJECT_CHECK", PASSY_NA)
        and bool(env.exact_claim_text or env.claims)
    )


def _style(env: RoleEnvelope, mode: RequestMode) -> bool:
    return (
        env.status == Status.PASS
        and _g(env, "TERM_EXPRESSION_GATE", PASSY)
        and _g(env, "CLAIM_STYLE_GATE", PASSY)
        and _g(env, "NON_PATENT_TECHNICAL_READER_GATE", PASSY)
        and _g(env, "GEOMETRIC_OBJECT_GATE", PASSY_NA)
        and env.finalized_status == FinalizedStatus.FINALIZED_FOR_SUCCESS
        and bool(env.exact_claim_text or env.claims)
    )


def _success(env: RoleEnvelope, mode: RequestMode) -> bool:
    return env.status == Status.PASS and env.handoff_ready


def _syntax(env: RoleEnvelope, mode: RequestMode) -> bool:
    return (
        env.status == Status.PASS
        and env.handoff_ready
        and _g(env, "NON_PATENT_TECHNICAL_READER_GATE", PASSY)
        and _g(env, "GEOMETRIC_OBJECT_GATE", PASSY_NA)
    )


def _oa_independent(env: RoleEnvelope, mode: RequestMode) -> bool:
    # Hard rule: OA_FINAL_GATE UNVERIFIED — SPEC_NOT_PROVIDED never blocks AUTHORING_DRAFT.
    if not _g(env, "OA_DRAFT_GATE", PASSY):
        return False
    if env.gate("OA_FINAL_GATE") is None:
        return False
    if mode == RequestMode.FINALIZATION:
        return _g(env, "OA_FINAL_GATE", PASSY)
    return True


def _oa_dependent(env: RoleEnvelope, mode: RequestMode) -> bool:
    if not _g(env, "DEPENDENT_OA_DRAFT_GATE", PASSY):
        return False
    if env.gate("DEPENDENT_OA_FINAL_GATE") is None:
        return False
    if mode == RequestMode.FINALIZATION:
        return _g(env, "DEPENDENT_OA_FINAL_GATE", PASSY)
    return True


def _blind(env: RoleEnvelope, mode: RequestMode) -> bool:
    return env.execution_status == ExecStatus.BLIND_COMPLETE


def _picture(env: RoleEnvelope, mode: RequestMode) -> bool:
    return (
        env.status in (Status.PASS, Status.PASS_RANGE)
        and _g(env, "NON_PATENT_TECHNICAL_READER_GATE", PASSY)
        and _g(env, "GEOMETRIC_OBJECT_GATE", PASSY_NA)
    )


def _dep_architect(env: RoleEnvelope, mode: RequestMode) -> bool:
    return (
        env.status == Status.PASS
        and _g(env, "DEPENDENT_DESIGN_GATE", {GateValue.LOCKED})
        and any(c.classification == CandidateClass.TECHNICAL_SOLUTION_CANDIDATE for c in env.candidates)
    )


def _dep_success(env: RoleEnvelope, mode: RequestMode) -> bool:
    return env.status == Status.PASS and env.handoff_ready


CONTRACTS: dict[tuple[str, Scope], GateContract] = {
    ("claim-architect", Scope.INDEPENDENT): GateContract(
        "claim-architect", ["DESIGN_GATE"], _architect, ["DESIGN_GATE"]
    ),
    ("claim-drafter", Scope.INDEPENDENT): GateContract(
        "claim-drafter",
        ["DRAFTER_GATE", "PRE_STYLE_NON_PATENT_TECHNICAL_READER_CHECK", "PRE_STYLE_GEOMETRIC_OBJECT_CHECK"],
        _drafter,
        ["DRAFTER_GATE"],
    ),
    ("claim-drafter", Scope.DEPENDENT_SET): GateContract(
        "claim-drafter",
        ["DRAFTER_GATE", "PRE_STYLE_NON_PATENT_TECHNICAL_READER_CHECK", "PRE_STYLE_GEOMETRIC_OBJECT_CHECK"],
        _drafter,
        ["DRAFTER_GATE"],
    ),
    ("claim-style-adjuster", Scope.INDEPENDENT): GateContract(
        "claim-style-adjuster",
        ["TERM_EXPRESSION_GATE", "CLAIM_STYLE_GATE", "NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
        _style,
        ["TERM_EXPRESSION_GATE", "CLAIM_STYLE_GATE"],
    ),
    ("claim-style-adjuster", Scope.DEPENDENT_SET): GateContract(
        "claim-style-adjuster",
        ["TERM_EXPRESSION_GATE", "CLAIM_STYLE_GATE", "NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
        _style,
        ["TERM_EXPRESSION_GATE", "CLAIM_STYLE_GATE"],
    ),
    ("claim-success-reviewer", Scope.INDEPENDENT): GateContract(
        "claim-success-reviewer", ["CLAIM_STYLE_GATE", "TERM_EXPRESSION_GATE"], _success, []
    ),
    ("claim-success-reviewer", Scope.DEPENDENT_SET): GateContract(
        "claim-success-reviewer", ["CLAIM_STYLE_GATE", "TERM_EXPRESSION_GATE"], _dep_success, []
    ),
    ("syntax-scope-reviewer", Scope.INDEPENDENT): GateContract(
        "syntax-scope-reviewer",
        ["NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
        _syntax,
        ["NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
    ),
    ("syntax-scope-reviewer", Scope.DEPENDENT_SET): GateContract(
        "syntax-scope-reviewer",
        ["NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
        _syntax,
        ["NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
    ),
    ("oa-strategy-reviewer", Scope.INDEPENDENT): GateContract(
        "oa-strategy-reviewer", ["OA_DRAFT_GATE", "OA_FINAL_GATE"], _oa_independent, ["OA_DRAFT_GATE", "OA_FINAL_GATE"]
    ),
    ("oa-strategy-reviewer", Scope.DEPENDENT_SET): GateContract(
        "oa-strategy-reviewer",
        ["DEPENDENT_OA_DRAFT_GATE", "DEPENDENT_OA_FINAL_GATE"],
        _oa_dependent,
        ["DEPENDENT_OA_DRAFT_GATE", "DEPENDENT_OA_FINAL_GATE"],
    ),
    ("blind-claim-reconstruction-reviewer", Scope.INDEPENDENT): GateContract(
        "blind-claim-reconstruction-reviewer", [], _blind, []
    ),
    ("blind-claim-reconstruction-reviewer", Scope.DEPENDENT_SINGLE): GateContract(
        "blind-claim-reconstruction-reviewer", [], _blind, []
    ),
    ("picture-claim-reconstruction-reviewer", Scope.INDEPENDENT): GateContract(
        "picture-claim-reconstruction-reviewer",
        ["NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
        _picture,
        ["NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
    ),
    ("picture-claim-reconstruction-reviewer", Scope.DEPENDENT_SINGLE): GateContract(
        "picture-claim-reconstruction-reviewer",
        ["NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
        _picture,
        ["NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
    ),
    ("dependent-claim-strategy-architect", Scope.DEPENDENT_SET): GateContract(
        "dependent-claim-strategy-architect", ["DEPENDENT_DESIGN_GATE"], _dep_architect, ["DEPENDENT_DESIGN_GATE"]
    ),
}


def contract_for(role: str, scope: Scope) -> GateContract:
    try:
        return CONTRACTS[(role, scope)]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(f"no gate contract for {role} / {scope}") from exc


# An EXISTING_SET_EDIT reviews its one edited claim in a single call that applies these role files in order.
COMBINED_REVIEW = ("claim-success-reviewer", "syntax-scope-reviewer", "oa-strategy-reviewer")


def combined_contract(roles: tuple[str, ...], scope: Scope) -> GateContract:
    """Every member contract must pass on the one envelope; required gates and report lines are their union."""
    members = [contract_for(r, scope) for r in roles]
    return GateContract(
        "+".join(roles),
        list(dict.fromkeys(g for c in members for g in c.required_gates)),
        lambda env, mode: all(c.predicate(env, mode) for c in members),
        list(dict.fromkeys(g for c in members for g in c.report_tokens)),
    )
