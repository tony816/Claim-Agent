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
    def roles_with(path):
        return {c.role for c in prov.calls if "<<<FILE " + path in c.sources_block}

    for path in (cl.SHEET_PATH, cl.ENFORCE_SHEET_PATH):
        assert {"claim-architect", "claim-drafter", "claim-success-reviewer"} <= roles_with(path)
    assert roles_with(cl.SHEET_PATH) <= {"claim-architect", "claim-drafter", "claim-success-reviewer"}
    assert "dependent-claim-strategy-architect" in roles_with(cl.ENFORCE_SHEET_PATH)
    # S12 never reaches a role that designs, drafts or styles the independent claim
    assert roles_with(cl.PATENT_SHEET_PATH) and not roles_with(cl.PATENT_SHEET_PATH) & {"claim-architect", "claim-drafter", "claim-style-adjuster"}
    oa = [c for c in prov.calls if c.role == "oa-strategy-reviewer"]
    assert oa and all("권리행사 판례 점검" in c.packet_text for c in oa)
    assert not any("권리행사 판례 점검" in c.packet_text for c in prov.calls if c.role != "oa-strategy-reviewer")


@pytest.mark.parametrize("kind,path,build,records", [
    ("patentability", cl.PATENT_SHEET_PATH, cl.sheet_patentability, cl.PATENT_FILE),
    ("enforcement", cl.ENFORCE_SHEET_PATH, cl.sheet_enforcement, cl.ENFORCE_FILE),
])
def test_s12_s13_are_generated_from_records(kind, path, build, records):
    data = cl.load_records(ROOT / records)
    assert data["records"] and all(r["kind"] == kind for r in data["records"])
    assert (ROOT / path).read_text(encoding="utf-8") == build(data)


def test_enforcement_flags_find_risky_wording():
    text = ("【청구항 1】\n베이스와 커버를 포함하는 거치 장치에 있어서,\n상기 베이스를 회전시키기 위한 구동수단; 및\n"
            "사용자가 단말로 각도를 입력하면 서버가 상기 구동수단을 제어하는 제어부를 포함하고,\n상기 베이스의 두께는 3 mm 이상 5 mm 이하인 거치 장치.\n"
            "【청구항 2】\n제1항에 있어서, 상기 구동수단은 모터인 거치 장치.\n【청구항 3】\n거치 장치에 있어서, 베이스를 포함하는 거치 장치.")
    flags = cl.enforcement_flags(text)
    by_claim = {}
    for f in flags:
        by_claim.setdefault(f["claim"], set()).add(f["type_code"])
    assert {"W02", "W04", "W05", "W09"} <= by_claim["1"]
    assert "2" not in by_claim                                  # dependent claim: no preamble flag, no risky wording
    assert "W04" not in by_claim.get("3", set())                # a bare name before '에 있어서' lists no elements
    assert sum(1 for f in flags if f["claim"] == "1" and f["type_code"] == "W05") == 1


def test_feature_type_guess_uses_words_then_neighbours():
    data = {"records": [{"type_code": "F-SHAPE", "claim_feature": "홀더 브라켓 수직 설치", "deciding_reason": "결로 방지", "other_side": ""}],
            "type_labels": {}}
    assert cl.guess_type(data, "가열 온도 범위 300 이상")[0] == "F-NUMERIC"
    assert cl.guess_type(data, "센서 신호로 모터를 정지")[0] == "F-CONTROL"
    assert cl.guess_type(data, "홀더 브라켓")[0] == "F-SHAPE" and cl.guess_type(data, "홀더 브라켓")[1] == "비슷한 기록"


def test_extracted_quote_is_relocated_in_db_text():
    import sqlite3

    from claim_agent.graph import caselaw_extract as cx

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE pdf_pages(document_id, physical_page, text)")
    # the PDF text layer moves digits and Latin abbreviations ('선행발명 4를' → '를.4', 'SMT 부품' → '부품SMT') and breaks pages
    db.executemany("INSERT INTO pdf_pages VALUES ('D', ?, ?)", [
        (3, "통상의 기술자가 선행발명 를.4 접하더라도 부품SMT 등과 같은 전자소자를 수직 방향으로 설치하겠다는"),
        (4, "기술적 동기를 가질 수 있다고 볼 수 없다."),
    ])
    meta = {"document_id": "D", "case_number": "2021허1", "court": "특허법원", "decision_date": "2022-01-01", "case_type": "등록무효"}
    rec = {"kind": "patentability", "requirement": "진보성", "outcome": "인정", "type_code": "F-SHAPE", "claim_feature": "수직 설치",
           "deciding_reason": "동기 없음", "page": 3,
           "quote": "통상의 기술자가 선행발명 4를 접하더라도 SMT 부품 등과 같은 전자소자를 수직 방향으로 설치하겠다는 기술적 동기를 가질 수 있다고 볼 수 없다."}
    kept, rejected = cx.build_records(db, meta, {"records": [rec, {**rec, "quote": "판결문에 없는 문장을 모델이 지어낸 인용문이다"}]})
    assert len(kept) == 1 and len(rejected) == 1
    k = kept[0]
    assert k["quote_spans_next_page"] and k["quote"].startswith("통상의 기술자가") and k["quote"].endswith("볼 수 없다")
    assert cl.quote_ok(db, k)


DB = ROOT / "graph" / "patent_case_law"


@pytest.mark.skipif(not (DB / "graph.sqlite").is_file(), reason="판례 DB 미설치 (docs/caselaw.md)")
def test_installed_db_passes_and_quotes_match(lessons):
    p = cl.Paths(DB, LESSONS)
    assert cl.validate(p)["status"] == "PASS"
    with closing(cl.connect(p)) as db:
        assert all(cl.quote_ok(db, r) for r in lessons["records"])
