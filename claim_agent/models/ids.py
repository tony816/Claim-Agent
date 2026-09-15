"""Identifier formats and revision bumping.

Formats come from the role files and web/HANDOFF_TEMPLATES.md:
  md-<cid>-<rN>-NN, dmd-<dsid>-<drN>-NN, sr-, dsr-, bs-<cid>-<rN>-NN,
  dbs-<dsid>-<drN>-<target>-NN, locks dcl-/fcl-/ddsl-/fdsl-.
Style records have no prescribed prefix; we use sty-/dsty-. Syntax, OA and
reference-compare records use syn-/oa-/rc- (and d- prefixed dependent variants).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_REV = re.compile(r"^r(\d+)$")
_DREV = re.compile(r"^d(\d+)$")
_DDREV = re.compile(r"^dd(\d+)$")
_DRREV = re.compile(r"^dr(\d+)$")


def _bump(value: str, pattern: re.Pattern[str], prefix: str) -> str:
    m = pattern.match(value)
    if not m:
        raise ValueError(f"invalid revision token {value!r} for prefix {prefix}")
    return f"{prefix}{int(m.group(1)) + 1}"


def bump_revision(rev: str) -> str:
    return _bump(rev, _REV, "r")


def bump_design_revision(rev: str) -> str:
    return _bump(rev, _DREV, "d")


def bump_dependent_design_revision(rev: str) -> str:
    return _bump(rev, _DDREV, "dd")


def bump_dependent_revision(rev: str) -> str:
    return _bump(rev, _DRREV, "dr")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str, n: int = 8) -> str:
    return sha256_text(text)[:n]


RECORD_PREFIX = {
    "meaning_draft": "md",
    "dependent_meaning_draft": "dmd",
    "style": "sty",
    "dependent_style": "dsty",
    "success": "sr",
    "dependent_success": "dsr",
    "syntax": "syn",
    "dependent_syntax": "dsyn",
    "oa": "oa",
    "dependent_oa": "doa",
    "blind": "bs",
    "dependent_blind": "dbs",
    "reference_compare": "rc",
    "dependent_reference_compare": "drc",
    "design": "dg",
    "dependent_design": "ddg",
    "draft_claim_lock": "dcl",
    "final_claim_lock": "fcl",
    "draft_dependent_set_lock": "ddsl",
    "final_dependent_set_lock": "fdsl",
    "baseline_set": "bl",
}


@dataclass
class Identifiers:
    candidate_id: str
    revision: str = "r1"
    design_revision: str = "d1"
    dependent_set_id: str | None = None
    dependent_design_revision: str | None = None
    dependent_revision: str | None = None
    target_claim_id: str | None = None
    input_revision: str = "i1"
    extra: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, str | None]:
        return {
            "candidate_id": self.candidate_id,
            "revision": self.revision,
            "design_revision": self.design_revision,
            "dependent_set_id": self.dependent_set_id,
            "dependent_design_revision": self.dependent_design_revision,
            "dependent_revision": self.dependent_revision,
            "target_claim_id": self.target_claim_id,
        }


def record_id(kind: str, ids: Identifiers, seq: int = 1) -> str:
    """Deterministic record id for a record kind and identifier set."""
    prefix = RECORD_PREFIX[kind]
    nn = f"{seq:02d}"
    if kind in {"design"}:
        return f"{prefix}-{ids.candidate_id}-{ids.design_revision}-{nn}"
    if kind in {"dependent_design"}:
        return f"{prefix}-{ids.dependent_set_id}-{ids.dependent_design_revision}-{nn}"
    if kind == "dependent_blind" or kind == "dependent_reference_compare":
        return f"{prefix}-{ids.dependent_set_id}-{ids.dependent_revision}-{ids.target_claim_id}-{nn}"
    if kind.startswith("dependent_") or kind.endswith("dependent_set_lock"):
        return f"{prefix}-{ids.dependent_set_id}-{ids.dependent_revision}-{nn}"
    return f"{prefix}-{ids.candidate_id}-{ids.revision}-{nn}"


def input_revision_id(material_digest: str) -> str:
    """input_revision is derived from the digest of the raw-material set."""
    return f"i-{material_digest[:8]}"
