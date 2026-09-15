"""Pre-run call/token/cost estimates for live evals and budget previews (no LLM)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..claim_scope import parse_target
from ..models.request import RunRequest
from ..store.telemetry import estimate_cost

# Fallback per-call token profile used when no live telemetry exists yet.
DEFAULT_PROFILE = {"prompt_tokens": 30_000, "cached_tokens": 0, "thoughts_tokens": 4_000, "output_tokens": 6_000}
NON_LIVE_PROVIDERS = {"replay", "scripted", ""}


@dataclass
class RunEstimate:
    calls: int
    calls_by_role: dict[str, int]
    tokens: dict[str, int]
    cost: float | None
    basis: str   # "telemetry(N rows)" | "default profile"

    def render(self) -> str:
        cost = f"${self.cost:.4f}" if self.cost is not None else "N/A (telemetry.pricing 미설정)"
        roles = ", ".join(f"{r}×{n}" for r, n in self.calls_by_role.items())
        return f"예상 호출 {self.calls}회 ({roles}); 입력 {self.tokens['prompt_tokens']:,} / 출력 {self.tokens['output_tokens'] + self.tokens['thoughts_tokens']:,} 토큰; 추정 비용 {cost}; 근거: {self.basis}"


def expected_calls(req: RunRequest, n_targets_default: int = 3) -> dict[str, int]:
    """Planned main-phase calls for a clean run (no returns): mirrors CLAUDE.md steps 1-19."""
    if req.request_mode.value == "REVIEW_ONLY":
        return {r: 1 for r in (req.reviewers or ["syntax-scope-reviewer"])}
    if req.authoring_scope == "EXISTING_SET_EDIT":
        n = len(parse_target(req.dependent_target) or ()) or 1
        return {   # one combined success+syntax+OA review call
            "dependent-claim-strategy-architect": 1, "claim-drafter": 1, "claim-style-adjuster": 2, "claim-success-reviewer": 1,
            "blind-claim-reconstruction-reviewer": n, "picture-claim-reconstruction-reviewer": n,
        }
    calls = {
        "claim-architect": 1, "claim-drafter": 1, "claim-style-adjuster": 2,  # tool phase + main
        "claim-success-reviewer": 1, "syntax-scope-reviewer": 1, "oa-strategy-reviewer": 1,
        "blind-claim-reconstruction-reviewer": 1, "picture-claim-reconstruction-reviewer": 1,
    }
    if req.dependent:
        try:
            targets = parse_target(req.dependent_target)
        except ValueError:
            targets = None
        n = len(targets) if targets else n_targets_default
        calls["dependent-claim-strategy-architect"] = 1
        calls["claim-drafter"] += 1
        calls["claim-style-adjuster"] += 2
        calls["claim-success-reviewer"] += 1
        calls["syntax-scope-reviewer"] += 1
        calls["oa-strategy-reviewer"] += 1
        calls["blind-claim-reconstruction-reviewer"] += n
        calls["picture-claim-reconstruction-reviewer"] += n
    return calls


def role_profiles(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, int]], int]:
    """Average live token usage per role from telemetry rows (main/tool_phase only)."""
    acc: dict[str, dict[str, int]] = {}
    cnt: dict[str, int] = {}
    used = 0
    for r in rows:
        if r.get("phase") not in ("main", "tool_phase") or r.get("provider") in NON_LIVE_PROVIDERS or str(r.get("provider", "")).startswith("record("):
            continue
        used += 1
        a = acc.setdefault(r["role"], dict.fromkeys(DEFAULT_PROFILE, 0))
        cnt[r["role"]] = cnt.get(r["role"], 0) + 1
        for k in DEFAULT_PROFILE:
            a[k] += int(r.get(k, 0) or 0)
    return {role: {k: v // cnt[role] for k, v in a.items()} for role, a in acc.items()}, used


def estimate_run(req: RunRequest, model: str, pricing: dict[str, dict[str, float]], telemetry_rows: list[dict[str, Any]] | None = None) -> RunEstimate:
    calls = expected_calls(req)
    profiles, used = role_profiles(telemetry_rows or [])
    tokens = dict.fromkeys(DEFAULT_PROFILE, 0)
    cost_total = 0.0
    priced = bool(pricing.get(model))
    for role, n in calls.items():
        prof = profiles.get(role, DEFAULT_PROFILE)
        for k in tokens:
            tokens[k] += prof[k] * n
        if priced:
            c = estimate_cost(model, prof, pricing)
            cost_total += (c or 0.0) * n
    return RunEstimate(sum(calls.values()), calls, tokens, round(cost_total, 4) if priced else None, f"telemetry({used} rows)" if used else "default profile")
