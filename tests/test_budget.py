"""Budget guard, usage accounting and the per-run performance summary."""
from __future__ import annotations

from pathlib import Path

from claim_agent.pipeline.engine import Decision
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.runtime import build_runtime

from . import scripted_roles as R

ROOT = Path(__file__).resolve().parents[1]


def _rt(tmp_path, **over):
    base = {"paths.runs_dir": str(tmp_path / "runs"), "paths.lessons_dir": str(tmp_path / "lessons"), "cache.enabled": False, "pipeline.max_concurrency": 1}
    return build_runtime(ROOT, None, None, {**base, **over})


def test_usage_totals_and_performance_summary(tmp_path, request_indep):
    rt = _rt(tmp_path, **{"telemetry.pricing": {"gemini-3.8-flash": {"input_per_m": 1.0, "cached_per_m": 0.1, "output_per_m": 4.0}}})
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    state = engine.run(engine.start(request_indep, "budget-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK"
    assert state.usage["calls"] == 9 and state.usage["unpriced_calls"] == 0 and state.usage["cost_usd"] > 0   # 8 main + style tool phase
    assert set(state.stage_usage) == {"ARCHITECT", "DRAFT", "STYLE", "SUCCESS", "SYNTAX", "OA", "BLIND", "PICTURE"}
    report = (rt.store.run_dir("budget-01") / "report.md").read_text(encoding="utf-8")
    assert "## 성능 요약" in report and "| **합계** | 9 |" in report and "$" in report


def test_budget_limit_halts_before_the_next_call_and_resumes(tmp_path, request_indep):
    rt = _rt(tmp_path, **{"pipeline.max_calls": 3})
    prov = ScriptedProvider(R.happy_script(dependent=False))
    engine = rt.engine(prov)
    state = engine.run(engine.start(request_indep, "budget-02"))
    assert state.outcome == "HALTED_BUDGET_LIMIT" and state.halt.kind == "BUDGET_LIMIT" and state.halt.stage == "STYLE"
    assert len(prov.calls) == 3 and "pipeline.max_calls" in state.halt.message
    report = (rt.store.run_dir("budget-02") / "report.md").read_text(encoding="utf-8")
    assert "BUDGET_LIMIT" in report
    # raise the limit and resume: the halted stage re-runs, nothing before it is repeated
    rt2 = _rt(tmp_path, **{"pipeline.max_calls": 20})
    engine2 = rt2.engine(prov)
    state2 = engine2.resume("budget-02", Decision())
    assert state2.outcome == "DRAFT_CLAIM_LOCK" and state2.usage["calls"] == 10   # the interrupted style stage repeats its tool phase
    assert [c.role for c in prov.calls[:4]] == ["claim-architect", "claim-drafter", "claim-style-adjuster", "claim-style-adjuster"]


def test_unpriced_model_reports_na(tmp_path, request_indep):
    rt = _rt(tmp_path)
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    state = engine.run(engine.start(request_indep, "budget-03"))
    assert state.usage["unpriced_calls"] == state.usage["calls"]
    report = (rt.store.run_dir("budget-03") / "report.md").read_text(encoding="utf-8")
    assert "telemetry.pricing" in report
