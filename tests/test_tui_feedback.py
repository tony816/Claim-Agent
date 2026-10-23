from __future__ import annotations

import asyncio
import sys

from textual.widgets import Button

from claim_agent.models.state import Halt
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.tui import ClaimAgentApp, Composer
from claim_agent.tui_support import Attachment, chat_resume_command, chat_resume_kind
from tests import scripted_roles as R


def test_chat_on_a_halted_run_is_its_decision():
    halt = {"reason_code": "SYNTAX_BLOCK"}
    assert chat_resume_kind("오목면의 주체는 기둥 둘레면", None) is None          # not halted: ordinary chat
    assert chat_resume_kind("오목면의 주체는 기둥 둘레면", halt) == "none"
    assert chat_resume_kind("명세서가 없으니 미검증 수용", halt) == "accept_unverified"
    assert chat_resume_kind("미검증으로 진행해 줘", halt) == "accept_unverified"
    assert chat_resume_kind("도면을 추가했어요", halt, new_material=True) == "restart"
    assert chat_resume_kind("KR1, KR2 2026-10-01", {"reason_code": "PRIOR_ART_SELECTION"}) == "select_prior_art"


def test_chat_resume_keeps_run_and_adds_only_new_material(tmp_path):
    existing = tmp_path / "기존.txt"
    added = tmp_path / "추가.txt"
    existing.write_text("기존", encoding="utf-8")
    added.write_text("추가", encoding="utf-8")
    state = {"run_id": "previous-run", "material_meta": [{"path": str(existing)}], "halt": {"reason_code": "SYNTAX_BLOCK"}}
    assert chat_resume_command(tmp_path, tmp_path / "idle", {**state, "halt": None}, "수정", "", [], "m") is None
    folder = tmp_path / "decision"
    cmd = chat_resume_command(tmp_path, folder, state, "관계를 명확히 수정해 주세요.", "추가 설명",
                              [Attachment(existing, "invention"), Attachment(added, "invention")], "gemini-3.8-flash")
    assert cmd[cmd.index("resume") + 1] == "previous-run"
    assert cmd[cmd.index("--restart-from") + 1] == "ARCHITECT"
    assert str(existing) not in cmd and str(added) in cmd and str(folder / "추가설명.txt") in cmd
    assert (folder / "decision.txt").read_text(encoding="utf-8") == "관계를 명확히 수정해 주세요."
    plain = chat_resume_command(tmp_path, tmp_path / "plain", state, "주체를 둘레면으로", "", [], "m")
    assert "--add-source" not in plain and "--restart-from" not in plain and "--run-id" not in plain


def test_tui_has_no_separate_feedback_controls_and_chat_resumes_a_halted_run(rt, request_indep, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    original_state = engine.run(engine.start(request_indep, "feedback-existing"))
    assert original_state.outcome == "DRAFT_CLAIM_LOCK"
    original_state.halt = Halt(stage="SYNTAX", role="syntax-scope-reviewer", kind="REVIEW", reason_code="TEST_DECISION", message="결정 필요")
    rt.store.save_state(original_state)
    extra = tmp_path / "추가 도면 설명.txt"
    extra.write_text("기둥 둘레면이 오목면을 이룬다.", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(rt.cfg.model_dump_json(), encoding="utf-8")
    original_spawn = asyncio.create_subprocess_exec
    spawned = []

    async def scripted_spawn(*args, **kwargs):
        if "claim_agent.cli" in args:
            spawned.append(args)
            program = "from claim_agent import cli; from claim_agent.provider.scripted import ScriptedProvider; from tests import scripted_roles as R; cli.make_provider=lambda *a, **k: ScriptedProvider(R.happy_script(dependent=False)); raise SystemExit(cli.main())"
            index = args.index("claim_agent.cli")
            args = (sys.executable, "-u", "-c", program, *args[index + 1:])
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", scripted_spawn)

    async def scenario():
        app = ClaimAgentApp(rt.cfg.project_root, config_path=config)
        async with app.run_test(size=(132, 44)) as pilot:
            assert not app.query("#mode")
            assert not app.query("#feedback") and not app.query("#feedback-run")
            app.last_pipeline_run = "feedback-existing"
            app.add_files([extra])
            app.query_one("#request", Composer).load_text("부품의 연결 관계를 다시 검토하여 수정해 주세요.")
            notes = []
            monkeypatch.setattr(app, "notify", lambda message, *a, **k: notes.append(str(message)))
            await pilot.pause()
            app.query_one("#start", Button).press()
            try:
                async with asyncio.timeout(5):      # Button.Pressed is delivered asynchronously
                    while not app.running:
                        await asyncio.sleep(0.05)
            except TimeoutError:
                raise AssertionError(f"start did not begin a run; notifications={notes}") from None
            async with asyncio.timeout(30):
                while app.running:
                    await asyncio.sleep(.1)
            await pilot.pause()
            assert spawned and "resume" in spawned[0] and "feedback-existing" in spawned[0]
            current = app.current_state()
            assert current["run_id"] == "feedback-existing"
            assert current["candidate"]["design_revision"] == "d2"
            assert current["outcome"] == "DRAFT_CLAIM_LOCK"
            assert "청구항" in app.result_text
    asyncio.run(scenario())
