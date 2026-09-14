"""Loopback-only chat workspace. The existing CLI owns all model/pipeline work."""
from __future__ import annotations

import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

from .config import load_config
from .live_events import EventReader
from .tui_support import child_options, read_state, validate_attachment

ASSETS = Path(__file__).with_name("web_assets")
MAX_UPLOAD = 20 * 1024 * 1024


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


class Workspace:
    def __init__(self, root: Path, config: Path | None = None):
        self.root = root.resolve()
        self.config = config.resolve() if config else None
        self.cfg = load_config(self.config, self.root)
        self.folder = self.root / ".tui" / "web"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        for path in (self.folder / "sessions").glob("*/session.json"):
            data = read_state(path)
            if data:
                for message in data["messages"]:
                    if message.get("status") == "running":
                        message["status"] = "stopped"
                        message["text"] += "\n\n[프로그램이 종료되어 응답이 중단되었습니다.]"
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

    def create(self) -> dict:
        with self.lock:
            sid = uuid.uuid4().hex
            session = dict(id=sid, title="새 대화", messages=[], files=[], run_id=None, updated=time.time())
            self.sessions[sid] = session
            self.persist(session)
            return session

    def snapshot(self, sid: str) -> dict:
        with self.lock:
            self.directory(sid)
            # Do not expose backend Gemini history (binary payloads) or filesystem paths.
            result = json.loads(json.dumps(self.sessions[sid]))
            job = self.jobs.get(sid)
            result["running"] = bool(job and not job["done"])
            result["live_log"] = job["log"][-300000:] if job else ""
            return result

    def upload(self, sid: str, name: str, data: bytes) -> dict:
        with self.lock:
            folder = self.directory(sid)
            if not name or name != Path(name).name or "/" in name or "\\" in name or ":" in name or name.endswith((".", " ")):
                raise ValueError("올바른 파일 이름이 아닙니다.")
            if len(data) > MAX_UPLOAD:
                raise ValueError("파일은 하나당 20MB까지 첨부할 수 있습니다.")
            # Validate the original name as well as its extension; never accept .env.
            if name.lower().startswith(".env"):
                raise ValueError("API 키가 담긴 .env 파일은 첨부할 수 없습니다.")
            fid = uuid.uuid4().hex
            path = folder / "uploads" / fid / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            try:
                validate_attachment(path)
                if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    path.read_text(encoding="utf-8-sig")
            except (ValueError, UnicodeError):
                path.unlink()
                raise ValueError("지원 형식: UTF-8 텍스트, MD, JSON, YAML, CSV, PNG, JPG, WEBP")
            item = dict(id=fid, name=name, size=len(data))
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
            all_files = [str(validate_attachment(folder / "uploads" / fid / known[fid]["name"]).path) for fid in dict.fromkeys([*earlier_files, *selected])]
            req = job_dir / "chat.json"
            save_json(req, dict(text=text, material=material, reference=context,
                                files=all_files, history=history, model=model, run_id=session["run_id"],
                                ui_hints=dict(selected_mode=mode, dependent=bool(data.get("dependent")), target=str(data.get("target", "2~8")))))
            command = [sys.executable, "-u", "-m", "claim_agent.chat", "--project-root", str(self.root), "--request", str(req)]
            if self.config:
                command += ["--config", str(self.config)]
            mid = uuid.uuid4().hex
            session["messages"].extend([
                dict(id=uuid.uuid4().hex, role="user", text=text, files=[known[f] for f in dict.fromkeys(selected)], mode=mode),
                dict(id=mid, role="assistant", text="", status="running", mode=mode, log_id=rid),
            ])
            session["title"] = session["messages"][0]["text"][:36]
            session["updated"] = time.time()
            self.persist(session)
            job = dict(done=False, stopped=False, process=None, log="", mid=mid, folder=job_dir,
                       mode="CHAT", run_id=None, previous_report=0)
            self.jobs[sid] = job
            thread = threading.Thread(target=self.execute, args=(sid, job, command), daemon=True)
            job["thread"] = thread
            thread.start()

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
        job["log"] = self.redact(job["log"])[-300000:]

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
                job["log"] += "\n" + self.redact((job["folder"] / "console.log").read_text(encoding="utf-8", errors="replace"))
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
                job["log"] += "\n" + self.redact(str(exc))
        finally:
            process = job.get("process")
            if process and process.poll() is None:
                process.kill()
                process.wait()
            with self.lock:
                if job["mode"] != "CHAT" and (self.cfg.path("runs_dir") / job["run_id"] / "state.json").exists():
                    session["run_id"] = job["run_id"]
                (job["folder"] / "display.log").write_text(job["log"][-300000:], encoding="utf-8")
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
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
                    self.json(dict(model=workspace.cfg.model.default, sessions=[dict(id=s["id"], title=s["title"]) for s in sessions]))
            elif url.path == "/api/session":
                self.json(workspace.snapshot(query.get("id", [""])[0]))
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
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("잘못된 요청입니다.")
            if url.path == "/api/new":
                self.json(workspace.create())
            elif url.path == "/api/send":
                workspace.start(data.get("id", ""), data)
                self.json({"ok": True})
            elif url.path == "/api/stop":
                workspace.stop(data.get("id", ""))
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
    save_json(metadata, dict(pid=os.getpid(), origin=server.origin, token=server.token))
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
