"""sources/10(심사 이력 경험 기준)의 `OA 300건 그래프 근거 요약` 절을 OA 300건 기록에서 다시 만든다. 모델을 부르지 않는다.

본문(E7·E8·exam_pattern 분류표·사용 경계)은 사람이 고치는 글이고, 표식(GENERATED:START/END) 사이만 이 모듈이 바꾼다.
`claim-agent graph oa guide --write`로 쓰고, 시험이 파일과 생성 결과가 같은지 본다.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from . import oa300 as oa
from .oa_tool import NUM_ISSUES, _issue_hits

GUIDE_PATH = "sources/10_심사이력_경험기준.md"
START, END = "<!-- GENERATED:START -->", "<!-- GENERATED:END -->"
SECTIONS = {"A": "생활필수품", "B": "처리조작·운수", "C": "화학·야금", "D": "섬유·종이", "E": "고정구조물", "F": "기계·조명·가열", "G": "물리", "H": "전기"}


def section(cases: list[dict]) -> str:
    s = oa.stats(cases)
    n = s["cases"]
    regs = sorted(c["registered"] for c in cases if c["registered"])
    allow_merged = sum(1 for c in cases if any(o["allowable"] for o in c["oas"]) and "종속항병합" in c["strategies"])
    final_by_amend = sum(1 for c in cases for o in c["oas"] if o["final"] and "보정" in o["summary"])
    total_types = sum(s["types"].values()) or 1
    L = [START, "## OA 300건 그래프 근거 요약 (자동 생성)", "",
         f"Neo4j GraphRAG 데이터셋 `{oa.DATASET}`: 등록 {regs[0] if regs else '?'}~{regs[-1] if regs else '?'}의 한국 등록 특허 {n}건, "
         f"의견제출통지 {s['oa_total']}건. 사례 요약은 모델이 쓴 것이고 의견서 본문이 없어 대응 방식은 최초·등록 독립항 요지 비교로 추정한 값이다. "
         f"요약 {sum(c['truncated'] for c in cases)}건은 검색 도구가 1,500자에서 잘라 끝부분을 그래프 관계로 채웠다. 아래 수는 낱말 규칙으로 센 미검토 값이다"
         "(`claim-agent graph oa import`·`guide`).", "",
         "### 거절 근거와 대응", "",
         "- 거절 근거(사례 수): " + ", ".join(f"{k} {v}({oa.pct(v, n)})" for k, v in s["grounds"].most_common(7)) + ".",
         "- 대응 방식(추정, 사례 수): " + ", ".join(f"{k} {v}({oa.pct(v, n)})" for k, v in s["strategies"].most_common(7)) + ".",
         f"- 통지 1회로 등록 {s['one_oa']}건({oa.pct(s['one_oa'], n)}), 최후통지까지 간 사례 {s['final']}건 — 최후통지 요약이 보정으로 생긴 새 거절이유를 든 통지 {final_by_amend}건.",
         "", "### 등록 경로 A의 300건 기준 (특허 가능 종속항 병합)", "",
         f"- 통지서에 '등록 가능 청구항'이 적시된 사례 {s['allowable_cases']}건({oa.pct(s['allowable_cases'], n)}, 통지 {s['allowable_oas']}건). "
         f"그중 종속항 병합으로 등록 {allow_merged}건.",
         f"- 종속항 병합(최초 종속항을 독립항에 합침)이 대응 방식에 있는 사례 {s['merged']}건({oa.pct(s['merged'], n)}). 등록 독립항에 더한 구성은 사례당 중앙값 {s['added_median']}개.",
         f"- 심사관이 보정 방향을 시사한 통지 {s['suggestion']}건.",
         "", "### 등록 독립항에 더한 구성의 유형", "",
         f"더한 구성 {sum(len(c['added']) for c in cases)}개를 낱말 규칙으로 나눴다(한 구성이 여러 유형일 수 있음). `exam_pattern` 열은 읽기 도움이며 같은 기준이 아니다 — "
         "분류는 위 `exam_pattern` 표와 원자료 문장으로만 한다.", "",
         "| 유형 | 수 | 비율 | 가까운 `exam_pattern` |", "|---|---|---|---|"]
    for code, label in oa.FEATURE_TYPES.items():
        L.append(f"| {label} | {s['types'][code]} | {oa.pct(s['types'][code], total_types)} | {oa.EXAM_PATTERN_HINT[code]} |")
    L += ["", "### IPC 섹션별", "", "| 섹션 | 사례 | 신규성 거절 | 종속항 병합 | 등록 가능 적시 | 많이 더한 유형(구성요소 추가·명확화 제외) |", "|---|---|---|---|---|---|"]
    for sec, name in SECTIONS.items():
        cs = [c for c in cases if c["subclass"][:1] == sec]
        if not cs:
            continue
        t = oa.type_counts(cs)
        top = ", ".join(f"{oa.FEATURE_TYPES[k]} {v}" for k, v in t.most_common() if k not in ("PART", "CLAR"))
        top = ", ".join(top.split(", ")[:3])
        g = Counter(x for c in cs for x in oa.grounds(c))
        L.append(f"| {sec} {name} | {len(cs)} | {oa.pct(g['신규성'], len(cs))} | {oa.pct(sum('종속항병합' in c['strategies'] for c in cs), len(cs))} | "
                 f"{oa.pct(sum(any(o['allowable'] for o in c['oas']) for c in cs), len(cs))} | {top or '—'} |")
    hits = _issue_hits(cases)
    L += ["", "### 수치·조건 한정이 다투어진 방식", "",
          f"수치·범위 문맥이 있는 통지 요약을 심사관 논리로 나눴다(한 통지가 여러 논리에 걸릴 수 있음). 대응 방식에 수치·조건한정이 있는 사례 {s['strategies']['수치·조건한정']}건.", ""]
    for code, label, _, _ in NUM_ISSUES:
        L.append(f"- {label}: 통지 {len(hits[code])}건.")
    L += ["", "- 이 절은 E7·등록 경로 A의 규모를 300건으로 넓혀 본 집계다. 의견서 논증(E8)은 이 데이터에 없으므로 E8은 30건 표본만 근거로 한다.", END]
    return "\n".join(L)


def replace(text: str, generated: str) -> str:
    head, sep, rest = text.partition(START)
    _, sep2, tail = rest.partition(END)
    if not sep or not sep2:
        raise ValueError(f"{GUIDE_PATH}에 생성 절 표식({START} … {END})이 없습니다.")
    return head + generated + tail


def build(root: Path) -> str:
    """지금 파일의 본문에 새로 만든 생성 절을 넣은 전체 글. 기록 파일이 없으면 FileNotFoundError."""
    return replace((root / GUIDE_PATH).read_text(encoding="utf-8"), section(oa.load(root)))
