"""Local HTTP boundaries and actual subprocess streaming, without API charges."""
from __future__ import annotations

import gzip
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


def test_project_presets_instructions_lock_and_files_for_every_session(workspace, monkeypatch):
    project = workspace.create_project({"name": "  클립 홀더 출원  ", "instructions": "종속항은 2~4항까지.\r\n청구항만 출력.", "user_lock": "제1항 문언 유지"})
    assert project["name"] == "클립 홀더 출원" and project["instructions"] == "종속항은 2~4항까지.\n청구항만 출력."
    pid = project["id"]
    invention = workspace.project_upload(pid, "발명설명.md", "발명 원자료".encode())
    prior = workspace.project_upload(pid, "선행.txt", "선행기술".encode(), "prior_art")
    spec = workspace.project_upload(pid, "명세서.txt", "명세서 전문".encode(), "spec")
    assert (invention["category"], prior["category"], spec["category"]) == ("invention", "prior_art", "spec")
    with pytest.raises(ValueError, match="한 파일만"):
        workspace.project_upload(pid, "명세서2.txt", b"x", "spec")
    with pytest.raises(ValueError, match="도면에는"):
        workspace.project_upload(pid, "도면.txt", b"x", "drawing")
    for name, category in [(".env", "invention"), ("../x.txt", "invention"), ("a.txt", "claims")]:
        with pytest.raises(ValueError):
            workspace.project_upload(pid, name, b"data", category)
    with pytest.raises(ValueError, match="프로젝트를 찾을 수 없습니다"):
        workspace.create("f" * 32)
    first = workspace.create(pid)["id"]
    second = workspace.create(pid)["id"]
    standalone = workspace.create()["id"]
    snap = workspace.snapshot(first)
    assert snap["project"] == dict(id=pid, name="클립 홀더 출원", files=3, has_instructions=True, has_user_lock=True)
    assert workspace.snapshot(standalone)["project"] is None
    assert [s["id"] for s in workspace.project_snapshot(pid)["sessions"]] == [second, first]
    own = workspace.upload(second, "추가.txt", "대화 첨부".encode())
    commands = []

    def capture(sid, job, command):
        commands.append(command)
        job["done"] = True

    monkeypatch.setattr(workspace, "execute", capture)
    workspace.start(second, {"text": "독립항 작성해줘", "files": [own["id"]]})
    workspace.start(standalone, {"text": "안녕"})
    wait_for(lambda: len(commands) == 2)
    req = json.loads(Path(commands[0][commands[0].index("--request") + 1]).read_text(encoding="utf-8"))
    names = [Path(p).name for p in req["files"]]
    assert names == ["발명설명.md", "선행.txt", "명세서.txt", "추가.txt"]     # project presets first, then the turn's attachment
    assert req["instructions"] == "종속항은 2~4항까지.\n청구항만 출력." and req["user_lock"] == "제1항 문언 유지"
    assert req["project"] == dict(id=pid, name="클립 홀더 출원")
    categories = {a["name"]: a["category"] for a in req["attachments"]}
    assert categories == {"발명설명.md": "invention", "선행.txt": "prior_art", "명세서.txt": "spec"}
    assert all(a["project_file_id"] for a in req["attachments"]) and "추가.txt" not in categories
    plain = json.loads(Path(commands[1][commands[1].index("--request") + 1]).read_text(encoding="utf-8"))
    assert plain["files"] == [] and plain["instructions"] == "" and plain["user_lock"] == "" and plain["project"] is None
    # Survives a restart; removing a file and deleting the project keep the conversations.
    restored = web.Workspace(workspace.root)
    assert restored.projects[pid]["files"][1]["name"] == "선행.txt" and restored.sessions[first]["project_id"] == pid
    restored.project_remove_file(pid, prior["id"])
    assert [f["name"] for f in restored.project_snapshot(pid)["files"]] == ["발명설명.md", "명세서.txt"]
    assert not (restored.project_dir(pid) / "files" / prior["id"]).exists()
    restored.delete_project(pid)
    assert pid not in restored.projects and restored.sessions[first]["project_id"] is None
    assert not (workspace.folder / "projects" / pid).exists()
    assert web.Workspace(workspace.root).sessions[second]["project_id"] is None


