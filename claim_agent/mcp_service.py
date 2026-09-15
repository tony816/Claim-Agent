"""Claim-Agent operations for the remote MCP server: the web workspace's conversations without its HTTP layer.

Every tool goes through `web.Workspace`, so a request from Claude takes the same path as one typed in the browser:
the router classifies it, the gated pipeline runs in a child process, and answers, logs and runs are stored in the
same folders. Nothing here decides a route, writes claim text or reports a gate the pipeline did not record.
"""
from __future__ import annotations

import base64
import binascii
import time
from pathlib import Path

from .tui_support import RESUME_KINDS
from .web import Workspace

MAX_WAIT_S = 120         # one status call blocks at most this long; claude.ai allows 240 s per tool call
POLL_S = 1.0


class ClaimAgentService:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    @classmethod
    def open(cls, project_root: Path, config: Path | None = None) -> ClaimAgentService:
        return cls(Workspace(project_root, config))

    # ---- conversations

    def list_conversations(self, limit: int = 20) -> list[dict]:
        ws = self.workspace
        with ws.lock:
            sessions = sorted(ws.sessions.values(), key=lambda s: s["updated"], reverse=True)[:max(1, min(limit, 100))]
            return [dict(conversation_id=s["id"], title=s["title"], updated=s["updated"], run_id=s.get("run_id"),
                         running=bool(ws.jobs.get(s["id"]) and not ws.jobs[s["id"]]["done"])) for s in sessions]

    def new_conversation(self) -> dict:
        session = self.workspace.create()
        return dict(conversation_id=session["id"])

    # ---- attachments

    def attach_text(self, conversation_id: str, name: str, content: str) -> dict:
        """A UTF-8 text material (.txt/.md/...) as a conversation file; its id is passed to `send`."""
        item = self.workspace.upload(conversation_id, name, content.replace("\r\n", "\n").encode("utf-8"))
        return dict(attachment_id=item["id"], name=item["name"], size=item["size"])

    def attach_file(self, conversation_id: str, name: str, content_base64: str) -> dict:
        """A binary material (PDF, DOCX, HWPX, HWP, PNG, JPG, WEBP) sent as base64."""
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("content_base64가 올바른 base64가 아닙니다.") from None
        item = self.workspace.upload(conversation_id, name, data)
        return dict(attachment_id=item["id"], name=item["name"], size=item["size"])

    # ---- requests

    def send(self, conversation_id: str, text: str, attachment_ids: list[str] | None = None, target_mode: str | None = None,
             target_claim: int | None = None, target_segments: str | None = None) -> dict:
        """Start one turn. `target_mode` (independent | single | range) is the composer's 작성 대상, a hint the router
        and the scope guard use; without it the request is classified from the text alone."""
        data: dict = dict(text=text, files=list(attachment_ids or []), mode="CHAT")
        if target_mode is not None:
            data.update(mode="AUTHORING_DRAFT", target_mode=target_mode, target_claim=target_claim, target_segments=target_segments)
        self.workspace.start(conversation_id, data)
        return dict(message_id=self._last_assistant(conversation_id)["id"], status="running")

    def resume(self, conversation_id: str, kind: str, decision: str = "", attachment_ids: list[str] | None = None,
               scope: str | None = None) -> dict:
        """Continue the conversation's halted or finished run with a user decision (see RESUME_KINDS)."""
        if kind not in RESUME_KINDS:
            raise ValueError("kind는 " + ", ".join(RESUME_KINDS) + " 중 하나여야 합니다.")
        self.workspace.resume(conversation_id, dict(kind=kind, text=decision, files=list(attachment_ids or []), scope=scope))
        return dict(message_id=self._last_assistant(conversation_id)["id"], status="running")

    def stop(self, conversation_id: str) -> dict:
        self.workspace.stop(conversation_id)
        return dict(stopped=True)

    # ---- results

    def status(self, conversation_id: str, message_id: str | None = None, wait_seconds: float = 0) -> dict:
        """The turn's state; once it is no longer running, the answer as shown in the web conversation.

        `wait_seconds` (capped at MAX_WAIT_S) blocks until the turn finishes or the time is up, so a caller polling a
        multi-minute pipeline run makes a few calls rather than hundreds.
        """
        deadline = time.monotonic() + max(0.0, min(float(wait_seconds or 0), MAX_WAIT_S))
        while True:
            message = self._message(conversation_id, message_id)
            if message.get("status") != "running" or time.monotonic() >= deadline:
                break
            time.sleep(POLL_S)
        ws = self.workspace
        with ws.lock:
            run_id = message.get("pipeline_run_id") or ws.sessions[conversation_id].get("run_id")
            run = ws.run_status(run_id)
        finished = message.get("status") != "running"
        return dict(message_id=message["id"], status=message.get("status"), route=message.get("execution_mode"),
                    answer=message.get("text") if finished else None, run=run,
                    next=None if finished else "status를 wait_seconds와 함께 다시 호출하세요. 파이프라인 실행은 수 분이 걸립니다.")

    def full_report(self, conversation_id: str, message_id: str | None = None) -> dict:
        """The complete report (gate tables, each role's own report) of the turn's pipeline run."""
        ws = self.workspace
        message = self._message(conversation_id, message_id)
        with ws.lock:
            run_id = message.get("pipeline_run_id") or ws.sessions[conversation_id].get("run_id")
            if not run_id or not ws.run_status(run_id):
                raise ValueError("이 응답에는 파이프라인 실행 보고서가 없습니다.")
            path = ws.cfg.path("runs_dir") / run_id / "report.md"
            return dict(run_id=run_id, report=ws.redact(path.read_text(encoding="utf-8")) if path.exists() else "보고서가 아직 없습니다.")

    # ---- helpers

    def _last_assistant(self, conversation_id: str) -> dict:
        return self._message(conversation_id, None)

    def _message(self, conversation_id: str, message_id: str | None) -> dict:
        ws = self.workspace
        with ws.lock:
            ws.directory(conversation_id)
            messages = [m for m in ws.sessions[conversation_id]["messages"] if m["role"] == "assistant"]
            found = next((m for m in messages if m["id"] == message_id), None) if message_id else (messages[-1] if messages else None)
            if not found:
                raise ValueError("응답을 찾을 수 없습니다.")
            return dict(found)
