"""개선 루프 ① 피드백 파이프라인: aggregate telemetry across runs (pure Python).

두 가지를 한다.
  - 반응적 분석: 이미 일어난 실패를 역할×게이트×사유로 군집해 패턴 카드를 만든다.
  - 선제 최적화: 최근 창과 이전 구간을 비교해 드리프트·급증·도구 오용을 치명적 실패 전에 표면화한다.
LLM은 사용하지 않는다.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..store.telemetry import read_telemetry

PASSY = {"PASS", "PASS-RANGE", "NOT_APPLICABLE", "LOCKED", "BLIND_COMPLETE"}

# 정식 명세서·선행기술 미제공에 따른 UNVERIFIED는 CLAUDE.md가 DRAFT 게이트 PASS와 공존을 허용한
# 비게이팅 상태다. 결함으로 세면 모든 AUTHORING_DRAFT run이 실패로 집계되어 신호가 묻힌다.
NON_GATING_GATES = {"OA_FINAL_GATE", "DEPENDENT_OA_FINAL_GATE", "INVENTIVE_STEP"}
NON_GATING_REASONS = {"SPEC_NOT_PROVIDED", "PRIOR_ART_NOT_PROVIDED"}


def is_gating_failure(gate: str, value: str, reason: str | None = None) -> bool:
    """게이트 값이 실제로 파이프라인을 막는 실패인지."""
    if value in PASSY:
        return False
    if value == "UNVERIFIED" and (gate in NON_GATING_GATES or (reason or "") in NON_GATING_REASONS):
        return False
    return True

DEFAULT_WINDOW = 10
DEFAULT_THRESHOLD = 0.15      # 비-PASS 비율 상승폭(퍼센트포인트/100)
RATIO_THRESHOLD = 1.5         # 지연·출력 토큰 배수
SPIKE_RUNS = 3                # 연속 N run에서 같은 (역할, 게이트, 사유)가 나오면 급증
SEVERE_HALTS = {"LOOP_LIMIT", "ENVELOPE_INVALID", "ENVELOPE_REPORT_MISMATCH"}


@dataclass
class Alert:
    kind: str                 # DRIFT_WARNING | SPIKE | TOOL_ANOMALY
    key: str
    detail: str
    before: str = "-"
    after: str = "-"
    runs: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    run_id: str
    created_at: float
    outcome: str
    halt: dict[str, Any] | None
    rows: list[dict[str, Any]] = field(default_factory=list)

    def main_rows(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r.get("phase") in (None, "main")]

    def tool_rows(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r.get("phase") == "tool"]

    def gate_keys(self) -> set[tuple[str, str, str]]:
        """(role, gate, reason) triples that were not PASS in this run."""
        out: set[tuple[str, str, str]] = set()
        for r in self.main_rows():
            for gate, val in (r.get("gates") or {}).items():
                if is_gating_failure(gate, val, r.get("reason_code")):
                    out.add((r["role"], gate, r.get("reason_code") or val))
        return out


@dataclass
class FeedbackReport:
    runs: int = 0
    calls: int = 0
    since: str | None = None
    window: int = DEFAULT_WINDOW
    threshold: float = DEFAULT_THRESHOLD
    alerts: list[Alert] = field(default_factory=list)
    per_role: dict[str, dict[str, float]] = field(default_factory=dict)
    status_by_role: dict[str, Counter] = field(default_factory=dict)
    gate_failures: Counter = field(default_factory=Counter)        # (role, gate, value, reason)
    check_failures: Counter = field(default_factory=Counter)       # check name
    return_flows: Counter = field(default_factory=Counter)         # (role, next_step)
    loop_limit_hits: int = 0
    parse_repairs: int = 0
    cache_hits: int = 0
    tool_calls: int = 0
    tool_refused: int = 0
    tool_empty: int = 0
    tool_flagged: int = 0
    non_gating_unverified: int = 0
    outcomes: Counter = field(default_factory=Counter)
    pattern_cards: list[dict[str, Any]] = field(default_factory=list)
    halts: list[dict[str, Any]] = field(default_factory=list)

    def render_md(self) -> str:
        out = ["# Claim-Agent 피드백 리포트", ""]
        out.append(f"- 집계 run 수: {self.runs} / 호출 수: {self.calls}" + (f" / since {self.since}" if self.since else ""))
        out.append(f"- 캐시 적중: {self.cache_hits} / 복구 호출: {self.parse_repairs} / 루프 한도 도달: {self.loop_limit_hits}")
        out.append(f"- 보조 소스 도구: {self.tool_calls}회 (거부 {self.tool_refused} / 무결과 {self.tool_empty} / 재검토 대상 조각 {self.tool_flagged})")
        out.append(f"- 비게이팅 UNVERIFIED(명세서·선행기술 미제공): {self.non_gating_unverified}건 — 결함이 아니므로 패턴 집계에서 제외")
        out.append("")

        out.append(f"## 선제 경고 (최근 {self.window} run 대 이전 구간, 임계 {self.threshold:.0%})")
        out.append("")
        if self.alerts:
            out.append("| 종류 | 대상 | 이전 | 최근 | 내용 | run |")
            out.append("|---|---|---|---|---|---|")
            for a in self.alerts:
                out.append(f"| {a.kind} | {a.key} | {a.before} | {a.after} | {a.detail} | {', '.join(a.runs[:4]) or '-'} |")
            out.append("")
            out.append("경고는 치명적 실패가 아니라 잠재 리스크다. 사람이 검토해 무시하거나 `eval run --variant`로 검증한다.")
        else:
            out.append("(경고 없음 — 최근 구간이 이전과 유의미하게 다르지 않다)")
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
        out.append("다음 단계: 반복 패턴은 `claim-agent lessons propose --from-feedback`으로 교훈 초안을 만들고, 사람이 승인한 뒤에만 주입된다. 개별 실패의 원인은 `claim-agent runs rca <run_id>`, 변형 프롬프트 비교는 `claim-agent eval run --variant`.")
        return "\n".join(out) + "\n"


def _norm_check(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip())[:80]


def _collect(runs_dir: Path, since: str | None) -> list[RunSummary]:
    out: list[RunSummary] = []
    if not runs_dir.exists():
        return out
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        state_path = run_dir / "state.json"
        if not state_path.exists():
            continue
        if since and run_dir.name < f"run-{since.replace('-', '')}":
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
        out.append(
            RunSummary(
                run_id=run_dir.name,
                created_at=float(state.get("created_at") or 0.0),
                outcome=state.get("outcome", "UNKNOWN"),
                halt=state.get("halt"),
                rows=read_telemetry(run_dir / "telemetry.jsonl"),
            )
        )
    out.sort(key=lambda r: (r.created_at, r.run_id))
    return out


def _rates(runs: list[RunSummary]) -> dict[str, dict[str, float]]:
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0.0, "non_pass": 0.0, "latency": 0.0, "output": 0.0, "repair": 0.0})
    for run in runs:
        for r in run.main_rows():
            a = agg[r["role"]]
            a["calls"] += 1
            a["latency"] += r.get("latency_ms", 0)
            a["output"] += r.get("output_tokens", 0) + r.get("thoughts_tokens", 0)
            a["repair"] += 1 if r.get("repair_used") else 0
            if r.get("status") not in PASSY or any(is_gating_failure(g, v, r.get("reason_code")) for g, v in (r.get("gates") or {}).items()):
                a["non_pass"] += 1
    return {
        role: {
            "calls": a["calls"],
            "non_pass_rate": a["non_pass"] / a["calls"],
            "repair_rate": a["repair"] / a["calls"],
            "latency": a["latency"] / a["calls"],
            "output": a["output"] / a["calls"],
        }
        for role, a in agg.items()
        if a["calls"]
    }


def detect_alerts(runs: list[RunSummary], window: int = DEFAULT_WINDOW, threshold: float = DEFAULT_THRESHOLD) -> list[Alert]:
    """최근 창과 이전 구간을 비교해 드리프트·급증·도구 오용을 표면화한다."""
    alerts: list[Alert] = []
    recent = runs[-window:]
    baseline = runs[: max(0, len(runs) - window)]
    recent_ids = [r.run_id for r in recent]

    # ---- 드리프트: 역할별 비-PASS 비율·복구율·지연·출력 토큰 ----
    if baseline and recent:
        base_rates, recent_rates = _rates(baseline), _rates(recent)
        for role, cur in sorted(recent_rates.items()):
            base = base_rates.get(role)
            if base is None or cur["calls"] < 2 or base["calls"] < 2:
                continue
            if cur["non_pass_rate"] - base["non_pass_rate"] >= threshold:
                alerts.append(Alert("DRIFT_WARNING", f"{role} 비-PASS", "실패율 상승 — 프롬프트·기준 드리프트 점검", f"{base['non_pass_rate']:.0%}", f"{cur['non_pass_rate']:.0%}", recent_ids))
            if cur["repair_rate"] - base["repair_rate"] >= threshold:
                alerts.append(Alert("DRIFT_WARNING", f"{role} 출력 복구", "봉투 계약 위반 증가 — max_output_tokens·thinking_level 점검", f"{base['repair_rate']:.0%}", f"{cur['repair_rate']:.0%}", recent_ids))
            for field_name, label, unit in (("latency", "지연", "ms"), ("output", "출력 토큰", "tok")):
                if base[field_name] > 0 and cur[field_name] / base[field_name] >= RATIO_THRESHOLD:
                    alerts.append(Alert("DRIFT_WARNING", f"{role} {label}", f"{cur[field_name] / base[field_name]:.1f}배 증가", f"{base[field_name]:.0f}{unit}", f"{cur[field_name]:.0f}{unit}", recent_ids))

    # ---- 급증: 같은 (역할, 게이트, 사유)가 최근 N run 연속 ----
    tail = runs[-SPIKE_RUNS:]
    if len(tail) == SPIKE_RUNS:
        common = set.intersection(*(r.gate_keys() for r in tail)) if all(r.gate_keys() for r in tail) else set()
        for role, gate, reason in sorted(common):
            alerts.append(Alert("SPIKE", f"{role} / {gate}", f"{SPIKE_RUNS} run 연속 비-PASS ({reason})", "-", f"{SPIKE_RUNS}/{SPIKE_RUNS}", [r.run_id for r in tail]))

    severe = [r for r in recent if (r.halt or {}).get("kind") in SEVERE_HALTS]
    if len(severe) >= 2:
        kinds = Counter((r.halt or {}).get("kind") for r in severe)
        alerts.append(Alert("SPIKE", "심각 중지", f"최근 창에서 {', '.join(f'{k} {v}회' for k, v in kinds.items())}", "-", f"{len(severe)}/{len(recent)}", [r.run_id for r in severe]))

    # ---- 도구 오용 ----
    tool_rows = [(r.run_id, t) for r in recent for t in r.tool_rows()]
    if tool_rows:
        refused = [rid for rid, t in tool_rows if (t.get("tool") or {}).get("refused")]
        empty = [rid for rid, t in tool_rows if (t.get("tool") or {}).get("name") == "search_style_corpus" and not (t.get("tool") or {}).get("results") and not (t.get("tool") or {}).get("refused")]
        flagged = [rid for rid, t in tool_rows if (t.get("tool") or {}).get("flagged_review")]
        searches = [t for _, t in tool_rows if (t.get("tool") or {}).get("name") == "search_style_corpus"]
        if refused:
            alerts.append(Alert("TOOL_ANOMALY", "search_style_corpus 거부", "분야·아키텍처 검색 등 금지된 질의 시도", "-", f"{len(refused)}회", sorted(set(refused))))
        if searches and len(empty) / len(searches) > 0.5:
            alerts.append(Alert("TOOL_ANOMALY", "코퍼스 무결과", "정확 일치 조각을 찾지 못함 — 확정 개념 표면 용어를 재확인", "-", f"{len(empty)}/{len(searches)}", sorted(set(empty))))
        if flagged:
            alerts.append(Alert("TOOL_ANOMALY", "재검토 대상 조각 사용", "코퍼스의 알려진 재검토 대상 청구항을 참조함", "-", f"{len(flagged)}회", sorted(set(flagged))))
    return alerts


def build_feedback(runs_dir: Path, since: str | None = None, window: int = DEFAULT_WINDOW, threshold: float = DEFAULT_THRESHOLD) -> FeedbackReport:
    runs = _collect(runs_dir, since)
    rep = FeedbackReport(since=since, window=window, threshold=threshold, runs=len(runs))
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "prompt": 0, "cached": 0, "output": 0, "latency": 0, "non_pass": 0})
    patterns: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    for run in runs:
        rep.outcomes[run.outcome] += 1
        if run.halt:
            h = run.halt
            rep.halts.append({"run_id": run.run_id, **{k: h.get(k) for k in ("kind", "stage", "role", "reason_code", "message")}})
            if h.get("kind") == "LOOP_LIMIT":
                rep.loop_limit_hits += 1
        for row in run.tool_rows():
            t = row.get("tool") or {}
            rep.tool_calls += 1
            rep.tool_refused += 1 if t.get("refused") else 0
            rep.tool_empty += 1 if t.get("name") == "search_style_corpus" and not t.get("results") and not t.get("refused") else 0
            rep.tool_flagged += 1 if t.get("flagged_review") else 0
        for row in run.main_rows():
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
                if not is_gating_failure(gate, val, row.get("reason_code")):
                    if val not in PASSY:
                        rep.non_gating_unverified += 1
                    continue
                non_pass = True
                reason = row.get("reason_code") or ""
                rep.gate_failures[(role, gate, val, reason)] += 1
                key = (row.get("invention_primary") or "UNKNOWN", role, gate, reason or val)
                card = patterns.setdefault(key, {"count": 0, "runs": [], "checks": Counter()})
                card["count"] += 1
                if run.run_id not in card["runs"]:
                    card["runs"].append(run.run_id)
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
    rep.alerts = detect_alerts(runs, window, threshold)
    return rep
