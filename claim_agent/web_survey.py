"""웹 `선행기술조사` 페이지의 작업 관리: 조사 하나를 claim_agent.prior_art.survey 하위 프로세스로 돌리고 그 폴더의 파일을 읽어 보인다.

대화(세션)와 따로 둔다. 조사 결과는 대화 기록·원자료·검수 대상에 들어가지 않으며 PRIOR_ART_SET도 아니다(sources/14).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .config import SETTINGS_ENV
from .tui_support import child_options, read_state

IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".webp"}
MAX_TEXT = 100_000
MAX_PROGRESS = 400


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class Surveys:
    def __init__(self, workspace):
        self.ws = workspace
        self.folder = workspace.folder / "surveys"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        for path in self.folder.glob("*/survey.json"):
            data = read_state(path)
            if data and re.fullmatch(r"[a-f0-9]{32}", str(data.get("id", ""))):
                if data.get("status") == "running":
                    data["status"] = "stopped"
                    data["error"] = "프로그램이 종료되어 조사가 중단되었습니다."
                self.items[data["id"]] = data

    # ---- 공통
    def directory(self, sid: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", sid or "") or sid not in self.items:
            raise ValueError("조사를 찾을 수 없습니다.")
        return self.folder / sid

    def persist(self, item: dict) -> None:
        _save(self.directory(item["id"]) / "survey.json", item)

    def running(self) -> int:
        return sum(1 for job in self.jobs.values() if not job["done"])

    def options(self) -> dict:
        from .model_settings import connection
        from .prior_art import survey, wips

        cfg = self.ws.cfg
        kind = cfg.provider_for(survey.ROLE)
        return dict(countries=wips.COUNTRIES, depths=list(survey.DEPTHS), wips=wips.available(), provider=kind,
                    model=cfg.model_for(survey.ROLE), connected=connection(cfg, kind)["connected"])

    def list(self) -> dict:
        with self.ws.lock:
            rows = sorted(self.items.values(), key=lambda x: x["created"], reverse=True)
            return dict(surveys=[{k: s.get(k) for k in ("id", "title", "status", "created", "countries", "cutoff", "depth")} for s in rows],
                        **self.options())

    # ---- 첨부
    def upload(self, name: str, data: bytes) -> dict:
        if Path(name or "").suffix.lower() in IMAGE_SUFFIX:
            raise ValueError("선행기술조사는 글 자료만 받습니다. 도면은 구성 설명을 글로 적어 주세요.")
        with self.ws.lock:
            item = self.ws._store_upload(self.folder / "uploads", name, data)
            item.pop("category", None)
            return item

    def _upload_path(self, fid: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", fid or ""):
            raise ValueError("첨부 파일을 찾을 수 없습니다.")
        found = [p for p in (self.folder / "uploads" / fid).glob("*") if p.is_file()]
        if not found:
            raise ValueError("첨부 파일을 찾을 수 없습니다. 다시 첨부해 주세요.")
        return found[0]

    # ---- 시작·중지
    def start(self, data: dict) -> dict:
        from .model_settings import connection
        from .prior_art import survey, wips

        text = str(data.get("text") or "").strip()
        if len(text) > MAX_TEXT:
            raise ValueError(f"발명 설명은 {MAX_TEXT:,}자까지 적을 수 있습니다. 긴 자료는 파일로 첨부하세요.")
        files = [{"name": p.name, "path": str(p)} for p in (self._upload_path(str(f)) for f in data.get("files") or [])]
        if not text and not files:
            raise ValueError("발명 내용을 적거나 발명 자료 파일을 첨부하세요.")
        countries = [c for c in data.get("countries") or [] if c in wips.COUNTRIES]
        if not countries:
            raise ValueError("조사할 나라를 하나 이상 고르세요.")
        cutoff = str(data.get("cutoff") or "").strip()
        if cutoff and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff):
            raise ValueError("기준일은 YYYY-MM-DD 형식입니다.")
        depth = str(data.get("depth") or "normal")
        if depth not in survey.DEPTHS:
            raise ValueError("조사 깊이를 고르세요.")
        if not wips.available():
            raise ValueError(".env에 WIPS_API_KEY가 없습니다. 선행기술조사는 WIPS MCP로만 검색합니다.")
        cfg = self.ws.cfg
        status = connection(cfg, cfg.provider_for(survey.ROLE))
        if not status["connected"]:
            raise ValueError("모델 설정에서 '선행기술 검색' 역할의 연결을 확인하세요. " + status["message"])
        with self.ws.lock:
            sid = uuid.uuid4().hex
            title = (text.splitlines()[0] if text else files[0]["name"])[:40]
            item = dict(id=sid, title=title, status="running", created=time.time(), countries=countries, cutoff=cutoff, depth=depth,
                        files=[f["name"] for f in files], text=text)
            self.items[sid] = item
            folder = self.directory(sid)
            folder.mkdir(parents=True)
            request = folder / "request.json"
            _save(request, dict(text=text, files=files, countries=countries, cutoff=cutoff, depth=depth))
            self.persist(item)
            command = [sys.executable, "-u", "-m", "claim_agent.prior_art.survey", "--project-root", str(self.ws.root), "--request", str(request)]
            if self.ws.config:
                command += ["--config", str(self.ws.config)]
            job = dict(done=False, stopped=False, process=None, settings=self.ws.pin_settings(folder))
            self.jobs[sid] = job
            job["thread"] = threading.Thread(target=self._execute, args=(sid, job, command), daemon=True)
            job["thread"].start()
            return dict(id=sid)

    def _execute(self, sid: str, job: dict, command: list[str]) -> None:
        folder = self.folder / sid
        item = self.items[sid]
        try:
            options = child_options(self.ws.root)
            options["env"][SETTINGS_ENV] = str(job["settings"])
            with (folder / "console.log").open("w", encoding="utf-8") as console:
                with self.ws.lock:
                    process = subprocess.Popen(command, stdout=console, stderr=subprocess.STDOUT, **options)
                    job["process"] = process
                    if job["stopped"]:
                        from .processes import terminate_tree

                        terminate_tree(process)
                process.wait()
            result = read_state(folder / "result.json") or {}
            with self.ws.lock:
                if job["stopped"]:
                    item["status"], item["error"] = "stopped", "조사를 중지했습니다."
                elif result.get("status") == "complete":
                    item["status"] = "complete"
                else:
                    tail = (folder / "console.log").read_text(encoding="utf-8", errors="replace")[-600:].strip()
                    item["status"] = "error"
                    item["error"] = self.ws.redact(result.get("error") or tail or "조사가 끝나지 않았습니다.")
        except Exception as exc:  # noqa: BLE001 - the page needs the reason
            with self.ws.lock:
                item["status"], item["error"] = "error", self.ws.redact(str(exc))
        finally:
            process = job.get("process")
            if process and process.poll() is None:
                from .processes import terminate_tree

                terminate_tree(process)
                process.wait()
            with self.ws.lock:
                job["done"] = True
                item["finished"] = time.time()
                if sid in self.items:
                    self.persist(item)

    def stop(self, sid: str) -> None:
        with self.ws.lock:
            self.directory(sid)
            job = self.jobs.get(sid)
            if job and not job["done"]:
                job["stopped"] = True
                if job["process"] and job["process"].poll() is None:
                    from .processes import terminate_tree

                    terminate_tree(job["process"])

    def delete(self, sid: str) -> None:
        with self.ws.lock:
            folder = self.directory(sid)
            job = self.jobs.get(sid)
            if job and not job["done"]:
                raise ValueError("진행 중인 조사입니다. 먼저 중지한 뒤 삭제하세요.")
            self.jobs.pop(sid, None)
            del self.items[sid]
            shutil.rmtree(folder, ignore_errors=True)

    def close(self) -> None:
        for sid in list(self.jobs):
            self.stop(sid)
        for job in list(self.jobs.values()):
            job["thread"].join(timeout=5)

    # ---- 읽기
    def snapshot(self, sid: str) -> dict:
        with self.ws.lock:
            folder = self.directory(sid)
            item = dict(self.items[sid])
            running = bool(self.jobs.get(sid)) and not self.jobs[sid]["done"]
        progress = []
        path = folder / "progress.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_PROGRESS:]:
                try:
                    progress.append(json.loads(line))
                except ValueError:
                    continue      # a line being written
        candidates = read_state(folder / "candidates.json") if (folder / "candidates.json").exists() else None
        result = read_state(folder / "result.json") or {}
        report = self.ws.redact(result.get("report", "")) if result.get("status") == "complete" else ""
        return dict(survey=item, running=running, progress=progress, candidates=candidates if isinstance(candidates, list) else [], report=report)

    def export(self, sid: str) -> tuple[bytes, str]:
        snap = self.snapshot(sid)
        if not snap["report"]:
            raise ValueError("내보낼 보고서가 없습니다.")
        name = re.sub(r"[\\/:*?\"<>|\s]+", "_", snap["survey"]["title"]).strip("_")[:40] or "survey"
        return snap["report"].encode("utf-8"), f"선행기술조사_{name}.md"
