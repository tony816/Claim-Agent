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
    phase: str = "main"


def estimate_cost(model: str, usage: dict[str, int], pricing: dict[str, dict[str, float]]) -> float | None:
    p = pricing.get(model)
    if not p:
        return None
    m = 1_000_000
    cached = usage.get("cached_tokens", 0)
    prompt = max(0, usage.get("prompt_tokens", 0) - cached)
    out = usage.get("output_tokens", 0) + usage.get("thoughts_tokens", 0)
    return round(prompt / m * p.get("input_per_m", 0) + cached / m * p.get("cached_per_m", 0) + out / m * p.get("output_per_m", 0), 6)


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
