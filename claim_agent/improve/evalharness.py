"""개선 루프 ② 실험: eval cases with expected gate outcomes, baseline vs variant."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..models.request import RunRequest
from ..models.state import RunState


@dataclass
class EvalCase:
    case_id: str
    path: Path
    request: RunRequest
    expected: dict[str, Any] = field(default_factory=dict)
    fixtures_dir: Path | None = None

    @classmethod
    def load(cls, case_dir: Path) -> EvalCase:
        req = RunRequest.from_yaml(case_dir / "request.yaml")
        exp_path = case_dir / "expected.yaml"
        expected = yaml.safe_load(exp_path.read_text(encoding="utf-8")) if exp_path.exists() else {}
        fx = case_dir / "fixtures"
        return cls(case_dir.name, case_dir, req, expected or {}, fx if fx.exists() else None)


@dataclass
class EvalResult:
    case_id: str
    variant_id: str
    run_id: str
    outcome: str
    checks: list[dict[str, Any]]
    tokens: dict[str, int]
    calls: int
    loops: int

    @property
    def passed(self) -> bool:
        return all(c["ok"] for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "passed": self.passed}


def _gate_of(state: RunState, stage: str, gate: str) -> str | None:
    for r in reversed(list(state.records.values())):
        if r.stage == stage and not r.superseded and gate in r.gates:
            return r.gates[gate]
    for r in reversed(list(state.records.values())):
        if r.stage == stage and gate in r.gates:
            return r.gates[gate]
    return None


def evaluate(state: RunState, expected: dict[str, Any], final_text: str | None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if "outcome" in expected:
        checks.append({"name": "outcome", "expected": expected["outcome"], "actual": state.outcome, "ok": state.outcome == expected["outcome"]})
    for key, allowed in (expected.get("gates") or {}).items():
        stage, gate = key.split(".", 1)
        actual = _gate_of(state, stage, gate)
        allowed_list = allowed if isinstance(allowed, list) else [allowed]
        checks.append({"name": key, "expected": allowed_list, "actual": actual, "ok": actual in allowed_list})
    text = final_text or ""
    for s in expected.get("must_contain") or []:
        checks.append({"name": f"must_contain:{s}", "expected": True, "actual": s in text, "ok": s in text})
    for s in expected.get("must_not_contain") or []:
        checks.append({"name": f"must_not_contain:{s}", "expected": True, "actual": s not in text, "ok": s not in text})
    if "max_loops" in expected:
        checks.append({"name": "max_loops", "expected": expected["max_loops"], "actual": state.total_loops, "ok": state.total_loops <= expected["max_loops"]})
    return checks


def summarize(state: RunState, telemetry_rows: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
    tokens = {"prompt": 0, "cached": 0, "output": 0, "thoughts": 0}
    for r in telemetry_rows:
        tokens["prompt"] += r.get("prompt_tokens", 0)
        tokens["cached"] += r.get("cached_tokens", 0)
        tokens["output"] += r.get("output_tokens", 0)
        tokens["thoughts"] += r.get("thoughts_tokens", 0)
    return tokens, len(telemetry_rows)


def write_results(out_dir: Path, results: list[EvalResult], shadow_diffs: list[dict[str, Any]] | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    payload = {"results": [r.as_dict() for r in results], "shadow_diffs": shadow_diffs or []}
    (out_dir / f"{ts}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Eval 결과 {ts}", "", "| case | variant | run | outcome | passed | calls | loops | prompt | cached | output |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.case_id} | {r.variant_id} | {r.run_id} | {r.outcome} | {'✅' if r.passed else '❌'} | {r.calls} | {r.loops} | {r.tokens['prompt']} | {r.tokens['cached']} | {r.tokens['output']} |")
    lines.append("")
    for r in results:
        failed = [c for c in r.checks if not c["ok"]]
        if failed:
            lines.append(f"## {r.case_id} / {r.variant_id} 실패 항목")
            for c in failed:
                lines.append(f"- {c['name']}: expected {c['expected']} / actual {c['actual']}")
            lines.append("")
    if shadow_diffs:
        lines.append("## Shadow 비교 (baseline vs variant, 단계 단위)")
        lines.append("")
        for d in shadow_diffs:
            lines.append(f"- {d['run_id']} seq {d['seq']} {d['role']}: {d['diff'] or '동일'}")
    p = out_dir / f"{ts}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
