"""HTTP surface for 개선 / Approval Inbox: /api/improve, mine, decide, metrics."""
from __future__ import annotations

import json
import threading
from urllib.request import Request, urlopen

from claim_agent import web
from claim_agent.improve.audit import AuditLog
from claim_agent.provider.scripted import ScriptedProvider

from . import scripted_roles as R


def _workspace(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    config = tmp_path / "config.json"
    cfg = rt.cfg.model_copy(deep=True)
    cfg.paths.improve_dir = str(tmp_path / "improve")
    cfg.paths.eval_dir = str(tmp_path / "eval")
    (tmp_path / "eval" / "cases").mkdir(parents=True, exist_ok=True)
    config.write_text(cfg.model_dump_json(), encoding="utf-8")
    workspace = web.Workspace(rt.cfg.project_root, config)
    workspace.folder = tmp_path / "web-ui"
    workspace.sessions = {}
    return workspace


def test_inbox_endpoints_propose_and_approve_without_injecting(rt, request_indep, tmp_path, monkeypatch):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    state = engine.run(engine.start(request_indep, "web-improve-run"))
    assert state.outcome == "DRAFT_CLAIM_LOCK"
    state.feedback = {"text": "형상 귀속이 잘못됐습니다. 다시 봐 주세요.", "action": "meaning"}
    rt.store.save_state(state)

    workspace = _workspace(rt, tmp_path, monkeypatch)
    server = web.Server(workspace)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def get(path, headers):
        return urlopen(Request(server.origin + path, headers=headers), timeout=10)

    def post(path, body, headers):
        return urlopen(Request(server.origin + path, data=json.dumps(body).encode(),
                               headers={**headers, "Content-Type": "application/json", "X-Claim-Request": "1"}), timeout=60)

    try:
        with get("/?token=" + server.token, {}) as response:
            cookie = response.headers["Set-Cookie"].split(";")[0]
            page = response.read().decode()
            assert 'id="improve-panel"' in page and 'id="improve-mine"' in page and 'data-tab="cases"' in page
        headers = {"Cookie": cookie}

        with get("/api/improve", headers) as response:
            empty = json.load(response)
            assert empty["lessons"] == [] and empty["counts"]["pending_lessons"] == 0
            assert "escaped_to_lock_rate" in empty["metrics"]

        with post("/api/improve/mine", {}, headers) as response:
            mined = json.load(response)
            assert mined["lessons"], "사용자가 오류를 지적한 LOCK run에서 교훈 초안이 나와야 한다"
        lesson_id = mined["lessons"][0]

        with get("/api/improve", headers) as response:
            data = json.load(response)
            card = next(c for c in data["lessons"] if c["id"] == lesson_id)
            assert card["gate_status"] == "PENDING" and card["injected"] is False
            assert card["reflection"]["what_failed"] and card["impact"]["role_count"] >= 1
            assert data["counts"]["pending_lessons"] >= 1

        with post("/api/improve/decide", {"lesson_id": lesson_id, "action": "edit_approve",
                                          "text_ko": "판정 근거를 보고서에 한 줄로 남긴다.",
                                          "target_roles": ["claim-success-reviewer"], "note": "문구만 다듬음"}, headers) as response:
            approved = json.load(response)
            assert approved["gate_status"] == "CANDIDATE_APPROVED" and approved["injected"] is False
            assert approved["text_ko"] == "판정 근거를 보고서에 한 줄로 남긴다."
            assert approved["approval"]["original_proposal"]["text_ko"] != approved["text_ko"]

        # 승인했는데도 주입 집합은 비어 있다
        assert workspace.improve_service().lessons.injection_text("claim-success-reviewer") == ""
        assert workspace.improve_service().lessons.digest() == "none"

        with get("/api/improve", headers) as response:
            after = json.load(response)
            assert after["counts"]["awaiting_eval"] >= 1
            actions = [row["action"] for row in after["audit"]]
            assert "MINE" in actions and "CURATE" in actions and "LESSON_EDIT_APPROVED" in actions

        with get("/api/improve/metrics", headers) as response:
            metrics = json.load(response)
            assert metrics["locked_runs"] >= 1 and metrics["escaped_to_lock_count"] >= 1
    finally:
        server.shutdown()


def test_case_decision_promotes_into_the_regression_set(rt, tmp_path, monkeypatch):
    workspace = _workspace(rt, tmp_path, monkeypatch)
    svc = workspace.improve_service()
    seed = svc.cfg.path("eval_dir") / "cases" / "seed"
    (seed / "sources").mkdir(parents=True)
    (seed / "request.yaml").write_text("request_mode: AUTHORING_DRAFT\ncandidate_id: seed\nrequest_text: 원 요청\n", encoding="utf-8")
    (seed / "expected.yaml").write_text("outcome: DRAFT_CLAIM_LOCK\n", encoding="utf-8")
    (seed / "sources" / "invention.md").write_text("설명", encoding="utf-8")

    from claim_agent.improve.curate import EvalCandidate

    cand = EvalCandidate(case_id="adv-web", mutation_type="SCOPE_DRIFT", seed_case="seed",
                         injected_defect="범위를 바꾸는 표면 수정", failure_mode_id="FM:web",
                         expected_first_detector={"role": "claim-style-adjuster", "stage": "STYLE", "gate": "CLAIM_STYLE_GATE"},
                         must_not_pass_gates=["CLAIM_STYLE_GATE"])
    svc.candidates.write_from_seed(cand)

    card = workspace.improve_decide({"case_id": "adv-web", "action": "approve", "note": "승인"})
    assert card["status"] == "approved" and card["approval"]["approved_by"] == "web-user"
    cases = sorted(p.parent.name for p in (svc.cfg.path("eval_dir") / "cases").glob("*/request.yaml"))
    assert cases == ["adv-web", "seed"]
    assert [row["action"] for row in AuditLog(svc.improve_dir).read(action="CASE_APPROVED")]
