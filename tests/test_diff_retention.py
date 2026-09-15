"""Revision diff layout and retention purge."""
from __future__ import annotations

import json
import time
from pathlib import Path

from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.runtime import build_runtime
from claim_agent.store.diff import render_revision_diff, render_revision_history, sentence_diff
from claim_agent.store.retention import purge_all

from . import scripted_roles as R

ROOT = Path(__file__).resolve().parents[1]


def test_revision_diff_has_the_change_proposal_layout(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["claim-style-adjuster"] = [R.style(return_to="RETURN_TO_DRAFTER"), R.style(text=R.ROOT_CLAIM.replace("조임 나사", "조임나사"))]
    script["claim-drafter"] = [R.drafter(), R.drafter()]
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "diff-01"))
    assert state.candidate.revision == "r2"
    text = render_revision_diff(rt.store, state)
    for head in ("## 기존 문언", "## 제안 문언", "## 문장 단위 변경", "## 변경 이유", "## 권리범위 영향", "## 근거"):
        assert head in text
    assert "r1 → r2" in text and "DRAFTER_REVISION" in text and "절 결속 재작성 필요" in text
    assert "- 상기 클램프부의 하판을 관통하여 끝단이 책상 하면을 누르는 조임 나사;" in text and "+ 상기 클램프부의 하판을 관통하여 끝단이 책상 하면을 누르는 조임나사;" in text
    assert "sty-clip-holder-r2-01" in text and "draft_claim_lock" in text
    report = (rt.store.run_dir("diff-01") / "report.md").read_text(encoding="utf-8")
    assert "## 리비전 이력" in report and "| INDEPENDENT | r1 | d1 |" in report and "superseded" in report
    assert "찾을 수 없습니다" in render_revision_diff(rt.store, state, "r7", "r2")
    assert sentence_diff("A. B.", "A. B.") == []


def test_single_revision_has_no_history_section(rt, request_indep):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    state = engine.run(engine.start(request_indep, "diff-02"))
    assert render_revision_history(state) == ""
    assert "첫 리비전" in render_revision_diff(rt.store, state)


def test_purge_keeps_locked_runs_and_recent_requests(tmp_path, request_indep):
    rt = build_runtime(ROOT, None, None, {"paths.runs_dir": str(tmp_path / "runs"), "paths.lessons_dir": str(tmp_path / "lessons"), "cache.enabled": False, "pipeline.max_concurrency": 1})
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=False)))
    locked = engine.run(engine.start(request_indep, "old-locked"))
    assert locked.outcome == "DRAFT_CLAIM_LOCK"
    halted_script = R.happy_script(dependent=False)
    halted_script["claim-architect"] = [R.architect(locked=False)]
    rt.engine(ScriptedProvider(halted_script)).run(rt.engine(ScriptedProvider(halted_script)).start(request_indep, "old-halted"))
    old = time.time() - 40 * 86400
    for run in ("old-locked", "old-halted"):
        p = rt.store.run_dir(run) / "state.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["updated_at"] = old
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    rt.cfg.project_root = tmp_path
    req_old, req_new = tmp_path / ".tui" / "requests" / "web-old", tmp_path / ".tui" / "requests" / "web-new"
    for d in (req_old, req_new):
        d.mkdir(parents=True)
        (d / "chat.json").write_text("{}", encoding="utf-8")
    import os
    os.utime(req_old / "chat.json", (old, old))
    os.utime(req_old, (old, old))
    preview = purge_all(rt, 30, keep_locks=True, dry_run=True)
    assert preview == {"runs": ["old-halted"], "requests": ["web-old"]} and rt.store.run_dir("old-halted").exists()
    result = purge_all(rt, 30, keep_locks=True)
    assert result["runs"] == ["old-halted"] and not rt.store.run_dir("old-halted").exists() and rt.store.run_dir("old-locked").exists()
    assert not req_old.exists() and req_new.exists()
    assert (tmp_path / "runs" / ".purge-log.jsonl").exists()
    assert purge_all(rt, 30, keep_locks=False)["runs"] == ["old-locked"]
