"""OA 도구(OA 300건 그래프): 사례 요약 파싱, 세 역산 도구, sources/10 생성 절, 웹 작업 도구 연결."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from claim_agent import free_work, tool_advice, web
from claim_agent.graph import oa300 as oa
from claim_agent.graph import oa_guide, oa_tool

ROOT = Path(__file__).resolve().parents[1]

FULL = """# 등록특허 10-3003637 「현상 매체, 열현상 방법, 및 열현상 시스템」 심사 경과

- 등록번호: 10-3003637 / 출원번호: 10-2023-7035034 / 공개번호: 10-2023-0156404
- 출원인: 아사히 가세이 가부시키가이샤 / 대리인: 김진회 외 1명
- IPC: D04H-001/435 (서브클래스 D04H)
- 출원일 2022-03-30, 등록결정일 2026-07-20, 등록일 2026-08-07, 우선심사 아니오, 의견제출통지 2회
- 핵심 기술 개념: 플렉소 열현상, 현상 매체, 공극률, 탄성 회복률, 폴리에스테르 부직포

## 최초 출원(공개공보) 독립항 요지
공극률 35~95%, 탄성 회복률 40~99%인 플렉소 열현상용 현상 매체

## 1차 의견제출통지서 (2025-01-08, 반도체심사추진단)
- 심사 대상 청구항: 제1-14항 / 거절 청구항: 제1-14항 / 등록 가능 청구항: 없음
- 거절 근거: 특허법 29조1항 (신규성) — 대상 제1-2, 5-7, 9-10항
- 거절 근거: 특허법 29조2항 (진보성) — 대상 제1-14항
- 인용문헌 인용발명1: WO 2013/154180 A1 (주인용)
- 거절이유 요약: 인용발명1 현상용 포재의 체적밀도·영률이 공극률·탄성회복률 범위와 겹쳐 동일하고, 흡수제거 방식은 인용발명2로 용이.

## 2차 의견제출통지서 (2025-09-17, 반도체심사추진단)
- 심사 대상 청구항: 제1-14항 / 거절 청구항: 제1-14항 / 등록 가능 청구항: 제4, 6항
- 거절 근거: 특허법 29조2항 (진보성) — 대상 제1-14항
- 거절이유 요약: 최후통지. '부직포' 부가 보정으로 새 거절이유 발생: 공극률은 반복 실험으로 얻을 최적화 수준.

## 출원인 대응
- 추정 대응 방식(최초 독립항과 등록 독립항 비교로 추정, 의견서 본문 미확인): 신규구성추가, 수치·조건한정
- 등록 독립항에 추가된 구성: 현상 매체를 부직포로 한정(상세설명 [0064] 근거)
- 등록 독립항에 추가된 구성: 재질을 폴리에스테르계 수지·공중합체로 한정
- 보정 요약: 1차에서 '부직포'를 추가했으나 최후통지를 받고, 2차에서 재질을 한정해 등록됨.

## 등록 독립항 요지
공극률 35~95%, 탄성 회복률 40~99%이고, 부직포이며, 폴리에스테르계 수지로 이루어진 플렉소 열현상용 현상 매체
"""

HINGE = """# 등록특허 10-2991448 「체결 디바이스」 심사 경과

- 등록번호: 10-2991448 / 출원번호: 10-2022-0000001 / 공개번호: 10-2023-0000001
- 출원인: 보아 테크놀러지 / 대리인: 홍길동
- IPC: B65H-075/00 (서브클래스 B65H)
- 출원일 2022-01-01, 등록결정일 2026-03-01, 등록일 2026-03-10, 우선심사 아니오, 의견제출통지 1회
- 핵심 기술 개념: 스풀, 레이스, 노브, 케이스 유닛

## 최초 출원(공개공보) 독립항 요지
케이스 유닛, 레이스가 감기는 스풀, 케이스를 덮는 노브를 포함하는 체결 디바이스

## 1차 의견제출통지서 (2025-05-01, 특허심사기획국)
- 심사 대상 청구항: 제1-12항 / 거절 청구항: 제1-3, 5, 8, 9항 / 등록 가능 청구항: 제4, 6, 7, 10-12항
- 거절 근거: 특허법 29조2항 (진보성) — 대상 제1-3, 5, 8, 9항
- 거절이유 요약: 스풀과 노브는 인용발명1에 개시되어 결합이 용이함.

## 출원인 대응
- 추정 대응 방식(최초 독립항과 등록 독립항 비교로 추정, 의견서 본문 미확인): 종속항병합
- 등록 독립항에 추가된 구성: 스풀 위에 위치된 결합 유닛
- 보정 요약: 등록가능 청구항 12의 결합 유닛을 스풀 위 배치로 한정해 독립항에 편입함.

