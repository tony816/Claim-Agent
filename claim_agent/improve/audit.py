"""ACE 운영 안전 규칙 ⑦: 모든 자동 제안과 승인·거절 이력을 남기는 append-only 감사 로그.

improve/audit.jsonl 한 줄이 한 사건이다. 청구항 문언과 발명 기술내용은 넣지 않는다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ACTIONS = (
    "MINE", "REFLECT", "CURATE",
    "LESSON_PROPOSED", "LESSON_APPROVED", "LESSON_REJECTED", "LESSON_EDIT_APPROVED", "LESSON_HELD",
    "LESSON_ACTIVATED", "LESSON_EVAL_FAILED",
    "CASE_PROPOSED", "CASE_APPROVED", "CASE_REJECTED", "CASE_EDIT_APPROVED",
    "REGRESSION_RUN",
)


class AuditLog:
    def __init__(self, root: Path):
        self.path = root / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, action: str, actor: str = "system", **fields: Any) -> dict[str, Any]:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, "actor": actor, **fields}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def read(self, limit: int | None = None, action: str | None = None, subject: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if action and row.get("action") != action:
                continue
            if subject and subject not in (row.get("lesson_id"), row.get("case_id"), row.get("failure_id")):
                continue
            rows.append(row)
        return rows[-limit:] if limit else rows
