"""Loopback-only chat workspace. The existing CLI owns all model/pipeline work."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .claim_scope import resolve_claim_target
from .config import load_config
from .live_events import EventReader
from .sources.extract import DOC_EXT, ExtractionError, extract_text
from .tui_support import CATEGORIES, RESUME_KINDS, child_options, read_state, resume_command, validate_attachment

ASSETS = Path(__file__).with_name("web_assets")
MAX_UPLOAD = 20 * 1024 * 1024
MAX_LIVE_LOG = 300000       # sliding window of the live log kept in memory and rendered in the browser
MIN_GZIP = 1400             # responses above this are gzipped when the browser accepts it
MAX_PROJECT_TEXT = 100000
PROJECT_TEXT_FIELDS = ("name", "description", "instructions", "user_lock")


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def code_fingerprint(package_dir: Path | None = None) -> str:
    """Content hash of the installed claim_agent package (web server, assets and every module the child
    processes import). The launcher restarts a live server whose fingerprint differs from the checkout."""
    import hashlib

    base = package_dir or Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".html", ".js", ".css"} and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(base)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


class Workspace:
    def __init__(self, root: Path, config: Path | None = None):
        self.root = root.resolve()
        self.config = config.resolve() if config else None
        self.cfg = load_config(self.config, self.root)
        self.folder = self.root / ".tui" / "web"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        for path in (self.folder / "projects").glob("*/project.json"):
            data = read_state(path)
            if data and re.fullmatch(r"[a-f0-9]{32}", str(data.get("id", ""))):
                self.projects[data["id"]] = data
        for path in (self.folder / "sessions").glob("*/session.json"):
            data = read_state(path)
            if data:
                for message in data["messages"]:
                    if message.get("status") == "running":
                        message["status"] = "stopped"
                        message["text"] += "\n\n[프로그램이 종료되어 응답이 중단되었습니다.]"
                if data.get("project_id") not in self.projects:
                    data["project_id"] = None   # The project folder was removed; the conversation stays.
                self.sessions[data["id"]] = data

    def redact(self, text: str) -> str:
        for name in {self.cfg.model.api_key_env, "GEMINI_API_KEY", "GOOGLE_API_KEY"}:
            value = os.environ.get(name)
            if value:
                text = text.replace(value, "[API KEY]")
        return text

    def directory(self, sid: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", sid) or sid not in self.sessions:
            raise ValueError("대화를 찾을 수 없습니다.")
        return self.folder / "sessions" / sid

    def persist(self, session: dict) -> None:
        save_json(self.directory(session["id"]) / "session.json", session)

    def delete(self, sid: str) -> None:
        """Remove a conversation and its uploads; pipeline runs under runs/ are kept for the audit trail."""
        import shutil

        with self.lock:
            folder = self.directory(sid)
            job = self.jobs.get(sid)
            if job and not job["done"]:
                raise ValueError("응답 중인 대화입니다. 먼저 중지한 뒤 삭제하세요.")
            self.jobs.pop(sid, None)
            del self.sessions[sid]
            shutil.rmtree(folder, ignore_errors=True)

    def running_count(self) -> int:
        with self.lock:
            return sum(1 for job in self.jobs.values() if not job["done"])

    def create(self, project_id: str | None = None) -> dict:
        """A conversation, optionally bound to a project whose instructions and source files preset every turn."""
        with self.lock:
            if project_id:
                self.project_dir(project_id)
            sid = uuid.uuid4().hex
            session = dict(id=sid, title="새 대화", messages=[], files=[], run_id=None, updated=time.time(), project_id=project_id or None)
            self.sessions[sid] = session
            self.persist(session)
            if project_id:
                self.projects[project_id]["updated"] = time.time()
                self.persist_project(self.projects[project_id])
            return session

    # ---- projects: a folder of preset instructions, USER_LOCK and source files shared by its conversations

    def project_dir(self, pid: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", str(pid)) or pid not in self.projects:
            raise ValueError("프로젝트를 찾을 수 없습니다.")
        return self.folder / "projects" / pid

    def persist_project(self, project: dict) -> None:
        save_json(self.project_dir(project["id"]) / "project.json", project)

    def _project_fields(self, project: dict, data: dict) -> None:
        for key in PROJECT_TEXT_FIELDS:
            if key not in data:
                continue
            value = data[key]
            if not isinstance(value, str):
                raise ValueError("프로젝트 설정은 문자열이어야 합니다.")
            value = value.replace("\r\n", "\n")
            if key == "name":
                value = value.strip()[:80]
                if not value:
                    raise ValueError("프로젝트 이름을 입력하세요.")
            elif len(value) > MAX_PROJECT_TEXT:
                raise ValueError("프로젝트 설정은 항목당 10만 글자까지 저장할 수 있습니다.")
            project[key] = value

    def create_project(self, data: dict) -> dict:
        with self.lock:
            pid = uuid.uuid4().hex
            project = dict(id=pid, name="새 프로젝트", description="", instructions="", user_lock="", files=[],
                           created=time.time(), updated=time.time())
            self._project_fields(project, data)
            self.projects[pid] = project
            (self.folder / "projects" / pid).mkdir(parents=True, exist_ok=True)
            self.persist_project(project)
            return self.project_snapshot(pid)

    def update_project(self, pid: str, data: dict) -> dict:
        with self.lock:
            self.project_dir(pid)
            project = self.projects[pid]
            self._project_fields(project, data)
            project["updated"] = time.time()
            self.persist_project(project)
            return self.project_snapshot(pid)

    def delete_project(self, pid: str) -> None:
        """Remove the project folder; its conversations are kept and detached (their runs already hold copies)."""
        import shutil

        with self.lock:
            folder = self.project_dir(pid)
            for session in self.sessions.values():
                if session.get("project_id") == pid:
                    if self.jobs.get(session["id"]) and not self.jobs[session["id"]]["done"]:
                        raise ValueError("이 프로젝트의 대화가 아직 응답 중입니다. 완료하거나 중지한 뒤 삭제하세요.")
            for session in self.sessions.values():
                if session.get("project_id") == pid:
                    session["project_id"] = None
                    self.persist(session)
            del self.projects[pid]
            shutil.rmtree(folder, ignore_errors=True)

    def project_snapshot(self, pid: str) -> dict:
        with self.lock:
            self.project_dir(pid)
            result = json.loads(json.dumps(self.projects[pid]))
            result["sessions"] = [dict(id=s["id"], title=s["title"], updated=s["updated"])
                                  for s in sorted(self.sessions.values(), key=lambda x: x["updated"], reverse=True) if s.get("project_id") == pid]
            result["categories"] = CATEGORIES
            return result

    def project_upload(self, pid: str, name: str, data: bytes, category: str = "invention") -> dict:
        with self.lock:
            folder = self.project_dir(pid)
            if category not in CATEGORIES:
                raise ValueError("자료 종류는 발명 자료·도면·선행기술·정식 명세서 중 하나여야 합니다.")
            project = self.projects[pid]
            if category == "spec" and any(f["category"] == "spec" for f in project["files"]):
                raise ValueError("정식 명세서는 프로젝트당 한 파일만 등록할 수 있습니다.")
            item = self._store_upload(folder / "files", name, data, category)
            project["files"].append(item)
            project["updated"] = time.time()
            self.persist_project(project)
            return item

    def project_remove_file(self, pid: str, fid: str) -> None:
        import shutil

        with self.lock:
            folder = self.project_dir(pid)
            project = self.projects[pid]
            if not re.fullmatch(r"[a-f0-9]{32}", str(fid)) or not any(f["id"] == fid for f in project["files"]):
                raise ValueError("프로젝트 파일을 찾을 수 없습니다.")
            project["files"] = [f for f in project["files"] if f["id"] != fid]
            project["updated"] = time.time()
            self.persist_project(project)
            shutil.rmtree(folder / "files" / fid, ignore_errors=True)

    def project_inputs(self, session: dict) -> dict:
        """Preset inputs of the session's project for one request: instructions, USER_LOCK and categorized files."""
        pid = session.get("project_id")
        if not pid or pid not in self.projects:
            return dict(instructions="", user_lock="", files=[], attachments=[], project=None)
        folder = self.project_dir(pid)
        project = self.projects[pid]
        attachments = []
        for item in project["files"]:
            path = folder / "files" / item["id"] / item["name"]
            checked = validate_attachment(path, item.get("category", "invention"))
            attachments.append(dict(path=str(checked.path), category=checked.category, name=item["name"], project_file_id=item["id"]))
        return dict(instructions=project.get("instructions", "").strip(), user_lock=project.get("user_lock", "").strip(),
                    files=[a["path"] for a in attachments], attachments=attachments,
                    project=dict(id=pid, name=project["name"]))

    def run_status(self, run_id: str | None) -> dict | None:
        """Halt/outcome/usage summary of the session's pipeline run (no claim text, no paths)."""
        if not run_id or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            return None
        state = read_state(self.cfg.path("runs_dir") / run_id / "state.json")
        if not state:
            return None
        halt = state.get("halt") or None
        cand = state.get("candidate") or {}
        dep = state.get("dependent") or {}
        return dict(
            run_id=run_id, outcome=state.get("outcome"), stage=state.get("stage"),
            revision=cand.get("revision"), design_revision=cand.get("design_revision"),
            halt=dict(kind=halt.get("kind"), stage=halt.get("stage"), role=halt.get("role"), reason_code=halt.get("reason_code"),
                      message=self.redact(str(halt.get("message", "")))[:600],
                      open_issues=[dict(kind=o.get("kind"), code=o.get("code"), text=self.redact(str(o.get("text", "")))[:400]) for o in (halt.get("open_issues") or [])[:5]]) if halt else None,
            usage=state.get("usage") or {},
            stages=[dict(stage=r.get("stage"), role=r.get("role"), status=r.get("status"), gates=r.get("gates") or {}, superseded=r.get("superseded"), stale=r.get("stale"))
                    for r in (state.get("records") or {}).values()],
            dependent=dict(reconstruction_gate=(dep.get("current") or {}).get("reconstruction_gate"), draft_set_lock=dep.get("draft_set_lock"), final_set_lock=dep.get("final_set_lock")) if dep else None,
            draft_claim_lock=cand.get("draft_claim_lock"), final_claim_lock=cand.get("final_claim_lock"),
        )

    def snapshot(self, sid: str, log_from: int | None = None) -> dict:
        """Session state for one poll. `log_from` is the client's live-log cursor: only what came after it is sent.

        Without a cursor (first load) the whole window is sent with `log_reset`, which tells the client to replace
        its buffer rather than append. A cursor that fell behind the window, or points past a restarted job, also
        gets a reset, so the browser can never silently stitch a gap into the log.
        """
        with self.lock:
            self.directory(sid)
            # Do not expose backend Gemini history (binary payloads) or filesystem paths.
            result = json.loads(json.dumps(self.sessions[sid]))
            job = self.jobs.get(sid)
            result["running"] = bool(job and not job["done"])
            base, log = (job["log_base"], job["log"]) if job else (0, "")
            cursor = base + len(log)
            reset = log_from is None or not base <= log_from <= cursor
            result["live_log"] = log if reset else log[log_from - base:]
            result["log_cursor"] = cursor
            result["log_reset"] = reset
            result["run"] = self.run_status(self.sessions[sid].get("run_id"))
            pid = self.sessions[sid].get("project_id")
            project = self.projects.get(pid) if pid else None
            result["project"] = dict(id=pid, name=project["name"], files=len(project["files"]),
                                     has_instructions=bool(project.get("instructions", "").strip()),
                                     has_user_lock=bool(project.get("user_lock", "").strip())) if project else None
            return result

    def _store_upload(self, base: Path, name: str, data: bytes, category: str = "invention") -> dict:
        if not name or name != Path(name).name or "/" in name or "\\" in name or ":" in name or name.endswith((".", " ")):
            raise ValueError("올바른 파일 이름이 아닙니다.")
        if len(data) > MAX_UPLOAD:
            raise ValueError("파일은 하나당 20MB까지 첨부할 수 있습니다.")
        # Validate the original name as well as its extension; never accept .env.
        if name.lower().startswith(".env"):
            raise ValueError("API 키가 담긴 .env 파일은 첨부할 수 없습니다.")
        fid = uuid.uuid4().hex
        path = base / fid / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        try:
            checked = validate_attachment(path, category)
            if path.suffix.lower() in DOC_EXT:
                extract_text(path)          # rejects scanned/encrypted/corrupt documents at upload time
            elif path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                path.read_text(encoding="utf-8-sig")
        except ExtractionError as exc:
            path.unlink()
            raise ValueError(f"{name}: {exc}") from None
        except ValueError as exc:
            path.unlink()
            if "도면에는" in str(exc):
                raise
            raise ValueError("지원 형식: UTF-8 텍스트, MD, JSON, YAML, CSV, PDF, DOCX, HWPX, HWP, PNG, JPG, WEBP") from None
        except UnicodeError:
            path.unlink()
            raise ValueError("지원 형식: UTF-8 텍스트, MD, JSON, YAML, CSV, PDF, DOCX, HWPX, HWP, PNG, JPG, WEBP") from None
        return dict(id=fid, name=name, size=len(data), category=checked.category)

    def upload(self, sid: str, name: str, data: bytes) -> dict:
        with self.lock:
            folder = self.directory(sid)
            item = self._store_upload(folder / "uploads", name, data)
            item.pop("category", None)
            self.sessions[sid]["files"].append(item)
            self.persist(self.sessions[sid])
            return item

    def start(self, sid: str, data: dict) -> None:
        with self.lock:
            folder = self.directory(sid)
            session = self.sessions[sid]
            if self.jobs.get(sid) and not self.jobs[sid]["done"]:
                raise ValueError("현재 응답을 완료하거나 중지한 뒤 보내세요.")
            text = str(data.get("text", "")).strip()
            if not text or len(text) > 200000:
                raise ValueError("메시지를 입력하세요. 최대 20만 글자까지 보낼 수 있습니다.")
            if not (os.environ.get(self.cfg.model.api_key_env) or os.environ.get("GOOGLE_API_KEY")):
                raise ValueError("프로젝트 .env에 API 키를 저장한 뒤 다시 실행해 주세요.")
            mode = data.get("mode", "CHAT")
            if mode not in {"CHAT", "AUTHORING_DRAFT"}:
                raise ValueError("지원하지 않는 대화 모드입니다.")
            selected = data.get("files", [])
            if not isinstance(selected, list) or any(not isinstance(x, str) for x in selected):
                raise ValueError("첨부 파일 목록을 확인하세요.")
            known = {item["id"]: item for item in session["files"]}
            if any(fid not in known for fid in selected):
                raise ValueError("이 대화에 첨부된 파일만 사용할 수 있습니다.")
            # 작성 대상: 독립항만 / 특정 항 1개 / 종속항 범위 — validated here, then a hint for the router and a
            # deterministic narrowing in conversation_pipeline (the request text still has precedence).
            dependent, target, target_mode = resolve_claim_target(data)
            rid = "web-" + uuid.uuid4().hex
            job_dir = self.root / ".tui" / "requests" / rid
            model = self.cfg.model.default
            # Every web request is classified. The selection is a hint, never a
            # bypass around interpretation of the user's current question.
            job_dir.mkdir(parents=True)
            history = (read_state(folder / "history.json") or {}).get("history", [])
            prior_users = [m for m in session["messages"] if m["role"] == "user"]
            material = "\n\n".join(m["text"] for m in prior_users)
            context = "\n\n".join(m["text"] for m in session["messages"] if m.get("mode") == "AUTHORING_DRAFT" and m.get("status") in {"complete", "review"})
            earlier_files = [f["id"] for m in prior_users for f in m.get("files", [])]
            preset = self.project_inputs(session)
            session_files = [str(validate_attachment(folder / "uploads" / fid / known[fid]["name"]).path) for fid in dict.fromkeys([*earlier_files, *selected])]
            all_files = list(dict.fromkeys([*preset["files"], *session_files]))
            req = job_dir / "chat.json"
            save_json(req, dict(text=text, material=material, reference=context,
                                files=all_files, attachments=preset["attachments"], history=history, model=model, run_id=session["run_id"],
                                instructions=preset["instructions"], user_lock=preset["user_lock"], project=preset["project"],
                                ui_hints=dict(selected_mode=mode, dependent=dependent, target=target or "", target_mode=target_mode)))
            command = [sys.executable, "-u", "-m", "claim_agent.chat", "--project-root", str(self.root), "--request", str(req)]
            if self.config:
                command += ["--config", str(self.config)]
            mid = uuid.uuid4().hex
            session["messages"].extend([
                dict(id=uuid.uuid4().hex, role="user", text=text, files=[known[f] for f in dict.fromkeys(selected)], mode=mode,
                     claim_target=dict(mode=target_mode, dependent=dependent, target=target)),
                dict(id=mid, role="assistant", text="", status="running", mode=mode, log_id=rid),
            ])
            session["title"] = session["messages"][0]["text"][:36]
            session["updated"] = time.time()
            self.persist(session)
            job = dict(done=False, stopped=False, process=None, log="", log_base=0, mid=mid, folder=job_dir,
                       mode="CHAT", run_id=None, previous_report=0)
            self.jobs[sid] = job
            thread = threading.Thread(target=self.execute, args=(sid, job, command), daemon=True)
            job["thread"] = thread
            thread.start()

    def diff(self, run_id: str, scope: str = "INDEPENDENT") -> str:
        from .runtime import build_runtime
        from .store.diff import render_revision_diff

        rt = build_runtime(self.root, self.config)
        return render_revision_diff(rt.store, rt.store.load_state(run_id), scope=scope if scope in ("INDEPENDENT", "DEPENDENT_SET") else "INDEPENDENT")

    def export(self, run_id: str, fmt: str, with_evidence: bool = False) -> tuple[bytes, str, str]:
        from .runtime import build_runtime
        from .store.export import export_run

        rt = build_runtime(self.root, self.config)
        state = rt.store.load_state(run_id)
        path = export_run(rt.store, state, fmt, with_evidence=with_evidence)
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if fmt == "docx" else "text/markdown; charset=utf-8"
        return path.read_bytes(), path.name, mime

    def resume(self, sid: str, data: dict) -> None:
        """Continue the session's halted run with a user decision (same run, same USER_LOCK)."""
        with self.lock:
            folder = self.directory(sid)
            session = self.sessions[sid]
            if self.jobs.get(sid) and not self.jobs[sid]["done"]:
                raise ValueError("현재 응답을 완료하거나 중지한 뒤 재개하세요.")
            run_id = session.get("run_id")
            status = self.run_status(run_id)
            if not status:
                raise ValueError("이 대화에는 재개할 작업이 없습니다.")
            kind = str(data.get("kind", "none"))
            if kind not in RESUME_KINDS:
                raise ValueError("지원하지 않는 재개 종류입니다.")
            text = str(data.get("text", "")).strip()
            if kind != "accept_unverified" and not text:
                raise ValueError("결정 내용을 입력하세요.")
            if not status["halt"] and kind in ("none", "accept_unverified"):
                raise ValueError("중지된 작업이 아닙니다. 스타일·의미·재설계 중 하나를 고르세요.")
            if not (os.environ.get(self.cfg.model.api_key_env) or os.environ.get("GOOGLE_API_KEY")):
                raise ValueError("프로젝트 .env에 API 키를 저장한 뒤 다시 실행해 주세요.")
            selected = data.get("files", [])
            known = {item["id"]: item for item in session["files"]}
            if not isinstance(selected, list) or any(fid not in known for fid in selected):
                raise ValueError("이 대화에 첨부된 파일만 사용할 수 있습니다.")
            add = [str(validate_attachment(folder / "uploads" / fid / known[fid]["name"]).path) for fid in selected]
            scope = data.get("scope") if data.get("scope") in ("INDEPENDENT", "DEPENDENT") else None
            rid = "web-" + uuid.uuid4().hex
            job_dir = self.root / ".tui" / "requests" / rid
            job_dir.mkdir(parents=True)
            decision = job_dir / "decision.txt"
            decision.write_text(text or "비게이팅 UNVERIFIED 수용", encoding="utf-8")
            command = resume_command(self.root, run_id, decision, kind, self.cfg.model.default, add, scope)
            if self.config:
                command += ["--config", str(self.config)]
            report = self.cfg.path("runs_dir") / run_id / "report.md"
            mid = uuid.uuid4().hex
            labels = {"none": "같은 단계 재실행", "style": "스타일만 수정", "meaning": "의미 수정", "redesign": "재설계", "restart": "처음부터", "accept_unverified": "미검증 수용"}
            session["messages"].extend([
                dict(id=uuid.uuid4().hex, role="user", text=f"[재개 · {labels[kind]}] {text}".strip(), files=[known[f] for f in selected], mode="AUTHORING_DRAFT"),
                dict(id=mid, role="assistant", text="", status="running", mode="AUTHORING_DRAFT", log_id=rid, execution_mode="RESUME"),
            ])
            session["updated"] = time.time()
            self.persist(session)
            job = dict(done=False, stopped=False, process=None, log="", log_base=0, mid=mid, folder=job_dir,
                       mode="RESUME", run_id=run_id, previous_report=report.stat().st_mtime_ns if report.exists() else 0)
            self.jobs[sid] = job
            thread = threading.Thread(target=self.execute, args=(sid, job, command), daemon=True)
            job["thread"] = thread
            thread.start()

    def trim_log(self, job: dict) -> None:
        """Redact, then keep the last MAX_LIVE_LOG characters, counting what fell off the front.

        `log_base + len(log)` is a cursor that only grows, so a client can ask for everything after the position it
        already has instead of re-downloading the whole window on every poll. Re-redacting the whole buffer never
        changes the part a client has already received (it holds no raw key any more), so old cursors stay valid.
        """
        redacted = self.redact(job["log"])
        job["log_base"] = job.get("log_base", 0) + max(0, len(redacted) - MAX_LIVE_LOG)
        job["log"] = redacted[-MAX_LIVE_LOG:]

    def consume_events(self, job: dict, message: dict, reader: EventReader) -> None:
        for event in reader.read():
            kind, role = event.get("kind"), event.get("role", "에이전트")
            label = role + (" · 항 " + str(event["target"]) if event.get("target") else "")
            if kind == "delta":
                if job.get("call_id") != event.get("call_id"):
                    job["log"] += f"\n── {label} · 응답 ──\n"
                job["log"] += event.get("text", "")
                if job["mode"] == "CHAT" and role == "대화":
                    message["text"] += self.redact(event.get("text", ""))
            elif kind == "request":
                job["log"] += f"\n── {label} · 요청 ──\n"
                for key in ("system", "sources", "text", "images"):
                    if event.get(key):
                        job["log"] += str(event[key]) + "\n"
                job["log"] += f"\n── {label} · 응답 ──\n"
            elif kind == "route":
                message["execution_mode"] = event["mode"]
                message["route_reason"] = self.redact(event.get("reason", ""))
                job["log"] += f"\n[자동 분류: {event['mode']}] {event.get('reason', '')}\n"
            else:
                job["log"] += f"\n[{label} · {kind}] " + json.dumps(event, ensure_ascii=False) + "\n"
            job["call_id"] = event.get("call_id")
        self.trim_log(job)

    def execute(self, sid: str, job: dict, command: list[str]) -> None:
        session = self.sessions[sid]
        message = next(m for m in session["messages"] if m["id"] == job["mid"])
        reader = EventReader(job["folder"] / "events.jsonl")
        try:
            options = child_options(self.root)
            options["env"]["CLAIM_AGENT_EVENT_LOG"] = str(reader.path)
            with (job["folder"] / "console.log").open("w", encoding="utf-8") as console:
                with self.lock:
                    process = subprocess.Popen(command, stdout=console, stderr=subprocess.STDOUT, **options)
                    job["process"] = process
                    if job["stopped"]:
                        process.terminate()
                while process.poll() is None:
                    with self.lock:
                        self.consume_events(job, message, reader)
                    time.sleep(0.15)
                with self.lock:
                    self.consume_events(job, message, reader)
            with self.lock:
                job["log"] += "\n" + (job["folder"] / "console.log").read_text(encoding="utf-8", errors="replace")
                self.trim_log(job)
                if job["stopped"]:
                    message["status"] = "stopped"
                    message["text"] += "\n\n[응답을 중지했습니다.]"
                elif job["mode"] == "CHAT":
                    result = read_state(job["folder"] / "response.json")
                    if process.returncode != 0 or not result:
                        raise ValueError("응답을 받지 못했습니다. 작업 로그에서 오류를 확인하고 다시 보내세요.")
                    message["text"] = self.redact(result["answer"])
                    if result.get("finish_reason") == "MAX_TOKENS":
                        message["text"] += "\n\n[응답 길이 제한에 도달했습니다. 이어서 설명해 달라고 요청하세요.]"
                    save_json(self.directory(sid) / "history.json", {"history": result["history"]})
                    message["status"] = result.get("status", "complete")
                    message["execution_mode"] = result.get("effective_mode", "CHAT")
                    message["pipeline_run_id"] = result.get("run_id")
                    if result.get("revision_path"):
                        message["revision_path"] = result["revision_path"]
                    if result.get("run_id") and result.get("effective_mode") in {"AUTHORING_DRAFT", "FINALIZATION"}:
                        session["run_id"] = result["run_id"]
                else:
                    report = self.cfg.path("runs_dir") / job["run_id"] / "report.md"
                    if not report.exists() or report.stat().st_mtime_ns == job["previous_report"]:
                        raise ValueError("새 보고서가 생성되지 않았습니다. 작업 로그에서 중지 원인을 확인하세요.")
                    message["text"] = self.redact(report.read_text(encoding="utf-8"))
                    message["status"] = "complete" if process.returncode == 0 else "review"
        except Exception as exc:
            with self.lock:
                message["status"] = "error"
                message["text"] += "\n\n" + self.redact(str(exc))
                job["log"] += "\n" + str(exc)
                self.trim_log(job)
        finally:
            process = job.get("process")
            if process and process.poll() is None:
                process.kill()
                process.wait()
            with self.lock:
                if job["mode"] != "CHAT" and (self.cfg.path("runs_dir") / job["run_id"] / "state.json").exists():
                    session["run_id"] = job["run_id"]
                (job["folder"] / "display.log").write_text(job["log"], encoding="utf-8")
                job["done"] = True
                session["updated"] = time.time()
                self.persist(session)

    def stop(self, sid: str) -> None:
        with self.lock:
            self.directory(sid)
            job = self.jobs.get(sid)
            if job and not job["done"]:
                job["stopped"] = True
                if job["process"] and job["process"].poll() is None:
                    job["process"].terminate()

    def close(self) -> None:
        for sid in list(self.jobs):
            self.stop(sid)
        for job in list(self.jobs.values()):
            job["thread"].join(timeout=5)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, workspace: Workspace, port: int = 0):
        self.workspace = workspace
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log bootstrap tokens or message contents to the console.

    def send(self, status: int, body: bytes, content_type="application/json; charset=utf-8", cookie=False):
        encoding = ""
        if len(body) >= MIN_GZIP and "gzip" in self.headers.get("Accept-Encoding", ""):
            import gzip

            body, encoding = gzip.compress(body, 6), "gzip"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", f"claim_agent={self.server.token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status=200):
        self.send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def authorized(self) -> bool:
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            return False
        if self.headers.get("Origin") not in (None, self.server.origin):
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        if "claim_agent" in cookie:
            token = cookie["claim_agent"].value
        elif "claim_copa" in cookie:  # Sessions opened before the rename.
            token = cookie["claim_copa"].value
        else:
            token = self.headers.get("X-Claim-Token", "")
        return secrets.compare_digest(token, self.server.token)

    def do_GET(self):
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/" and self.headers.get("Host") == self.server.origin.removeprefix("http://") and secrets.compare_digest(query.get("token", [""])[0], self.server.token):
                self.send(200, (ASSETS / "index.html").read_bytes(), "text/html; charset=utf-8", cookie=True)
                return
            if not self.authorized():
                self.json({"error": "실행기를 더블클릭해서 접속해 주세요."}, 403)
                return
            workspace = self.server.workspace
            if url.path in ("/", "/app.js", "/style.css"):
                name, mime = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}[url.path]
                self.send(200, (ASSETS / name).read_bytes(), mime + "; charset=utf-8")
            elif url.path == "/api/sessions":
                with workspace.lock:
                    sessions = sorted(workspace.sessions.values(), key=lambda x: x["updated"], reverse=True)
                    projects = sorted(workspace.projects.values(), key=lambda x: x["updated"], reverse=True)
                    self.json(dict(model=workspace.cfg.model.default, code_version=code_fingerprint(), busy=workspace.running_count(),
                                   sessions=[dict(id=s["id"], title=s["title"], project_id=s.get("project_id")) for s in sessions],
                                   projects=[dict(id=p["id"], name=p["name"], files=len(p["files"]),
                                                  sessions=sum(1 for s in sessions if s.get("project_id") == p["id"])) for p in projects]))
            elif url.path == "/api/project":
                self.json(workspace.project_snapshot(query.get("id", [""])[0]))
            elif url.path == "/api/session":
                cursor = query.get("log_from", [""])[0]
                self.json(workspace.snapshot(query.get("id", [""])[0], int(cursor) if cursor.isdigit() else None))
            elif url.path == "/api/diff":
                sid = query.get("id", [""])[0]
                with workspace.lock:
                    workspace.directory(sid)
                    run_id = workspace.sessions[sid].get("run_id")
                    if not run_id or not workspace.run_status(run_id):
                        raise ValueError("대조할 작업이 없습니다.")
                    self.json({"text": workspace.redact(workspace.diff(run_id, query.get("scope", ["INDEPENDENT"])[0]))})
            elif url.path == "/api/export":
                sid = query.get("id", [""])[0]
                fmt = query.get("format", ["docx"])[0]
                with workspace.lock:
                    workspace.directory(sid)
                    run_id = workspace.sessions[sid].get("run_id")
                    if not run_id or not workspace.run_status(run_id):
                        raise ValueError("내보낼 작업이 없습니다.")
                    payload, filename, mime = workspace.export(run_id, fmt, query.get("evidence", ["0"])[0] == "1")
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
            elif url.path == "/api/log":
                sid, mid = query.get("id", [""])[0], query.get("message", [""])[0]
                with workspace.lock:
                    workspace.directory(sid)
                    message = next((m for m in workspace.sessions[sid]["messages"] if m["id"] == mid and m.get("log_id")), None)
                    if not message:
                        raise ValueError("로그를 찾을 수 없습니다.")
                    path = workspace.root / ".tui" / "requests" / message["log_id"] / "display.log"
                    self.json({"text": workspace.redact(path.read_text(encoding="utf-8")) if path.exists() else "작업 중입니다."})
            else:
                self.json({"error": "페이지를 찾을 수 없습니다."}, 404)
        except (ValueError, OSError, KeyError) as exc:
            self.json({"error": self.server.workspace.redact(str(exc))}, 400)

    def do_POST(self):
        if not self.authorized() or self.headers.get("X-Claim-Request") != "1":
            self.json({"error": "허용되지 않은 요청입니다."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_UPLOAD:
                raise ValueError("요청은 20MB까지 허용됩니다.")
            raw = self.rfile.read(length)
            url = urlsplit(self.path)
            workspace = self.server.workspace
            if url.path == "/api/upload":
                query = parse_qs(url.query)
                self.json(workspace.upload(query.get("id", [""])[0], query.get("name", [""])[0], raw))
                return
            if url.path == "/api/project/upload":
                query = parse_qs(url.query)
                self.json(workspace.project_upload(query.get("id", [""])[0], query.get("name", [""])[0], raw, query.get("category", ["invention"])[0]))
                return
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("잘못된 요청입니다.")
            if url.path == "/api/new":
                project_id = data.get("project_id")
                if project_id is not None and not isinstance(project_id, str):
                    raise ValueError("프로젝트를 찾을 수 없습니다.")
                self.json(workspace.create(project_id or None))
            elif url.path == "/api/project/new":
                self.json(workspace.create_project(data))
            elif url.path == "/api/project/update":
                self.json(workspace.update_project(str(data.get("id", "")), data))
            elif url.path == "/api/project/delete":
                workspace.delete_project(str(data.get("id", "")))
                self.json({"ok": True})
            elif url.path == "/api/project/remove-file":
                workspace.project_remove_file(str(data.get("id", "")), str(data.get("file", "")))
                self.json({"ok": True})
            elif url.path == "/api/send":
                workspace.start(data.get("id", ""), data)
                self.json({"ok": True})
            elif url.path == "/api/resume":
                workspace.resume(data.get("id", ""), data)
                self.json({"ok": True})
            elif url.path == "/api/stop":
                workspace.stop(data.get("id", ""))
                self.json({"ok": True})
            elif url.path == "/api/delete":
                workspace.delete(str(data.get("id", "")))
                self.json({"ok": True})
            elif url.path == "/api/shutdown":
                self.json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.json({"error": "요청을 찾을 수 없습니다."}, 404)
        except (ValueError, OSError, KeyError) as exc:
            self.json({"error": self.server.workspace.redact(str(exc))}, 400)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Claim-Agent 웹 대화")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    workspace = Workspace(args.project_root, args.config)
    server = Server(workspace)
    metadata = workspace.folder / "server.json"
    save_json(metadata, dict(pid=os.getpid(), origin=server.origin, token=server.token, code_version=code_fingerprint()))
    if not args.no_browser:
        webbrowser.open(server.origin + "/?token=" + server.token)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        workspace.close()
        server.server_close()
        if (read_state(metadata) or {}).get("pid") == os.getpid():
            metadata.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
