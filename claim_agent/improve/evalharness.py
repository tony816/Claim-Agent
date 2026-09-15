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

INDEPENDENT_STAGES = {"ARCHITECT", "DRAFT", "STYLE", "SUCCESS", "SYNTAX", "OA", "BLIND", "PICTURE", "LOCK"}


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


def _latest(state: RunState, kind: str):
    for r in reversed(list(state.records.values())):
        if r.kind == kind and not r.superseded:
            return r
    return None


def evaluate(state: RunState, expected: dict[str, Any], final_text: str | None, tokens: dict[str, int] | None = None, calls: int | None = None) -> list[dict[str, Any]]:
    """Golden-set checks. Fields (all optional) in expected.yaml:

    outcome, gates{STAGE.GATE: [allowed]}, must_contain, must_not_contain (alias forbidden_terms),
    skeleton_terms (every 주골격 term must appear in the exact text), user_lock_preserved,
    expected_invention_type (architect's invention_type.primary), reconstruction (allowed picture verdicts),
    max_loops, max_calls, max_output_tokens_total.
    """
    from ..pipeline.claimtext import contains_user_lock

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
    for s in list(expected.get("must_not_contain") or []) + list(expected.get("forbidden_terms") or []):
        checks.append({"name": f"must_not_contain:{s}", "expected": True, "actual": s not in text, "ok": s not in text})
    for s in expected.get("skeleton_terms") or []:
        checks.append({"name": f"skeleton_term:{s}", "expected": True, "actual": s in text, "ok": s in text})
    if expected.get("user_lock_preserved"):
        lock = state.request.get("user_lock")
        ok = bool(lock) and contains_user_lock(text, lock)
        checks.append({"name": "user_lock_preserved", "expected": True, "actual": ok, "ok": ok})
    if "expected_invention_type" in expected:
        design = _latest(state, "design")
        actual = design.invention_primary if design else None
        checks.append({"name": "expected_invention_type", "expected": expected["expected_invention_type"], "actual": actual, "ok": actual == expected["expected_invention_type"]})
    if "reconstruction" in expected:
        allowed_list = expected["reconstruction"] if isinstance(expected["reconstruction"], list) else [expected["reconstruction"]]
        pic = _latest(state, "reference_compare")
        actual = pic.status if pic else None
        checks.append({"name": "reconstruction", "expected": allowed_list, "actual": actual, "ok": actual in allowed_list})
        if state.request.get("dependent"):
            gate = state.dependent.current.reconstruction_gate if state.dependent and state.dependent.current else None
            checks.append({"name": "DEPENDENT_RECONSTRUCTION_GATE", "expected": ["PASS"], "actual": gate, "ok": gate == "PASS"})
    if "design_revision" in expected:
        actual = state.candidate.design_revision
        checks.append({"name": "design_revision", "expected": expected["design_revision"], "actual": actual, "ok": actual == expected["design_revision"]})
    if "independent_stages_run" in expected:
        ran = any(r.stage in INDEPENDENT_STAGES for r in state.records.values())
        checks.append({"name": "independent_stages_run", "expected": expected["independent_stages_run"], "actual": ran, "ok": ran == expected["independent_stages_run"]})
    if "dependent_claim_nos" in expected:
        actual = sorted(c["claim_no"] for c in state.dependent.current.claims) if state.dependent and state.dependent.current else []
        checks.append({"name": "dependent_claim_nos", "expected": expected["dependent_claim_nos"], "actual": actual, "ok": actual == sorted(expected["dependent_claim_nos"])})
    if "halt_contains" in expected:
        message = f"{state.halt.reason_code or ''} {state.halt.message}" if state.halt else ""
        checks.append({"name": "halt_contains", "expected": expected["halt_contains"], "actual": message[:200], "ok": expected["halt_contains"] in message})
    if expected.get("baseline_unchanged"):
        base = state.baseline_set
        ok = bool(base) and all(c.text in text for c in base.claims if c.claim_no not in base.edit_targets)
        checks.append({"name": "baseline_unchanged", "expected": True, "actual": ok, "ok": ok})
    if "max_loops" in expected:
        checks.append({"name": "max_loops", "expected": expected["max_loops"], "actual": state.total_loops, "ok": state.total_loops <= expected["max_loops"]})
    if "max_calls" in expected and calls is not None:
        checks.append({"name": "max_calls", "expected": expected["max_calls"], "actual": calls, "ok": calls <= expected["max_calls"]})
    if "max_output_tokens_total" in expected and tokens is not None:
        out = tokens.get("output", 0) + tokens.get("thoughts", 0)
        checks.append({"name": "max_output_tokens_total", "expected": expected["max_output_tokens_total"], "actual": out, "ok": out <= expected["max_output_tokens_total"]})
    return checks


