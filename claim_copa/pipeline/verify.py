"""Cross-checks between the JSON envelope and the Korean report / orchestrator state."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models.contracts import GateContract
from ..models.envelope import RoleEnvelope
from ..models.ids import Identifiers
from .claimtext import exact_sha256

_GATE_LINE = r"{gate}\s*[:：]\s*\**\s*([A-Z_\-]+)"


@dataclass
class CrossCheckResult:
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _report_gate_value(report: str, gate: str) -> str | None:
    m = re.search(_GATE_LINE.format(gate=re.escape(gate)), report)
    return m.group(1).strip("*").upper() if m else None


def cross_check(
    env: RoleEnvelope,
    contract: GateContract,
    ids: Identifiers,
    expected_record_id: str,
    expected_exact_text: str | None = None,
    preloaded_paths: list[str] | None = None,
    require_report: bool = True,
) -> CrossCheckResult:
    res = CrossCheckResult()
    e = env.identifiers
    if e.candidate_id and e.candidate_id != ids.candidate_id:
        res.problems.append(f"candidate_id echo mismatch: {e.candidate_id} != {ids.candidate_id}")
    if e.revision and e.revision != ids.revision:
        res.problems.append(f"revision echo mismatch: {e.revision} != {ids.revision}")
    if e.design_revision and e.design_revision not in (ids.design_revision, "N/A"):
        res.problems.append(f"design_revision echo mismatch: {e.design_revision} != {ids.design_revision}")
    if e.record_id and e.record_id != expected_record_id:
        res.problems.append(f"record_id echo mismatch: {e.record_id} != {expected_record_id}")
    if ids.dependent_revision and e.dependent_revision and e.dependent_revision != ids.dependent_revision:
        res.problems.append("dependent_revision echo mismatch")
    if ids.target_claim_id and e.target_claim_id and e.target_claim_id != ids.target_claim_id:
        res.problems.append("target_claim_id echo mismatch")

    if expected_exact_text is not None and env.exact_claim_text is not None:
        if exact_sha256(env.exact_claim_text.strip()) != exact_sha256(expected_exact_text.strip()):
            res.problems.append("exact_claim_text echo differs from the stored exact text")

    if require_report:
        if not env.report_markdown or len(env.report_markdown.strip()) < 40:
            res.problems.append("report_markdown missing or too short")
        else:
            for gate in contract.report_tokens:
                env_val = env.gate(gate)
                if env_val is None:
                    continue
                rep_val = _report_gate_value(env.report_markdown, gate)
                if rep_val is None:
                    res.problems.append(f"report_markdown lacks a '{gate}:' line")
                elif rep_val != env_val.value.upper():
                    res.problems.append(f"{gate} differs: envelope={env_val.value} report={rep_val}")
            # OA: both gates must be present as separate lines (never a single 종합 판정).
            if contract.role == "oa-strategy-reviewer":
                names = ("OA_DRAFT_GATE", "OA_FINAL_GATE") if env.gate("OA_DRAFT_GATE") is not None else ("DEPENDENT_OA_DRAFT_GATE", "DEPENDENT_OA_FINAL_GATE")
                for n in names:
                    if _report_gate_value(env.report_markdown, n) is None:
                        res.problems.append(f"OA report must state {n} separately")

    if preloaded_paths is not None and env.sources_read:
        allowed = set(preloaded_paths)
        extra = [p for p in env.sources_read if p.replace("\\", "/").lstrip("./").startswith("sources/") and p not in allowed]
        if extra:
            res.problems.append(f"sources_read lists files that were not pre-loaded: {extra}")
    return res
