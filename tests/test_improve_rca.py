"""개선 루프 증분: RCA, 선제 경고, 교훈 역전파, 도구 텔레메트리."""
from __future__ import annotations

import json
from pathlib import Path

from claim_agent.improve.feedback import build_feedback
from claim_agent.improve.lessons import LessonStore, propose_from_feedback, propose_with_llm
from claim_agent.improve.rca import build_rca
from claim_agent.provider.base import CallResult
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.store.telemetry import read_telemetry

from . import scripted_roles as R


# --------------------------------------------------------------------------- RCA
def test_rca_localizes_the_returning_style_record(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(return_to="RETURN_TO_DRAFTER"), R.style()]
    script["claim-drafter"] = [R.drafter(), R.drafter()]
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "run-rca-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK"

    rep = build_rca(rt.store, "run-rca-01")
    stages = [s.stage for s in rep.trace]
    assert stages[:4] == ["ARCHITECT", "DRAFT", "STYLE", "DRAFT"]          # 되돌아간 순서가 그대로 보인다
    assert rep.trace[1].superseded and rep.trace[1].record_id == "md-clip-holder-r1-01"
    assert rep.loop_waste_calls >= 1 and rep.loop_waste_tokens > 0
    assert any("RETURN_TO_DRAFTER" in t for t in rep.transitions)
    assert any("revision r1 → 폐기" in t for t in rep.transitions)
    # 실패하지 않고 끝난 run이지만 첫 비-PASS 지점은 style r1이다
    assert rep.fault is not None
    assert rep.fault.record_id == "sty-clip-holder-r1-01"
    assert rep.fault.gates["CLAIM_STYLE_GATE"] == "RETURN_TO_DRAFTER"
    assert rep.fault.last_pass_stage and "DRAFT" in rep.fault.last_pass_stage
    assert rep.fault.open_issues and "절 결속" in rep.fault.open_issues[0]["text"]
    md = rep.render_md()
    assert "1. 워크플로 추적" in md and "2. 결함 국소화" in md and "3. 패턴 인식" in md and "4. 영향 평가" in md
    assert "drafter PRE_STYLE부터" in md


def test_rca_on_loop_limit_run_scores_priority_and_waste(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(return_to="RETURN_TO_DRAFTER")] * 4
    script["claim-drafter"] = [R.drafter()] * 4
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "run-rca-02"))
    assert state.outcome == "HALTED_LOOP_LIMIT"

    rep = build_rca(rt.store, "run-rca-02")
    assert rep.fault is not None and rep.fault.kind == "LOOP_LIMIT"
    assert rep.priority >= 3                                   # 심각도 3 × 반복 1 이상
    assert rep.loop_waste_tokens > 0
    assert any("설계 계약" in a for a in rep.next_actions)


def test_rca_on_clean_run_reports_no_fault(rt, request_indep):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    engine.run(engine.start(request_indep, "run-rca-03"))
    rep = build_rca(rt.store, "run-rca-03")
    assert rep.fault is None and rep.priority == 0
    assert "LOCK까지 도달" in rep.render_md()


