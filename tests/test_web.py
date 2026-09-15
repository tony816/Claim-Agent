"""Local HTTP boundaries and actual subprocess streaming, without API charges."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from claim_agent import web


def wait_for(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.04)
    raise AssertionError("Operation did not complete")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    workspace = web.Workspace(tmp_path)
    yield workspace
    workspace.close()


@pytest.fixture
def fake_chat(tmp_path, monkeypatch):
    script = tmp_path / "chat_worker.py"
    script.write_text('''import json, os, sys, time
from pathlib import Path
p=Path(sys.argv[1]); req=json.loads(p.read_text(encoding="utf-8"))
log=Path(os.environ["CLAIM_AGENT_EVENT_LOG"])
def emit(kind,text):
    with log.open("a",encoding="utf-8") as f:
        f.write(json.dumps(dict(kind=kind,role="대화",text=text),ensure_ascii=False)+"\\n")
emit("request",req["text"])
emit("delta","한글 답변")
time.sleep(0.8 if req["text"]!="stop" else 120)
emit("delta"," 완료")
answer="한글 답변 완료"
history=req["history"]+[dict(role="user",parts=[dict(text=req["text"])]),dict(role="model",parts=[dict(text=answer)])]
(p.parent/"response.json").write_text(json.dumps(dict(answer=answer,history=history),ensure_ascii=False),encoding="utf-8")
''', encoding="utf-8")
    original = subprocess.Popen

    def spawn(command, **kwargs):
        if "claim_agent.chat" in command:
            command = [sys.executable, str(script), command[command.index("--request") + 1]]
        return original(command, **kwargs)

    monkeypatch.setattr(web.subprocess, "Popen", spawn)


def test_stream_followup_restart_and_stop(workspace, fake_chat):
    sid = workspace.create()["id"]
    for question in ["한글 질문", "이 답변을 짧게 수정해 줘"]:
        workspace.start(sid, {"text": question})
        wait_for(lambda: "한글 답변" in workspace.snapshot(sid)["messages"][-1]["text"])
        assert workspace.snapshot(sid)["running"]
        with pytest.raises(ValueError, match="현재 응답"):
            workspace.start(sid, {"text": "duplicate"})
        wait_for(lambda: not workspace.snapshot(sid)["running"])
        assert workspace.snapshot(sid)["messages"][-1]["status"] == "complete"
    history = json.loads((workspace.directory(sid) / "history.json").read_text(encoding="utf-8"))["history"]
    assert len(history) == 4 and history[2]["parts"][0]["text"].startswith("이 답변")
    restored = web.Workspace(workspace.root)
    assert len(restored.snapshot(sid)["messages"]) == 4
    workspace.start(sid, {"text": "stop"})
    wait_for(lambda: workspace.snapshot(sid)["messages"][-1]["text"])
    workspace.stop(sid)
    wait_for(lambda: not workspace.snapshot(sid)["running"])
    assert workspace.snapshot(sid)["messages"][-1]["status"] == "stopped"
    assert workspace.jobs[sid]["process"].poll() is not None
    assert len(json.loads((workspace.directory(sid) / "history.json").read_text(encoding="utf-8"))["history"]) == 4


def test_http_auth_upload_isolation_and_unicode(workspace):
    server = web.Server(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, body=None, headers=None):
        req = Request(server.origin + path, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers or {})
        return urlopen(req, timeout=3)

    try:
        with pytest.raises(HTTPError) as e:
            call("/api/sessions")
        assert e.value.code == 403
        with call("/?token=" + server.token) as response:
            cookie = response.headers["Set-Cookie"].split(";")[0]
            assert "HttpOnly" in response.headers["Set-Cookie"]
            assert "메시지 입력" in response.read().decode()
        headers = {"Cookie": cookie, "X-Claim-Request": "1"}
        with pytest.raises(HTTPError):
            call("/api/new", {}, {**headers, "Origin": "https://evil.example"})
        with pytest.raises(HTTPError):
            call("/api/new", {}, {"Cookie": cookie})
        with call("/api/new", {}, headers) as response:
            sid = json.load(response)["id"]
        item = workspace.upload(sid, "한글 자료.txt", "원자료".encode())
        for name in ["../secret.txt", ".env", ".env.txt", "x.exe", "C:\\secret.txt", "secret.txt:stream"]:
            with pytest.raises(ValueError):
                workspace.upload(sid, name, b"data")
        other = workspace.create()["id"]
        with pytest.raises(ValueError, match="첨부"):
            workspace.start(other, {"text": "질문", "files": [item["id"]]})
        with call("/api/session?id=" + sid, headers=headers) as response:
            data = json.load(response)
            assert data["files"][0]["name"] == "한글 자료.txt"
        with pytest.raises(HTTPError):
            call("/.env", headers=headers)
        with call("/api/sessions", headers=headers) as response:
            assert b"offline-web-secret" not in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_draft_uses_earlier_user_material_and_uploads(workspace, monkeypatch):
    sid = workspace.create()["id"]
    file = workspace.upload(sid, "발명.txt", "발명 원자료".encode())
    workspace.sessions[sid]["messages"] = [dict(id="earlier", role="user", text="기존에 설명한 발명", files=[file])]
    commands = []

    def capture(sid, job, command):
        commands.append(command)
        job["done"] = True

    monkeypatch.setattr(workspace, "execute", capture)
    workspace.start(sid, {"mode": "AUTHORING_DRAFT", "text": "이걸로 독립항 작성해줘"})
    wait_for(lambda: commands)
    assert "claim_agent.chat" in commands[0]
    req = json.loads(Path(commands[0][commands[0].index("--request") + 1]).read_text(encoding="utf-8"))
    assert any(Path(p).name == "발명.txt" for p in req["files"])
    assert "기존에 설명한 발명" in req["material"]
    assert req["ui_hints"]["selected_mode"] == "AUTHORING_DRAFT"


def test_auto_chat_keeps_user_sources_separate_from_generated_reports(workspace, monkeypatch):
    sid = workspace.create()["id"]
    file = workspace.upload(sid, "원문.txt", "청구항 원문".encode())
    workspace.sessions[sid]["messages"] = [
        dict(id="u", role="user", text="사용자 발명 설명", files=[file]),
        dict(id="a", role="assistant", text="모델 작성 보고서", mode="AUTHORING_DRAFT", status="complete"),
    ]
    commands = []
    def capture(sid, job, command):
        commands.append(command)
        job["done"] = True
    monkeypatch.setattr(workspace, "execute", capture)
    workspace.start(sid, {"text": "그렇게 수정해줘", "mode": "CHAT"})
    wait_for(lambda: commands)
    req = json.loads(Path(commands[0][commands[0].index("--request") + 1]).read_text(encoding="utf-8"))
    assert req["material"] == "사용자 발명 설명"
    assert req["reference"] == "모델 작성 보고서"
    assert Path(req["files"][0]).name == "원문.txt"


def test_role_json_is_logged_without_streaming_into_answer(workspace, tmp_path):
    from claim_agent.live_events import EventReader, EventWriter
    path = tmp_path / "route-events.jsonl"
    writer = EventWriter(path)
    writer.emit("route", role="요청 분류", mode="REVIEW_ONLY", reason="특허 의견")
    writer.emit("delta", role="oa-strategy-reviewer", text='{"status":"UNVERIFIED"}')
    job, message = dict(mode="CHAT", log=""), dict(text="")
    workspace.consume_events(job, message, EventReader(path))
    assert message["text"] == ""
    assert message["execution_mode"] == "REVIEW_ONLY"
    assert "UNVERIFIED" in job["log"]


def test_manual_advice_does_not_bypass_router_or_resume_authoring(workspace, monkeypatch):
    from tests.test_routing import ADVICE_REQUEST
    sid = workspace.create()["id"]
    workspace.sessions[sid]["run_id"] = "old-authoring-run"
    calls = []
    def capture(sid, job, command):
        calls.append((job, command))
        job["done"] = True
    monkeypatch.setattr(workspace, "execute", capture)
    workspace.start(sid, dict(text=ADVICE_REQUEST, mode="AUTHORING_DRAFT", dependent=True, target="2~8"))
    wait_for(lambda: calls)
    job, command = calls[0]
    assert "claim_agent.chat" in command and "resume" not in command
    assert job["mode"] == "CHAT"
    req = json.loads(Path(command[command.index("--request") + 1]).read_text(encoding="utf-8"))
    assert req["text"] == ADVICE_REQUEST and req["run_id"] == "old-authoring-run"
    assert req["ui_hints"]["selected_mode"] == "AUTHORING_DRAFT"
    assert workspace.sessions[sid]["run_id"] == "old-authoring-run"


def test_web_auto_draft_records_effective_mode_and_preserves_run(rt, request_indep, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    config = tmp_path / "auto-config.json"
    config.write_text(rt.cfg.model_dump_json(), encoding="utf-8")
    original = subprocess.Popen
    def scripted(command, **kwargs):
        if "claim_agent.chat" in command:
            program = "from claim_agent import chat,routing,runtime; from claim_agent.routing import RouteDecision; from claim_agent.provider.scripted import ScriptedProvider; from tests import scripted_roles as R; routing.classify_request=lambda *a: RouteDecision(mode='AUTHORING_DRAFT',reason='청구항 수정 요청'); runtime.make_provider=lambda *a,**k: ScriptedProvider(R.happy_script(dependent=False)); raise SystemExit(chat.main())"
            command = [sys.executable, "-u", "-c", program, *command[command.index("claim_agent.chat") + 1:]]
        return original(command, **kwargs)
    monkeypatch.setattr(web.subprocess, "Popen", scripted)
    workspace = web.Workspace(rt.cfg.project_root, config)
    workspace.folder = tmp_path / "auto-web-ui"
    workspace.sessions = {}
    try:
        sid = workspace.create()["id"]
        for prompt in ["첨부한 발명으로 청구항 작성해줘", "그렇게 수정해줘"]:
            workspace.start(sid, {"text": prompt, "mode": "CHAT"})
            wait_for(lambda: not workspace.snapshot(sid)["running"], timeout=30)
            message = workspace.snapshot(sid)["messages"][-1]
            assert message["status"] == "complete", message["text"]
            assert message["execution_mode"] == "AUTHORING_DRAFT"
            assert message["pipeline_run_id"] == workspace.sessions[sid]["run_id"]
        state = rt.store.load_state(workspace.sessions[sid]["run_id"])
        assert state.candidate.revision == "r2" and state.candidate.design_revision == "d2"
        assert state.candidate.draft_claim_lock
    finally:
        workspace.close()


def test_web_feedback_resumes_and_rechecks_same_run(rt, request_indep, tmp_path, monkeypatch):
    from claim_agent.provider.scripted import ScriptedProvider
    from tests import scripted_roles as R
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    initial = engine.run(engine.start(request_indep, "web-feedback-existing"))
    assert initial.outcome == "DRAFT_CLAIM_LOCK"
    config = tmp_path / "config.json"
    config.write_text(rt.cfg.model_dump_json(), encoding="utf-8")
    original = subprocess.Popen

    def scripted(command, **kwargs):
        if "claim_agent.chat" in command:
            program = "from claim_agent import chat,routing,runtime; from claim_agent.routing import RouteDecision; from claim_agent.provider.scripted import ScriptedProvider; from tests import scripted_roles as R; routing.classify_request=lambda *a: RouteDecision(mode='AUTHORING_DRAFT',reason='수정 요청'); runtime.make_provider=lambda *a,**k: ScriptedProvider(R.happy_script(dependent=False)); raise SystemExit(chat.main())"
            command = [sys.executable, "-u", "-c", program, *command[command.index("claim_agent.chat") + 1:]]
        return original(command, **kwargs)

    monkeypatch.setattr(web.subprocess, "Popen", scripted)
    workspace = web.Workspace(rt.cfg.project_root, config)
    workspace.folder = tmp_path / "web-ui"
    workspace.sessions = {}
    try:
        sid = workspace.create()["id"]
        workspace.sessions[sid]["run_id"] = initial.run_id
        workspace.start(sid, {"text": "부품 연결 관계를 다시 검토해서 수정해줘", "mode": "AUTHORING_DRAFT"})
        wait_for(lambda: not workspace.snapshot(sid)["running"], timeout=30)
        state = json.loads((rt.cfg.path("runs_dir") / initial.run_id / "state.json").read_text(encoding="utf-8"))
        assert state["candidate"]["revision"] == "r2"
        assert state["candidate"]["design_revision"] == "d2"
        assert state["outcome"] == "DRAFT_CLAIM_LOCK"
        assert workspace.snapshot(sid)["messages"][-1]["status"] == "complete"
    finally:
        workspace.close()
