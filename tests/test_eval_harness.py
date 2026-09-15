"""Golden-set checks, case scaffold and pre-run estimates (no API)."""
from __future__ import annotations

from claim_agent.improve.estimate import estimate_run, expected_calls
from claim_agent.improve.evalharness import evaluate, scaffold_case, summarize
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.store.telemetry import read_telemetry

from . import scripted_roles as R


def _run(rt, request, run_id="eval-h-01"):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=request.dependent)))
    state = engine.run(engine.start(request, run_id))
    tokens, calls = summarize(state, read_telemetry(rt.store.telemetry_path(run_id)))
    text = (state.candidate.current.exact_text or "") + "\n" + ((state.dependent.current.exact_text or "") if state.dependent and state.dependent.current else "")
    return state, text, tokens, calls


def test_golden_checks_pass_on_scripted_run(rt, request_dep):
    request_dep.user_lock = "위쪽으로 개방된 복수의 슬롯"
    state, text, tokens, calls = _run(rt, request_dep)
    expected = {
        "outcome": "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK",
        "expected_invention_type": "PHYSICAL",
        "reconstruction": ["PASS", "PASS-RANGE"],
        "skeleton_terms": ["베이스", "조임 나사", "홀더 본체", "슬롯"],
        "forbidden_terms": ["팔부"],
        "user_lock_preserved": True,
        "max_calls": 30,
        "max_output_tokens_total": 100_000,
        "max_loops": 0,
    }
    checks = evaluate(state, expected, text, tokens, calls)
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]
    names = {c["name"] for c in checks}
    assert {"expected_invention_type", "reconstruction", "DEPENDENT_RECONSTRUCTION_GATE", "user_lock_preserved", "max_calls"} <= names


def test_golden_checks_fail_closed(rt, request_indep):
    state, text, tokens, calls = _run(rt, request_indep, "eval-h-02")
    checks = evaluate(state, {"expected_invention_type": "PROCESS", "skeleton_terms": ["없는용어"], "user_lock_preserved": True, "max_calls": 1}, text, tokens, calls)
    assert [c["name"] for c in checks if not c["ok"]] == ["skeleton_term:없는용어", "user_lock_preserved", "expected_invention_type", "max_calls"]


def test_expected_calls_and_estimate(request_dep, request_indep):
    assert sum(expected_calls(request_indep).values()) == 9          # 8 main + style tool phase
    assert sum(expected_calls(request_dep).values()) == 9 + 7 + 2 * 3  # targets 2~4
    est = estimate_run(request_dep, "m", {"m": {"input_per_m": 1.0, "cached_per_m": 0.1, "output_per_m": 4.0}})
    assert est.calls == 22 and est.cost and est.cost > 0 and est.basis == "default profile"
    rows = [{"phase": "main", "provider": "gemini", "role": "claim-architect", "prompt_tokens": 100, "cached_tokens": 0, "thoughts_tokens": 10, "output_tokens": 20}]
    est2 = estimate_run(request_indep, "m", {}, rows)
    assert est2.cost is None and est2.basis == "telemetry(1 rows)" and "N/A" in est2.render()


def test_scaffold_case(tmp_path):
    d = scaffold_case(tmp_path, "new-case")
    assert (d / "request.yaml").exists() and (d / "expected.yaml").exists() and (d / "sources" / "invention.md").exists()
    assert "candidate_id: new-case" in (d / "request.yaml").read_text(encoding="utf-8")
