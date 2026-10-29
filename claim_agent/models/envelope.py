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
    EFFECT_LINKS,
    EXAM_PATTERN_RISK,
    GATE_NAMES,
    CandidateClass,
    DisclosureStatus,
    ExecStatus,
    FinalizedStatus,
    GateValue,
    InventionPrimary,
    NextStep,
    NoveltyConclusion,
    RiskLevel,
    Sketchability,
    Status,
    StopBasis,
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
    FUNCTION_DEDUCTION_GATE: GateValue | None = None   # 성공조건 조건 2 구성–기능 연역시험
    OA_DRAFT_GATE: GateValue | None = None
    OA_FINAL_GATE: GateValue | None = None
    DEPENDENT_DESIGN_GATE: GateValue | None = None
    DEPENDENT_OA_DRAFT_GATE: GateValue | None = None
    DEPENDENT_OA_FINAL_GATE: GateValue | None = None
    INVENTIVE_STEP: GateValue | None = None
    PRE_STYLE_NON_PATENT_TECHNICAL_READER_CHECK: GateValue | None = None
    PRE_STYLE_GEOMETRIC_OBJECT_CHECK: GateValue | None = None
    PRE_STYLE_FUNCTION_DEDUCTION_CHECK: GateValue | None = None
    PREEXAM_NOVELTY_GATE: GateValue | None = None   # the pre-examiner's suggestion; the engine decides (설계 5.3)

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
    ENFORCEMENT_VALUE_GATE: GateValue | None = None
    PRIOR_ART_CONTRIBUTION_GATE: GateValue | None = None
    planned_claim_no: int | None = None
    parent_claim_no: int | None = None
    # 08 §1.5·5.4: one-line enforcement scenario and the EV1–EV5 results that back ENFORCEMENT_VALUE_GATE.
    enforcement_scenario: str | None = None
    enforcement_failed_tests: str | None = None   # e.g. "EV2, EV4"; schema stays flat (no nested arrays)
    # 08 §5.4 BUSINESS_FIT: ordering signal from the optional user BUSINESS_CONTEXT; never a gate or technical evidence.
    business_fit: str | None = None
    business_fit_basis: str | None = None
    # 10_심사이력_경험기준 ordering signal (설계 14.2); never changes the classification or the 08 gates.
    exam_pattern: str | None = None
    design_change_risk: str | None = None
    exam_pattern_basis: str | None = None


class CitationFill(BaseModel):
    """A blank citation (제항, 제?항) in the user's set that the dependent design filled, with the antecedents it read."""
    model_config = ConfigDict(extra="ignore")
    claim_no: int
    parent_claim_no: int
    basis: str | None = None


class DesignElement(BaseModel):
    """One unit of the design contract that PRE_EXAM compares and the spec handoff table lists (설계 5·14.4)."""
    model_config = ConfigDict(extra="ignore")
    element_id: str
    kind: str = "LIMITATION"         # SKELETON (주골격 명제) | LIMITATION (독립항 한정)
    text: str
    in_independent: bool = True
    source_location: str | None = None
    basis: str | None = None         # DIRECT | DERIVED | UNCONFIRMED


class PreexamRow(BaseModel):
    """COMPARE: one design element against one document (09 P1·P2)."""
    model_config = ConfigDict(extra="ignore")
    element_id: str
    status: DisclosureStatus
    quote: str = ""
    where: str = ""
    note: str = ""


class NoveltyItem(BaseModel):
    """JUDGE: novelty against one document (09 P3·P4·P6)."""
    model_config = ConfigDict(extra="ignore")
    doc_id: str
    conclusion: NoveltyConclusion
    missing: str = ""                # element ids absent from this document, comma separated
    reason: str = ""


class RemainingDifference(BaseModel):
    """JUDGE: a design element no sealed document discloses, with its invention-material location (09 P12, 10 E7·E8)."""
    model_config = ConfigDict(extra="ignore")
    element_id: str
    source_location: str = ""
    note: str = ""
    exam_pattern: str | None = None
    design_change_risk: str | None = None
    effect_link: str | None = None
    why_not_simple: str | None = None
    numeric_basis: str | None = None


