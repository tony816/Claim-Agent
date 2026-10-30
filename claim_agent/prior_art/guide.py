"""선행기술조사 지침(sources/14)의 `그래프 근거 요약` 절을 판례·OA 그래프에서 다시 만든다. 모델을 부르지 않는다.

절차·경계 본문은 사람이 고치는 글이고, 표식(GENERATED:START/END) 사이만 이 모듈이 바꾼다.
`claim-agent graph survey-guide --write`로 쓰고, 시험이 파일과 생성 결과가 같은지 본다.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from ..graph import caselaw as cl
from ..graph.precedent import PrecedentGraph

GUIDE_PATH = "sources/14_선행기술조사_특허성판단_지침.md"
START, END = "<!-- GENERATED:START -->", "<!-- GENERATED:END -->"


def oa_path(cfg) -> Path:
    p = Path(cfg.graph.precedent_path)
    return p if p.is_absolute() else cfg.project_root / p


def _top(counter: Counter, n: int = 3) -> str:
    return ", ".join(f"{k} {v}" for k, v in counter.most_common(n)) or "—"


def section(patent: dict, pg: PrecedentGraph) -> str:
    R = patent["records"]
    req = Counter(r["requirement"] for r in R)
    L = [START, "## 그래프 근거 요약 (자동 생성)", "",
         "### 판례 그래프 — 특징 유형별 신규성·진보성 판단", "",
         f"판례 DB `{cl.RELEASE_ID}`의 특허성 기록 {len(R)}개(사건 {len({r['document_id'] for r in R})}건, 신규성 {req['신규성']} · "
         f"진보성 {req['진보성']}, 내용 해시 `{cl._digest(R)}`). 미검토 모델 추출이며 인용문만 판례 원문과 코드로 대조했다. "
         "분포는 법원에 온 사건의 분포이고 등록률이 아니다.", "",
         "| 유형 | 기록 | 부정 | 인정 | 부정 비율 | 부정 이유 상위 | 인정 이유 상위 |", "|---|---|---|---|---|---|---|"]
    rows = []
    for code, label in (patent.get("type_labels") or {}).items():
        rs = [r for r in R if r["type_code"] == code]
        if rs:
            rows.append((code, label, rs))
    for code, label, rs in sorted(rows, key=lambda x: -len(x[2])):
        c = Counter(r["outcome"] for r in rs)
        lost = Counter(x for r in rs if r["outcome"] == "부정" for x in r["reason_factors"])
        won = Counter(x for r in rs if r["outcome"] == "인정" for x in r["reason_factors"])
        L.append(f"| {code} {label} | {len(rs)} | {c['부정']} | {c['인정']} | {round(100 * c['부정'] / len(rs))}% | {_top(lost)} | {_top(won)} |")
    won_all = Counter(x for r in R if r["outcome"] == "인정" for x in r["reason_factors"])
    lost_all = Counter(x for r in R if r["outcome"] == "부정" for x in r["reason_factors"])
    L += ["", f"- 인정 이유 전체 상위: {_top(won_all, 5)}.", f"- 부정 이유 전체 상위: {_top(lost_all, 5)}.", "",
          "### OA 그래프 — 진보성 거절 뒤 등록까지 더해진 한정", "",
          f"심사 이력 {len(pg.data.get('cases') or {})}건(보정 문구 {len(pg.limitations)}개, 효과 주장 {len(pg.effects)}개). "
          "낱말 규칙 분류이며 사람 검토 전이다. 등록된 건만 본 생존 편향이 있다.", "",
          "| 더한 한정 분류 | 보정 문구 수 | 진보성 거절 뒤 청구항 1에 더해진 수 | 최초 종속항에서 온 수 |", "|---|---|---|---|"]
    for p, s in pg.pattern_stats().items():
        L.append(f"| {p} | {s.get('all', 0)} | {s.get('claim1_after_inventive_step', 0)} | {s.get('from_original_dependent', 0)} |")
    L += ["",
          "- 심사관이 설계변경으로 넘기지 못한 차이는 구체 메커니즘이었다: 두 구성의 상대 위치·결합 형태, 여러 하위 구성이 이루는 한 동작, "
          "조건별 분기, 모드와 그동안의 동작, 동작 순서, 목적+위치+재질(`10_심사이력_경험기준.md` E7 b).",
          "- 재료·형상·방식의 선택이나 널리 알려진 항목만 더한 차이는 설계변경으로 함께 거절되었다. 수치는 범위 안팎의 이유·비교가 없으면 "
          "`반복 실험으로 최적값 도출`로 넘어갔다(E7 c).", END]
    return "\n".join(L)


def replace(text: str, generated: str) -> str:
    head, sep, rest = text.partition(START)
    _, sep2, tail = rest.partition(END)
    if not sep or not sep2:
        raise ValueError(f"{GUIDE_PATH}에 생성 절 표식({START} … {END})이 없습니다.")
    return head + generated + tail


def build(cfg) -> str:
    """지금 파일의 본문에 새로 만든 생성 절을 넣은 전체 글. 그래프 파일이 없으면 FileNotFoundError·CaseLawError."""
    path = cfg.project_root / GUIDE_PATH
    patent = cl.load_records(cfg.project_root / cl.PATENT_FILE)
    return replace(path.read_text(encoding="utf-8"), section(patent, PrecedentGraph.load(oa_path(cfg))))
