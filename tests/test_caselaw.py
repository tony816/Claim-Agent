"""Case-law layer: lesson records, the S11 caution sheet, finding lookup, Jev confirm wiring, and the read-only DB check."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from claim_agent.graph import caselaw as cl

ROOT = Path(__file__).resolve().parents[1]
LESSONS = ROOT / "docs" / "caselaw-lessons.json"


@pytest.fixture(scope="module")
def lessons():
    return cl.load_lessons(LESSONS)


def test_sheet_file_is_generated_from_lessons(lessons):
    # sources/11 is code output; a hand edit or a changed lesson file shows up here
    assert (ROOT / cl.SHEET_PATH).read_text(encoding="utf-8") == cl.sheet(lessons)


def test_sheet_has_no_quotes_or_spec_lessons(lessons):
    text = cl.sheet(lessons)
    assert "C1 " in text and "P4 기능적 표현" in text and "근거 미확인" in text
    for r in lessons["records"]:
        assert r["quote"] not in text and r["lesson"] not in text


def test_find_ranks_same_kind_first(lessons):
    hits = cl.find(lessons, "청구항의 '내외'라는 표현이 허용하는 범위가 불명확함", group="명확성", k=3)
    assert hits and hits[0][1]["case_number"] == "2021허193"
    assert all(r["requirement"] == "명확성" for _, r in hits)
    assert cl.find(lessons, "!!!") == []


def test_confirm_asks_one_question_per_candidate(lessons):
    seen = {}

    class Fake:
        def noul_batch(self, state, questions):
            seen.update(state=state, questions=questions)
            return {"status": "RUN", "probabilities": {k: 0.9 for k in questions}}

    cands = [r for _, r in cl.find(lessons, "기능만 있고 구조가 없는 수단", k=2)]
    res = cl.confirm("기능만 있고 구조가 없는 수단", cands, Fake())
    assert set(res["probabilities"]) == {"case_0", "case_1"} == set(seen["state"]["cases"])
    assert all(cl.JEV_QUESTION in q for q in seen["questions"].values())
    assert cl.label(0.9) == "Jev 확인" and cl.label(0.6) == "약한 관련" and cl.label(0.2) == "관련 낮음" and cl.label(None) == "Jev 미확인"


def test_missing_db_is_an_error_not_an_empty_db(tmp_path):
    p = cl.Paths(tmp_path / "none", LESSONS)
    with pytest.raises(cl.CaseLawError):
        cl.validate(p)
    assert not (tmp_path / "none" / "graph.sqlite").exists()


def test_engine_sends_sheet_only_to_generating_roles_and_success(tmp_path, request_dep):
    from claim_agent.provider.scripted import ScriptedProvider
    from claim_agent.runtime import build_runtime

    from . import scripted_roles as R

    rt = build_runtime(ROOT, None, None, {"paths.runs_dir": str(tmp_path / "runs"), "paths.lessons_dir": str(tmp_path / "lessons"),
                                          "cache.enabled": False, "pipeline.max_concurrency": 1})
    prov = ScriptedProvider(R.happy_script(dependent=True))
    engine = rt.engine(prov)
    state = engine.run(engine.start(request_dep, "caselaw-01"))
    assert state.outcome, state.halt
    marker = "<<<FILE " + cl.SHEET_PATH
    got = {c.role for c in prov.calls if marker in c.sources_block}
    assert {"claim-architect", "claim-drafter", "claim-success-reviewer"} <= got
    assert got <= {"claim-architect", "claim-drafter", "claim-success-reviewer"}


DB = ROOT / "graph" / "patent_case_law"


@pytest.mark.skipif(not (DB / "graph.sqlite").is_file(), reason="판례 DB 미설치 (docs/caselaw.md)")
def test_installed_db_passes_and_quotes_match(lessons):
    p = cl.Paths(DB, LESSONS)
    assert cl.validate(p)["status"] == "PASS"
    with closing(cl.connect(p)) as db:
        assert all(cl.quote_ok(db, r) for r in lessons["records"])
