"""오프라인 픽스처: WIPS·Jev 응답을 기록한 뒤 그대로 재생하고, 없는 픽스처는 WipsError·FAILED로 처리한다."""
from __future__ import annotations

import json

import pytest

from claim_agent.prior_art import fixtures, wips
from claim_agent.prior_art.jev import JevClient


class LiveStub:
    def __init__(self):
        self.calls = []

    def call(self, tool, args):
        self.calls.append(tool)
        if tool == "get_patent_ai_search":
            return {"success": True, "data": {"results": [{"skey": "7", "ctry": "KR", "mngNum": "10-7", "adjustScoreDp": "88"}]}}
        if tool == "get_patent_search_onlyengine":
            return {"success": True, "data": {"row_list": [{"SKEY": "7", "CC": "KR", "XN_D": "10-7", "KC_D": "A", "TIK": "냉각판", "XD": "20200101"}]}}
        raise wips.WipsError(f"WIPS {tool} 실패: NOT_FOUND")


def test_fixture_key_is_stable():
    a = fixtures.fixture_key({"tool": "t", "args": {"b": 1, "a": "가"}})
    assert a == fixtures.fixture_key({"args": {"a": "가", "b": 1}, "tool": "t"}) and len(a) == 16
    assert all(c in "0123456789abcdef" for c in a) and a != fixtures.fixture_key({"tool": "t", "args": {"a": "나", "b": 1}})


def test_wips_record_then_replay(tmp_path):
    live = LiveStub()
    recorded = wips.search(fixtures.RecordingWips(live, tmp_path), "냉각판", ["KR"], 5)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 2 and files[0].startswith("get_patent_ai_search-") and files[1].startswith("get_patent_search_onlyengine-")
    args = {"keyword": "냉각판", "ctry": "KR", "targetCtry": "KR", "size": 5}
    key = fixtures.fixture_key({"tool": "get_patent_ai_search", "args": args})
    assert (tmp_path / f"get_patent_ai_search-{key}.json").is_file()
    replay = fixtures.FixtureWips(tmp_path)
    assert wips.search(replay, "냉각판", ["KR"], 5) == recorded and recorded[0]["published"] == "2020-01-01"
    with pytest.raises(wips.WipsError):                     # 실패한 호출은 기록하지 않는다
        fixtures.RecordingWips(live, tmp_path).call("get_patent_spec_text", {"section": "all_claim", "skey": "7"})
    assert len(list(tmp_path.iterdir())) == 2


def test_wips_missing_fixture_raises(tmp_path):
    key = fixtures.fixture_key({"tool": "get_patent_spec_text", "args": {"skey": "1"}})
    with pytest.raises(wips.WipsError, match=f"WIPS 픽스처 없음: get_patent_spec_text {key}"):
        fixtures.FixtureWips(tmp_path).call("get_patent_spec_text", {"skey": "1"})
    text, meta = wips.fetch(fixtures.FixtureWips(tmp_path), {"skey": "1", "ctry": "KR", "number": "10-1", "abstract": "요약"})
    assert "【요약】\n요약" in text and any("픽스처 없음" in w for w in meta["warnings"])   # fetch는 경고로 계속한다


def test_jev_record_then_replay(tmp_path):
    def runner(args, timeout):
        if args[0] == "run":
            return {"answers": {"d0": {"noul": 0.9}, "d1": {"noul": 0.2}}}
        return {"answers": {"answer": {"noul": 0.4}}}

    state = {"invention": "냉각판", "docs": {"d0": "A", "d1": "B"}}
    rec = fixtures.RecordingJev(JevClient(runner), tmp_path)
    batch = rec.noul_batch(state, {"d0": "d0?", "d1": "d1?"})
    single = rec.noul(state, "하나?")
    names = sorted(p.name for p in tmp_path.iterdir())
    assert [n.split("-")[0] for n in names] == ["noul", "noul_batch"]
    replay = fixtures.FixtureJev(tmp_path)
    assert replay.noul_batch(state, {"d0": "d0?", "d1": "d1?"}) == batch and batch["probabilities"] == {"d0": 0.9, "d1": 0.2}
    assert replay.noul(state, "하나?") == single and single["probability"] == 0.4
    stored = json.loads((tmp_path / names[1]).read_text(encoding="utf-8"))
    assert stored["status"] == "RUN"


def test_jev_missing_fixture_is_failed(tmp_path):
    out = fixtures.FixtureJev(tmp_path).noul_batch({"a": 1}, {"q": "?"})
    assert out["status"] == "FAILED" and out["probabilities"] == {} and out["error"].startswith("Jev 픽스처 없음: noul_batch ")
    one = fixtures.FixtureJev(tmp_path).noul({"a": 1}, "?")
    assert one["status"] == "FAILED" and one["probability"] is None and one["question_count"] == 1

    def boom(args, timeout):
        raise RuntimeError("down")

    failed = fixtures.RecordingJev(JevClient(boom), tmp_path).noul_batch({"a": 1}, {"q": "?"})
    assert failed["status"] == "FAILED" and not list(tmp_path.iterdir())    # 실패는 저장하지 않는다
