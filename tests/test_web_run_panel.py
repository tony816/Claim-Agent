"""HTTP surface for the run panel: session snapshot run status, /api/diff, /api/export."""
from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from claim_agent import web
from claim_agent.provider.scripted import ScriptedProvider

from . import scripted_roles as R


def web_halt():
    from claim_agent.models.state import Halt

    return Halt(stage="SYNTAX", role="syntax-scope-reviewer", kind="REVIEW", reason_code="TEST_DECISION", message="결정 필요")


def test_run_panel_endpoints(rt, request_indep, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(return_to="RETURN_TO_DRAFTER"), R.style(text=R.ROOT_CLAIM.replace("조임 나사", "조임나사"))]
    script["claim-drafter"] = [R.drafter(), R.drafter()]
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "panel-run"))
    assert state.outcome == "DRAFT_CLAIM_LOCK"
    config = tmp_path / "config.json"
    config.write_text(rt.cfg.model_dump_json(), encoding="utf-8")
    workspace = web.Workspace(rt.cfg.project_root, config)
    workspace.folder = tmp_path / "web-ui"
    workspace.sessions = {}
    server = web.Server(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, headers):
        return urlopen(Request(server.origin + path, headers=headers), timeout=5)

    try:
        with call("/?token=" + server.token, {}) as response:
            cookie = response.headers["Set-Cookie"].split(";")[0]
            page = response.read().decode()
            assert 'id="run-panel"' in page and 'id="resume-form"' not in page and "export-docx" in page
        headers = {"Cookie": cookie}
        sid = workspace.create()["id"]
        workspace.sessions[sid]["run_id"] = "panel-run"
        with call("/api/session?id=" + sid, headers) as response:
            snap = json.load(response)
            run = snap["run"]
            assert run["outcome"] == "DRAFT_CLAIM_LOCK" and run["revision"] == "r2" and run["halt"] is None
            assert run["usage"]["calls"] > 0 and any(s["stage"] == "PICTURE" for s in run["stages"]) and run["draft_claim_lock"]
        with call("/api/diff?id=" + sid, headers) as response:
            text = json.load(response)["text"]
            assert "## 기존 문언" in text and "## 제안 문언" in text and "r1 → r2" in text
        with call("/api/export?id=" + sid + "&format=md", headers) as response:
            body = response.read().decode()
            assert response.headers["Content-Disposition"].startswith("attachment;") and R.ROOT_CLAIM.replace("조임 나사", "조임나사") in body.replace("\r\n", "\n")
        with call("/api/export?id=" + sid + "&format=docx&evidence=1", headers) as response:
            assert response.headers["Content-Type"].startswith("application/vnd.openxmlformats") and response.read()[:2] == b"PK"
        sid2 = workspace.create()["id"]
        workspace.sessions[sid2]["run_id"] = "panel-run"
        workspace.sessions[sid2]["messages"] = [dict(id="m1", role="assistant", text="요약", status="complete", pipeline_run_id="panel-run")]
        with call(f"/api/report?id={sid2}&message=m1", headers) as response:
            report = json.load(response)["text"]
            assert "## 성능 요약" in report and "| 단계 | 역할 | record_id |" in report
        with pytest.raises(HTTPError):                       # a conversation with no pipeline run has no report
            call(f"/api/report?id={workspace.create()['id']}&message=m1", headers)
        assert "app.js" in (web.ASSETS / "index.html").read_text(encoding="utf-8")
        js = (web.ASSETS / "app.js").read_text(encoding="utf-8")
        assert "renderRun" in js and "/api/resume" not in js and "/api/diff" in js and "/api/report" in js
        sid3 = workspace.create()["id"]
        workspace.sessions[sid3]["run_id"] = "panel-run"
        state.halt = web_halt()
        rt.store.save_state(state)
        resumed = []
        monkeypatch.setattr(workspace, "resume", lambda s, d: resumed.append((s, d)))
        workspace.start(sid3, {"text": "미검증 수용", "files": []})
        assert workspace.jobs.get(sid3) is None
        # Moderator on: a plain message on a halted run is read first, with the halt attached; the run keeps waiting.
        started = []
        monkeypatch.setattr(workspace, "require_connection", lambda *a: None)
        monkeypatch.setattr(workspace, "execute", lambda s, job, command: (started.append(job), job.update(done=True)))
        workspace.start(sid3, {"text": "왜 멈췄어?", "files": []})
        workspace.jobs[sid3]["thread"].join()
        packet = json.loads((started[0]["folder"] / "chat.json").read_text(encoding="utf-8"))
        assert packet["ui_hints"]["halted"]["stage"] == state.halt.stage and [d["kind"] for _, d in resumed] == ["accept_unverified"]
        # Moderator off: the message is the halted run's decision, as before.
        monkeypatch.setattr(workspace.cfg.conversation, "moderator", "off")
        workspace.start(sid3, {"text": "오목면의 주체는 기둥 둘레면", "files": []})
        assert [d["kind"] for _, d in resumed] == ["accept_unverified", "none"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        workspace.close()
