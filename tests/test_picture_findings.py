"""The dependent reconstruction gate keeps every target's picture findings in its halt and in the return it sends."""
from __future__ import annotations

from pathlib import Path

from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.runtime import build_runtime

from . import scripted_roles as R

ROOT = Path(__file__).resolve().parents[1]


def _rt(tmp_path):
    over = {"paths.runs_dir": str(tmp_path / "runs"), "paths.lessons_dir": str(tmp_path / "lessons"), "cache.enabled": False, "pipeline.max_concurrency": 1}
    return build_runtime(ROOT, None, None, over)


def _review(text: str):
    def f(spec):
        return R._base(spec, "REVIEW", {"NON_PATENT_TECHNICAL_READER_GATE": "REVIEW", "GEOMETRIC_OBJECT_GATE": "PASS"}, ["- 최종 판정: REVIEW"],
                       next_step="USER_DECISION", stop_basis="SOURCE_FACT_ABSENT", open_issues=[{"kind": "USER_DECISION", "code": "SKETCH_PARTIAL", "text": text, "return_to": None}])
    return f


def _back_to_drafter(text: str):
    def f(spec):
        return R._base(spec, "BLOCK", {"NON_PATENT_TECHNICAL_READER_GATE": "BLOCK", "GEOMETRIC_OBJECT_GATE": "PASS"}, ["- 최종 판정: BLOCK"],
                       next_step="RETURN_TO_DRAFTER", open_issues=[{"kind": "BLOCK", "code": "CLAUSE", "text": text, "return_to": "RETURN_TO_DRAFTER"}])
    return f


def test_a_picture_review_halt_keeps_the_findings_the_user_needs(tmp_path, request_dep):
    """Regression for run replay-e69bc8-155158: the halt said 'REVIEW OTHER' with no issues, so the answer claimed none were given."""
    rt = _rt(tmp_path)
    script = R.happy_script(dependent=True)
    script["picture-claim-reconstruction-reviewer"] = [R.picture(), R.picture(), _review("삽입 입구 방향 미정"), R.picture()]
    prov = ScriptedProvider(script)
    state = rt.engine(prov).run(rt.engine(prov).start(request_dep, "pic-halt-01"))
    assert state.outcome == "HALTED_REVIEW" and state.halt.stage == "DEP_RECON", state.halt
    assert [o["text"] for o in state.halt.open_issues] == ["제3항: 삽입 입구 방향 미정"]
    assert state.halt.record_id and "제3항 비교 보고서" in (state.halt.report_markdown or "")


def test_a_return_carries_every_failed_target_into_one_revision(tmp_path, request_dep):
    rt = _rt(tmp_path)
    script = R.happy_script(dependent=True)
    script["picture-claim-reconstruction-reviewer"] = [R.picture(), _back_to_drafter("제2항 결속 결함"), _back_to_drafter("제3항 결속 결함"), R.picture(),
                                                       R.picture(), R.picture(), R.picture()]
    for role, extra in (("claim-drafter", R.drafter("DEPENDENT_SET")), ("claim-style-adjuster", R.style("DEPENDENT_SET")), ("claim-success-reviewer", R.success()),
                        ("syntax-scope-reviewer", R.syntax()), ("oa-strategy-reviewer", R.oa("DEPENDENT_SET"))):
        script[role].append(extra)
    script["blind-claim-reconstruction-reviewer"] += [R.blind()] * 3
    prov = ScriptedProvider(script)
    state = rt.engine(prov).run(rt.engine(prov).start(request_dep, "pic-return-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state.halt
    redraft = [c for c in prov.calls if c.role == "claim-drafter"][-1]
    assert "제2항: 제2항 결속 결함" in redraft.packet_text and "제3항: 제3항 결속 결함" in redraft.packet_text
