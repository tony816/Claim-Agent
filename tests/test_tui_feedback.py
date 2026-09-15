from __future__ import annotations

import asyncio
import sys

from textual.widgets import Select

from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.tui import ClaimAgentApp, Composer
from claim_agent.tui_support import Attachment, feedback_command
from tests import scripted_roles as R


def test_feedback_keeps_run_and_adds_only_new_material(tmp_path):
    existing = tmp_path / "기존.txt"
    added = tmp_path / "추가.txt"
    existing.write_text("기존", encoding="utf-8")
    added.write_text("추가", encoding="utf-8")
    state = {"run_id": "previous-run", "material_meta": [{"path": str(existing)}]}
    folder = tmp_path / "feedback"
    cmd = feedback_command(tmp_path, folder, state, "관계를 명확히 수정해 주세요.", "추가 설명",
                           [Attachment(existing, "invention"), Attachment(added, "invention")], "gemini-3.8-flash")
    assert cmd[cmd.index("resume") + 1] == "previous-run"
    assert cmd[cmd.index("--restart-from") + 1] == "ARCHITECT"
    assert str(existing) not in cmd
    assert str(added) in cmd
    assert (folder / "feedback.txt").read_text(encoding="utf-8") == "관계를 명확히 수정해 주세요."
    assert "--add-source" in cmd and "--run-id" not in cmd


def test_feedback_revises_existing_pipeline_and_rechecks_gates(rt, request_indep, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    original_state = engine.run(engine.start(request_indep, "feedback-existing"))
    assert original_state.outcome == "DRAFT_CLAIM_LOCK"
    config = tmp_path / "config.json"
    config.write_text(rt.cfg.model_dump_json(), encoding="utf-8")
    original_spawn = asyncio.create_subprocess_exec

    async def scripted_spawn(*args, **kwargs):
        if "claim_agent.cli" in args:
            program = "from claim_agent import cli; from claim_agent.provider.scripted import ScriptedProvider; from tests import scripted_roles as R; cli.make_provider=lambda *a, **k: ScriptedProvider(R.happy_script(dependent=False)); raise SystemExit(cli.main())"
            index = args.index("claim_agent.cli")
            args = (sys.executable, "-u", "-c", program, *args[index + 1:])
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", scripted_spawn)

    async def scenario():
        app = ClaimAgentApp(rt.cfg.project_root, config_path=config)
        async with app.run_test(size=(132, 44)) as pilot:
            app.query_one("#mode", Select).value = "FEEDBACK"
            app.query_one("#feedback-run", Select).value = "feedback-existing"
            app.query_one("#request", Composer).load_text("부품의 연결 관계를 다시 검토하여 수정해 주세요.")
            await pilot.click("#start")
            async with asyncio.timeout(5):          # the Button.Pressed message can still be in flight when click() returns
                while not app.running:
                    await asyncio.sleep(0.05)
            async with asyncio.timeout(30):
                while app.running:
                    await asyncio.sleep(.1)
            await pilot.pause()
            current = app.current_state()
            assert current["run_id"] == "feedback-existing"
            assert current["candidate"]["revision"] == "r2"
            assert current["candidate"]["design_revision"] == "d2"
            assert current["outcome"] == "DRAFT_CLAIM_LOCK"
            assert current["request"]["invention_sources"] == original_state.request["invention_sources"]
            assert not app.query_one("#log-panel").display
            assert "청구항" in app.result_text
    asyncio.run(scenario())
