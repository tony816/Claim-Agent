"""Retention: raw material, packets and reports are confidential; delete them on a schedule."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


def purge_requests(root: Path, older_than_days: float, dry_run: bool = False, now: float | None = None) -> list[str]:
    """Delete .tui/requests/<id> folders (chat/pipeline intake copies) older than the horizon."""
    horizon = (now or time.time()) - older_than_days * 86400
    folder = root / ".tui" / "requests"
    removed: list[str] = []
    if not folder.exists():
        return removed
    for d in sorted(p for p in folder.iterdir() if p.is_dir()):
        try:
            mtime = max([d.stat().st_mtime] + [p.stat().st_mtime for p in d.rglob("*") if p.is_file()])
        except OSError:
            continue
        if mtime > horizon:
            continue
        removed.append(d.name)
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)
    return removed


def purge_all(rt, older_than_days: float, keep_locks: bool = True, dry_run: bool = False) -> dict[str, list[str]]:
    runs = rt.store.purge(older_than_days, keep_locks=keep_locks, dry_run=dry_run)
    requests = purge_requests(rt.cfg.project_root, older_than_days, dry_run=dry_run)
    if not dry_run:
        log = rt.cfg.path("runs_dir") / ".purge-log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "older_than_days": older_than_days, "keep_locks": keep_locks, "runs": runs, "requests": requests}, ensure_ascii=False) + "\n")
    return {"runs": runs, "requests": requests}
