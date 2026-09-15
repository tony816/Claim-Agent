"""limitation_evidence: schema, deterministic 조건 4 check, record/export propagation."""
from __future__ import annotations

from claim_agent.models.envelope import ENVELOPE_JSON_SCHEMA, RoleEnvelope, schema_depth
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.store.export import render_markdown

from . import scripted_roles as R

ROWS = [
    {"limitation": "클램프부를 갖는 베이스", "role": "E", "source_name": "invention.md", "location": "§1 구성", "basis": "DIRECT"},
    {"limitation": "입구 폭이 바닥 폭보다 좁은 슬롯", "role": "C", "source_name": "invention.md", "location": "§2 슬롯", "basis": "DERIVED", "note": "도1 참조"},
]


def test_schema_stays_flat_and_envelope_counts_unconfirmed():
    assert schema_depth(ENVELOPE_JSON_SCHEMA) <= 3 and "limitation_evidence" in ENVELOPE_JSON_SCHEMA["properties"]
    env = RoleEnvelope.model_validate({"status": "PASS", "limitation_evidence": ROWS + [{"limitation": "탄성 리브", "basis": "UNCONFIRMED"}]})
    assert env.unconfirmed_evidence() == ["탄성 리브"]


def _architect_with(rows):
    base = R.architect()

    def f(spec):
        env = base(spec)
        env["limitation_evidence"] = rows
        return env
    return f


def test_pass_with_unconfirmed_evidence_halts_as_review(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["claim-architect"] = [_architect_with(ROWS + [{"limitation": "탄성 리브", "basis": "UNCONFIRMED", "note": "원자료 없음"}])]
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "evid-01"))
    assert state.outcome == "HALTED_REVIEW" and state.halt.stage == "ARCHITECT" and "EVIDENCE_UNCONFIRMED" in state.halt.message
    assert state.halt.open_issues[0]["code"] == "COND4" and state.halt.open_issues[0]["text"] == "탄성 리브"
    ref = state.records["dg-clip-holder-d1-01"]
    assert ref.issued is False and ref.evidence_counts == {"DIRECT": 1, "DERIVED": 1, "UNCONFIRMED": 1}
    assert len([c for c in engine.provider.calls if c.role == "claim-drafter"]) == 0


def test_confirmed_evidence_is_recorded_and_exported(rt, request_indep, tmp_path):
    script = R.happy_script(dependent=False)
    script["claim-architect"] = [_architect_with(ROWS)]
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "evid-02"))
    assert state.outcome == "DRAFT_CLAIM_LOCK"
    rec = rt.store.read_record("evid-02", "dg-clip-holder-d1-01")
    assert [r["basis"] for r in rec["limitation_evidence"]] == ["DIRECT", "DERIVED"] and rec["evidence_problem"] is None
    report = (rt.store.run_dir("evid-02") / "report.md").read_text(encoding="utf-8")
    assert "근거 D1/D1" in report
    md = render_markdown(rt.store, state, with_evidence=True)
    assert "## 부록: 한정별 근거표" in md and "입구 폭이 바닥 폭보다 좁은 슬롯" in md and "§2 슬롯" in md
