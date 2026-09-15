"""Follow-up edits take the cheapest legal revision path; new material always forces a redesign."""
from __future__ import annotations

from pathlib import Path

import pytest

from claim_agent import web
from claim_agent.conversation_pipeline import intake, revision_path, run_pipeline
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.routing import RouteDecision
from claim_agent.tui_support import resume_command

from . import scripted_roles as R
from .test_web import wait_for


def _previous(rt, request, run_id="rev-base"):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=request.dependent)))
    return engine.run(engine.start(request, run_id))


def test_route_decision_rejects_revision_kind_outside_authoring():
    with pytest.raises(ValueError):
        RouteDecision(mode="REVIEW_ONLY", reason="x", reviewers=["oa-strategy-reviewer"], revision_kind="STYLE_ONLY")
    assert RouteDecision(mode="AUTHORING_DRAFT", reason="x", revision_kind="STYLE_ONLY").revision_kind == "STYLE_ONLY"


def test_style_only_followup_reuses_design_and_skips_architect_and_drafter(rt, request_indep, tmp_path):
    previous = _previous(rt, request_indep)
    folder = tmp_path / "followup"
    folder.mkdir()
    request = dict(text="띄어쓰기만 고쳐줘: '조임 나사'는 붙여 써", files=[], history=[])
    blocks, paths = intake(request, folder, previous)
    route = RouteDecision(mode="AUTHORING_DRAFT", reason="표면 표기 수정", revision_kind="STYLE_ONLY")
    assert revision_path(route, request, blocks, paths, previous) == ("style", None)
    script = R.happy_script(dependent=False)
    provider = ScriptedProvider(script)
    result = run_pipeline(rt, provider, request, route, blocks, paths, folder, previous)
    state = rt.store.load_state(previous.run_id)
    assert result["run_id"] == previous.run_id and result["revision_path"] == "style"
    assert state.candidate.revision == "r2" and state.candidate.design_revision == "d1" and state.outcome == "DRAFT_CLAIM_LOCK"
    roles = [c.role for c in provider.calls if c.phase == "main"]
    assert "claim-architect" not in roles and "claim-drafter" not in roles and roles[0] == "claim-style-adjuster"
    assert len(roles) == 6   # style, success, syntax, oa, blind, picture
    assert any("revision 경로: style" in n for n in state.notes)


def test_meaning_followup_starts_at_drafter(rt, request_indep, tmp_path):
    previous = _previous(rt, request_indep, "rev-meaning")
    folder = tmp_path / "followup-m"
    folder.mkdir()
    request = dict(text="슬롯의 귀속 주체를 홀더 본체로 명시해서 절을 다시 써줘", files=[], history=[])
    blocks, paths = intake(request, folder, previous)
    route = RouteDecision(mode="AUTHORING_DRAFT", reason="관계 술어 수정", revision_kind="MEANING")
    provider = ScriptedProvider(R.happy_script(dependent=False))
    run_pipeline(rt, provider, request, route, blocks, paths, folder, previous)
    state = rt.store.load_state(previous.run_id)
    roles = [c.role for c in provider.calls if c.phase == "main"]
    assert roles[0] == "claim-drafter" and "claim-architect" not in roles
    assert state.candidate.revision == "r2" and state.candidate.design_revision == "d1"


def test_new_material_forces_redesign_even_when_router_says_style(rt, request_indep, tmp_path):
    previous = _previous(rt, request_indep, "rev-material")
    folder = tmp_path / "followup-x"
    folder.mkdir()
    extra = tmp_path / "추가설명.md"
    extra.write_text("새로운 기술 설명: 탄성 리브가 슬롯 입구를 좁힌다", encoding="utf-8")
    request = dict(text="띄어쓰기만 고쳐줘", files=[str(extra)], history=[])
    blocks, paths = intake(request, folder, previous)
    route = RouteDecision(mode="AUTHORING_DRAFT", reason="표면 수정", revision_kind="STYLE_ONLY")
    assert revision_path(route, request, blocks, paths, previous) == ("restart", None)
    provider = ScriptedProvider(R.happy_script(dependent=False))
    run_pipeline(rt, provider, request, route, blocks, paths, folder, previous)
    state = rt.store.load_state(previous.run_id)
    assert state.candidate.design_revision == "d2" and [c.role for c in provider.calls if c.phase == "main"][0] == "claim-architect"


