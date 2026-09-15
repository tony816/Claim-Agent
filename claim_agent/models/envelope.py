"""The single JSON envelope every role returns.

The envelope carries only orchestration-critical facts; the role's original
Korean 출력 형식 lives verbatim in ``report_markdown`` and is what the next role
receives as 전문. The JSON schema exported for Gemini is flat (depth <= 3), has
no dynamic keys and no additionalProperties.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import (
    GATE_NAMES,
    CandidateClass,
    ExecStatus,
    FinalizedStatus,
    GateValue,
    InventionPrimary,
    NextStep,
    Sketchability,
    Status,
    normalize_reason,
)


class EnvelopeIdentifiers(BaseModel):
    model_config = ConfigDict(extra="ignore")
    candidate_id: str = ""
    revision: str = ""
    design_revision: str = ""
    dependent_set_id: str | None = None
    dependent_design_revision: str | None = None
    dependent_revision: str | None = None
    target_claim_id: str | None = None
    record_id: str = ""


class Gates(BaseModel):
    model_config = ConfigDict(extra="ignore")
    DESIGN_GATE: GateValue | None = None
    DRAFTER_GATE: GateValue | None = None
    TERM_EXPRESSION_GATE: GateValue | None = None
    CLAIM_STYLE_GATE: GateValue | None = None
    NON_PATENT_TECHNICAL_READER_GATE: GateValue | None = None
    GEOMETRIC_OBJECT_GATE: GateValue | None = None
    OA_DRAFT_GATE: GateValue | None = None
    OA_FINAL_GATE: GateValue | None = None
    DEPENDENT_DESIGN_GATE: GateValue | None = None
    DEPENDENT_OA_DRAFT_GATE: GateValue | None = None
    DEPENDENT_OA_FINAL_GATE: GateValue | None = None
    INVENTIVE_STEP: GateValue | None = None
    PRE_STYLE_NON_PATENT_TECHNICAL_READER_CHECK: GateValue | None = None
    PRE_STYLE_GEOMETRIC_OBJECT_CHECK: GateValue | None = None

    def get(self, name: str) -> GateValue | None:
        return getattr(self, name, None)

    def present(self) -> dict[str, str]:
        return {k: str(v.value) for k, v in self.model_dump().items() if v is not None}


class GateReason(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gate: str
    reason_code: str


class Check(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    status: GateValue
    reason_code: str | None = None
    note: str | None = None


class ClaimItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    claim_no: int
    parent_claim_no: int | None = None
    dc_id: str | None = None
    text: str


class PerClaimGate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    claim_no: int
    verdict: GateValue
    NON_PATENT_TECHNICAL_READER_GATE: GateValue | None = None
    GEOMETRIC_OBJECT_GATE: GateValue | None = None


class InventionType(BaseModel):
    model_config = ConfigDict(extra="ignore")
    primary: InventionPrimary
    secondary: list[InventionPrimary] = Field(default_factory=list)
    hybrid: bool = False


class CandidateItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    dc_id: str
    classification: CandidateClass
    DEPENDENT_SOURCE_GATE: GateValue | None = None
    CAUSAL_CONTRIBUTION_GATE: GateValue | None = None
    CLAIMABILITY_GATE: GateValue | None = None
    PRIOR_ART_CONTRIBUTION_GATE: GateValue | None = None
    planned_claim_no: int | None = None
    parent_claim_no: int | None = None


class EvidenceBasis(str, Enum):  # noqa: UP042
    DIRECT = "DIRECT"            # 직접 기재
    DERIVED = "DERIVED"          # 통상의 기술자가 명확히 도출
    UNCONFIRMED = "UNCONFIRMED"  # 근거 미확인


class LimitationEvidence(BaseModel):
    """One row of the 한정별 근거표 (조건 4): limitation → source → location → basis."""
    model_config = ConfigDict(extra="ignore")
    limitation: str
    role: str | None = None          # F/E/C/N/I/S 또는 null
    source_name: str | None = None   # 원자료 파일명 또는 USER_LOCK
    location: str | None = None      # 단락·도면 번호·표 행
    basis: EvidenceBasis = EvidenceBasis.UNCONFIRMED
    note: str | None = None


class OpenIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str            # REVIEW | BLOCK | UNVERIFIED | EVIDENCE_UNCONFIRMED
    code: str = ""
    text: str
    return_to: str | None = None


class RoleEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    identifiers: EnvelopeIdentifiers = Field(default_factory=EnvelopeIdentifiers)
    execution_status: ExecStatus = ExecStatus.RUN
    status: Status
    reason_code: str | None = None
    gates: Gates = Field(default_factory=Gates)
    gate_reasons: list[GateReason] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    next_step: NextStep = NextStep.PROCEED
    handoff_ready: bool = False
    finalized_status: FinalizedStatus | None = None
    exact_claim_text: str | None = None
    claims: list[ClaimItem] = Field(default_factory=list)
    per_claim_gates: list[PerClaimGate] = Field(default_factory=list)
    invention_type: InventionType | None = None
    candidates: list[CandidateItem] = Field(default_factory=list)
    sketchability: Sketchability | None = None
    open_issues: list[OpenIssue] = Field(default_factory=list)
    limitation_evidence: list[LimitationEvidence] = Field(default_factory=list)
    sources_read: list[str] = Field(default_factory=list)
    aux_source_usage: str | None = None   # NOT_ACTIVATED | USED
    report_markdown: str = ""

    @field_validator("reason_code", mode="before")
    @classmethod
    def _norm_reason(cls, v: Any) -> str | None:
        return normalize_reason(v if isinstance(v, str) else None)

    def gate(self, name: str) -> GateValue | None:
        return self.gates.get(name)

    def gate_reason(self, gate: str) -> str | None:
        for gr in self.gate_reasons:
            if gr.gate == gate:
                return gr.reason_code
        return None

    def unconfirmed_evidence(self) -> list[str]:
        return [e.limitation for e in self.limitation_evidence if e.basis == EvidenceBasis.UNCONFIRMED]

    def non_pass_checks(self) -> list[str]:
        return [c.name for c in self.checks if c.status not in (GateValue.PASS, GateValue.NOT_APPLICABLE, GateValue.PASS_RANGE)]


# ---------------------------------------------------------------------------
# JSON schema for Gemini structured output (hand-written: flat, closed enums).
# ---------------------------------------------------------------------------

_GATE_ENUM = [g.value for g in GateValue]
_STATUS_ENUM = [s.value for s in Status]


def _str(desc: str = "", nullable: bool = False) -> dict[str, Any]:
    t: dict[str, Any] = {"type": ["string", "null"] if nullable else "string"}
    if desc:
        t["description"] = desc
    return t


def _enum(values: list[str], nullable: bool = False, desc: str = "") -> dict[str, Any]:
    t: dict[str, Any] = {"type": ["string", "null"] if nullable else "string", "enum": values + ([None] if nullable else [])}
    if desc:
        t["description"] = desc
    return t


def _int(nullable: bool = False) -> dict[str, Any]:
    return {"type": ["integer", "null"] if nullable else "integer"}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


ENVELOPE_JSON_SCHEMA: dict[str, Any] = _obj(
    {
        "identifiers": _obj(
            {
                "candidate_id": _str(),
                "revision": _str(),
                "design_revision": _str(),
                "dependent_set_id": _str(nullable=True),
                "dependent_design_revision": _str(nullable=True),
                "dependent_revision": _str(nullable=True),
                "target_claim_id": _str(nullable=True),
                "record_id": _str("오케스트레이터가 지정한 record_id를 그대로 기재"),
            },
            ["candidate_id", "revision", "design_revision", "record_id"],
        ),
        "execution_status": _enum([e.value for e in ExecStatus]),
        "status": _enum(_STATUS_ENUM, desc="역할 출력의 상태/종합 판정/최종 판정"),
        "reason_code": _str("상태 뒤에 붙는 사유 코드(예: SPEC_NOT_PROVIDED), 없으면 null", nullable=True),
        "gates": _obj({name: _enum(_GATE_ENUM, nullable=True) for name in GATE_NAMES}),
        "gate_reasons": _arr(_obj({"gate": _str(), "reason_code": _str()}, ["gate", "reason_code"])),
        "checks": _arr(
            _obj(
                {"name": _str(), "status": _enum(_GATE_ENUM), "reason_code": _str(nullable=True), "note": _str(nullable=True)},
                ["name", "status"],
            )
        ),
        "next_step": _enum([n.value for n in NextStep]),
        "handoff_ready": {"type": "boolean", "description": "인계 가능 / syntax·OA·DRAFT 진행 가능 여부"},
        "finalized_status": _enum([f.value for f in FinalizedStatus], nullable=True),
        "exact_claim_text": _str("작성·확정·검수 대상 청구항 전문(글자 단위 그대로)", nullable=True),
        "claims": _arr(
            _obj(
                {"claim_no": _int(), "parent_claim_no": _int(nullable=True), "dc_id": _str(nullable=True), "text": _str()},
                ["claim_no", "text"],
            )
        ),
        "per_claim_gates": _arr(
            _obj(
                {
                    "claim_no": _int(),
                    "verdict": _enum(_GATE_ENUM),
                    "NON_PATENT_TECHNICAL_READER_GATE": _enum(_GATE_ENUM, nullable=True),
                    "GEOMETRIC_OBJECT_GATE": _enum(_GATE_ENUM, nullable=True),
                },
                ["claim_no", "verdict"],
            )
        ),
        "invention_type": {
            "type": ["object", "null"],
            "properties": {
                "primary": _enum([p.value for p in InventionPrimary]),
                "secondary": _arr(_enum([p.value for p in InventionPrimary])),
                "hybrid": {"type": "boolean"},
            },
            "required": ["primary"],
        },
        "candidates": _arr(
            _obj(
                {
                    "dc_id": _str(),
                    "classification": _enum([c.value for c in CandidateClass]),
                    "DEPENDENT_SOURCE_GATE": _enum(_GATE_ENUM, nullable=True),
                    "CAUSAL_CONTRIBUTION_GATE": _enum(_GATE_ENUM, nullable=True),
                    "CLAIMABILITY_GATE": _enum(_GATE_ENUM, nullable=True),
                    "PRIOR_ART_CONTRIBUTION_GATE": _enum(_GATE_ENUM, nullable=True),
                    "planned_claim_no": _int(nullable=True),
                    "parent_claim_no": _int(nullable=True),
                },
                ["dc_id", "classification"],
            )
        ),
        "sketchability": _enum([s.value for s in Sketchability], nullable=True),
        "open_issues": _arr(
            _obj({"kind": _str(), "code": _str(), "text": _str(), "return_to": _str(nullable=True)}, ["kind", "text"])
        ),
        "limitation_evidence": _arr(
            _obj(
                {
                    "limitation": _str("한정 문언"),
                    "role": _str("F/E/C/N/I/S", nullable=True),
                    "source_name": _str("근거 원자료 파일명 또는 USER_LOCK", nullable=True),
                    "location": _str("근거 위치(단락·도면·표)", nullable=True),
                    "basis": _enum([b.value for b in EvidenceBasis], desc="DIRECT 직접 기재 / DERIVED 명확한 도출 / UNCONFIRMED 근거 미확인"),
                    "note": _str(nullable=True),
                },
                ["limitation", "basis"],
            )
        ),
        "sources_read": _arr(_str()),
        "aux_source_usage": _enum(["NOT_ACTIVATED", "USED"], nullable=True),
        "report_markdown": _str("역할 파일의 출력 형식을 그대로 따른 한국어 보고서 전문"),
    },
    ["identifiers", "execution_status", "status", "gates", "next_step", "handoff_ready", "report_markdown"],
)


def schema_depth(node: Any, depth: int = 0) -> int:
    """Maximum nesting depth of object/array nodes (used by tests and doctor)."""
    if isinstance(node, dict):
        best = depth
        for key in ("properties", "items"):
            if key in node:
                child = node[key]
                if key == "properties":
                    for sub in child.values():
                        best = max(best, schema_depth(sub, depth + 1))
                else:
                    best = max(best, schema_depth(child, depth + 1))
        return best
    return depth