class ScreenItem(BaseModel):
    """PRIOR_SCREEN model fallback: one candidate's relevance from its title and abstract."""
    model_config = ConfigDict(extra="ignore")
    skey: str
    relevance: str
    reason: str = ""


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
    stop_basis: StopBasis | None = None
    handoff_ready: bool = False
    finalized_status: FinalizedStatus | None = None
    exact_claim_text: str | None = None
    claims: list[ClaimItem] = Field(default_factory=list)
    per_claim_gates: list[PerClaimGate] = Field(default_factory=list)
    invention_type: InventionType | None = None
    candidates: list[CandidateItem] = Field(default_factory=list)
    citation_fills: list[CitationFill] = Field(default_factory=list)   # dependent-claim-strategy-architect, BASELINE_SET blanks only
    sketchability: Sketchability | None = None
    open_issues: list[OpenIssue] = Field(default_factory=list)
    limitation_evidence: list[LimitationEvidence] = Field(default_factory=list)
    # Prior-art search and pre-exam (설계 8절); empty for every other role.
    design_elements: list[DesignElement] = Field(default_factory=list)
    queries_ko: list[str] = Field(default_factory=list)
    queries_en: list[str] = Field(default_factory=list)
    provisional_elements: list[str] = Field(default_factory=list)
    problem: str | None = None
    screened: list[ScreenItem] = Field(default_factory=list)
    preexam_rows: list[PreexamRow] = Field(default_factory=list)
    doc_published: str | None = None
    doc_field: str | None = None
    doc_summary: str | None = None
    novelty: list[NoveltyItem] = Field(default_factory=list)
    inventive_primary_doc: str | None = None
    inventive_risk: RiskLevel | None = None
    inventive_reason: str | None = None
    remaining_differences: list[RemainingDifference] = Field(default_factory=list)
    sources_read: list[str] = Field(default_factory=list)
    aux_source_usage: str | None = None   # NOT_ACTIVATED | USED
    report_markdown: str = ""

    @field_validator("stop_basis", mode="before")
    @classmethod
    def _norm_stop_basis(cls, v: Any) -> str | None:
        return v if isinstance(v, str) and v in StopBasis.__members__ else None

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
_RISK3 = ["LOW", "MEDIUM", "HIGH"]


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
        "stop_basis": _enum([b.value for b in StopBasis], nullable=True,
                            desc="PASS도 RETURN_TO_*도 아닌 상태로 사용자에게 멈출 때만: 그 정지가 허용되는 사유. 그 밖에는 null"),
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
                    "ENFORCEMENT_VALUE_GATE": _enum(_GATE_ENUM, nullable=True, desc="08 제5.4절 권리행사 가치(EV1~EV5)"),
                    "PRIOR_ART_CONTRIBUTION_GATE": _enum(_GATE_ENUM, nullable=True),
                    "planned_claim_no": _int(nullable=True),
                    "parent_claim_no": _int(nullable=True),
                    "enforcement_scenario": _str("누가 무엇을 실시하면 이 한정의 충족을 어떻게 확인하고 회피하면 무엇을 잃는지 한 줄", nullable=True),
                    "enforcement_failed_tests": _str("실패한 EV 시험 (예: EV2, EV4); 없으면 null", nullable=True),
                    "business_fit": _enum(_RISK3 + ["UNVERIFIED"], nullable=True, desc="BUSINESS_CONTEXT 기준 순위 신호(게이트 아님)"),
                    "business_fit_basis": _str("BUSINESS_CONTEXT 근거 위치 또는 사업 확인 질문", nullable=True),
                    "exam_pattern": _enum(list(EXAM_PATTERN_RISK), nullable=True, desc="10_심사이력_경험기준 유형(순위 신호, 게이트 아님)"),
                    "design_change_risk": _enum(_RISK3, nullable=True),
                    "exam_pattern_basis": _str("유형 분류의 원자료 근거 위치", nullable=True),
                },
                ["dc_id", "classification"],
            )
        ),
        "citation_fills": _arr(
            _obj(
                {
                    "claim_no": _int(),
                    "parent_claim_no": _int(),
                    "basis": _str("그 항의 '상기' 용어와 그 선행 기재가 처음 나오는 항", nullable=True),
                },
                ["claim_no", "parent_claim_no"],
            )
        ),
        "design_elements": _arr(
            _obj(
                {
                    "element_id": _str("E1, E2 … 설계 안에서 고유"),
                    "kind": _enum(["SKELETON", "LIMITATION"]),
                    "text": _str("주골격 명제 또는 독립항 한정의 요지"),
                    "in_independent": {"type": "boolean"},
                    "source_location": _str("발명 원자료 근거 위치", nullable=True),
                    "basis": _enum([b.value for b in EvidenceBasis], nullable=True),
                },
                ["element_id", "kind", "text", "in_independent"],
            )
        ),
        "queries_ko": _arr(_str()),
        "queries_en": _arr(_str()),
        "provisional_elements": _arr(_str()),
        "problem": _str("해결 과제 요지 한두 문장", nullable=True),
        "screened": _arr(_obj({"skey": _str(), "relevance": _enum(["높음", "중간", "낮음"]), "reason": _str()}, ["skey", "relevance"])),
        "preexam_rows": _arr(
            _obj(
                {
                    "element_id": _str(),
                    "status": _enum([s.value for s in DisclosureStatus if s != DisclosureStatus.UNCONFIRMED]),
                    "quote": _str("선행문헌 원문 그대로 30~200자, 미개시면 빈 문자열"),
                    "where": _str("문단 번호 [0012] 등"),
                    "note": _str(),
                },
                ["element_id", "status", "quote"],
            )
        ),
        "doc_published": _str("문헌에 적힌 공개일", nullable=True),
        "doc_field": _str(nullable=True),
        "doc_summary": _str(nullable=True),
        "novelty": _arr(
            _obj(
                {"doc_id": _str(), "conclusion": _enum([c.value for c in NoveltyConclusion]), "missing": _str("그 문헌에 없는 요소 id, 쉼표 구분"), "reason": _str()},
                ["doc_id", "conclusion"],
            )
        ),
        "inventive_primary_doc": _str(nullable=True),
        "inventive_risk": _enum([r.value for r in RiskLevel], nullable=True),
        "inventive_reason": _str(nullable=True),
        "remaining_differences": _arr(
            _obj(
                {
                    "element_id": _str(),
                    "source_location": _str("발명 원자료 위치"),
                    "note": _str(),
                    "exam_pattern": _enum(list(EXAM_PATTERN_RISK), nullable=True),
                    "design_change_risk": _enum(_RISK3, nullable=True),
                    "effect_link": _enum(EFFECT_LINKS, nullable=True),
                    "why_not_simple": _str("단순 결합으로 안 되는 이유의 원자료 위치, 없으면 NONE", nullable=True),
                    "numeric_basis": _str("수치 요소의 이유·비교 원자료 위치, 없으면 NONE", nullable=True),
                },
                ["element_id", "source_location"],
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


# Prior-art and exam-history fields belong to a few roles only. Gemini rejects the union as too complex
# (400 INVALID_ARGUMENT on the live run), so each role receives the shared fields plus its own.
ROLE_EXTRA_FIELDS: dict[str, tuple[str, ...]] = {
    "claim-architect": ("design_elements",),
    "dependent-claim-strategy-architect": ("citation_fills",),
    "prior-art-searcher": ("queries_ko", "queries_en", "provisional_elements", "problem", "screened"),
    "prior-art-pre-examiner": ("preexam_rows", "doc_published", "doc_field", "doc_summary", "novelty", "inventive_primary_doc",
                               "inventive_risk", "inventive_reason", "remaining_differences"),
}
CANDIDATE_EXAM_FIELDS = ("exam_pattern", "design_change_risk", "exam_pattern_basis")   # dependent-claim-strategy-architect only
_ALL_EXTRA = {f for fields in ROLE_EXTRA_FIELDS.values() for f in fields}
_ROLE_SCHEMAS: dict[str, dict[str, Any]] = {}


def envelope_schema_for(role: str) -> dict[str, Any]:
    """ENVELOPE_JSON_SCHEMA without the fields other roles own. Shared and cached: callers must not mutate it."""
    if role not in _ROLE_SCHEMAS:
        import copy

        schema = copy.deepcopy(ENVELOPE_JSON_SCHEMA)
        for name in _ALL_EXTRA - set(ROLE_EXTRA_FIELDS.get(role, ())):
            schema["properties"].pop(name)
        if role != "dependent-claim-strategy-architect":
            for name in CANDIDATE_EXAM_FIELDS:
                schema["properties"]["candidates"]["items"]["properties"].pop(name)
        if role != "prior-art-pre-examiner":
            schema["properties"]["gates"]["properties"].pop("PREEXAM_NOVELTY_GATE")
        _ROLE_SCHEMAS[role] = schema
    return _ROLE_SCHEMAS[role]


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
