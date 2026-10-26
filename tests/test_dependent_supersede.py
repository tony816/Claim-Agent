"""A dependent revision replaced by a return supersedes its per-target blind and picture records too."""
from __future__ import annotations

from pathlib import Path

from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.runtime import build_runtime
from claim_agent.store.report import _failed_records

from . import scripted_roles as R

ROOT = Path(__file__).resolve().parents[1]


def test_a_returned_picture_block_is_superseded_and_not_reported_open(tmp_path, request_dep):
    """Regression for run web-d64078: the dr1 picture BLOCK stayed live after the loop, so the answer called a locked set unpassed."""
    over = {"paths.runs_dir": str(tmp_path / "runs"), "paths.lessons_dir": str(tmp_path / "lessons"), "cache.enabled": False, "pipeline.max_concurrency": 1}
    rt = build_runtime(ROOT, None, None, over)
    script = R.happy_script(dependent=True)
    script["picture-claim-reconstruction-reviewer"] = [R.picture(), R.picture("BLOCK", "RETURN_TO_DRAFTER"), R.picture(), R.picture(),
                                                       R.picture(), R.picture(), R.picture()]
    for role, extra in (("claim-drafter", R.drafter("DEPENDENT_SET")), ("claim-style-adjuster", R.style("DEPENDENT_SET")), ("claim-success-reviewer", R.success()),
                        ("syntax-scope-reviewer", R.syntax()), ("oa-strategy-reviewer", R.oa("DEPENDENT_SET"))):
        script[role].append(extra)
    script["blind-claim-reconstruction-reviewer"] += [R.blind()] * 3
    prov = ScriptedProvider(script)
    state = rt.engine(prov).run(rt.engine(prov).start(request_dep, "dep-supersede-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state.halt
    old = state.dependent.history[-1]
    old_target_ids = [rid for t in old.targets.values() for rid in (t.blind_record_id, t.picture_record_id) if rid]
    assert old_target_ids and all(state.records[rid].superseded for rid in old_target_ids)
    assert _failed_records(state) == []
