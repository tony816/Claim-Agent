"""작업 도구 패널의 판례 도구: 권리행사 점검·판례 찾기·특허성 신호를 판례 기록으로 만들어 사용자에게 보인다.

모델을 부르지 않는다. Jev를 쓸 수 있으면 후보의 관련성을 한 요청으로 묻고, 못 쓰면 글자 겹침 순 후보를 'Jev 미확인'으로 둔다.
결과는 미검토 참고이며 게이트·근거가 아니다. 대화 기록·원자료·검수 대상에 넣지 않는 것은 web.Workspace.caselaw가 맡는다
(판례 원문은 작성 역할에 넘기지 않는다 — CLAUDE.md 판례 DB, docs/caselaw.md).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path

from . import caselaw as cl

TOOLS = {"enforce": "권리행사 점검", "find": "판례 찾기", "features": "특허성 신호"}
MAX_TEXT = 4000
MAX_FEATURES = 8
# 판례 찾기가 보는 기록 묶음: (이름, 보여 줄 이름, Jev 질문)
FIND_SETS = (("lessons", "명확성·뒷받침·권리범위", cl.JEV_QUESTION), ("enforcement", "권리행사", cl.JEV_ENFORCE), ("patentability", "특허성", cl.JEV_PATENT))
OVERLAP_K, HINT_K, MAX_HINTS = 3, 2, 3      # 글자 겹침 후보 수, 짐작한 유형마다 더할 후보 수, 유형 수
PRIVATE = ("이 답은 사용자만 보는 참고입니다. 대화 기록에 넣지 않으므로 일반 에이전트·검수 역할·청구항 작성 역할은 읽지 않습니다. "
           "판례만으로 신규성·진보성을 판정하지 않고, 판례가 붙었다고 판정을 올리거나 내리지 않습니다.")


def run(tool: str, *, root: Path, paths: cl.Paths, text: str = "", claims: str = "", target: str = "", client=None) -> str:
    """tool의 답(마크다운). `claims`는 권리행사 점검 대상 청구항 블록, `text`는 입력창에 적은 지적·특징, `target`은 대상 메시지 이름.
    client는 Jev 클라이언트(시험용). None이면 jev.status()로 쓸 수 있을 때만 만든다."""
    if tool not in TOOLS:
        raise ValueError("지원하지 않는 판례 도구입니다.")
    text = text.strip()
    if tool == "enforce" and not claims.strip():
        raise ValueError("권리행사 점검은 【청구항 N】 문언이 있는 메시지를 대상으로 고르세요.")
    if tool != "enforce" and not text:
        raise ValueError(f"{TOOLS[tool]}는 입력창에 {'지적이나 청구항 문언' if tool == 'find' else '특징(한 줄에 하나)'}을 적고 Enter를 누르세요.")
    if len(text) > MAX_TEXT:
        raise ValueError(f"판례 도구에는 {MAX_TEXT}자까지 적을 수 있습니다.")
    try:
        body, pairs_note = {"enforce": _enforce, "find": _find, "features": _features}[tool](root, paths, text, claims, client)
    except (cl.CaseLawError, FileNotFoundError) as exc:
        raise ValueError(f"판례 기록을 읽지 못했습니다: {exc} (docs/caselaw.md)") from exc
    head = f"**판례 참고 (미검토 — 게이트·근거 아님)** · {TOOLS[tool]}" + (f" · 대상: {target}" if target else "")
    return "\n\n".join([head, PRIVATE, body, "※ " + pairs_note])


def _confirm(pairs: list[tuple[str, dict, str]], client) -> tuple[list[float | None] | None, str]:
    """쌍마다 Jev 확률. 쓸 수 없거나 실패하면 (None, 이유)."""
    if not pairs:
        return None, "Jev 미호출 — 후보 없음"
    if client is None:
        from ..prior_art import jev

        ok, why = jev.status()
        if not ok:
            return None, f"Jev 미확인 — {why} 아래 후보는 글자 겹침 순이며 관련성을 확인하지 않았습니다."
        client = jev.JevClient()
    res = cl.confirm_pairs(pairs, client)
    if res.get("status") != "RUN":
        return None, f"Jev 미확인 — {res.get('status')}: {res.get('error') or '응답 없음'}. 아래 후보는 글자 겹침 순입니다."
    usd = res.get("cost_usd")
    cost = "" if not isinstance(usd, int | float) else " · $0.0001 미만" if usd < 0.0001 else f" · ${usd:.4f}"
    probs = res.get("probabilities") or {}
    return ([probs.get(f"case_{i}") for i in range(len(pairs))],
            f"Jev 관련성 확인 1회(OpenRouter{cost}) — 확률 {cl.JEV_THRESHOLD} 미만 후보는 뺐습니다. {cl.STRONG} 이상은 'Jev 확인', 그 사이는 '약한 관련'.")


def _keep(cases: list[dict], probs: list[float | None] | None, start: int, limit: int) -> list[tuple[dict, float | None]]:
    """Jev가 돌았으면 기준 이상만, 아니면 순서대로 limit개."""
    rows = [(r, probs[start + i] if probs else None) for i, r in enumerate(cases)]
    if probs:
        rows = sorted([x for x in rows if x[1] is not None and x[1] >= cl.JEV_THRESHOLD], key=lambda x: -x[1])
    return rows[:limit]


def _verifier(paths: cl.Paths) -> tuple[sqlite3.Connection | None, str]:
    """인용문을 원문과 대조할 판례 DB 연결(호출자가 닫는다). DB가 없거나 검사에 실패하면 (None, 이유)."""
    try:
        db = cl.connect(paths)
    except (cl.CaseLawError, sqlite3.Error) as exc:
        return None, f"판례 DB 없음 — 인용 원문 대조 생략({exc})"
    return db, ""


def _case_line(r: dict, p: float | None, db) -> str:
    tag = cl.label(p) + (f" p={p:.2f}" if p is not None else "")
    kind = r.get("type_code") or r.get("tag") or ""
    parts = [f"**{cl._cite(r)}** ({r['decision_date']}) {r['requirement']} {r['outcome']}" + (f" [{kind}]" if kind else "") + f" · {tag}",
             f"청구항 문언: {cl._cut(r['claim_feature'], 120)}"]
    if r.get("other_side"):
        parts.append(f"상대 구성: {cl._cut(r['other_side'], 90)}")
    parts.append(f"법원이 본 사실: {cl._cut(cl._fact(r), 220)}")
    if r.get("evidence_basis"):
        parts.append(f"명세서 근거: {cl._cut(r['evidence_basis'], 120)}")
    if r.get("drafting_lesson"):
        parts.append(f"작성 교훈(판결 이유에서): {cl._cut(r['drafting_lesson'], 140)}")
    if db is not None:
        parts.append(f"인용(PDF {r['page']}쪽, 원문 대조 {'일치' if cl.quote_ok(db, r) else '불일치'}): “{cl._cut(r['quote'], 160)}”")
    return "- " + " — ".join(parts)


def _cases_block(rows: list[tuple[dict, float | None]], db, empty: str) -> list[str]:
    return [_case_line(r, p, db) for r, p in rows] or [f"- {empty}"]


def _enforce(root: Path, paths: cl.Paths, text: str, claims: str, client) -> tuple[str, str]:
    data = cl.load_records(root / cl.ENFORCE_FILE)
    report = cl.enforcement_report(data, claims, k=3)
    if not report:
        return ("코드 점검 유형(W01 배타·부정 한정, W02 기능적 표현, W04 전제부 구성, W05 수치 범위, W09 여러 실시 주체, W12 제조방법 기재 물건)에 "
                "해당하는 문언이 없습니다. 이 점검은 위 유형만 코드로 찾으므로, 다른 권리행사 위험이 없다는 뜻은 아닙니다."), "모델·Jev 호출 없음"
    pairs = [(f"{f['snippet']} — {f['question']}", r, cl.JEV_ENFORCE) for f in report for r in f["cases"]]
    probs, note = _confirm(pairs, client)
    db, db_note = _verifier(paths)
    lines, at = [], 0
    with closing(db) if db is not None else nullcontext():
        for f in report:
            lines += [f"### 청구항 {f['claim']} · {f['type_code']} {f['type_label']}", "", f"`{f['snippet']}`", "", f"질문: {f['question']}", ""]
            lines += _cases_block(_keep(f["cases"], probs, at, 2), db, "Jev가 관련 있다고 본 판례 없음")
            lines.append("")
            at += len(f["cases"])
    return "\n".join(lines).rstrip(), "코드 규칙 점검 — 판정이 아니라 질문입니다. 답할 근거는 원자료·설계 계약에서 찾습니다. " + note + (f" {db_note}" if db_note else "")


def _pool(records: list[dict], text: str, types: list[str], key: str) -> list[dict]:
    """글자 겹침 후보에, 지적 문장으로 짐작한 유형마다 그 유형 안의 후보를 더한다. 관련성은 Jev가 정한다."""
    out = [r for _, r in cl.find(records, text, k=OVERLAP_K)]
    for t in types[:MAX_HINTS]:
        rs = [r for r in records if r.get(key) == t]
        out += [r for _, r in cl.find(rs, text, k=HINT_K)] or cl._pick(rs, HINT_K)
    seen, unique = set(), []
    for r in out:
        ident = (r["document_id"], r["claim_feature"], r["quote_start"])
        if ident not in seen:
            seen.add(ident)
            unique.append(r)
    return unique


def _find(root: Path, paths: cl.Paths, text: str, claims: str, client) -> tuple[str, str]:
    lessons, enforcement = cl.load_lessons(paths.lessons), cl.load_records(root / cl.ENFORCE_FILE)
    flagged = list(dict.fromkeys(f["type_code"] for f in cl.enforcement_flags(text)))
    pools = {"lessons": _pool(lessons["records"], text, cl.hinted_tags(text), "tag"),
             "enforcement": _pool(enforcement["records"], text, flagged, "type_code"),
             "patentability": _pool(cl.load_records(root / cl.PATENT_FILE)["records"], text, [], "type_code")}
    found = [(label, question, pools[name]) for name, label, question in FIND_SETS]
    pairs = [(text, r, question) for _, question, cases in found for r in cases]
    if not pairs:
        return "글자가 겹치는 판례 기록이 없습니다. 청구항 문언과 무엇이 문제인지(예: `'대략 수평'은 기준면·허용 각도가 없음`)를 함께 적어 보세요.", "모델·Jev 호출 없음"
    probs, note = _confirm(pairs, client)
    db, db_note = _verifier(paths)
    lines, at = [], 0
    with closing(db) if db is not None else nullcontext():
        for label, _, cases in found:
            lines += [f"### {label}", ""] + _cases_block(_keep(cases, probs, at, 3), db, "맞는 판례 기록 없음") + [""]
            at += len(cases)
    how = "후보는 지적 문장과 판례 기록의 글자 2-gram 겹침, 그리고 문장의 낱말로 짐작한 유형으로 골랐습니다. "
    return "\n".join(lines).rstrip(), how + note + (f" {db_note}" if db_note else "")


def _features(root: Path, paths: cl.Paths, text: str, claims: str, client) -> tuple[str, str]:
    data = cl.load_records(root / cl.PATENT_FILE)
    known = data.get("type_labels") or {}
    items = []
    for line in [x.strip(" -\t") for x in text.splitlines() if x.strip(" -\t")][:MAX_FEATURES]:
        feature, _, code = line.partition("::")
        items.append((feature.strip(), code.strip() if code.strip() in known else None))
    signals = [cl.feature_signals(data, feature, code, k=3) for feature, code in items]
    pairs = [(s["feature"], r, cl.JEV_PATENT) for s in signals for r in s["cases"]]
    probs, note = _confirm(pairs, client)
    db, db_note = _verifier(paths)
    lines, at = [], 0
    with closing(db) if db is not None else nullcontext():
        for i, s in enumerate(signals, 1):
            guess = f" (유형 추정: {s['type_guessed']} — `특징::F-유형`으로 직접 줄 수 있음)" if s["type_guessed"] else ""
            outcomes = ", ".join(f"{k} {n}" for k, n in s["outcomes"].items()) or "기록 없음"
            lines += [f"### F{i} {cl._cut(s['feature'], 80)}", "", f"- 유형: {s['type_code']} {s['type_label']}{guess}", f"- 법원 판단 분포: {outcomes}"]
            if s["deny_factors"]:
                lines.append("- 부정 이유 상위: " + ", ".join(f"{k} {n}" for k, n in s["deny_factors"]))
            if s["grant_factors"]:
                lines.append("- 인정 이유 상위: " + ", ".join(f"{k} {n}" for k, n in s["grant_factors"]))
            lines += [f"- 원자료 확인: {q}" for q in s["questions"]]
            lines += ["", "비슷한 판례:", ""] + _cases_block(_keep(s["cases"], probs, at, 3), db, "맞는 판례 기록 없음") + [""]
            at += len(s["cases"])
    skipped = max(0, len([x for x in text.splitlines() if x.strip(" -\t")]) - MAX_FEATURES)
    more = f" 특징은 {MAX_FEATURES}개까지 봅니다({skipped}개 생략)." if skipped else ""
    return ("\n".join(lines).rstrip(), "판단 분포는 법원에 온 사건의 분포이며 등록 가능성 통계가 아닙니다. 신규성·진보성은 선행기술 대비로만 판정합니다."
            + more + " " + note + (f" {db_note}" if db_note else ""))
