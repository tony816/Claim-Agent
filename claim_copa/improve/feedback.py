"""개선 루프 ① 피드백 파이프라인: aggregate telemetry across runs (pure Python)."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..store.telemetry import read_telemetry

PASSY = {"PASS", "PASS-RANGE", "NOT_APPLICABLE", "LOCKED", "BLIND_COMPLETE"}


@dataclass
class FeedbackReport:
    runs: int = 0
    calls: int = 0
    since: str | None = None
    per_role: dict[str, dict[str, float]] = field(default_factory=dict)
    status_by_role: dict[str, Counter] = field(default_factory=dict)
    gate_failures: Counter = field(default_factory=Counter)        # (role, gate, value, reason)
    check_failures: Counter = field(default_factory=Counter)       # check name
    return_flows: Counter = field(default_factory=Counter)         # (role, next_step)
    loop_limit_hits: int = 0
    parse_repairs: int = 0
    cache_hits: int = 0
    outcomes: Counter = field(default_factory=Counter)
    pattern_cards: list[dict[str, Any]] = field(default_factory=list)
    halts: list[dict[str, Any]] = field(default_factory=list)

    def render_md(self) -> str:
        out = ["# Claim Copa 피드백 리포트", ""]
        out.append(f"- 집계 run 수: {self.runs} / 호출 수: {self.calls}" + (f" / since {self.since}" if self.since else ""))
        out.append(f"- 캐시 적중: {self.cache_hits} / 복구 호출: {self.parse_repairs} / 루프 한도 도달: {self.loop_limit_hits}")
        out.append("")
        out.append("## run 결과 분포")
        out.append("")
        for k, v in self.outcomes.most_common():
            out.append(f"- {k}: {v}")
        out.append("")
        out.append("## 역할별 비용·지연")
        out.append("")
        out.append("| 역할 | 호출 | 평균 prompt | 평균 cached | 평균 output+thoughts | 평균 지연(ms) | 비-PASS 비율 |")
        out.append("|---|---|---|---|---|---|---|")
        for role, m in sorted(self.per_role.items()):
            out.append(f"| {role} | {int(m['calls'])} | {m['prompt']:.0f} | {m['cached']:.0f} | {m['output']:.0f} | {m['latency']:.0f} | {m['non_pass_rate']:.0%} |")
        out.append("")
        out.append("## 비-PASS 게이트 × 사유")
        out.append("")
        out.append("| 역할 | 게이트 | 값 | 사유 | 횟수 |")
        out.append("|---|---|---|---|---|")
        for (role, gate, val, reason), n in self.gate_failures.most_common(30):
            out.append(f"| {role} | {gate} | {val} | {reason or '-'} | {n} |")
        out.append("")
        out.append("## 세부 시험 실패 상위 20")
        out.append("")
        for name, n in self.check_failures.most_common(20):
            out.append(f"- {name}: {n}")
        out.append("")
        out.append("## RETURN_TO_* 흐름")
        out.append("")
        for (role, step), n in self.return_flows.most_common():
            out.append(f"- {role} → {step}: {n}")
        out.append("")
        out.append("## 패턴 카드 (2회 이상 반복)")
        out.append("")
        for card in self.pattern_cards:
            out.append(f"- **{card['invention_type']} / {card['role']} / {card['gate']} / {card['reason']}** × {card['count']} — 예: {', '.join(card['runs'][:5])}")
            if card.get("checks"):
                out.append(f"  - 자주 실패한 시험: {', '.join(card['checks'][:5])}")
        if not self.pattern_cards:
            out.append("(없음)")
        out.append("")
        out.append("## 중지 이력")
        out.append("")
        for h in self.halts[:30]:
            out.append(f"- {h['run_id']}: {h['kind']} @ {h['stage']} ({h['role']}) {h.get('reason_code') or ''} — {str(h.get('message',''))[:120]}")
        out.append("")
        out.append("다음 단계: 반복 패턴은 `claim-copa lessons propose --from-run <run_id>`로 교훈 초안을 만들고, 사람이 승인한 뒤에만 주입된다. 변형 프롬프트는 `claim-copa eval run --variant`로 비교한다.")
        return "\n".join(out) + "\n"


def _norm_check(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip())[:80]


def build_feedback(runs_dir: Path, since: str | None = None) -> FeedbackReport:
    rep = FeedbackReport(since=since)
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "prompt": 0, "cached": 0, "output": 0, "latency": 0, "non_pass": 0})
    patterns: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        state_path = run_dir / "state.json"
        if not state_path.exists():
            continue
        if since and run_dir.name < f"run-{since.replace('-', '')}":
            continue
        rep.runs += 1
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
        rep.outcomes[state.get("outcome", "UNKNOWN")] += 1
        if state.get("halt"):
            h = state["halt"]
            rep.halts.append({"run_id": run_dir.name, **{k: h.get(k) for k in ("kind", "stage", "role", "reason_code", "message")}})
            if h.get("kind") == "LOOP_LIMIT":
                rep.loop_limit_hits += 1
        for row in read_telemetry(run_dir / "telemetry.jsonl"):
            if row.get("phase") not in (None, "main"):
                continue
            rep.calls += 1
            role = row["role"]
            a = agg[role]
            a["calls"] += 1
            a["prompt"] += row.get("prompt_tokens", 0)
            a["cached"] += row.get("cached_tokens", 0)
            a["output"] += row.get("output_tokens", 0) + row.get("thoughts_tokens", 0)
            a["latency"] += row.get("latency_ms", 0)
            rep.status_by_role.setdefault(role, Counter())[row.get("status", "")] += 1
            if row.get("cache_hit"):
                rep.cache_hits += 1
            if row.get("repair_used"):
                rep.parse_repairs += 1
            non_pass = row.get("status") not in PASSY
            for gate, val in (row.get("gates") or {}).items():
                if val not in PASSY:
                    non_pass = True
                    reason = row.get("reason_code") or ""
                    rep.gate_failures[(role, gate, val, reason)] += 1
                    key = (row.get("invention_primary") or "UNKNOWN", role, gate, reason or val)
                    card = patterns.setdefault(key, {"count": 0, "runs": [], "checks": Counter()})
                    card["count"] += 1
                    if run_dir.name not in card["runs"]:
                        card["runs"].append(run_dir.name)
                    for c in row.get("non_pass_checks") or []:
                        card["checks"][_norm_check(c)] += 1
            if non_pass:
                a["non_pass"] += 1
            for c in row.get("non_pass_checks") or []:
                rep.check_failures[_norm_check(c)] += 1
            if str(row.get("next_step", "")).startswith("RETURN_TO_"):
                rep.return_flows[(role, row["next_step"])] += 1
    for role, a in agg.items():
        n = a["calls"] or 1
        rep.per_role[role] = {"calls": a["calls"], "prompt": a["prompt"] / n, "cached": a["cached"] / n, "output": a["output"] / n, "latency": a["latency"] / n, "non_pass_rate": a["non_pass"] / n}
    for (itype, role, gate, reason), card in patterns.items():
        if card["count"] >= 2:
            rep.pattern_cards.append({"invention_type": itype, "role": role, "gate": gate, "reason": reason, "count": card["count"], "runs": card["runs"], "checks": [c for c, _ in card["checks"].most_common(5)]})
    rep.pattern_cards.sort(key=lambda c: -c["count"])
    return rep
