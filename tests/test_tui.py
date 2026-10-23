"""TUI file intake, request mapping and headless user flows; no live API calls."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from textual.events import Paste
from textual.widgets import Button, Checkbox, DataTable, Input, Select, TextArea

from claim_agent.tui import ClaimAgentApp, Composer
from claim_agent.tui_support import Attachment, normalize_path, parse_paths, validate_attachment


def test_paths_with_spaces_quotes_newlines_and_uri(tmp_path):
    one = tmp_path / "발명 설명.txt"
    two = tmp_path / "도면 1.png"
    one.write_text("원자료", encoding="utf-8")
    two.write_bytes(b"image")
    assert parse_paths(str(one), tmp_path) == [one]
    assert parse_paths(f'"{one}" "{two}"\n"{one}"', tmp_path) == [one, two]
    assert parse_paths(f"'{one}'\n'{two}'", tmp_path) == [one, two]
    assert normalize_path(one.as_uri(), tmp_path) == one


def test_attachment_classification_and_secret_rejection(tmp_path):
    image = tmp_path / "도면.png"
    image.write_bytes(b"image")
    assert validate_attachment(image, "invention").category == "drawing"
    assert validate_attachment(image, "prior_art").category == "prior_art"
    for name in (".env", ".env.local", "archive.zip"):
        path = tmp_path / name
        path.write_text("data")
        with pytest.raises(ValueError):
            validate_attachment(path, "invention")


def test_headless_paste_attachment_removal_and_resize(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    path = tmp_path / "공백 있는 자료.txt"
    path.write_text("자료", encoding="utf-8")

    async def scenario():
        app = ClaimAgentApp(tmp_path)
        async with app.run_test(size=(130, 45)) as pilot:
            assert not list(app.query("#category"))
            assert len(app.query_one("#attachments", DataTable).columns) == 1
            editor = app.query_one("#request", Composer)
            before = editor.text
            await editor._on_paste(Paste(f'"{path}"'))
            await pilot.pause()
            assert len(app.attachments) == 1
            assert editor.text == before
            await editor._on_paste(Paste("추가 요청\n둘째 줄"))
            assert "추가 요청\n둘째 줄" in editor.text
            await pilot.click("#remove")
            assert not app.attachments
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.query_one("#start", Button).region.bottom <= 24
            assert app.query_one("#stop", Button).disabled
    asyncio.run(scenario())


def test_headless_provider_switch_persists_and_locks_while_running(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async def scenario():
        app = ClaimAgentApp(tmp_path)
        async with app.run_test(size=(130, 45)) as pilot:
            selector, model = app.query_one("#provider", Select), app.query_one("#model", Input)
            assert selector.value == "gemini" and model.value == "gemini-3.8-flash"
            selector.value = "claude_oauth"
            await pilot.pause()
            assert app.cfg.provider.kind == "claude_oauth" and model.value == "sonnet"
            assert json.loads((tmp_path / ".tui" / "model-settings.json").read_text(encoding="utf-8"))["provider.kind"] == "claude_oauth"
            app.running = True                      # 실행 중에는 같은 run이 섞이지 않도록 되돌린다
            selector.value = "gemini"
            await pilot.pause()
            assert selector.value == "claude_oauth" and app.cfg.provider.kind == "claude_oauth"
    asyncio.run(scenario())


def test_headless_clipboard_text_and_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from claim_agent import tui
    path = tmp_path / "image.png"
    path.write_bytes(b"image")

    async def scenario():
        app = ClaimAgentApp(tmp_path)
        async with app.run_test(size=(130, 45)) as pilot:
            async def files(*args):
                return {"paths": [str(path)]}

            async def prose(*args):
                return {"text": "발명 설명\n" * 500}

            monkeypatch.setattr(tui, "native_input", files)
            worker = app.get_native("paste", app.query_one("#request"))
            await worker.wait()
            assert app.attachments == [Attachment(path, "drawing")]
            monkeypatch.setattr(tui, "native_input", prose)
            worker = app.get_native("paste", app.query_one("#paste"))
            await worker.wait()
            await pilot.pause()
            assert app.query_one("#material", Composer).text == "발명 설명\n" * 500
    asyncio.run(scenario())


def test_headless_claim_request_runs_through_chat(project_root, tmp_path, monkeypatch):
    """No mode picker: a claim request typed in the one composer is routed and runs the gated pipeline."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"paths": {"runs_dir": str(tmp_path / "runs"), "lessons_dir": str(tmp_path / "lessons")}}), encoding="utf-8")
    original_spawn = asyncio.create_subprocess_exec
    requests = []

    async def scripted_chat(*args, **kwargs):
        if "claim_agent.chat" in args:
            requests.append(json.loads(Path(args[args.index("--request") + 1]).read_text(encoding="utf-8")))
            program = ("from claim_agent import chat,routing,runtime; from claim_agent.routing import RouteDecision; "
                       "from claim_agent.provider.scripted import ScriptedProvider; from tests import scripted_roles as R; "
                       "routing.classify_request=lambda *a: RouteDecision(mode='AUTHORING_DRAFT',reason='청구항 작성 요청'); "
                       "runtime.make_provider=lambda *a,**k: ScriptedProvider(R.happy_script(dependent=False)); raise SystemExit(chat.main())")
            args = (sys.executable, "-u", "-c", program, *args[args.index("claim_agent.chat") + 1:])
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", scripted_chat)

    async def scenario():
        app = ClaimAgentApp(project_root, config_path=config)
        async with app.run_test(size=(130, 45)) as pilot:
            assert not app.query("#mode")
            app.query_one("#request", Composer).load_text("독립항 잠정안 작성")
            app.query_one("#material", Composer).load_text("오프라인 UI 연결 시험용 원자료")
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
            assert app.query_one("#start", Button).disabled
            async with asyncio.timeout(60):
                while app.running:
                    await asyncio.sleep(0.1)
            await pilot.pause()
            assert requests[0]["ui_hints"] == dict(dependent=False, target="", target_mode="independent")
            assert app.current_state()["outcome"] == "DRAFT_CLAIM_LOCK", notes
            assert app.last_pipeline_run and app.result_text and app.query_one("#result", TextArea).text == app.result_text
            assert not app.query_one("#copy", Button).disabled
            assert not app.query_one("#log-panel").display
            await pilot.click("#toggle-logs")
            assert app.query_one("#log-panel").display
    asyncio.run(scenario())