CASE_TEMPLATE_REQUEST = """# 골든 케이스 요청. 경로는 이 파일 기준 상대 경로.
request_mode: AUTHORING_DRAFT        # AUTHORING_DRAFT | FINALIZATION | REVIEW_ONLY
candidate_id: {case_id}
request_text: |
  <발명 명칭>의 독립항 1항 잠정안을 작성한다. 정식 명세서는 아직 없다. 선행기술은 제공하지 않는다.
invention_sources:
  - sources/invention.md              # 발명 설명 (텍스트). 도면은 drawings에.
drawings: []                          # - sources/도1.png
prior_art: []                         # 선행기술 파일 경로. 비우면 PRIOR_ART_SET: NONE
# user_lock_file: sources/user_lock.txt   # 확정 문언이 있으면 지정
dependent: false
dependent_target: null                # 예: "2~4"
dependent_set_id: {case_id}-dep
"""

CASE_TEMPLATE_EXPECTED = """# 골든 케이스 기대 결과. 모든 항목은 선택. 값이 없으면 검사하지 않는다.
outcome: DRAFT_CLAIM_LOCK             # 종속항 포함이면 DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK
gates:
  ARCHITECT.DESIGN_GATE: [LOCKED]
  STYLE.CLAIM_STYLE_GATE: [PASS]
  STYLE.TERM_EXPRESSION_GATE: [PASS]
  OA.OA_DRAFT_GATE: [PASS]
  OA.OA_FINAL_GATE: [UNVERIFIED]      # 정식 명세서 없음 → 비게이팅
expected_invention_type: PHYSICAL     # PHYSICAL | PROCESS | DATA_CONTROL | COMPOSITION
reconstruction: [PASS, PASS-RANGE]
skeleton_terms: []                    # 주골격의 SOURCE_EXACT_TERM (exact 문언에 모두 있어야 함)
forbidden_terms: []                   # CONCEPT_LABEL_ONLY 등 즉석 조어
must_contain: ["【청구항 1】"]
user_lock_preserved: false            # user_lock_file을 쓰면 true
max_loops: 2
max_calls: 12                         # 독립항만: 9 (스타일 도구 단계 포함), 종속항 n개: +7+2n
"""

CASE_TEMPLATE_SOURCE = """# 발명 설명 (원자료)

여기에 각 부품의 명칭·형상, 부품 간 결합·지지·운동 관계, 해결하는 문제와 효과를 아는 만큼 적는다.
없는 내용은 비워 둔다. 프로그램은 없는 내용을 만들어 채우지 않고 `근거 미확인`으로 표시한다.
"""


def scaffold_case(cases_dir: Path, case_id: str) -> Path:
    d = cases_dir / case_id
    if d.exists():
        raise FileExistsError(f"case already exists: {d}")
    (d / "sources").mkdir(parents=True)
    (d / "request.yaml").write_text(CASE_TEMPLATE_REQUEST.format(case_id=case_id), encoding="utf-8")
    (d / "expected.yaml").write_text(CASE_TEMPLATE_EXPECTED, encoding="utf-8")
    (d / "sources" / "invention.md").write_text(CASE_TEMPLATE_SOURCE, encoding="utf-8")
    return d


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