def test_project_http_endpoints(workspace):
    import io

    from PIL import Image

    server = web.Server(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        return urlopen(Request(server.origin + path, data=data, headers=headers or {}), timeout=3)

    try:
        with call("/?token=" + server.token) as response:
            cookie = response.headers["Set-Cookie"].split(";")[0]
            page = response.read().decode()
            assert 'id="project-panel"' in page and 'id="projects"' in page and 'id="new-project"' in page
        headers = {"Cookie": cookie, "X-Claim-Request": "1"}
        with pytest.raises(HTTPError) as e:
            call("/api/project/new", {"name": "x"}, {"Cookie": cookie})
        assert e.value.code == 403
        with call("/api/project/new", {"name": "프로젝트 A", "description": "설명"}, headers) as response:
            project = json.load(response)
        pid = project["id"]
        assert project["files"] == [] and project["sessions"] == [] and project["categories"]["spec"] == "정식 명세서"
        png = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(png, format="PNG")
        with call(f"/api/project/upload?id={pid}&name=%EB%8F%84%EB%A9%B4.png&category=drawing", headers=headers, raw=png.getvalue()) as response:
            drawing = json.load(response)
            assert drawing["name"] == "도면.png" and drawing["category"] == "drawing"
        with pytest.raises(HTTPError):
            call(f"/api/project/upload?id={pid}&name=x.txt&category=claims", headers=headers, raw=b"x")
        with call("/api/project/update", {"id": pid, "instructions": "청구항만 출력"}, headers) as response:
            assert json.load(response)["instructions"] == "청구항만 출력"
        with pytest.raises(HTTPError):
            call("/api/project/update", {"id": pid, "name": "   "}, headers)
        with call("/api/new", {"project_id": pid}, headers) as response:
            sid = json.load(response)["id"]
        with pytest.raises(HTTPError):
            call("/api/new", {"project_id": "0" * 32}, headers)
        with call("/api/project?id=" + pid, headers=headers) as response:
            data = json.load(response)
            assert data["sessions"][0]["id"] == sid and data["instructions"] == "청구항만 출력" and data["files"][0]["id"] == drawing["id"]
        with call("/api/sessions", headers=headers) as response:
            listing = json.load(response)
            assert listing["projects"][0] == dict(id=pid, name="프로젝트 A", files=1, sessions=1)
            assert next(s for s in listing["sessions"] if s["id"] == sid)["project_id"] == pid
        with call("/api/session?id=" + sid, headers=headers) as response:
            assert json.load(response)["project"]["name"] == "프로젝트 A"
        with call("/api/project/remove-file", {"id": pid, "file": drawing["id"]}, headers) as response:
            assert json.load(response)["ok"]
        with call("/api/project/delete", {"id": pid}, headers) as response:
            assert json.load(response)["ok"]
        with pytest.raises(HTTPError):
            call("/api/project?id=" + pid, headers=headers)
        with call("/api/session?id=" + sid, headers=headers) as response:
            assert json.load(response)["project"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_composer_claim_target_reaches_ui_hints_and_message(workspace, monkeypatch):
    sid = workspace.create()["id"]
    commands = []

    def capture(sid, job, command):
        commands.append(command)
        job["done"] = True

    monkeypatch.setattr(workspace, "execute", capture)
    workspace.start(sid, {"mode": "AUTHORING_DRAFT", "text": "청구항 작성", "target_mode": "single", "target_claim": 3})
    wait_for(lambda: len(commands) == 1)
    req = json.loads(Path(commands[0][commands[0].index("--request") + 1]).read_text(encoding="utf-8"))
    assert req["ui_hints"] == dict(selected_mode="AUTHORING_DRAFT", dependent=True, target="3", target_mode="single")
    assert workspace.snapshot(sid)["messages"][-2]["claim_target"] == dict(mode="single", dependent=True, target="3")
    workspace.start(sid, {"mode": "AUTHORING_DRAFT", "text": "청구항 작성", "target_mode": "range", "target_segments": [{"from": 2, "to": 4}, {"from": 6, "to": 6}]})
    wait_for(lambda: len(commands) == 2)
    req = json.loads(Path(commands[1][commands[1].index("--request") + 1]).read_text(encoding="utf-8"))
    assert req["ui_hints"]["target"] == "2~4,6" and req["ui_hints"]["target_mode"] == "range"
    workspace.start(sid, {"text": "안녕", "dependent": False, "target": "2~8"})      # legacy composer payload
    wait_for(lambda: len(commands) == 3)
    req = json.loads(Path(commands[2][commands[2].index("--request") + 1]).read_text(encoding="utf-8"))
    assert req["ui_hints"] == dict(selected_mode="CHAT", dependent=False, target="", target_mode="independent")
    with pytest.raises(ValueError, match="2항 이상"):
        workspace.start(sid, {"mode": "AUTHORING_DRAFT", "text": "청구항 작성", "target_mode": "range", "target_segments": [{"from": 1, "to": 3}]})
    assert len(workspace.sessions[sid]["messages"]) == 6


def test_delete_session_keeps_others_and_refuses_while_running(workspace, fake_chat):
    keep = workspace.create()["id"]
    gone = workspace.create()["id"]
    workspace.upload(gone, "자료.txt", b"x")
    folder = workspace.directory(gone)
    workspace.start(gone, {"text": "stop"})
    wait_for(lambda: workspace.snapshot(gone)["running"])
    with pytest.raises(ValueError, match="응답 중"):
        workspace.delete(gone)
    workspace.stop(gone)
    wait_for(lambda: not workspace.snapshot(gone)["running"])
    workspace.delete(gone)
    assert gone not in workspace.sessions and gone not in workspace.jobs and not folder.exists()
    assert keep in workspace.sessions and workspace.directory(keep).exists()
    with pytest.raises(ValueError):
        workspace.delete(gone)
    assert gone not in web.Workspace(workspace.root).sessions


def test_launcher_restarts_a_server_built_from_other_code(workspace, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("launch_web", Path(__file__).resolve().parents[1] / "scripts" / "launch_web.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    assert launcher.current_code_version(Path(__file__).resolve().parents[1]) == web.code_fingerprint()
    server = web.Server(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    metadata = tmp_path / "server.json"
    web.save_json(metadata, dict(pid=1, origin=server.origin, token=server.token, code_version=web.code_fingerprint()))
    try:
        assert launcher.existing_server(metadata) == server.origin + "/?token=" + server.token
        assert launcher.existing_server(metadata, web.code_fingerprint()) == server.origin + "/?token=" + server.token
        assert launcher.existing_server(metadata, "0000000000000000") is None       # asked the stale server to shut down
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert launcher.existing_server(metadata, web.code_fingerprint()) is None
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()


def test_sessions_endpoint_reports_code_version_busy_and_delete(workspace):
    server = web.Server(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    headers = {"X-Claim-Token": server.token, "X-Claim-Request": "1"}

    def call(path, body=None):
        return urlopen(Request(server.origin + path, data=json.dumps(body).encode() if body is not None else None, headers=headers), timeout=3)

    try:
        sid = workspace.create()["id"]
        with call("/api/sessions") as response:
            data = json.load(response)
            assert data["code_version"] == web.code_fingerprint() and data["busy"] == 0 and [s["id"] for s in data["sessions"]] == [sid]
        with call("/api/delete", {"id": sid}) as response:
            assert json.load(response)["ok"]
        with pytest.raises(HTTPError):
            call("/api/delete", {"id": sid})
        with call("/api/sessions") as response:
            assert json.load(response)["sessions"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_live_log_is_sent_as_a_delta_and_resets_only_on_a_gap(workspace, request):
    sid = workspace.create()["id"]
    job = dict(done=True, log="", log_base=0)
    workspace.jobs[sid] = job                       # a stand-in for a finished job: only its log matters here
    request.addfinalizer(lambda: workspace.jobs.pop(sid, None))
    job["log"] = "가" * 10
    workspace.trim_log(job)
    first = workspace.snapshot(sid)
    assert first["log_reset"] and first["live_log"] == "가" * 10 and first["log_cursor"] == 10
    job["log"] += "나" * 5 + "offline-web-secret"
    workspace.trim_log(job)
    delta = workspace.snapshot(sid, first["log_cursor"])
    assert delta["live_log"] == "나" * 5 + "[API KEY]" and not delta["log_reset"]
    assert delta["log_cursor"] == 10 + len(delta["live_log"])
    assert workspace.snapshot(sid, delta["log_cursor"])["live_log"] == ""       # nothing new: an empty delta
    # The window slides: a cursor that fell off the front gets the whole window back, never a stitched gap.
    job["log"] += "다" * web.MAX_LIVE_LOG
    workspace.trim_log(job)
    over = workspace.snapshot(sid, 1)
    assert over["log_reset"] and len(over["live_log"]) == web.MAX_LIVE_LOG
    assert over["log_cursor"] == delta["log_cursor"] + web.MAX_LIVE_LOG
    tail = workspace.snapshot(sid, over["log_cursor"] - 3)
    assert tail["live_log"] == "다다다" and not tail["log_reset"]
    assert workspace.snapshot(sid, 10**9)["log_reset"]                          # a cursor from an earlier job
    fresh = workspace.snapshot(workspace.create()["id"], 0)                     # a session that never ran a job
    assert fresh["live_log"] == "" and fresh["log_cursor"] == 0


def test_polling_payload_stays_flat_while_the_log_grows(workspace, fake_chat):
    server = web.Server(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    headers = {"X-Claim-Token": server.token, "Accept-Encoding": "gzip"}

    def poll(sid, cursor=None):
        """(bytes on the wire, bytes of JSON, parsed body)."""
        path = f"/api/session?id={sid}" + (f"&log_from={cursor}" if cursor is not None else "")
        with urlopen(Request(server.origin + path, headers=headers), timeout=3) as response:
            raw = response.read()
            body = gzip.decompress(raw) if response.headers.get("Content-Encoding") == "gzip" else raw
            return len(raw), len(body), json.loads(body.decode("utf-8"))

    try:
        sid = workspace.create()["id"]
        workspace.start(sid, {"text": "stop"})
        wait_for(lambda: workspace.snapshot(sid)["running"])
        job = workspace.jobs[sid]
        with workspace.lock:
            job["log"] += "긴 실시간 로그 " * 20000
            workspace.trim_log(job)
        wire_full, json_full, full = poll(sid)
        assert full["log_reset"] and len(full["live_log"]) > 100000
        assert json_full > 100000 and wire_full * 4 < json_full                 # gzip is applied on the wire
        with workspace.lock:
            job["log"] += "새 줄\n"
            workspace.trim_log(job)
        wire_delta, json_delta, delta = poll(sid, full["log_cursor"])
        assert delta["live_log"] == "새 줄\n" and not delta["log_reset"]
        assert json_delta < 2000 and json_delta * 50 < json_full                # payload no longer tracks log length
        assert wire_delta < 1000
        workspace.stop(sid)
        wait_for(lambda: not workspace.snapshot(sid)["running"])
        done = poll(sid, delta["log_cursor"])[2]                                # the console tail of the stopped job
        assert len(done["live_log"]) < 2000 and not done["log_reset"]
        idle = poll(sid, done["log_cursor"])[2]                                 # a finished job stops re-sending it
        assert idle["live_log"] == "" and idle["log_cursor"] == done["log_cursor"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
