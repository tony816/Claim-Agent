"""Closed input packet for the blind reviewer and a contamination scanner."""
from __future__ import annotations

from dataclasses import dataclass

from .. import PROTOCOL_VERSION

# Markers that must never appear in a blind packet outside the claim text itself.
FORBIDDEN_MARKERS = (
    "DESIGN_GATE",
    "DEPENDENT_DESIGN_GATE",
    "DC-",
    "주골격",
    "성공조건",
    "success_record",
    "style_record",
    "OA_DRAFT_GATE",
    "OA_FINAL_GATE",
    "syntax",
    "명세서",
    "도면",
    "효과",
    "수정 목표",
    "<<<FILE",
)


class BlindContamination(ValueError):
    pass


@dataclass(frozen=True)
class BlindPacket:
    claim_scope: str                      # INDEPENDENT | DEPENDENT_SINGLE
    candidate_id: str
    revision: str
    design_revision: str
    record_id: str
    claim_text: str | None = None         # INDEPENDENT only
    dependent_set_id: str | None = None
    dependent_design_revision: str | None = None
    dependent_revision: str | None = None
    target_claim_id: str | None = None
    parent_chain_text: str | None = None
    target_claim_text: str | None = None

    def render(self) -> str:
        lines = [
            "mode: BLIND_SNAPSHOT",
            f"protocol_version: {PROTOCOL_VERSION}",
            f"claim_scope: {self.claim_scope}",
            f"candidate_id: {self.candidate_id}",
            f"revision: {self.revision}",
            f"design_revision: {self.design_revision}",
            f"record_id: {self.record_id}",
        ]
        if self.claim_scope == "INDEPENDENT":
            if not self.claim_text:
                raise BlindContamination("INDEPENDENT blind packet requires claim_text")
            lines += ["청구항 전문:", self.claim_text]
        else:
            required = [self.dependent_set_id, self.dependent_design_revision, self.dependent_revision, self.target_claim_id, self.parent_chain_text, self.target_claim_text]
            if any(not v for v in required):
                raise BlindContamination("DEPENDENT_SINGLE blind packet is missing required fields")
            lines += [
                f"dependent_set_id: {self.dependent_set_id}",
                f"dependent_design_revision: {self.dependent_design_revision}",
                f"dependent_revision: {self.dependent_revision}",
                f"target_claim_id: {self.target_claim_id}",
                "parent_chain_text:",
                self.parent_chain_text or "",
                "target_claim_text:",
                self.target_claim_text or "",
            ]
        text = "\n".join(lines) + "\n"
        assert_isolated(text, claim_texts=[t for t in (self.claim_text, self.parent_chain_text, self.target_claim_text) if t])
        return text


def assert_isolated(packet_text: str, claim_texts: list[str]) -> None:
    """Raise if any forbidden marker appears outside the allowed claim texts."""
    residue = packet_text
    for t in claim_texts:
        residue = residue.replace(t, "")
    for marker in FORBIDDEN_MARKERS:
        if marker in residue:
            raise BlindContamination(f"blind packet contaminated by marker {marker!r}")
