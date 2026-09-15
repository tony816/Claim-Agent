"""Enforced per-(role, scope) source whitelist (from sources/README.md and role files).

Only keys listed here may be pre-loaded into a role's context. The blind
reviewer gets nothing. Conditional sources (ROUTING/CORPUS) are never
pre-loaded; they are reached only through the restricted corpus tool or
injected as exact fragments by the orchestrator for the success reviewer.
"""
from __future__ import annotations

from ..models.enums import Scope

SOURCE_WHITELIST: dict[tuple[str, Scope], list[str]] = {
    ("claim-architect", Scope.INDEPENDENT): ["README", "SUCCESS", "S07"],
    ("claim-drafter", Scope.INDEPENDENT): ["SUCCESS"],
    ("claim-drafter", Scope.DEPENDENT_SET): ["SUCCESS", "S08", "S05"],
    ("claim-style-adjuster", Scope.INDEPENDENT): ["README", "S07", "S04"],
    ("claim-style-adjuster", Scope.DEPENDENT_SET): ["README", "S07", "S04"],
    ("claim-success-reviewer", Scope.INDEPENDENT): ["README", "SUCCESS", "S07", "S04"],
    ("claim-success-reviewer", Scope.DEPENDENT_SET): ["README", "SUCCESS", "S07", "S04", "S08", "S05"],
    ("syntax-scope-reviewer", Scope.INDEPENDENT): ["S07", "S04"],
    ("syntax-scope-reviewer", Scope.DEPENDENT_SET): ["S07", "S04", "S08", "S05"],
    ("oa-strategy-reviewer", Scope.INDEPENDENT): ["S06"],
    ("oa-strategy-reviewer", Scope.DEPENDENT_SET): ["S06", "S08"],
    ("dependent-claim-strategy-architect", Scope.DEPENDENT_SET): ["README", "SUCCESS", "S05", "S07", "S08"],
    ("picture-claim-reconstruction-reviewer", Scope.INDEPENDENT): [],
    ("picture-claim-reconstruction-reviewer", Scope.DEPENDENT_SINGLE): [],
    ("blind-claim-reconstruction-reviewer", Scope.INDEPENDENT): [],
    ("blind-claim-reconstruction-reviewer", Scope.DEPENDENT_SINGLE): [],
}

# Roles that may receive the invention raw material (원자료). Blind never does.
INVENTION_SOURCE_ROLES = {
    "claim-architect",
    "claim-drafter",
    "claim-style-adjuster",
    "claim-success-reviewer",
    "syntax-scope-reviewer",
    "oa-strategy-reviewer",
    "dependent-claim-strategy-architect",
    "picture-claim-reconstruction-reviewer",
}


def sources_for(role: str, scope: Scope) -> list[str]:
    try:
        return list(SOURCE_WHITELIST[(role, scope)])
    except KeyError as exc:
        raise KeyError(f"role {role} is not allowed to run with scope {scope}") from exc
