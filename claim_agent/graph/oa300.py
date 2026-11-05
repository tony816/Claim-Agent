"""OA 300건 그래프(Neo4j GraphRAG 데이터셋 `kr-oa-300`)를 사례 기록으로 바꾸고, 명세서 작성(역산) 도구가 쓰는 집계를 만든다.

입력은 `graph/oa300/kg_dump.json`(git 제외): 그 데이터셋의 사례별 심사 경과 요약(마크다운 청크 300개)과 개체 관계를
`kg_search`로 모은 덤프다. 검색 도구가 청크 본문을 1,500자에서 자르므로 잘린 사례(`truncated`)는 끝부분(출원인 대응·
등록 독립항 요지)이 비어 있을 수 있고, 그때는 그래프 관계(ADDED_FEATURE·OVERCOME_BY)로 더한 구성과 대응 방식을 채운다.

기록(`docs/oa300-records.json`)은 낱말 규칙으로 뽑은 미검토 요약이다. 의견서 본문이 없어 대응 방식은 최초·등록 독립항
요지 비교로 추정한 값이고, 등록된 건만 있어 생존 편향이 있다. 모델을 부르지 않는다. 이 기록은 발명 원자료가 아니며
게이트 값을 바꾸지 않는다(CLAUDE.md 소스 권한 · sources/10).
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from statistics import median

DUMP_FILE = "graph/oa300/kg_dump.json"
RECORDS_FILE = "docs/oa300-records.json"
DATASET = "kr-oa-300"

STRATEGIES = ("종속항병합", "신규구성추가", "수치·조건한정", "표현명확화", "청구항삭제", "카테고리변경", "의견서만", "공지예외·서류보완", "보정내용미확인")
# 더한 구성(ClaimFeature) 유형: 낱말 규칙. 한 구성이 여러 유형일 수 있다. 순서는 보고서 표의 순서다.
FEATURE_TYPES = {
    "NUM": "수치·범위",
    "POS": "위치·결합·배치",
    "SEQ": "단계·순서",
    "COND": "조건·모드·판단",
    "SHAPE": "형상",
    "MAT": "재질·성분 특정",
    "DATA": "데이터·신호 내용",
    "FUNC": "목적·용도·기능",
    "PART": "구성요소 추가",
    "CLAR": "명확화·삭제",
}
# 이 유형이 sources/10 `exam_pattern`의 어느 코드에 가까운지(가이드 문서의 대응표). 같은 기준이 아니라 읽기 도움이다.
EXAM_PATTERN_HINT = {
    "NUM": "NUMERIC_WITH_REASON / NUMERIC_NO_REASON", "POS": "REL_POSITION_COUPLING", "SEQ": "OPERATION_ORDER",
    "COND": "CONDITIONAL_BRANCH / MODE_OPERATION", "SHAPE": "SELECTION_SHAPE (관계 없이 형상만일 때)",
    "MAT": "SELECTION_MATERIAL / PURPOSE_POSITION_MATERIAL", "DATA": "SELECTION_INPUT_DATA", "FUNC": "PURPOSE_POSITION_MATERIAL·SUBASSEMBLY_ACTION의 목적 부분",
    "PART": "SUBASSEMBLY_ACTION (하위 구성이 한 동작을 이룰 때)", "CLAR": "— (범위 축소가 아님)",
}
_NUM = re.compile(r"\d+(?:[.,]\d+)?\s*(?:~|∼|-|내지|to)\s*\d|\d+(?:\.\d+)?\s*(?:%|℃|°C|°|㎛|μm|um|nm|mm|cm|m\b|kg|g\b|mg|ml|mL|mM|M\b|ppm|ppb|wt|중량|몰|배|초|분|시간|Hz|kHz|MHz|GHz|V\b|kV|mA|A\b|W\b|kW|Pa|MPa|kPa|bar|rpm|dB|Mw|nM|μM)|이상|이하|초과|미만|상한|하한|비율|함량")
_RULES = [
    ("CLAR", re.compile(r"명확화|명시|오기|삭제|정정|표현 정리|용어 변경|카테고리")),
    ("POS", re.compile(r"위치|배치|(?<!단일)(?<!이중)(?<!삼중)결합|연결|체결|고정|사이|상부|하부|상단|하단|측면|일측|타측|내측|외측|내부|외부|인접|대향|마주|관통|삽입|수용|장착|설치|둘레|중첩|이격|접촉|밀착|감싸|형성된|마련된")),
    ("SEQ", re.compile(r"단계|순서|후에|후\b|이후|먼저|다음|순차|선행|뒤\b|과정|공정")),
    ("COND", re.compile(r"경우|때\b|시에|시\b|조건|모드|판단|비교|임계|기준값|기준치|초과 시|미만 시|분기|선택적|여부|감지되면|검출되면|따라")),
    ("SHAPE", re.compile(r"형상|모양|단면|곡면|경사|테이퍼|원형|사각|나선|깔때기|오목|볼록|홈|돌기|돌출|리브|플랜지|슬롯|두께|폭|직경|길이")),
    ("MAT", re.compile(r"재질|재료|소재|수지|금속|합금|고분자|폴리|세라믹|유리|섬유|부직포|화합물|조성|성분|첨가제|용매|촉매|분말|입자|코팅|필름|서열|항체|변이체|치환기|단량체|염\b")),
    ("DATA", re.compile(r"데이터|정보|신호|파라미터|메시지|패킷|식별자|인덱스|필드|쿼리|모델|이미지|영상|값을|좌표")),
    ("FUNC", re.compile(r"위하여|위한|용도|목적|기능|역할|하도록|되도록|방지|억제|향상|개선|치료|예방")),
    ("PART", re.compile(r"부\b|부재|유닛|모듈|장치|기구|센서|밸브|노즐|펌프|모터|회로|서버|단말|패널|전극|층\b|판\b|커버|하우징|케이스|프레임|플레이트|어셈블리")),
]


def feature_types(text: str) -> list[str]:
    """더한 구성 문구의 유형 코드(FEATURE_TYPES 순서). 아무 규칙에도 걸리지 않으면 PART."""
    t = text or ""
    found = {"NUM"} if _NUM.search(t) else set()
    found |= {code for code, rx in _RULES if rx.search(t)}
    return [c for c in FEATURE_TYPES if c in found] or ["PART"]


# ---- 마크다운 청크 → 사례 기록 ---------------------------------------------------------------------------------------

_HEAD = re.compile(r"^# 등록특허 (10-\d{7}) 「(.*?)」")
_OA_HEAD = re.compile(r"^## (\d+)차 (?:의견제출통지서|거절결정서?)\s*\(([\d-]+),\s*([^)]*)\)")
_GROUND = re.compile(r"특허법\s*([^\s(]+)\s*\(([^)]*)\)(?:\s*—\s*대상\s*(.*))?")
_LAW_LABEL = {"29조1항": "신규성", "29조2항": "진보성", "42조4항2호": "명확성", "42조4항1호": "뒷받침", "42조3항1호": "실시가능",
              "42조8항": "기재방법", "45조": "단일성", "47조2항": "신규사항", "32조": "불특허", "29조1항본문": "발명성립성"}


def _cut_tail(s: str) -> str:
    return s.rstrip().rstrip("…").rstrip()


def _field(line: str, key: str) -> str | None:
    if line.startswith(f"- {key}"):
        return line.split(":", 1)[1].strip() if ":" in line else ""
    return None


def parse_case(text: str) -> dict:
    """한 사례의 심사 경과 요약 마크다운을 기록으로 바꾼다. 잘린 청크는 있는 데까지만 채우고 `truncated`를 단다."""
    truncated = text.rstrip().endswith("…")
    lines = text.splitlines()
    case: dict = dict(reg_no="", title="", appl_no="", applicant="", ipc="", subclass="", filed="", decided="", registered="",
                      priority=None, oa_count=None, concepts=[], initial_claim="", granted_claim="", oas=[], strategies=[],
                      added=[], amend_summary="", note="", truncated=truncated)
    section, oa = "", None
    for raw in lines:
        line = raw.rstrip()
        if m := _HEAD.match(line):
            case["reg_no"], case["title"] = m.group(1), m.group(2)
            continue
        if line.startswith("## "):
            oa = None
            if m := _OA_HEAD.match(line):
                oa = dict(n=int(m.group(1)), date=m.group(2), dept=m.group(3).strip(), final=False, examined="", rejected="", allowable="",
                          grounds=[], citations=[], summary="", suggestion="")
                case["oas"].append(oa)
                section = "OA"
            elif line.startswith("## 최초 출원"):
                section = "INITIAL"
            elif line.startswith("## 출원인 대응"):
                section = "RESPONSE"
            elif line.startswith("## 등록 독립항 요지"):
                section = "GRANTED"
            elif line.startswith("## 비고"):
                section = "NOTE"
            else:
                section = ""
            continue
        if not line.strip():
            continue
        if section == "" or (section == "OA" and oa is None and line.startswith("- 등록번호")):
            if (v := _field(line, "등록번호")) is not None:
                parts = [p.strip() for p in v.split("/")]
                case["appl_no"] = next((p.split(":", 1)[1].strip() for p in parts if p.startswith("출원번호")), "")
            elif (v := _field(line, "출원인")) is not None:
                case["applicant"] = v.split("/")[0].strip()
            elif (v := _field(line, "IPC")) is not None:
                case["ipc"] = v.split("(")[0].strip()
                if m := re.search(r"서브클래스\s*([A-H]\d{2}[A-Z])", v):
                    case["subclass"] = m.group(1)
            elif line.startswith("- 출원일"):
                for key, name in (("filed", "출원일"), ("decided", "등록결정일"), ("registered", "등록일")):
                    if m := re.search(name + r"\s*([\d-]{10})", line):
                        case[key] = m.group(1)
                if m := re.search(r"우선심사\s*(예|아니오)", line):
                    case["priority"] = m.group(1) == "예"
                if m := re.search(r"의견제출통지\s*(\d+)회", line):
                    case["oa_count"] = int(m.group(1))
            elif (v := _field(line, "핵심 기술 개념")) is not None:
                case["concepts"] = [c.strip() for c in _cut_tail(v).split(",") if c.strip()]
        elif section == "INITIAL":
            case["initial_claim"] = (case["initial_claim"] + " " + _cut_tail(line)).strip()
        elif section == "GRANTED":
            case["granted_claim"] = (case["granted_claim"] + " " + _cut_tail(line)).strip()
        elif section == "NOTE":
            case["note"] = (case["note"] + " " + _cut_tail(line)).strip()
        elif section == "OA" and oa is not None:
            if (v := _field(line, "심사 대상 청구항")) is not None:
                bits = [b.strip() for b in line[2:].split("/")]
                for b in bits:
                    k, _, val = b.partition(":")
                    val = _cut_tail(val.strip())
                    if k.startswith("심사 대상"):
                        oa["examined"] = val
                    elif k.startswith("거절 청구항"):
                        oa["rejected"] = val
                    elif k.startswith("등록 가능"):
                        oa["allowable"] = "" if val == "없음" else val
            elif (v := _field(line, "거절 근거")) is not None:
                if m := _GROUND.search(v):
                    law = m.group(1)
                    oa["grounds"].append(dict(law=law, label=_LAW_LABEL.get(law, m.group(2).split("(")[0].strip()), claims=_cut_tail(m.group(3) or "")))
            elif line.startswith("- 인용문헌"):
                head, _, doc = line[2:].partition(":")
                role = re.search(r"\(([^)]*)\)\s*$", doc)
                oa["citations"].append(dict(label=head.replace("인용문헌", "").strip(), doc=re.sub(r"\s*\([^)]*\)\s*$", "", doc).strip(),
                                            role=role.group(1) if role else ""))
            elif (v := _field(line, "거절이유 요약")) is not None:
                oa["summary"] = _cut_tail(v)
                oa["final"] = oa["final"] or "최후" in v
            elif (v := _field(line, "심사관 보정 시사")) is not None:
                oa["suggestion"] = _cut_tail(v)
        elif section == "RESPONSE":
            if (v := _field(line, "추정 대응 방식")) is not None:
                case["strategies"] = [s.strip() for s in _cut_tail(v.split(":", 1)[-1] if v.startswith("최초") else v).split(",") if s.strip() in STRATEGIES]
            elif (v := _field(line, "등록 독립항에 추가된 구성")) is not None:
                v = _cut_tail(v)
                if v:
                    case["added"].append(v)
            elif (v := _field(line, "보정 요약")) is not None:
                case["amend_summary"] = _cut_tail(v)
    case["final_oa"] = any(o["final"] for o in case["oas"]) or "최후" in case["note"]
    return case


def _patent_key(name: str) -> str:
    m = re.match(r"(10-\d{7})", name or "")
    return m.group(1) if m else ""


def build_records(dump: dict) -> dict:
    """kg_search 덤프 → 사례 기록 묶음. 관계의 ADDED_FEATURE·OVERCOME_BY로 잘린 끝부분을 채운다."""
    rel_added: dict[str, list[str]] = {}
    rel_strat: dict[str, list[str]] = {}
    cites: dict[str, set[str]] = {}          # 사례 → 그 통지서가 인용한 문헌
    discloses: dict[str, set[str]] = {}      # 문헌 → 그래프가 그 문헌의 개시 내용으로 정리한 기술 개념
    for r in dump.get("relationships") or []:
        key = _patent_key(r.get("source", ""))
        if r.get("relation") == "ADDED_FEATURE" and key:
            rel_added.setdefault(key, []).append(r["target"])
        elif r.get("relation") == "OVERCOME_BY" and key:
            rel_strat.setdefault(key, []).append(r["target"])
        elif r.get("relation") == "CITES" and key:
            cites.setdefault(key, set()).add(r["target"])
        elif r.get("relation") == "DISCLOSES":
            discloses.setdefault(r["source"], set()).add(r["target"])
    cases = []
    for chunk in sorted(dump.get("chunks") or [], key=lambda c: c.get("source", "")):
        c = parse_case(chunk.get("text") or "")
        if not c["reg_no"]:
            c["reg_no"] = "10-" + Path(chunk.get("source", "")).stem
        c["source"] = chunk.get("source", "")
        key = c["reg_no"]
        filled = []
        if key in rel_added and (c["truncated"] or not c["added"]):
            known = set(c["added"])
            extra = [a for a in dict.fromkeys(rel_added[key]) if a not in known and not any(k.startswith(a[:12]) for k in known)]
            if extra:
                c["added"] += extra
                filled.append("added")
        if key in rel_strat and (c["truncated"] or not c["strategies"]):
            s = list(dict.fromkeys(c["strategies"] + [x for x in rel_strat[key] if x in STRATEGIES]))
            if s != c["strategies"]:
                c["strategies"] = s
                filled.append("strategies")
        c["filled_from_graph"] = filled
        c["added_types"] = [feature_types(a) for a in c["added"]]
        # 심사관이 인용문헌의 개시 내용으로 본 기술 개념(그래프 관계, LLM 추출). 덤프에 그 관계가 들어온 만큼만 있다.
        c["disclosed"] = sorted({t for doc in cites.get(key, ()) for t in discloses.get(doc, ())})
        cases.append(c)
    return dict(dataset=DATASET, note="Neo4j GraphRAG `kr-oa-300` 사례 요약(LLM 작성, 의견서 본문 없음, 대응 방식은 청구항 비교 추정)을 낱말 규칙으로 "
                "나눈 미검토 기록. 등록된 건만 있어 생존 편향이 있다. 발명 원자료·게이트·근거가 아니다.",
                truncated=sum(c["truncated"] for c in cases), cases=cases)


def import_dump(root: Path, dump: Path | None = None) -> dict:
    data = build_records(json.loads((dump or root / DUMP_FILE).read_text(encoding="utf-8")))
    out = root / RECORDS_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load(root: Path) -> list[dict]:
    path = root / RECORDS_FILE
    if not path.exists():
        raise FileNotFoundError(f"{RECORDS_FILE}이 없습니다. `claim-agent graph oa import`로 만드세요.")
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


# ---- 비슷한 사례 ----------------------------------------------------------------------------------------------------

_IPC = re.compile(r"\b([A-H]\d{2}[A-Z])(?:\s*\d{1,4}/\d+)?\b")


def _grams(text: str) -> Counter:
    words = re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}", text or "")
    out: Counter = Counter()
    for w in words:
        w = w.lower()
        out.update(w[i:i + 2] for i in range(len(w) - 1))
    return out


def _case_text(c: dict) -> str:
    return " ".join([c["title"], " ".join(c["concepts"]), c["initial_claim"], c["granted_claim"], " ".join(c["added"])])


def query_ipcs(text: str) -> list[str]:
    return list(dict.fromkeys(_IPC.findall(text or "")))


_IDF: dict[int, tuple[dict[str, float], list[Counter]]] = {}


def _index(cases: list[dict]) -> tuple[dict[str, float], list[Counter]]:
    """사례 묶음의 2-gram과 IDF(같은 묶음이면 한 번만 만든다)."""
    key = id(cases)
    if key not in _IDF:
        grams = [_grams(_case_text(c)) for c in cases]
        df = Counter(t for g in grams for t in g)
        n = len(cases) or 1
        _IDF.clear()
        _IDF[key] = ({t: math.log((1 + n) / (1 + d)) + 1 for t, d in df.items()}, grams)
    return _IDF[key]


def similar(cases: list[dict], text: str, k: int = 12, ipcs: list[str] | None = None) -> list[tuple[float, dict]]:
    """글자 2-gram TF-IDF 코사인 + 같은 IPC 서브클래스 가산. 의미 검색이 아니다."""
    idf, grams = _index(cases)
    floor = min(idf.values()) if idf else 1.0
    q = {t: v * idf.get(t, floor) for t, v in _grams(text).items() if t in idf}
    ipcs = ipcs if ipcs is not None else query_ipcs(text)
    qn = math.sqrt(sum(v * v for v in q.values())) or 1.0
    scored = []
    for c, g in zip(cases, grams):
        w = {t: v * idf[t] for t, v in g.items()}
        dot = sum(v * w.get(t, 0) for t, v in q.items())
        s = dot / (qn * (math.sqrt(sum(v * v for v in w.values())) or 1.0))
        if c["subclass"] in ipcs:
            s += 0.15
        elif c["subclass"] and c["subclass"][:3] in {i[:3] for i in ipcs}:
            s += 0.05
        if s > 0.02:
            scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def neighbourhood(cases: list[dict], text: str, k: int = 12) -> tuple[list[tuple[float, dict]], list[dict], str]:
    """(가까운 사례 k개, 분야 집합, 분야 이름). 분야는 질의에 IPC가 있으면 그 서브클래스, 없으면 가까운 사례에 가장 많은 섹션(A~H)."""
    near = similar(cases, text, k)
    ipcs = query_ipcs(text)
    if ipcs:
        field = [c for c in cases if c["subclass"] in ipcs]
        name = "IPC " + ", ".join(ipcs)
        if len(field) >= 5:
            return near, field, name
    sections = Counter(c["subclass"][:1] for _, c in near if c["subclass"])
    if sections:
        sec = sections.most_common(1)[0][0]
        return near, [c for c in cases if c["subclass"][:1] == sec], f"IPC 섹션 {sec}(가까운 사례 기준)"
    return near, cases, "전체"


# ---- 집계 ---------------------------------------------------------------------------------------------------------

def grounds(c: dict) -> set[str]:
    return {g["label"] for o in c["oas"] for g in o["grounds"]}


def type_counts(cs: list[dict]) -> Counter:
    return Counter(t for c in cs for ts in c["added_types"] for t in ts)


def stats(cs: list[dict]) -> dict:
    """사례 묶음의 기본 집계."""
    n = len(cs) or 1
    g = Counter(x for c in cs for x in grounds(c))
    strat = Counter(s for c in cs for s in c["strategies"])
    oas = [o for c in cs for o in c["oas"]]
    added_n = [len(c["added"]) for c in cs if c["added"]]
    return dict(cases=len(cs), grounds=g, strategies=strat, oa_total=len(oas),
                one_oa=sum(1 for c in cs if (c["oa_count"] or len(c["oas"])) == 1), final=sum(c["final_oa"] for c in cs),
                allowable_oas=sum(1 for o in oas if o["allowable"]), allowable_cases=sum(1 for c in cs if any(o["allowable"] for o in c["oas"])),
                merged=strat["종속항병합"], added_median=_num(median(added_n)) if added_n else 0, types=type_counts(cs),
                novelty_share=round(100 * g["신규성"] / n), suggestion=sum(1 for o in oas if o["suggestion"]), n=n)


def _num(x: float):
    return int(x) if float(x).is_integer() else round(x, 1)


def pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "—"


def cut(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