# --------------------------------------------------------------------------- 선제 경고
def _fake_run(runs_dir: Path, run_id: str, created_at: float, rows: list[dict], outcome: str = "DRAFT_CLAIM_LOCK", halt: dict | None = None) -> None:
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"run_id": run_id, "created_at": created_at, "outcome": outcome, "halt": halt}), encoding="utf-8")
    (d / "telemetry.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def _row(role: str, *, gates: dict | None = None, status: str = "PASS", latency: int = 1000, output: int = 500, repair: bool = False, phase: str = "main", tool: dict | None = None) -> dict:
    return {
        "role": role, "phase": phase, "status": status, "gates": gates or {}, "latency_ms": latency,
        "output_tokens": output, "thoughts_tokens": 0, "prompt_tokens": 1000, "cached_tokens": 0,
        "repair_used": repair, "non_pass_checks": [], "next_step": "PROCEED", "reason_code": None,
        "invention_primary": "PHYSICAL", "seq": 1, "stage": "STYLE", "tool": tool or {},
    }


def test_drift_and_spike_alerts(tmp_path):
    runs = tmp_path / "runs"
    for i in range(8):   # 기준 구간: 모두 통과
        _fake_run(runs, f"run-base-{i:02d}", 1000.0 + i, [_row("claim-style-adjuster"), _row("syntax-scope-reviewer")])
    for i in range(3):   # 최근 창: 같은 게이트가 연속 실패
        _fake_run(
            runs, f"run-recent-{i:02d}", 2000.0 + i,
            [_row("claim-style-adjuster", gates={"CLAIM_STYLE_GATE": "REVIEW"}, status="REVIEW", latency=4000), _row("syntax-scope-reviewer")],
            outcome="HALTED_REVIEW",
        )
    rep = build_feedback(runs, window=3)
    kinds = {a.kind for a in rep.alerts}
    assert "DRIFT_WARNING" in kinds and "SPIKE" in kinds
    drift = next(a for a in rep.alerts if a.kind == "DRIFT_WARNING" and "비-PASS" in a.key)
    assert drift.before == "0%" and drift.after == "100%"
    spike = next(a for a in rep.alerts if a.kind == "SPIKE")
    assert "CLAIM_STYLE_GATE" in spike.key and len(spike.runs) == 3
    assert any("지연" in a.key for a in rep.alerts)       # 1000ms → 4000ms
    assert "선제 경고" in rep.render_md()


def test_no_alerts_when_stable(tmp_path):
    runs = tmp_path / "runs"
    for i in range(6):
        _fake_run(runs, f"run-ok-{i:02d}", 1000.0 + i, [_row("claim-style-adjuster")])
    assert build_feedback(runs, window=3).alerts == []


def test_severe_halt_spike_and_tool_anomalies(tmp_path):
    runs = tmp_path / "runs"
    _fake_run(runs, "run-t-00", 1.0, [_row("claim-style-adjuster")])
    for i in (1, 2):
        _fake_run(
            runs, f"run-t-{i:02d}", 1.0 + i,
            [
                _row("claim-style-adjuster"),
                _row("claim-style-adjuster", phase="tool", tool={"name": "search_style_corpus", "mode": "TERM_EXACT", "query_len": 5, "results": 0, "refused": True, "flagged_review": False}),
            ],
            outcome="HALTED_ENVELOPE_INVALID", halt={"kind": "ENVELOPE_INVALID", "stage": "STYLE", "role": "claim-style-adjuster"},
        )
    rep = build_feedback(runs, window=3)
    assert any(a.kind == "SPIKE" and a.key == "심각 중지" for a in rep.alerts)
    assert any(a.kind == "TOOL_ANOMALY" and "거부" in a.key for a in rep.alerts)
    assert rep.tool_calls == 2 and rep.tool_refused == 2


# --------------------------------------------------------------------------- 역전파
def _feedback_with_pattern(runs_dir: Path) -> object:
    for i in range(2):
        _fake_run(
            runs_dir, f"run-p-{i:02d}", 10.0 + i,
            [{**_row("syntax-scope-reviewer", gates={"GEOMETRIC_OBJECT_GATE": "BLOCK"}, status="BLOCK"), "reason_code": "OTHER", "non_pass_checks": ["형상·공간 객체 귀속시험"]}],
            outcome="HALTED_BLOCK",
        )
    return build_feedback(runs_dir, window=5)


def test_propose_from_feedback_is_deduped_and_pending(tmp_path):
    rep = _feedback_with_pattern(tmp_path / "runs")
    assert rep.pattern_cards and rep.pattern_cards[0]["count"] == 2
    ls = LessonStore(tmp_path / "lessons")
    first = propose_from_feedback(rep, ls, min_count=2)
    assert len(first) == 1
    lesson = first[0]
    assert lesson.status == "pending" and lesson.key == "syntax-scope-reviewer|GEOMETRIC_OBJECT_GATE|OTHER"
    assert "GEOMETRIC_OBJECT_GATE" in lesson.text_ko and "형상·공간 객체 귀속시험" in lesson.text_ko
    assert set(lesson.evidence) == {"run-p-00", "run-p-01"}
    assert ls.injection_text("syntax-scope-reviewer") == ""      # 승인 전에는 주입되지 않는다

    # 같은 패턴이 한 번 더 나와도 새 교훈을 만들지 않고 근거만 는다
    _fake_run(
        tmp_path / "runs", "run-p-02", 12.0,
        [{**_row("syntax-scope-reviewer", gates={"GEOMETRIC_OBJECT_GATE": "BLOCK"}, status="BLOCK"), "reason_code": "OTHER"}],
        outcome="HALTED_BLOCK",
    )
    again = propose_from_feedback(build_feedback(tmp_path / "runs", window=5), ls, min_count=2)
    assert len(ls.list("pending")) == 1
    assert "run-p-02" in again[0].evidence

    ls.approve(lesson.id)
    assert "GEOMETRIC_OBJECT_GATE" in ls.injection_text("syntax-scope-reviewer")


def test_propose_from_feedback_respects_min_count(tmp_path):
    rep = _feedback_with_pattern(tmp_path / "runs")
    ls = LessonStore(tmp_path / "lessons")
    assert propose_from_feedback(rep, ls, min_count=5) == []
    assert ls.list() == []


def test_llm_draft_is_optin_pending_and_marked(tmp_path):
    ls = LessonStore(tmp_path / "lessons")

    class _Drafter:
        name = "scripted"

        def __init__(self):
            self.specs = []

        def generate(self, spec):
            self.specs.append(spec)
            data = {"text_ko": "형상 술어의 귀속 주체를 판정하기 전에 관찰 단면과 실제 면을 표로 분리해 기록한다.", "target_roles": ["syntax-scope-reviewer"], "rationale": "반복 실패"}
            return CallResult(text=json.dumps(data, ensure_ascii=False), parsed=data, model="m", provider="scripted")

    prov = _Drafter()
    lesson = propose_with_llm(prov, "gemini-3.8-flash", "역할: syntax-scope-reviewer\n실패 시험: 형상·공간 객체 귀속시험", ls, "syntax-scope-reviewer", ["run-x:syn-1"], "syntax-scope-reviewer|GEOMETRIC_OBJECT_GATE|OTHER")
    assert lesson is not None and lesson.llm_drafted and lesson.status == "pending"
    assert ls.injection_text("syntax-scope-reviewer") == ""
    spec = prov.specs[0]
    assert spec.use_cache is False and spec.json_schema is not None and spec.gen.thinking_level == "LOW"
    assert "청구항" not in spec.packet_text or "청구항 문언" not in spec.packet_text

    # 같은 key면 중복 생성 대신 근거만 는다
    second = propose_with_llm(prov, "gemini-3.8-flash", "…", ls, "syntax-scope-reviewer", ["run-y:syn-1"], "syntax-scope-reviewer|GEOMETRIC_OBJECT_GATE|OTHER")
    assert len(ls.list("pending")) == 1 and "run-y:syn-1" in second.evidence


def test_llm_draft_without_grounds_returns_none(tmp_path):
    ls = LessonStore(tmp_path / "lessons")

    class _Empty:
        name = "scripted"

        def generate(self, spec):
            return CallResult(text='{"text_ko": "", "rationale": "근거 부족"}', parsed={"text_ko": "", "rationale": "근거 부족"}, model="m", provider="scripted")

    assert propose_with_llm(_Empty(), "m", "…", ls, "claim-drafter", [], "k") is None
    assert ls.list() == []


# --------------------------------------------------------------------------- 도구 텔레메트리
def test_tool_calls_are_logged_without_query_text(rt, request_indep):
    """스타일 조정자가 실제로 코퍼스 도구를 부른 경우 phase=tool 행이 남는다."""
    script = R.happy_script(dependent=False)

    class _ToolUsingProvider(ScriptedProvider):
        def generate(self, spec):
            if spec.phase == "tool_phase" and spec.tools:
                by_name = {f.__name__: f for f in spec.tools}
                by_name["open_routing_index"]()
                by_name["search_style_corpus"](mode="TERM_EXACT", query="길이 방향")
                by_name["search_style_corpus"](mode="TERM_EXACT", query="TF_생활위생")   # 금지 질의 → 거부
                return CallResult(text="SEARCH_DONE", parsed=None, model=spec.model, provider=self.name, finish_reason="STOP")
            return super().generate(spec)

    engine = rt.engine(_ToolUsingProvider(script))
    state = engine.run(engine.start(request_indep, "run-tool-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt

    rows = read_telemetry(rt.store.telemetry_path("run-tool-01"))
    tool_rows = [r for r in rows if r["phase"] == "tool"]
    assert [r["tool"]["name"] for r in tool_rows] == ["open_routing_index", "search_style_corpus", "search_style_corpus"]
    search = [r for r in tool_rows if r["tool"]["name"] == "search_style_corpus"]
    assert search[0]["tool"]["results"] >= 1 and search[0]["tool"]["refused"] is False
    assert search[1]["tool"]["refused"] is True and search[1]["tool"]["results"] == 0
    assert all("길이 방향" not in json.dumps(r, ensure_ascii=False) for r in rows)     # 질의 원문은 남기지 않는다
    assert search[0]["tool"]["query_len"] == len("길이 방향")
    assert all(r["record_id"] == "sty-clip-holder-r1-01" for r in tool_rows)

    phase_row = next(r for r in rows if r["phase"] == "tool_phase")
    assert phase_row["tool"]["calls"] == 3 and phase_row["tool"]["activated"] is True

    # success reviewer가 같은 조각과 라우팅 인덱스를 받았는지
    success_call = next(c for c in engine.provider.calls if c.role == "claim-success-reviewer")
    assert "조건부 보조 소스: USED" in success_call.packet_text
    rec = rt.store.read_record("run-tool-01", "sty-clip-holder-r1-01")
    assert rec["aux_source_usage"] == "USED" and "EX-" in rec["tool_log"]
