"""blind → picture runs alongside OA: picture never reads the OA report, and OA is still adopted before it."""
from __future__ import annotations

from claim_agent.provider.scripted import ScriptedProvider

from . import scripted_roles as R


def test_independent_picture_gets_no_oa_report(rt, request_indep):
    provider = ScriptedProvider(R.happy_script(dependent=False))
    engine = rt.engine(provider)
    state = engine.run(engine.start(request_indep, "pic-no-oa"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    picture = next(c for c in provider.calls if c.role == "picture-claim-reconstruction-reviewer")
    assert "OA 보고서" not in picture.packet_text
    assert state.candidate.current.records.get("oa") and state.candidate.current.records.get("reference_compare")


def test_independent_picture_failure_returns_after_oa_passes(rt, request_indep):
    script = R.happy_script(dependent=False)
    for role in ("claim-drafter", "claim-style-adjuster", "claim-success-reviewer", "syntax-scope-reviewer", "oa-strategy-reviewer", "blind-claim-reconstruction-reviewer"):
        script[role] = script[role] * 2
    script["picture-claim-reconstruction-reviewer"] = [R.picture(return_to="RETURN_TO_DRAFTER"), R.picture()]
    provider = ScriptedProvider(script)
    engine = rt.engine(provider)
    state = engine.run(engine.start(request_indep, "pic-return"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.candidate.loop_counts["DRAFTER"] == 1
    second_draft = [c for c in provider.calls if c.role == "claim-drafter"][1]
    assert "관찰 단면과 실제 면 혼동" in second_draft.packet_text
