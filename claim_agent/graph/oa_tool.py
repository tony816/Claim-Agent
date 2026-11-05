"""작업 도구 패널의 OA 도구: OA 300건 그래프(등록 사례의 심사 경과 요약)로 명세서·청구항 작성 단계를 역산해 보는 참고 조회.

- `oa_dependents` 종속항 역산: 등록 독립항에 편입된 구성 = 처음부터 종속항으로 넣어 둘 가치가 있던 한정. 분야별로 어떤 유형의
  한정이 실제 방어선이 됐는지, 내 종속항 세트가 그 유형을 덮는지 본다.
- `oa_gap` 독립항 갭: 최초 독립항 요지와 등록 독립항 요지를 나란히 놓아 처음에 어디까지 넓게 써도 됐는지의 분야별 감각과
  과도하게 넓었던 독립항의 전형(신규성 거절)을 본다.
- `oa_numeric` 수치한정 뒷받침: 수치·조건 한정이 다투어진 방식(겹침·최적화·불명확·뒷받침)을 세고, 명세서에 범위·실시예·비교예를
  어느 밀도로 적어 둘지 체크리스트로 묻는다.

모델·Jev를 부르지 않는다. 답은 판례 도구와 같이 사용자만 보는 참고이며 대화 기록·원자료·검수 대상에 넣지 않는다
(web.conversational). 이 기록으로 한정·종속항 후보를 만들거나 게이트 값을 바꾸지 않는다(sources/10 사용 경계).
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from . import oa300 as oa

TOOLS = {"oa_dependents": "종속항 역산", "oa_gap": "독립항 갭", "oa_numeric": "수치한정 뒷받침"}
MAX_TEXT = 12000
NEAR_K = 6
PRIVATE = ("이 답은 사용자만 보는 참고입니다. 대화 기록에 넣지 않으므로 일반 에이전트·검수 역할·청구항 작성 역할은 읽지 않습니다. "
           "등록된 300건의 심사 경과 요약(의견서 본문 없음, 대응 방식은 청구항 비교 추정, 낱말 규칙 분류)에서 센 값이며 등록 가능성 예측이 아닙니다. "
           "이 기록으로 한정이나 종속항 후보를 만들지 않습니다 — 청구하는 구성은 원자료에 있어야 합니다.")


def run(tool: str, *, root: Path, text: str = "", claims: str = "", target: str = "") -> str:
    """tool의 답(마크다운). `claims`는 대상 메시지의 청구항 블록(없어도 됨), `text`는 입력창에 적은 분야·특징·문언."""
    if tool not in TOOLS:
        raise ValueError("지원하지 않는 OA 도구입니다.")
    text, claims = text.strip(), claims.strip()
    if not text and not claims:
        raise ValueError(f"{TOOLS[tool]}는 대상 청구항을 고르거나 입력창에 분야·청구항 문언을 적고 Enter를 누르세요.")
    if len(text) + len(claims) > MAX_TEXT:
        raise ValueError(f"OA 도구에는 {MAX_TEXT}자까지 넣을 수 있습니다.")
    try:
        cases = oa.load(root)
    except FileNotFoundError as exc:
        raise ValueError(f"OA 기록을 읽지 못했습니다: {exc}") from exc
    body, note = {"oa_dependents": _dependents, "oa_gap": _gap, "oa_numeric": _numeric}[tool](cases, text, claims)
    head = f"**OA 참고 (미검토 — 게이트·근거 아님)** · {TOOLS[tool]}" + (f" · 대상: {target}" if target else "")
    return "\n\n".join([head, PRIVATE, body, "※ " + note])


# ---- 공통 ------------------------------------------------------------------------------------------------------------

def _claims(claims: str) -> list:
    if not claims:
        return []
    from ..pipeline.claimtext import ClaimParseError, parse_claim_set

    try:
        return parse_claim_set(claims)
    except ClaimParseError:
        return []


def _types_label(codes: list[str]) -> str:
    return "·".join(oa.FEATURE_TYPES[c] for c in codes)


def _case_head(c: dict) -> str:
    n = c["oa_count"] or len(c["oas"])
    return (f"**{c['reg_no']} 「{oa.cut(c['title'], 40)}」** ({c['subclass'] or 'IPC ?'}, 통지 {n}회" + (", 최후통지" if c["final_oa"] else "")
            + (", 본문 일부 잘림" if c["truncated"] else "") + ")")


def _allowable(c: dict) -> str:
    hits = [f"{o['n']}차 {o['allowable']}" for o in c["oas"] if o["allowable"]]
    return "; ".join(hits)


def _near_block(near: list[tuple[float, dict]], line) -> list[str]:
    return [line(c) for _, c in near] or ["- 글자가 겹치는 사례가 없습니다. 분야(IPC 서브클래스, 예: `B65D`)나 핵심 구성 낱말을 함께 적어 보세요."]


def _field_line(name: str, field: list[dict], everyone: list[dict]) -> str:
    return f"분야: {name} — 사례 {len(field)}건 (전체 {len(everyone)}건)"


# ---- 9. 종속항 역산 ---------------------------------------------------------------------------------------------------

def _dependent_types(parsed: list) -> list[tuple[int, list[str]]]:
    """내 종속항마다 더하는 내용의 유형(부모항 인용 문구를 뺀 본문에 같은 낱말 규칙)."""
    out = []
    indep = next((c for c in parsed if c.is_independent), None)
    # 종속항 끝의 발명 명칭(독립항과 같은 마지막 낱말들)은 더하는 내용이 아니므로 뗀다('체결 디바이스'의 '체결' 등).
    title = re.sub(r"[.\s]+$", "", indep.body).split()[-3:] if indep else []
    for c in parsed:
        if c.is_independent:
            continue
        body = re.sub(r"^\s*제\s*\d+\s*항[^,，]*?(?:있어서|에 있어서|에 따른|에서)\s*,?", "", c.body.strip())
        words = re.sub(r"[.\s]+$", "", body).split()
        for k in range(len(title), 0, -1):
            if len(words) > k and words[-k:] == title[-k:]:
                words = words[:-k]
                break
        out.append((c.claim_no, oa.feature_types(" ".join(words))))
    return out


def _dependents(cases: list[dict], text: str, claims: str) -> tuple[str, str]:
    query = "\n".join(x for x in (text, claims) if x)
    near, field, name = oa.neighbourhood(cases, query, NEAR_K)
    fs, al = oa.stats(field), oa.stats(cases)
    ft, at = fs["types"], al["types"]
    f_total, a_total = sum(ft.values()) or 1, sum(at.values()) or 1
    merged_after_allow = sum(1 for c in field if any(o["allowable"] for o in c["oas"]) and "종속항병합" in c["strategies"])
    L = [f"### 이 분야에서 방어선이 된 한정", "", _field_line(name, field, cases), "",
         f"- 종속항을 독립항에 합쳐 등록: {fs['merged']}건({oa.pct(fs['merged'], fs['cases'])}, 전체 {oa.pct(al['merged'], al['cases'])}).",
         f"- 심사관이 통지서에 '등록 가능 청구항'을 적시: {fs['allowable_cases']}건({oa.pct(fs['allowable_cases'], fs['cases'])}) — 그중 종속항 병합으로 등록 {merged_after_allow}건.",
         f"- 등록 독립항에 더한 구성은 사례당 중앙값 {fs['added_median']}개.", "",
         "| 더한 구성 유형 | 이 분야 | 비율 | 전체 300건 비율 |", "|---|---|---|---|"]
    for code, label in oa.FEATURE_TYPES.items():
        if ft[code] or at[code]:
            L.append(f"| {label} | {ft[code]} | {oa.pct(ft[code], f_total)} | {oa.pct(at[code], a_total)} |")
    parsed = _claims(claims)
    deps = _dependent_types(parsed)
    if deps:
        mine = Counter(t for _, ts in deps for t in ts)
        top = [code for code, _ in ft.most_common() if code not in ("PART", "CLAR")][:5]
        L += ["", "### 내 종속항이 덮는 유형", "", "| 유형 | 이 분야에서 더해진 수 | 내 종속항 |", "|---|---|---|"]
        for code in top:
            nos = [str(no) for no, ts in deps if code in ts]
            L.append(f"| {oa.FEATURE_TYPES[code]} | {ft[code]} | {', '.join('제' + n + '항' for n in nos) or '없음'} |")
        missing = [oa.FEATURE_TYPES[c] for c in top if not mine[c]]
        L += ["", (f"- 이 분야에서 자주 방어선이 된 유형 가운데 내 종속항에 없는 것: {', '.join(missing)}. 원자료에 그 유형의 구성이 있을 때만 "
                   "종속항 후보로 08 게이트(과제→특징→원리→효과)를 거치게 하세요. 원자료에 없으면 만들지 않습니다." if missing else
                   "- 이 분야에서 자주 방어선이 된 유형이 내 종속항에 모두 있습니다."),
              f"- 종속항 {len(deps)}개를 낱말 규칙으로 분류했습니다(항마다 여러 유형일 수 있음)."]
    elif claims:
        L += ["", "- 대상 메시지에 종속항이 없어 '내 종속항이 덮는 유형'은 건너뛰었습니다."]

    def line(c: dict) -> str:
        added = "; ".join(f"{oa.cut(a, 60)} [{_types_label(t)}]" for a, t in zip(c["added"], c["added_types"])) or "기록 없음"
        out = [f"- {_case_head(c)}", f"  - 최초 독립항: {oa.cut(c['initial_claim'], 140)}", f"  - 더한 구성: {added}",
               f"  - 대응(추정): {', '.join(c['strategies']) or '기록 없음'}" + (f" · 등록 가능 청구항 적시: {_allowable(c)}" if _allowable(c) else "")]
        if c["amend_summary"]:
            out.append(f"  - 보정 요약: {oa.cut(c['amend_summary'], 160)}")
        return "\n".join(out)

    L += ["", "### 가까운 사례 (글자 겹침 순)", ""] + _near_block(near, line)
    return "\n".join(L), ("'등록 가능 청구항'과 종속항 병합은 통지서 요약에 적힌 대로 셌고, 더한 구성의 유형은 낱말 규칙 분류입니다. "
                          "더한 구성이 최초 종속항에서 왔는지 본문 문단에서 왔는지는 이 데이터에 사례 단위(대응 방식)로만 있습니다. "
                          "같은 판단의 30건 의견서 기반 근거는 sources/10 E7을 보세요.")


# ---- 10. 독립항 갭 ---------------------------------------------------------------------------------------------------

def _gap(cases: list[dict], text: str, claims: str) -> tuple[str, str]:
    query = "\n".join(x for x in (text, claims) if x)
    near, field, name = oa.neighbourhood(cases, query, NEAR_K)
    fs, al = oa.stats(field), oa.stats(cases)
    L = ["### 처음에 어디까지 넓게 썼고, 등록까지 얼마나 좁혔나", "", _field_line(name, field, cases), "",
         f"- 신규성 거절(최초 독립항이 인용발명 하나에 그대로 읽힘): {fs['grounds']['신규성']}건({oa.pct(fs['grounds']['신규성'], fs['cases'])}, 전체 {oa.pct(al['grounds']['신규성'], al['cases'])}) — 과도하게 넓었던 신호.",
         f"- 진보성 거절: {fs['grounds']['진보성']}건({oa.pct(fs['grounds']['진보성'], fs['cases'])}). 명확성 지적: {fs['grounds']['명확성']}건({oa.pct(fs['grounds']['명확성'], fs['cases'])}).",
         f"- 통지 1회로 등록: {fs['one_oa']}건({oa.pct(fs['one_oa'], fs['cases'])}, 전체 {oa.pct(al['one_oa'], al['cases'])}). 최후통지까지 간 사례: {fs['final']}건.",
         f"- 등록까지 더한 구성 수 중앙값 {fs['added_median']}개(전체 {al['added_median']}개).",
         "- 대응 방식(추정): " + ", ".join(f"{k} {v}" for k, v in fs["strategies"].most_common(5)) + "."]
    broad = [c for _, c in oa.similar(field, query, 40) if "신규성" in oa.grounds(c)][:4]
    if broad:
        L += ["", "### 과도하게 넓었던 전형 (이 분야의 신규성 거절, 가까운 순)", ""]
        for c in broad:
            first = next((o for o in c["oas"] if any(g["label"] == "신규성" for g in o["grounds"])), c["oas"][0])
            L += [f"- {_case_head(c)}", f"  - 최초 독립항: {oa.cut(c['initial_claim'], 150)}",
                  f"  - 심사관이 본 인용발명: {oa.cut(first['summary'], 170)}",
                  f"  - 등록 독립항: {oa.cut(c['granted_claim'], 150) if c['granted_claim'] else '본문 잘림 — 더한 구성: ' + '; '.join(oa.cut(a, 50) for a in c['added'])}"]
    parsed = _claims(claims)
    indep = [c for c in parsed if c.is_independent]
    if indep:
        known = Counter(t for _, c in near for t in c["disclosed"])
        flat = re.sub(r"\s+", "", indep[0].body)
        overlap = [t for t in known if len(t) >= 2 and re.sub(r"\s+", "", t) in flat]
        L += ["", f"### 내 독립항(제{indep[0].claim_no}항)과 견주기", "",
              "- 아래 가까운 사례의 '심사관이 본 인용발명'에 나온 구성만으로 내 독립항이 다 채워지는지 확인하세요. 그렇다면 신규성 거절 전형과 같은 넓이입니다.",
              f"- 가까운 사례에서 인용문헌이 개시한 개념으로 정리된 낱말 가운데 내 독립항에 그대로 있는 것: {', '.join(overlap) if overlap else '없음(그래프 관계가 들어온 사례만 봄)'}."]

    def line(c: dict) -> str:
        first = c["oas"][0] if c["oas"] else {"summary": ""}
        return "\n".join([f"- {_case_head(c)} · 거절: {', '.join(sorted(oa.grounds(c))) or '기록 없음'}",
                          f"  - 최초: {oa.cut(c['initial_claim'], 130)}",
                          f"  - 심사관이 본 인용발명: {oa.cut(first['summary'], 150)}",
                          f"  - 등록: {oa.cut(c['granted_claim'], 130) if c['granted_claim'] else '본문 잘림 — 더한 구성: ' + '; '.join(oa.cut(a, 40) for a in c['added'])}"])

    L += ["", "### 가까운 사례 (글자 겹침 순)", ""] + _near_block(near, line)
    return "\n".join(L), ("요지는 사례 요약을 쓴 모델이 줄인 문장이라 청구항 전문의 길이·구성 수와 같지 않습니다. 넓이의 감각을 보는 참고이며, "
                          "독립항의 최소충분 한정은 성공조건 조건 8과 선행기술 대비로만 정합니다(sources/10 '독립항 생성에 쓰지 않는 이유').")


# ---- 11. 수치한정 뒷받침 ----------------------------------------------------------------------------------------------

_NUM_CTX = re.compile(r"\d|수치|범위|함량|농도|온도|두께|[률율]|중량|몰|길이|폭|직경|압력|시간|크기|속도")
# (코드, 심사관 논리, 정규식, 명세서 체크 질문)
NUM_ISSUES = [
    ("OVERLAP", "인용발명 수치와 겹침·포함·환산 동일(신규성)", re.compile(r"겹|포함되|중첩|환산|실시예.{0,30}(?:동일|해당|만족)|범위.{0,12}(?:동일|일치|만족)"),
     "인용 수치와 겹칠 수 있는 범위인가? 겹치는 구간을 빼고도 효과가 서는 하위 범위와 그 구간의 실시예를 적어 두었나"),
    ("OPTIMIZE", "반복 실험·통상의 최적화·임계적 의의 없음(진보성)", re.compile(r"반복\s*실험|최적화|최적\s*수치|최적값|임계적\s*의의|통상의\s*(?:선택|최적|설계)|적절히\s*(?:선택|조절|설정)|단순\s*(?:설계\s*변경|수치)"),
     "상·하한마다 그 밖으로 가면 무엇이 나빠지는지(이유)와 범위 안팎 비교예·결과 수치를 적어 두었나"),
    ("UNCLEAR", "수치 표현·측정 조건 불명확", re.compile(r"'약'|‘약’|약\s*\d|0이\s*포함|측정\s*(?:방법|조건)|단위|기준(?:이|이\s*없|\s*불명)|상한(?:이|만)|하한(?:이|만)"),
     "측정 방법·조건·단위와 0·상한 포함 여부를 정의했나, '약'·'이상' 같은 표현이 기준 없이 남지 않았나"),
    ("SUPPORT", "범위 전체의 뒷받침·실시가능", re.compile(r"뒷받침|실시예.{0,20}(?:만|일부|뿐)|범위\s*전체|실시\s*가능|재현"),
     "실시예 수치가 범위의 양 끝 가까이까지 흩어져 범위 전체를 뒷받침하나"),
]


def numerics(text: str) -> list[str]:
    """문언 속 수치 한정 구절(앞뒤 문맥 조금)."""
    out = []
    for m in re.finditer(r"\d+(?:[.,]\d+)?\s*(?:~|∼|-|내지)\s*\d+(?:[.,]\d+)?\s*[^\s,;.]{0,6}|\d+(?:[.,]\d+)?\s*[^\s\d,;.]{0,5}\s*(?:이상|이하|초과|미만)", text or ""):
        cut_at = max(text.rfind(ch, 0, m.start()) for ch in (",", ";", "\n"))
        tail = re.match(r"[^\s,;.]*", text[m.end():])
        s, e = max(cut_at + 1, m.start() - 30), m.end() + len(tail.group(0))
        out.append(re.sub(r"\s+", " ", text[s:e]).strip())
    return list(dict.fromkeys(out))[:8]


def _issue_hits(cases: list[dict]) -> dict[str, list[tuple[dict, dict]]]:
    hits: dict[str, list[tuple[dict, dict]]] = {code: [] for code, *_ in NUM_ISSUES}
    for c in cases:
        for o in c["oas"]:
            s = o["summary"]
            if not _NUM_CTX.search(s):
                continue
            for code, _, rx, _ in NUM_ISSUES:
                if rx.search(s):
                    hits[code].append((c, o))
    return hits


def _numeric(cases: list[dict], text: str, claims: str) -> tuple[str, str]:
    query = "\n".join(x for x in (text, claims) if x)
    mine = numerics(query)
    hits = _issue_hits(cases)
    oas = sum(len(c["oas"]) for c in cases)
    num_cases = [c for c in cases if any("NUM" in t for t in c["added_types"])]
    strat = sum(1 for c in cases if "수치·조건한정" in c["strategies"])
    L = ["### 내 문언의 수치 한정", ""]
    L += [f"- `{m}`" for m in mine] or ["- 수치 범위·이상/이하 구절을 찾지 못했습니다. 아래 집계와 체크리스트만 봅니다."]
    L += ["", f"### 이 데이터에서 수치가 다투어진 방식 (전체 {len(cases)}건 · 통지 {oas}건)", "",
          "| 심사관 논리 | 통지 수 | 예 |", "|---|---|---|"]
    for code, label, _, _ in NUM_ISSUES:
        ex = hits[code][0] if hits[code] else None
        L.append(f"| {label} | {len(hits[code])} | " + (f"{ex[0]['reg_no']}: {oa.cut(ex[1]['summary'], 90)}" if ex else "—") + " |")
    L += ["", f"- 대응 방식에 수치·조건한정이 있는 등록 {strat}건, 등록 독립항에 수치·범위를 더한 사례 {len(num_cases)}건.",
          "- 더한 수치 예: " + ("; ".join([oa.cut(a, 50) for c in num_cases[:40] for a, t in zip(c["added"], c["added_types"]) if "NUM" in t][:6]) or "없음") + ".",
          "", "### 명세서 체크리스트 (수치·조건 한정마다)", ""]
    for i, (code, label, _, question) in enumerate(NUM_ISSUES, 1):
        L.append(f"{i}. {question} — 이 데이터에서 '{label}'로 다투어진 통지 {len(hits[code])}건.")
    L.append(f"{len(NUM_ISSUES) + 1}. 보정할 때 근거 문단을 들 수 있게, 범위와 그 결과·이유를 같은 용어로 한 문단 안에 적었나 — "
             "등록 사례는 범위 뒤에 그 결과를 이어 적었다(sources/10 E7 c·E8 d, 30건 의견서 기준).")
    if mine:
        L += ["", "### 내 수치와 가까운 사례", ""]
        pool = [c for c in cases if c in num_cases or any(_NUM_CTX.search(o["summary"]) and any(rx.search(o["summary"]) for _, _, rx, _ in NUM_ISSUES) for o in c["oas"])]
        for m in mine[:4]:
            near = oa.similar(pool, m + "\n" + query, 2)
            L.append(f"- `{m}`")
            for _, c in near:
                o = next((o for o in c["oas"] if any(rx.search(o["summary"]) for _, _, rx, _ in NUM_ISSUES)), c["oas"][0])
                added = "; ".join(oa.cut(a, 45) for a, t in zip(c["added"], c["added_types"]) if "NUM" in t) or "수치 추가 없음"
                L.append(f"  - {_case_head(c)} — 지적: {oa.cut(o['summary'], 120)} → 더한 수치: {added}")
            if not near:
                L.append("  - 가까운 사례 없음")
    return "\n".join(L), ("심사관 논리는 통지서 요약 문장의 낱말로 나눴고 한 통지가 여러 논리에 걸릴 수 있습니다. 체크리스트는 질문이며, "
                          "원자료에 없는 수치·비교예를 만들지 않습니다 — 없으면 발명자 확인 질문으로 남깁니다.")