def test_dependent_checkbox_is_a_router_hint(tmp_path):
    async def scenario():
        app = ClaimAgentApp(tmp_path)
        async with app.run_test(size=(130, 45)):
            assert app.claim_hints() == dict(dependent=False, target="", target_mode="independent")
            app.query_one("#dependent", Checkbox).value = True
            app.query_one("#dep-target", Input).value = "2~4"
            assert app.claim_hints() == dict(dependent=True, target="2~4", target_mode="range")
            app.query_one("#dep-target", Input).value = "많이"
            with pytest.raises(ValueError):
                app.claim_hints()
    asyncio.run(scenario())


def test_headless_stop_reaps_child(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    original_spawn = asyncio.create_subprocess_exec

    async def sleeping_chat(*args, **kwargs):
        if "claim_agent.chat" in args:
            args = (sys.executable, "-u", "-c", "import time; print('waiting', flush=True); time.sleep(120)")
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", sleeping_chat)

    async def scenario():
        app = ClaimAgentApp(tmp_path)
        async with app.run_test(size=(130, 45)) as pilot:
            app.query_one("#request", Composer).load_text("독립항 잠정안 작성")
            app.query_one("#material", Composer).load_text("시험 자료")
            await pilot.click("#start")
            async with asyncio.timeout(10):
                while app.process is None:
                    await asyncio.sleep(0.05)
            process = app.process
            await pilot.click("#stop")
            async with asyncio.timeout(10):
                while app.running:
                    await asyncio.sleep(0.05)
            assert process.returncode is not None
            assert app.stop_requested
            assert not app.query_one("#start", Button).disabled
    asyncio.run(scenario())


def test_cancel_native_dialog_reaps_helper(tmp_path, monkeypatch):
    from claim_agent import tui_support
    original = asyncio.create_subprocess_exec
    processes = []

    async def start_sleeping_helper(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(120)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(tui_support.asyncio, "create_subprocess_exec", start_sleeping_helper)

    async def scenario():
        task = asyncio.create_task(tui_support.native_input(tmp_path, "pick"))
        async with asyncio.timeout(10):
            while not processes:
                await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
    asyncio.run(scenario())
