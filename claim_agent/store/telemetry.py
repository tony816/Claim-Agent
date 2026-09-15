"""Per-call telemetry (JSON lines). Never contains claim text or raw material."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TelemetryRow:
    ts: float
    run_id: str
    seq: int
    stage: str
    role: str
    scope: str
    request_mode: str
    model: str
    provider: str
    variant_id: str
    source_set_id: str
    lessons_hash: str
    candidate_id: str
    revision: str
    design_revision: str
    dependent_set_id: str | None
    dependent_revision: str | None
    target_claim_id: str | None
    loop_index: int
    retry_count: int
    prompt_tokens: int
    cached_tokens: int
    thoughts_tokens: int
    output_tokens: int
    latency_ms: int
    cache_hit: bool
    parse_ok: bool
    repair_used: bool
    execution_status: str
    status: str
    reason_code: str | None
    gates: dict[str, str] = field(default_factory=dict)
    non_pass_checks: list[str] = field(default_factory=list)
    next_step: str = ""
    handoff_ready: bool = False
    invention_primary: str | None = None
    cost_estimate: float | None = None
    phase: str = "main"          # main | repair | tool_phase | tool | shadow
    record_id: str | None = None
    tool: dict[str, Any] = field(default_factory=dict)   # phase="tool" rows only; never carries query text


def estimate_cost(model: str, usage: dict[str, int], pricing: dict[str, dict[str, float]]) -> float | None:
    p = pricing.get(model)
    if not p:
        return None
    m = 1_000_000
    cached = usage.get("cached_tokens", 0)
    prompt = max(0, usage.get("prompt_tokens", 0) - cached)
    out = usage.get("output_tokens", 0) + usage.get("thoughts_tokens", 0)
    return round(prompt / m * p.get("input_per_m", 0) + cached / m * p.get("cached_per_m", 0) + out / m * p.get("output_per_m", 0), 6)


def usage_row(model: str, usage: dict[str, int], pricing: dict[str, dict[str, float]]) -> dict[str, float]:
    """One call in the shape RunState.usage accumulates; the engine and the router meter both count with it."""
    cost = estimate_cost(model, usage, pricing)
    return {"calls": 1, "prompt_tokens": usage.get("prompt_tokens", 0), "cached_tokens": usage.get("cached_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0), "thoughts_tokens": usage.get("thoughts_tokens", 0),
            "cost_usd": cost or 0.0, "unpriced_calls": 0 if cost is not None else 1}


def add_usage(bucket: dict[str, float], row: dict[str, float]) -> dict[str, float]:
    for k, v in row.items():
        bucket[k] = bucket.get(k, 0) + v
    return bucket


class UsageMeter:
    """Counts the calls made through a provider outside the engine (request routing, plain chat)."""

    def __init__(self, provider: Any, pricing: dict[str, dict[str, float]]):
        self.provider = provider
        self.pricing = pricing
        self.usage: dict[str, float] = {}

    def generate(self, spec: Any) -> Any:
        result = self.provider.generate(spec)
        add_usage(self.usage, usage_row(spec.model, result.usage or {}, self.pricing))
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)


class TelemetryWriter:
    def __init__(self, path: Path, enabled: bool = True):
        self.path = path
        self.enabled = enabled

    def write(self, row: TelemetryRow) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


def read_telemetry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def now() -> float:
    return time.time()