## 등록 독립항 요지
최초 독립항 + 스풀 위에 위치된 결합 유닛을 포함하는 체결 디바이스
"""

CUT = HINGE.replace("10-2991448", "10-2991449").replace("「체결 디바이스」", "「레이스 체결 디바이스」").split("## 출원인 대응")[0] + "## 출원인 대응\n- 추정 대응…"
CLAIMS = ("【청구항 1】\n레이스가 감기는 스풀; 및 상기 스풀을 덮는 노브를 포함하는 체결 디바이스.\n\n"
          "【청구항 2】\n제1항에 있어서, 상기 노브의 직경은 20~40mm인 체결 디바이스.")


def dump(*texts: str, relationships=()) -> dict:
    return dict(chunks=[dict(source=f"case{i}.md", text=t) for i, t in enumerate(texts)], relationships=list(relationships))


@pytest.fixture
def root(tmp_path):
    rels = [dict(source="10-2991449 레이스 체결 디바이스", relation="ADDED_FEATURE", target="노브 내측의 래칫 결합부"),
            dict(source="10-2991449 레이스 체결 디바이스", relation="OVERCOME_BY", target="종속항병합")]
    data = oa.build_records(dump(FULL, HINGE, CUT, relationships=rels))
    (tmp_path / "docs").mkdir()
    (tmp_path / oa.RECORDS_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_parse_case_reads_every_section_of_the_summary():
    c = oa.parse_case(FULL)
    assert (c["reg_no"], c["subclass"], c["oa_count"], c["priority"], c["truncated"]) == ("10-3003637", "D04H", 2, False, False)
    assert c["initial_claim"].startswith("공극률 35~95%") and c["granted_claim"].endswith("현상 매체")
    first, second = c["oas"]
    assert [g["label"] for g in first["grounds"]] == ["신규성", "진보성"] and first["allowable"] == ""
    assert first["citations"] == [dict(label="인용발명1", doc="WO 2013/154180 A1", role="주인용")]
    assert second["final"] and second["allowable"] == "제4, 6항" and c["final_oa"]
    assert c["strategies"] == ["신규구성추가", "수치·조건한정"] and len(c["added"]) == 2
    assert c["amend_summary"].startswith("1차에서")


def test_a_cut_summary_is_filled_from_the_graph_relations(root):
    cases = {c["reg_no"]: c for c in oa.load(root)}
    cut = cases["10-2991449"]
    assert cut["truncated"] and cut["added"] == ["노브 내측의 래칫 결합부"] and cut["strategies"] == ["종속항병합"]
    assert set(cut["filled_from_graph"]) == {"added", "strategies"}
    assert not cases["10-2991448"]["filled_from_graph"]                  # a whole summary keeps its own list


def test_feature_types_follow_the_word_rules():
    assert oa.feature_types("스풀 위에 위치된 결합 유닛") == ["POS", "PART"]
    assert "NUM" in oa.feature_types("반복주파수 100kHz 이상") and "SEQ" in oa.feature_types("건조 후 소성하는 단계")
    assert "POS" not in oa.feature_types("A1·A2를 단일결합·O·S로 한정")
    assert oa.feature_types("그 밖의 무언가") == ["PART"]


def test_dependents_counts_defence_types_and_checks_my_dependent_claims(root):
    answer = oa_tool.run("oa_dependents", root=root, text="B65H 레이스 스풀", claims=CLAIMS, target="내 메시지 #1")
    assert answer.startswith("**OA 참고 (미검토 — 게이트·근거 아님)** · 종속항 역산 · 대상: 내 메시지 #1")
    assert "'등록 가능 청구항'을 적시: 2건" in answer and "### 내 종속항이 덮는 유형" in answer
    assert "| 위치·결합·배치 |" in answer and "내 종속항에 없는 것: 위치·결합·배치" in answer
    assert "10-2991448 「체결 디바이스」" in answer and "등록 가능 청구항 적시: 1차 제4, 6, 7, 10-12항" in answer


def test_gap_shows_the_too_broad_pattern_and_numeric_lists_my_numbers(root):
    gap = oa_tool.run("oa_gap", root=root, text="현상 매체 공극률 부직포 D04H")
    assert "과도하게 넓었던 전형" in gap and "10-3003637" in gap and "인용발명1 현상용 포재" in gap
    numeric = oa_tool.run("oa_numeric", root=root, claims=CLAIMS)
    assert "`상기 노브의 직경은 20~40mm인`" in numeric
    assert "| 반복 실험·통상의 최적화·임계적 의의 없음(진보성) | 1 |" in numeric
    assert "| 인용발명 수치와 겹침·포함·환산 동일(신규성) | 1 |" in numeric and "### 명세서 체크리스트" in numeric


def test_the_oa_tool_refuses_what_it_cannot_answer(root, tmp_path):
    with pytest.raises(ValueError, match="지원하지 않는"):
        oa_tool.run("draft", root=root, text="x")
    with pytest.raises(ValueError, match="대상 청구항을 고르거나"):
        oa_tool.run("oa_gap", root=root, text="  ")
    with pytest.raises(ValueError, match="12000자"):
        oa_tool.run("oa_gap", root=root, text="가" * 12001)
    with pytest.raises(ValueError, match="OA 기록을 읽지 못했습니다"):
        oa_tool.run("oa_gap", root=tmp_path / "empty", text="x")


def test_records_file_and_sources_10_section_match_their_generators():
    if not (ROOT / oa.DUMP_FILE).is_file():
        pytest.skip("OA 300건 덤프(graph/oa300, git 제외)가 이 컴퓨터에 없음")
    saved = json.loads((ROOT / oa.RECORDS_FILE).read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(oa.build_records(json.loads((ROOT / oa.DUMP_FILE).read_text(encoding="utf-8"))), ensure_ascii=False))
    assert len(saved["cases"]) == 300
    assert (ROOT / oa_guide.GUIDE_PATH).read_text(encoding="utf-8") == oa_guide.build(ROOT)


def test_guide_replace_needs_markers():
    with pytest.raises(ValueError, match="표식"):
        oa_guide.replace("본문만 있음", oa_guide.START + "x" + oa_guide.END)


def test_recommender_keeps_an_oa_tool_only_with_claims_or_a_field():
    advice = tool_advice.Advice(summary="s", tools=[tool_advice.ToolPick(tool="oa_numeric", reason="r", focus=""),
                                                    tool_advice.ToolPick(tool="oa_gap", reason="r", focus="B65H 스풀")])
    assert [t["tool"] for t in tool_advice.clean(advice, {"claims": ""})["tools"]] == ["oa_gap"]
    assert [t["tool"] for t in tool_advice.clean(advice, {"claims": CLAIMS})["tools"]] == ["oa_numeric", "oa_gap"]


@pytest.fixture
def workspace(tmp_path, root, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-web-secret")
    original, calls = oa_tool.run, []

    def offline(tool, **kw):
        calls.append(dict(tool=tool, **{k: kw[k] for k in ("text", "claims", "target")}))
        return original(tool, **{**kw, "root": root})

    monkeypatch.setattr(oa_tool, "run", offline)
    ws = web.Workspace(tmp_path / "web")
    ws.oa_calls = calls
    yield ws
    ws.close()


def test_oa_turns_show_in_the_conversation_but_never_reach_a_model(workspace):
    sid = workspace.create(None, free_work.FREE)["id"]
    folder = workspace.directory(sid)
    workspace.sessions[sid]["messages"] = [dict(id="u", role="user", text="발명 설명"),
                                           dict(id="a", role="assistant", text="초안\n\n" + CLAIMS, mode="FREE", status="complete")]
    workspace.caselaw(sid, {"tool": "oa_dependents", "message": "a", "text": "B65H"})
    assert workspace.oa_calls[-1] == dict(tool="oa_dependents", text="B65H", claims=free_work.review_target("초안\n\n" + CLAIMS), target="에이전트 답변 #2")
    asked, answer = workspace.sessions[sid]["messages"][-2:]
    assert asked["mode"] == answer["mode"] == web.CASELAW and asked["text"] == "[OA 도구 · 종속항 역산] 에이전트 답변 #2의 청구항 · B65H"
    assert answer["caselaw"] == dict(tool="oa_dependents", source="a") and "종속항 역산" in answer["text"]
    workspace.caselaw(sid, {"tool": "oa_gap", "text": "현상 매체 공극률"})     # no target: the field words alone
    assert workspace.oa_calls[-1]["claims"] == "" and workspace.sessions[sid]["messages"][-2]["text"] == "[OA 도구 · 독립항 갭] 현상 매체 공극률"
    assert [m["id"] for m in web.conversational(workspace.sessions[sid]["messages"])] == ["u", "a"]
    assert not (folder / "history.json").exists()
    with pytest.raises(ValueError, match="대상 청구항을 고르거나"):
        workspace.caselaw(sid, {"tool": "oa_numeric", "text": ""})
