"""The remote MCP server's operations run on the web workspace, offline (fake chat worker, no API charges)."""
from __future__ import annotations

import json

import pytest

from claim_agent.mcp_service import ClaimAgentService
from tests.test_web import fake_chat, wait_for, workspace  # noqa: F401 - pytest fixtures


def test_send_then_status_waits_for_the_same_answer_the_web_shows(workspace, fake_chat):  # noqa: F811
    service = ClaimAgentService(workspace)
    cid = service.new_conversation()["conversation_id"]
    started = service.send(cid, "한글 질문")
    assert started["status"] == "running"
    running = service.status(cid, started["message_id"])          # no wait: returns at once, tells the caller to poll
    assert running["status"] == "running" and running["answer"] is None and "wait_seconds" in running["next"]
    done = service.status(cid, started["message_id"], wait_seconds=20)
    assert done["status"] == "complete" and done["answer"] == "한글 답변 완료"
    assert done["answer"] == workspace.snapshot(cid)["messages"][-1]["text"]
    listed = service.list_conversations()
    assert listed[0]["conversation_id"] == cid and listed[0]["running"] is False


def test_attachments_reach_the_request_and_bad_input_is_refused(workspace, fake_chat):  # noqa: F811
    service = ClaimAgentService(workspace)
    cid = service.new_conversation()["conversation_id"]
    item = service.attach_text(cid, "claims.md", "【청구항 1】\r\n장치.")
    assert (workspace.directory(cid) / "uploads" / item["attachment_id"] / "claims.md").read_bytes() == "【청구항 1】\n장치.".encode()
    with pytest.raises(ValueError, match="base64"):
        service.attach_file(cid, "drawing.png", "not base64!")
    with pytest.raises(ValueError, match=".env"):
        service.attach_text(cid, ".env", "GEMINI_API_KEY=x")
    message = service.send(cid, "청구항 검토", attachment_ids=[item["attachment_id"]], target_mode="single", target_claim=1)
    service.status(cid, message["message_id"], wait_seconds=20)
    requests = sorted((workspace.root / ".tui" / "requests").glob("web-*/chat.json"))
    request = json.loads(requests[-1].read_text(encoding="utf-8"))
    assert any(f.endswith("claims.md") for f in request["files"])
    assert request["ui_hints"]["selected_mode"] == "AUTHORING_DRAFT" and request["ui_hints"]["target_mode"] == "single"
    with pytest.raises(ValueError, match="응답을 찾을 수 없습니다"):
        service.status(cid, "0" * 32)
    with pytest.raises(ValueError, match="kind"):
        service.resume(cid, "delete-everything")
    with pytest.raises(ValueError, match="대화를 찾을 수 없습니다"):
        service.status("f" * 32)
