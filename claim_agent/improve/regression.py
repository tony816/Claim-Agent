"""ACE ⑤ Regression Gate: 사람이 승인한 교훈이라도 평가를 통과해야 주입된다.

    human approve → candidate-approved → 관련 eval + 핵심 regression eval → baseline 대비 비교
    → 회귀 없음 + 목표 failure 개선 → active-approved

교훈을 주입하는 경로를 새로 만들지 않는다. 기존 `Variant.prompt_suffix`가 "이 문구를 이 역할의
system instruction에 덧붙인 채로 돌려보라"를 이미 지원하므로, 승인 대기 교훈을 `lessons/approved/`에
넣지 않고도 그 효과를 측정할 수 있다.

비용 주의: fixtures 리플레이는 프롬프트를 바꿔도 녹화된 응답을 그대로 돌려주므로 **교훈의 효과를
측정하지 못한다.** 리플레이 결과는 엔진 회귀 검사로만 쓰고, 목표 failure 개선 항목은 UNVERIFIED로
남긴다. 실제 개선 판정에는 `mode="live"`가 필요하다.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..store.telemetry import read_telemetry
from .evalharness import EvalCase, EvalResult, evaluate, summarize
from .experiments import Variant
from .lessons import Lesson, LessonStore

# 교훈이 target_roles를 비워 두면 모든 역할에 붙는다. 그때 쓰는 역할 목록.
ALL_ROLES_SENTINEL = "*"

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_UNVERIFIED = "UNVERIFIED"


@dataclass
class Criterion:
    name: str
    baseline: Any
    candidate: Any
    ok: bool | None            # None = 판정 불가(UNVERIFIED)
    note: str = ""

    @property
    def verdict(self) -> str:
        return VERDICT_UNVERIFIED if self.ok is None else (VERDICT_PASS if self.ok else VERDICT_FAIL)


@dataclass
class RegressionResult:
    lesson_id: str
    mode: str                                    # replay | live
    verdict: str = VERDICT_UNVERIFIED
    criteria: list[dict[str, Any]] = field(default_factory=list)
    target_cases: list[str] = field(default_factory=list)
    core_cases: list[str] = field(default_factory=list)
    skipped_cases: list[str] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    created_at: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render_md(self) -> str:
        out = [f"# Regression Gate — {self.lesson_id}", ""]
        out.append(f"- 판정: **{self.verdict}** (모드: {self.mode})")
        out.append(f"- 목표 케이스: {', '.join(self.target_cases) or '없음'} / 핵심 회귀 케이스: {', '.join(self.core_cases) or '없음'}")
        if self.skipped_cases:
            out.append(f"- 건너뛴 케이스(fixtures 없음): {', '.join(self.skipped_cases)}")
        out.append("")
        out.append("| 기준 | baseline | candidate | 판정 | 비고 |")
        out.append("|---|---|---|---|---|")
        for c in self.criteria:
            mark = {"PASS": "✅", "FAIL": "❌", "UNVERIFIED": "—"}[c["verdict"]]
            out.append(f"| {c['name']} | {c['baseline']} | {c['candidate']} | {mark} {c['verdict']} | {c.get('note') or ''} |")
        out.append("")
        if self.note:
            out.append(f"> {self.note}")
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- 실행


def candidate_variant(lesson: Lesson, roles: list[str]) -> Variant:
    """승인 대기 교훈을 `lessons/approved/`에 넣지 않고 프롬프트에만 얹는 variant."""
    targets = lesson.target_roles or roles
    suffix = f"- ({lesson.id} 후보) {lesson.text_ko.strip()}"
    return Variant(name=f"cand-{lesson.id}", description=f"regression gate for {lesson.id}",
                   prompt_suffix={r: suffix for r in targets})


def run_cases(project_root: Path, config_path: Path | None, variant: Variant, cases: list[EvalCase],
              mode: str = "replay", run_prefix: str = "reg") -> tuple[list[EvalResult], list[str]]:
    """케이스를 한 variant로 실행한다. fixtures가 없는 케이스는 replay 모드에서 건너뛴다."""
    from ..runtime import build_runtime, make_provider

    results: list[EvalResult] = []
    skipped: list[str] = []
    for case in cases:
        if mode == "replay" and not case.fixtures_dir:
            skipped.append(case.case_id)
            continue
        vrt = build_runtime(project_root, config_path, variant)
        provider = make_provider(vrt, "replay" if mode == "replay" else "gemini", case.fixtures_dir if mode == "replay" else None)
        engine = vrt.engine(provider)
        run_id = f"{run_prefix}-{case.case_id}-{variant.name}-{time.strftime('%Y%m%d%H%M%S')}"
        state = engine.run(engine.start(case.request, run_id))
        cur = state.candidate.current
        text = (cur.exact_text if cur else None) or ""
        if state.dependent and state.dependent.current and state.dependent.current.exact_text:
            text += "\n" + state.dependent.current.exact_text
        tokens, calls = summarize(state, read_telemetry(vrt.store.telemetry_path(run_id)))
        checks = evaluate(state, case.expected, text, tokens, calls)
        checks += adversarial_checks(state, case.expected)
        results.append(EvalResult(case.case_id, variant.variant_id, run_id, state.outcome, checks, tokens, calls, state.total_loops))
    return results, skipped


def adversarial_checks(state: Any, expected: dict[str, Any]) -> list[dict[str, Any]]:
    """적대 케이스 전용 검사. 일반 케이스(`adversarial` 없음)에서는 아무것도 하지 않는다.

    적대 케이스는 통과가 아니라 **걸리는 것**이 정답이다.
    """
    if not expected.get("adversarial"):
        return []
    out: list[dict[str, Any]] = []
    rows = [r for r in state.records.values()]
    det = expected.get("expected_first_detector") or {}

    for gate in expected.get("must_not_pass_gates") or []:
        values = [r.gates[gate] for r in rows if gate in r.gates]
        ok = bool(values) and any(v not in ("PASS", "PASS-RANGE", "LOCKED", "NOT_APPLICABLE") for v in values)
        out.append({"name": f"must_not_pass:{gate}", "expected": "non-PASS", "actual": values or None, "ok": ok})

    if det.get("role") or det.get("stage"):
        failed = [r for r in rows if r.status not in ("PASS", "PASS-RANGE", "LOCKED", "NOT_APPLICABLE", "BLIND_COMPLETE")
                  or any(v not in ("PASS", "PASS-RANGE", "LOCKED", "NOT_APPLICABLE") for v in r.gates.values())]
        first = failed[0] if failed else None
        actual = f"{first.stage}/{first.role}" if first else None
        want = f"{det.get('stage')}/{det.get('role')}"
        out.append({"name": "first_detector", "expected": want, "actual": actual, "ok": actual == want})

    if "escaped_to_lock_must_be" in expected:
        escaped = state.outcome.endswith("LOCK")
        want = bool(expected["escaped_to_lock_must_be"])
        out.append({"name": "escaped_to_lock", "expected": want, "actual": escaped, "ok": escaped == want})

    if expected.get("expected_return_to"):
        steps = {r.gates.get(g) for r in rows for g in r.gates} | {state.halt.return_to if state.halt else None}
        ok = expected["expected_return_to"] in {s for s in steps if s}
        out.append({"name": "expected_return_to", "expected": expected["expected_return_to"], "actual": sorted(s for s in steps if s), "ok": ok})
    return out


def _pass_map(results: list[EvalResult]) -> dict[str, bool]:
    return {r.case_id: r.passed for r in results}


def _sum(results: list[EvalResult], key: str) -> int:
    if key == "calls":
        return sum(r.calls for r in results)
    if key == "loops":
        return sum(r.loops for r in results)
    return sum(r.tokens.get("output", 0) + r.tokens.get("thoughts", 0) for r in results)


def _bad_verdicts(results: list[EvalResult]) -> int:
    """중지로 끝난 케이스 수. 잘못된 BLOCK/REVIEW 증가를 근사한다(적대 케이스는 제외)."""
    return sum(1 for r in results if r.outcome.startswith("HALTED") and not r.case_id.startswith("adv-"))


def evaluate_gate(lesson: Lesson, baseline: list[EvalResult], candidate: list[EvalResult],
                  target_cases: list[str], mode: str) -> RegressionResult:
    """평가 기준 5가지를 baseline 대비로 판정한다."""
    res = RegressionResult(lesson_id=lesson.id, mode=mode, created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                           target_cases=target_cases,
                           core_cases=[r.case_id for r in baseline if r.case_id not in target_cases],
                           runs=[r.run_id for r in candidate])
    base_map, cand_map = _pass_map(baseline), _pass_map(candidate)
    criteria: list[Criterion] = []

    # ① 목표 failure detection 개선 -------------------------------------------------
    base_target = [base_map.get(c) for c in target_cases if c in base_map]
    cand_target = [cand_map.get(c) for c in target_cases if c in cand_map]
    if not target_cases or not cand_target:
        criteria.append(Criterion("목표 failure 탐지 개선", "-", "-", None, "목표 적대 케이스가 없다 — 승인된 eval case를 먼저 만든다"))
    elif mode == "replay":
        criteria.append(Criterion("목표 failure 탐지 개선", sum(1 for x in base_target if x), sum(1 for x in cand_target if x), None,
                                  "리플레이는 프롬프트 변화를 반영하지 않는다 — live 실행이 필요하다"))
    else:
        improved = sum(1 for x in cand_target if x) >= sum(1 for x in base_target if x) and all(cand_target)
        criteria.append(Criterion("목표 failure 탐지 개선", f"{sum(1 for x in base_target if x)}/{len(base_target)}",
                                  f"{sum(1 for x in cand_target if x)}/{len(cand_target)}", improved))

    # ② 기존 PASS case 회귀 없음 ----------------------------------------------------
    regressed = sorted(c for c, ok in base_map.items() if ok and c not in target_cases and cand_map.get(c) is False)
    criteria.append(Criterion("기존 PASS 케이스 회귀", f"{sum(1 for c, ok in base_map.items() if ok)}건 PASS",
                              f"회귀 {len(regressed)}건", not regressed, ", ".join(regressed)))

    # ③ 잘못된 BLOCK/REVIEW 증가 ----------------------------------------------------
    b, c = _bad_verdicts(baseline), _bad_verdicts(candidate)
    criteria.append(Criterion("비-적대 케이스의 중지 증가", b, c, c <= b))

    # ④ 호출 수·토큰 변화 -----------------------------------------------------------
    for key, label, tol in (("calls", "호출 수", 1.20), ("output", "출력 토큰", 1.30)):
        bv, cv = _sum(baseline, key), _sum(candidate, key)
        ok = True if bv == 0 else cv <= bv * tol
        criteria.append(Criterion(f"{label} 변화", bv, cv, ok, f"허용 {int((tol - 1) * 100)}% 이내"))

    # ⑤ RETURN loop 증가 ------------------------------------------------------------
    bl, cl = _sum(baseline, "loops"), _sum(candidate, "loops")
    criteria.append(Criterion("RETURN 루프 합계", bl, cl, cl <= bl))

    res.criteria = [{"name": x.name, "baseline": x.baseline, "candidate": x.candidate, "verdict": x.verdict, "note": x.note} for x in criteria]
    if any(x.ok is False for x in criteria):
        res.verdict = VERDICT_FAIL
    elif any(x.ok is None for x in criteria):
        res.verdict = VERDICT_UNVERIFIED
    else:
        res.verdict = VERDICT_PASS
    if res.verdict == VERDICT_UNVERIFIED:
        res.note = "판정 불가 항목이 있어 활성화하지 않는다. 목표 적대 케이스를 승인하고 live 모드로 다시 실행한다."
    elif res.verdict == VERDICT_FAIL:
        res.note = "회귀 또는 비용 증가가 확인되었다. 교훈은 approved_but_failed_eval로 남고 주입되지 않는다."
    return res


def run_gate(project_root: Path, config_path: Path | None, lessons: LessonStore, lesson_id: str,
             eval_dir: Path, roles: list[str], mode: str = "replay",
             core_cases: list[str] | None = None) -> tuple[RegressionResult, Lesson]:
    """관련 적대 케이스 + 핵심 regression set을 baseline/candidate 두 번 돌려 게이트를 판정한다."""
    lesson, _ = lessons.get(lesson_id)
    from .lessons import GATE_CANDIDATE, GATE_FAILED_EVAL

    # 한 번 떨어진 교훈은 문구를 고치지 않고도 다시 돌릴 수 있다(케이스를 승인했거나 live로 바꾼 경우).
    if lesson.gate_status not in (GATE_CANDIDATE, GATE_FAILED_EVAL):
        raise ValueError(f"{lesson_id}: 사람이 승인한 candidate만 regression eval을 돌린다 (현재 {lesson.gate_status})")

    all_cases = [EvalCase.load(p.parent) for p in sorted((eval_dir / "cases").glob("*/request.yaml"))]
    targets = [c for c in all_cases if c.expected.get("failure_mode_id") == lesson.failure_mode_id
               or c.case_id.startswith("adv-") and lesson.failure_mode_id and lesson.failure_mode_id.split("|")[1] in c.case_id]
    core = [c for c in all_cases if c not in targets and (core_cases is None or c.case_id in core_cases)]
    cases = targets + core
    if not cases:
        result = RegressionResult(lesson_id=lesson_id, mode=mode, verdict=VERDICT_UNVERIFIED,
                                  created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                                  note="실행할 eval 케이스가 없다. 골든 케이스를 먼저 만든다.")
        return result, lessons.fail_eval(lesson_id, result.as_dict())

    base_results, skipped = run_cases(project_root, config_path, Variant.default(), cases, mode, "reg-base")
    cand_results, _ = run_cases(project_root, config_path, candidate_variant(lesson, roles), cases, mode, "reg-cand")
    result = evaluate_gate(lesson, base_results, cand_results, [c.case_id for c in targets], mode)
    result.skipped_cases = skipped
    record = result.as_dict()
    lesson = lessons.activate(lesson_id, record) if result.verdict == VERDICT_PASS else lessons.fail_eval(lesson_id, record)
    return result, lesson


def write_result(out_dir: Path, result: RegressionResult) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"regression-{result.lesson_id}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(result.render_md(), encoding="utf-8")
    return path
