"""Run store: runs/<run_id>/ with state.json, calls/, records/, report.md."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from ..models.request import MaterialBundle
from ..models.state import RunState


class RunStore:
    def __init__(self, runs_dir: Path):
        self.runs_dir = runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ run ids
    def new_run_id(self, prefix: str | None = None) -> str:
        day = time.strftime("%Y%m%d")
        base = f"run-{day}"
        n = 1
        while (self.runs_dir / f"{base}-{n:02d}").exists():
            n += 1
        return f"{base}-{n:02d}" if not prefix else f"{prefix}-{n:02d}"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def list_runs(self) -> list[str]:
        return sorted(p.name for p in self.runs_dir.iterdir() if p.is_dir() and (p / "state.json").exists())

    # ------------------------------------------------------------ state
    def save_state(self, state: RunState) -> None:
        state.updated_at = time.time()
        d = self.run_dir(state.run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def load_state(self, run_id: str) -> RunState:
        p = self.run_dir(run_id) / "state.json"
        if not p.exists():
            raise FileNotFoundError(f"run {run_id} not found under {self.runs_dir}")
        return RunState.model_validate_json(p.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ materials
    def save_materials(self, run_id: str, bundle: MaterialBundle) -> None:
        d = self.run_dir(run_id) / "request"
        d.mkdir(parents=True, exist_ok=True)
        for item in bundle.items:
            target = d / f"{item.category}-{item.sha256[:8]}-{item.name}"
            if not target.exists():
                shutil.copyfile(item.path, target)
        if bundle.user_lock:
            (d / "USER_LOCK.txt").write_text(bundle.user_lock, encoding="utf-8")

    # ------------------------------------------------------------ calls / records
    def write_call(self, run_id: str, seq: int, role: str, scope: str, payload: dict[str, Any], report_md: str | None) -> str:
        d = self.run_dir(run_id) / "calls"
        d.mkdir(parents=True, exist_ok=True)
        base = f"{seq:03d}-{role}-{scope}"
        (d / f"{base}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if report_md is not None:
            (d / f"{base}.md").write_text(report_md, encoding="utf-8")
        return f"calls/{base}.json"

    def write_record(self, run_id: str, record_id: str, payload: dict[str, Any], md: str | None = None) -> str:
        d = self.run_dir(run_id) / "records"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{record_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if md is not None:
            (d / f"{record_id}.md").write_text(md, encoding="utf-8")
        return f"records/{record_id}.json"

    def read_record(self, run_id: str, record_id: str) -> dict[str, Any]:
        p = self.run_dir(run_id) / "records" / f"{record_id}.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def read_record_report(self, run_id: str, record_id: str) -> str:
        rec = self.read_record(run_id, record_id)
        return rec.get("report_markdown", "")

    def write_report(self, run_id: str, text: str) -> Path:
        p = self.run_dir(run_id) / "report.md"
        p.write_text(text, encoding="utf-8")
        return p

    def write_shadow(self, run_id: str, seq: int, role: str, payload: dict[str, Any]) -> None:
        d = self.run_dir(run_id) / "shadow"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{seq:03d}-{role}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def telemetry_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "telemetry.jsonl"
