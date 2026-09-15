"""Streaming, event logs and multi-turn chat without network calls."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

from claim_agent.chat import generate_turn, user_message
from claim_agent.live_events import EventReader, EventWriter, visible_text
from claim_agent.provider.gemini import GeminiProvider
from tests.test_gemini_provider import _client, _spec


def chunk(text, finish=None, thought=False):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text=text, thought=thought)]), finish_reason=finish)], usage_metadata=None)


def test_event_reader_handles_partial_utf8_and_redacts(tmp_path):
    path = tmp_path / "events.jsonl"
    writer = EventWriter(path, ["secret-key"])
    reader = EventReader(path)
    writer.emit("request", text="한글 secret-key")
    assert reader.read()[0]["text"] == "한글 [API KEY]"
    data = (json.dumps({"kind": "delta", "text": "응답"}, ensure_ascii=False) + "\n").encode("utf-8")
    with path.open("ab") as stream:
        stream.write(data[:-4])
    assert reader.read() == []
    with path.open("ab") as stream:
        stream.write(data[-4:])
    assert reader.read() == [{"kind": "delta", "text": "응답"}]
    assert visible_text(chunk("not-public", thought=True)) == ""


def test_provider_stream_keeps_json_and_blind_isolation(tmp_path):
    client = _client()
    client.models.generate_content_stream = lambda **kwargs: iter([chunk("hidden", thought=True), chunk('{"status":'), chunk('"PASS"}', "STOP")])
    path = tmp_path / "events.jsonl"
    provider = GeminiProvider(client, events=EventWriter(path))
    result = provider.generate(_spec(role="blind-claim-reconstruction-reviewer", sources_block="", use_cache=False))
    assert result.parsed == {"status": "PASS"}
    assert result.finish_reason == "STOP"
    events = EventReader(path).read()
    assert events[0]["sources"] == ""
    assert events[0]["text"] == "PKT"
    assert "".join(e["text"] for e in events if e["kind"] == "delta") == result.text
    assert "hidden" not in path.read_text(encoding="utf-8")


def test_stream_retry_does_not_mix_failed_output(tmp_path):
    client = _client()
    attempts = []

    def stream(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            yield chunk("partial")
            raise RuntimeError("503 unavailable")
        yield chunk('{"ok":true}', "STOP")

    client.models.generate_content_stream = stream
    path = tmp_path / "events.jsonl"
    provider = GeminiProvider(client, retry_attempts=2, backoff_s=[0], events=EventWriter(path))
    result = provider.generate(_spec())
    assert result.text == '{"ok":true}'
    rows = EventReader(path).read()
    requests = [e for e in rows if e["kind"] == "request"]
    assert [e["attempt"] for e in requests] == [1, 2]
    assert requests[0]["call_id"] != requests[1]["call_id"]
    assert any(e["kind"] == "error" for e in rows)


def test_tool_phase_keeps_sdk_function_calling(tmp_path):
    path = tmp_path / "events.jsonl"
    provider = GeminiProvider(_client(), events=EventWriter(path))
    result = provider.generate(_spec(tools=[lambda: "ok"], json_schema=None))
    assert result.text
    assert [e["kind"] for e in EventReader(path).read()] == ["request", "delta", "response_end"]


def test_chat_retains_roles_and_attachments(tmp_path):
    calls = []
    client = _client()

    def stream(**kwargs):
        calls.append(kwargs)
        return iter([chunk("비공개", thought=True), chunk("답변"), chunk("입니다.", "STOP")])

    client.models.generate_content_stream = stream
    source = tmp_path / "원자료.txt"
    source.write_text("첨부 내용", encoding="utf-8")
    events = EventWriter(tmp_path / "events.jsonl")
    provider = GeminiProvider(client, events=events)
    first = generate_turn(provider, "gemini-3.8-flash", [], user_message("질문", "", [str(source)]), events)
    second = generate_turn(provider, "gemini-3.8-flash", first["history"], user_message("이어서 설명", "", []), events)
    assert [m.role for m in calls[1]["contents"]] == ["user", "model", "user"]
    assert "첨부 내용" in calls[1]["contents"][0].parts[1].text
    assert len(second["history"]) == 4
    assert second["answer"] == "답변입니다."
    assert "비공개" not in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


def test_tui_chat_stream_and_auto_collapse(tmp_path, monkeypatch):
    from textual.widgets import TextArea

    from claim_agent.tui import ClaimAgentApp, Composer
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    script = tmp_path / "fake_chat.py"
    script.write_text('''import json, os, sys, time
from pathlib import Path
p=Path(sys.argv[1]); r=json.loads(p.read_text(encoding="utf-8"))
log=Path(os.environ["CLAIM_AGENT_EVENT_LOG"])
def emit(kind, text=""):
    with log.open("a", encoding="utf-8") as f: f.write(json.dumps({"kind":kind,"role":"대화","text":text},ensure_ascii=False)+"\\n")
emit("request", r["text"])
emit("delta", "실시간 답변")
time.sleep(1.2)
answer="실시간 답변 완료"
emit("delta", " 완료")
emit("response_end")
history=r["history"]+[{"role":"user","parts":[{"text":r["text"]}]},{"role":"model","parts":[{"text":answer}]}]
(p.parent/"response.json").write_text(json.dumps({"answer":answer,"history":history,"finish_reason":"STOP"},ensure_ascii=False),encoding="utf-8")
''', encoding="utf-8")
    original = asyncio.create_subprocess_exec

    async def fake_chat(*args, **kwargs):
        if "claim_agent.chat" in args:
            args = (sys.executable, str(script), args[args.index("--request") + 1])
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_chat)

    async def scenario():
        app = ClaimAgentApp(tmp_path)
        async with app.run_test(size=(130, 45)) as pilot:
            assert not app.query_one("#log-panel").display
            for i in range(2):
                app.query_one("#request", Composer).load_text(f"질문 {i}")
                await pilot.click("#start")
                assert app.running and app.query_one("#log-panel").display
                async with asyncio.timeout(10):
                    while not app.chat_stream:
                        await asyncio.sleep(.05)
                    assert app.running
                    assert "실시간 답변" in app.query_one("#result", TextArea).text
                    while app.running:
                        await asyncio.sleep(.05)
                await pilot.pause()
                assert not app.query_one("#log-panel").display
                assert len(app.chat_history) == (i + 1) * 2
                assert not app.query_one("#request", Composer).text
                before = app.query_one("#logs", TextArea).text
                await pilot.click("#toggle-logs")
                assert app.query_one("#log-panel").display
                assert app.query_one("#logs", TextArea).text == before
            await pilot.click("#new-chat")
            assert not app.chat_history and not app.result_text
    asyncio.run(scenario())
