"""문체 조정의 정확 검색 도구가 스타일 참고용 청구항 그래프(cs2 jev_graph_lossless)를 읽는다. 없으면 문체 코퍼스로 검색한다."""
from __future__ import annotations

from pathlib import Path

import pytest

from claim_agent.graph import claims as cg
from claim_agent.pipeline.corpus_tool import ToolLog, make_tools
from claim_agent.provider.base import CallResult
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.runtime import build_runtime

from . import scripted_roles as R

ROOT = Path(__file__).resolve().parents[1]
GRAPH = (ROOT / "../cs2-spec-writer/graph/jev_graph_lossless").resolve()
installed = pytest.mark.skipif(not (GRAPH / "graph.sqlite3").is_file(), reason="청구항 그래프 미설치 (docs/graph_layers.md)")


def _rt(tmp_path: Path, **over):
    return build_runtime(ROOT, None, None, {"paths.runs_dir": str(tmp_path / "runs"), "paths.lessons_dir": str(tmp_path / "lessons"),
                                            "cache.enabled": False, "pipeline.max_concurrency": 1, **over})


@pytest.fixture
def corpus_text(tmp_path):
    return _rt(tmp_path).sources.get("CORPUS").text


@installed
def test_the_graph_holds_the_whole_corpus_and_counts_spellings(corpus_text):
    g = cg.ClaimGraph.open(GRAPH, corpus_text)
    assert len(g.claims) == 1014 and sum(1 for c in g.claims.values() if c.corpus) == 143      # every corpus claim found
    assert sorted(c.corpus for c in g.claims.values() if c.flagged_review) == ["EX-08 청구항 3", "EX-10 청구항 5", "EX-11 청구항 6"]
    spaced, joined = g.search("길이 방향", 2), g.search("길이방향", 2)
    assert spaced[0] > joined[0] >= 1 and spaced[1] >= 1                    # 24 vs 1 claims on 2026-11-02
    assert all(c.corpus for c, _ in spaced[2])                              # selected claims come first
    assert "길이 방향" in spaced[2][0][1]
    chain = g.chain("76ee697274d038b6:001:c10")
    assert [c.claim_no for c in chain] == [1, 10] and chain[1].parents == ("76ee697274d038b6:001:c1",)
    with pytest.raises(KeyError):
        g.chain("nope")


@installed
def test_style_tools_search_the_graph_and_open_parent_chains(corpus_text, tmp_path):
    rt = _rt(tmp_path)
    log = ToolLog()
    tools = {f.__name__: f for f in make_tools(rt.sources, log, cg.ClaimGraph.open(GRAPH, corpus_text))}
    assert set(tools) == {"open_routing_index", "search_style_corpus", "open_claim_chain"}
    found = tools["search_style_corpus"](mode="TERM_EXACT", query="상기 복수의 내완충부는")
    assert found["total"] == 4 and found["in_corpus"] == 0 and len(found["results"]) == 2
    first = found["results"][0]
    assert first["corpus"] is None and first["cites"] == [1] and "코퍼스 미수록" in first["label"]
    chain = tools["open_claim_chain"](claim_id=first["claim_id"])
    assert [c["label"].split(",")[0] for c in chain["claims"]] == ["사례 1 청구항 1", "사례 1 청구항 2"] and "옮기지 않는다" in chain["note"]
    assert tools["open_claim_chain"](claim_id="x")["claims"] == []
    assert tools["search_style_corpus"](mode="TERM_EXACT", query="TF_생활위생")["error"]                 # field queries stay refused
    text = log.render()
    assert log.used and "사용 소스: 스타일 참고용 청구항 그래프" in text and "정확 일치 4항, 코퍼스 수록 0항" in text
    assert "open_claim_chain(claim_id='x'): " in text and "없는 항" in text


def test_without_the_graph_the_tools_search_the_corpus_and_say_why(tmp_path):
    rt = _rt(tmp_path)
    problem = ""
    try:
        cg.ClaimGraph.open(tmp_path / "none", rt.sources.get("CORPUS").text)
    except cg.ClaimGraphError as exc:
        problem = str(exc)
    assert "청구항 그래프가 없습니다" in problem
    log = ToolLog()
    tools = {f.__name__: f for f in make_tools(rt.sources, log, None, problem)}
    assert set(tools) == {"open_routing_index", "search_style_corpus"}
    assert tools["search_style_corpus"](mode="TERM_EXACT", query="길이 방향")["results"]
    assert "청구항_문체학습용_분야별검색최적화본.md (청구항 그래프 미사용 — 청구항 그래프가 없습니다" in log.render()


def test_a_graph_that_does_not_match_its_checksums_is_refused(tmp_path, corpus_text):
    (tmp_path / "graph.sqlite3").write_bytes(b"not the graph")
    (tmp_path / "SHA256SUMS.txt").write_text("0" * 64 + "  graph.sqlite3\n", encoding="utf-8")
    with pytest.raises(cg.ClaimGraphError, match="SHA256SUMS"):
        cg.ClaimGraph.open(tmp_path, corpus_text)


@pytest.mark.parametrize("graph_dir", ["missing", "installed"])
def test_the_style_tool_phase_names_the_source_it_searched(tmp_path, request_indep, graph_dir):
    if graph_dir == "installed" and not (GRAPH / "graph.sqlite3").is_file():
        pytest.skip("청구항 그래프 미설치")
    rt = _rt(tmp_path, **{"graph.claim_graph_dir": str(tmp_path / "none") if graph_dir == "missing" else str(GRAPH)})
    seen: dict[str, object] = {}

    class _Tools(ScriptedProvider):
        def generate(self, spec):
            if spec.phase == "tool_phase" and spec.tools:
                by_name = {f.__name__: f for f in spec.tools}
                seen["tools"] = sorted(by_name)
                by_name["search_style_corpus"](mode="TERM_EXACT", query="길이 방향")
                return CallResult(text="SEARCH_DONE", parsed=None, model=spec.model, provider=self.name, finish_reason="STOP")
            if spec.role == "claim-style-adjuster" and spec.phase == "main":
                seen["packet"] = spec.packet_text
            return super().generate(spec)

    engine = rt.engine(_Tools(R.happy_script(dependent=False)))
    state = engine.run(engine.start(request_indep, "run-graph-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    if graph_dir == "missing":
        assert seen["tools"] == ["open_routing_index", "search_style_corpus"] and "청구항 그래프 미사용" in seen["packet"]
    else:
        assert "open_claim_chain" in seen["tools"] and "사용 소스: 스타일 참고용 청구항 그래프" in seen["packet"]
        assert "코퍼스 수록" in seen["packet"]
