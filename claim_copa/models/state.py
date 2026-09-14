"""Persistent run state (runs/<run_id>/state.json)."""
from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .enums import RequestMode, StyleChangeMode


class Stage(str, Enum):
    ARCHITECT = "ARCHITECT"
    DRAFT = "DRAFT"
    STYLE = "STYLE"
    SUCCESS = "SUCCESS"
    SYNTAX = "SYNTAX"
    OA = "OA"
    BLIND = "BLIND"
    PICTURE = "PICTURE"
    LOCK = "LOCK"
    DEP_ARCHITECT = "DEP_ARCHITECT"
    DEP_DRAFT = "DEP_DRAFT"
    DEP_STYLE = "DEP_STYLE"
    DEP_SUCCESS = "DEP_SUCCESS"
    DEP_SYNTAX = "DEP_SYNTAX"
    DEP_OA = "DEP_OA"
    DEP_RECON = "DEP_RECON"
    DEP_LOCK = "DEP_LOCK"
    REVIEW_ONLY = "REVIEW_ONLY"
    DONE = "DONE"
    HALTED = "HALTED"


INDEPENDENT_ORDER = [Stage.ARCHITECT, Stage.DRAFT, Stage.STYLE, Stage.SUCCESS, Stage.SYNTAX, Stage.OA, Stage.BLIND, Stage.PICTURE, Stage.LOCK]
DEPENDENT_ORDER = [Stage.DEP_ARCHITECT, Stage.DEP_DRAFT, Stage.DEP_STYLE, Stage.DEP_SUCCESS, Stage.DEP_SYNTAX, Stage.DEP_OA, Stage.DEP_RECON, Stage.DEP_LOCK]


class RecordRef(BaseModel):
    record_id: str
    kind: str
    role: str
    scope: str
    stage: str
    status: str
    execution_status: str = "RUN"
    gates: dict[str, str] = Field(default_factory=dict)
    issued: bool = True
    superseded: bool = False
    stale: bool = False
    text_sha256: str | None = None
    call_file: str = ""
    ids: dict[str, Any] = Field(default_factory=dict)
    source_set_id: str = ""
    created_at: float = Field(default_factory=time.time)


class RevisionState(BaseModel):
    revision: str
    design_revision: str
    style_change_mode: StyleChangeMode = StyleChangeMode.INITIAL_FROM_DRAFTER
    change_goal: str | None = None
    prior_revision: str | None = None
    meaning_draft_text: str | None = None
    exact_text: str | None = None
    exact_sha256: str | None = None
    records: dict[str, str] = Field(default_factory=dict)   # kind -> record_id


class CandidateState(BaseModel):
    candidate_id: str
    design_revision: str = "d1"
    revision: str = "r1"
    design_record_id: str | None = None
    current: RevisionState | None = None
    history: list[RevisionState] = Field(default_factory=list)
    draft_claim_lock: str | None = None
    final_claim_lock: str | None = None
    loop_counts: dict[str, int] = Field(default_factory=dict)


class TargetRecon(BaseModel):
    target_claim_id: str
    parent_chain_text: str
    target_claim_text: str
    blind_record_id: str | None = None
    picture_record_id: str | None = None
    blind_status: str | None = None
    picture_status: str | None = None


class DependentRevisionState(BaseModel):
    dependent_revision: str
    dependent_design_revision: str
    style_change_mode: StyleChangeMode = StyleChangeMode.INITIAL_FROM_DRAFTER
    change_goal: str | None = None
    meaning_draft_text: str | None = None
    exact_text: str | None = None
    exact_sha256: str | None = None
    claims: list[dict[str, Any]] = Field(default_factory=list)   # [{claim_no, parent_claim_no, dc_id, text}]
    records: dict[str, str] = Field(default_factory=dict)
    targets: dict[str, TargetRecon] = Field(default_factory=dict)
    reconstruction_gate: str | None = None


class DependentSetState(BaseModel):
    dependent_set_id: str
    root_lock_id: str
    root_candidate_id: str
    root_revision: str
    root_design_revision: str
    dependent_design_revision: str = "dd1"
    dependent_revision: str = "dr1"
    design_record_id: str | None = None
    current: DependentRevisionState | None = None
    history: list[DependentRevisionState] = Field(default_factory=list)
    draft_set_lock: str | None = None
    final_set_lock: str | None = None
    loop_counts: dict[str, int] = Field(default_factory=dict)
    stale: bool = False


class Halt(BaseModel):
    stage: str
    role: str
    kind: str                     # REVIEW | BLOCK | UNVERIFIED | LOOP_LIMIT | ENVELOPE_INVALID | ERROR | USER_DECISION
    reason_code: str | None = None
    message: str = ""
    open_issues: list[dict[str, Any]] = Field(default_factory=list)
    return_to: str | None = None
    record_id: str | None = None
    report_markdown: str | None = None  # actual stopping role's analysis, not a generated summary


class RunState(BaseModel):
    run_id: str
    request: dict[str, Any]
    request_mode: RequestMode
    source_set_id: str
    variant_id: str = "default"
    lessons_hash: str = "none"
    input_revision: str = "i1"
    material_meta: list[dict[str, Any]] = Field(default_factory=list)
    spec_present: bool = False
    prior_art_present: bool = False
    stage: Stage = Stage.ARCHITECT
    candidate: CandidateState
    dependent: DependentSetState | None = None
    records: dict[str, RecordRef] = Field(default_factory=dict)
    halt: Halt | None = None
    outcome: str = "RUNNING"
    call_seq: int = 0
    total_loops: int = 0
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    notes: list[str] = Field(default_factory=list)
    review_reports: dict[str, str] = Field(default_factory=dict)

    def record(self, record_id: str | None) -> RecordRef | None:
        return self.records.get(record_id) if record_id else None

    def mark_superseded(self, record_ids: list[str]) -> None:
        for rid in record_ids:
            if rid in self.records:
                self.records[rid].superseded = True

    def mark_all_stale(self) -> None:
        for r in self.records.values():
            r.stale = True
        if self.dependent:
            self.dependent.stale = True
