"""TUI file intake, request mapping and headless user flows; no live API calls."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from textual.events import Paste
from textual.widgets import Button, DataTable, Select, TextArea

from claim_agent.models.request import RunRequest
from claim_agent.tui import ClaimAgentApp, Composer
from claim_agent.tui_support import Attachment, normalize_path, parse_paths, prepare_request, run_command, validate_attachment


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


def test_request_preserves_categories_and_pasted_source(tmp_path):
    specs = tmp_path / "명세서.txt"
    prior = tmp_path / "선행기술.txt"
    specs.write_text("명세서", encoding="utf-8")
    prior.write_text("선행기술", encoding="utf-8")
    path = prepare_request(tmp_path, "test", "요청", "사용자 제공 원자료", [Attachment(specs, "spec"), Attachment(prior, "prior_art")], True, "2~8", "FINALIZATION")
    req = RunRequest.from_yaml(path)
    assert req.request_text == "요청"
    assert req.spec_path == str(specs)
    assert req.prior_art == [str(prior)]
    assert req.dependent and req.dependent_target == "2~8"
    assert Path(req.invention_sources[0]).read_text(encoding="utf-8") == "사용자 제공 원자료"
    command = run_command(tmp_path, path, "test", "gemini-3.8-flash")
    assert command[0] == sys.executable
    assert command[command.index("--request-yaml") + 1] == str(path)
    assert "GEMINI_API_KEY" not in " ".join(command)


@pytest.mark.parametrize("request_text,source,mode", [("", "자료", "AUTHORING_DRAFT"), ("요청", "", "AUTHORING_DRAFT"), ("요청", "자료", "FINALIZATION")])
def test_invalid_request_never_creates_job(tmp_path, request_text, source, mode):
    with pytest.raises(ValueError):
        prepare_request(tmp_path, "test", request_text, source, [], False, "", mode)
    assert not (tmp_path / ".tui").exists()


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


def test_headless_offline_pipeline_result(project_root, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from claim_agent import tui
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"paths": {"runs_dir": str(tmp_path / "runs"), "lessons_dir": str(tmp_path / "lessons")}}), encoding="utf-8")
    original_command = run_command

    def offline_command(root, request, run_id, model):
        # The checked-in recording has fixed IDs for this sample candidate.
        data = json.loads(request.read_text(encoding="utf-8"))
        data["candidate_id"] = "clip-holder"
        request.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return original_command(root, request, run_id, model) + ["--replay", str(project_root / "eval/cases/sample-clip-holder/fixtures")]

    monkeypatch.setattr(tui, "run_command", offline_command)

    async def scenario():
        app = ClaimAgentApp(project_root, config_path=config)
        async with app.run_test(size=(130, 45)) as pilot:
            app.query_one("#mode", Select).value = "AUTHORING_DRAFT"
            app.query_one("#request", Composer).load_text("독립항 잠정안 작성")
            app.query_one("#material", Composer).load_text("오프라인 UI 연결 시험용 원자료")
            await pilot.click("#start")
            async with asyncio.timeout(5):          # the Button.Pressed message can still be in flight when click() returns
                while not app.running:
                    await asyncio.sleep(0.05)
            assert app.query_one("#start", Button).disabled
            async with asyncio.timeout(30):
                while app.running:
                    await asyncio.sleep(0.1)
            await pilot.pause()
            assert app.current_state()["outcome"] == "DRAFT_CLAIM_LOCK"
            assert app.result_text and app.query_one("#result", TextArea).text == app.result_text
            assert not app.query_one("#copy", Button).disabled
            assert not app.query_one("#log-panel").display
            await pilot.click("#toggle-logs")
            assert app.query_one("#log-panel").display
    asyncio.run(scenario())


def test_headless_stop_reaps_child(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from claim_agent import tui
    monkeypatch.setattr(tui, "run_command", lambda *args: [sys.executable, "-u", "-c", "import time; print('waiting', flush=True); time.sleep(120)"])

    async def scenario():
        app = ClaimAgentApp(tmp_path)
        async with app.run_test(size=(130, 45)) as pilot:
            app.query_one("#mode", Select).value = "AUTHORING_DRAFT"
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