def test_dependent_only_mention_targets_the_dependent_revision(rt, request_dep, tmp_path):
    previous = _previous(rt, request_dep, "rev-dep")
    folder = tmp_path / "followup-d"
    folder.mkdir()
    request = dict(text="3항의 조사만 고쳐줘", files=[], history=[])
    blocks, paths = intake(request, folder, previous)
    route = RouteDecision(mode="AUTHORING_DRAFT", reason="종속항 표면 수정", revision_kind="STYLE_ONLY")
    assert revision_path(route, request, blocks, paths, previous) == ("style", "DEPENDENT")
    script = {  # only dependent-stage responses are needed: the root revision stays r1
        "claim-style-adjuster": [R.style("DEPENDENT_SET")], "claim-success-reviewer": [R.success()], "syntax-scope-reviewer": [R.syntax()],
        "oa-strategy-reviewer": [R.oa("DEPENDENT_SET")], "blind-claim-reconstruction-reviewer": [R.blind()] * 3, "picture-claim-reconstruction-reviewer": [R.picture()] * 3,
    }
    provider = ScriptedProvider(script)
    run_pipeline(rt, provider, request, route, blocks, paths, folder, previous)
    state = rt.store.load_state(previous.run_id)
    assert state.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state.halt
    assert state.candidate.revision == "r1" and state.dependent.dependent_revision == "dr2" and state.dependent.draft_set_lock == "ddsl-clip-holder-dep-dr2-01"
    roles = [c.role for c in provider.calls if c.phase == "main"]
    assert roles[0] == "claim-style-adjuster" and "dependent-claim-strategy-architect" not in roles


def test_resume_command_flags_and_web_resume_endpoint(rt, request_indep, tmp_path, monkeypatch):
    cmd = resume_command(Path("/p"), "run-1", Path("/d.txt"), "style", "m", ["/a.png"], "DEPENDENT")
    assert "--apply-style-fix" in cmd and cmd[cmd.index("--add-source") + 1] == "/a.png" and cmd[cmd.index("--scope") + 1] == "DEPENDENT"
    with pytest.raises(ValueError):
        resume_command(Path("/p"), "run-1", Path("/d.txt"), "bogus", "m")
    # web: a halted run exposes its halt and accepts a resume decision that spawns the CLI
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    script = R.happy_script(dependent=False)
    script["claim-architect"] = [R.architect(locked=False)]
    engine = rt.engine(ScriptedProvider(script))
    halted = engine.run(engine.start(request_indep, "web-halted"))
    assert halted.outcome == "HALTED_REVIEW"
    config = tmp_path / "config.json"
    config.write_text(rt.cfg.model_dump_json(), encoding="utf-8")
    workspace = web.Workspace(rt.cfg.project_root, config)
    workspace.folder = tmp_path / "web-ui"
    workspace.sessions = {}
    captured = []
    def capture(sid, job, command):
        captured.append((job, command))
        job["done"] = True
    monkeypatch.setattr(workspace, "execute", capture)
    try:
        sid = workspace.create()["id"]
        with pytest.raises(ValueError, match="재개할 작업"):
            workspace.resume(sid, {"kind": "style", "text": "x"})
        workspace.sessions[sid]["run_id"] = "web-halted"
        snap = workspace.snapshot(sid)
        assert snap["run"]["halt"]["kind"] == "REVIEW" and snap["run"]["halt"]["stage"] == "ARCHITECT" and snap["run"]["outcome"] == "HALTED_REVIEW"
        assert snap["run"]["halt"]["open_issues"][0]["code"] == "GEOMETRY"
        with pytest.raises(ValueError, match="결정 내용"):
            workspace.resume(sid, {"kind": "none", "text": ""})
        workspace.resume(sid, {"kind": "redesign", "text": "오목면의 주체는 기둥 둘레면"})
        wait_for(lambda: captured)
        job, command = captured[0]
        assert job["mode"] == "RESUME" and job["run_id"] == "web-halted"
        assert "resume" in command and "web-halted" in command and "--redesign" in command
        assert workspace.sessions[sid]["messages"][-2]["text"].startswith("[재개 · 재설계]")
    finally:
        workspace.close()
