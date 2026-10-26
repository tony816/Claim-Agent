"""명세서 작성 인계표 (설계 14.4): per-limitation exam-response materials, assembled at LOCK from sealed records.

No model call. The table tells the specification writer (cs2-spec-writer) which limitation still lacks a liftable
sentence, a problem→effect chain, a difference from prior art or a numeric comparison in the raw material. It is a
record, never a LOCK condition, and it never proposes effects or numbers the raw material does not have.
"""
from __future__ import annotations

import json
from typing import Any

from ..models.prior_art import LABEL_NONE
from ..models.state import RunState

HAVE, PARTIAL, NONE, NA = "갖춤", "부족", "없음", "해당 없음"
COLUMNS = ["떼어 쓸 근거 문장", "문제→구성→효과", "관계에서 나오는 효과", "종래·선행과의 차이", "수치·비교"]


def _basis(b: str | None) -> str:
    return {"DIRECT": HAVE, "DERIVED": PARTIAL}.get((b or "").upper(), NONE)


def _numeric(pattern: str | None) -> str:
    return {"NUMERIC_WITH_REASON": HAVE, "NUMERIC_NO_REASON": PARTIAL}.get(pattern or "", NA)


def _todo(row: dict[str, Any]) -> list[str]:
    loc = row.get("source_location") or ""
    out = []
    for col in COLUMNS:
        v = row[col]
        if v in (PARTIAL, NONE):
            out.append(f"{col}: " + (f"원자료 위치 {loc}의 내용을 청구항과 같은 용어로 한두 문장 안에" if loc and col == COLUMNS[0] else "원자료에 없음 — 작성자 확인 필요"))
    return out


def _read(store, state: RunState, rid: str | None) -> dict[str, Any]:
    if not rid:
        return {}
    try:
        return store.read_record(state.run_id, rid)
    except (OSError, ValueError):
        return {}


def independent_rows(state: RunState, store) -> list[dict[str, Any]]:
    c = state.candidate
    design = _read(store, state, c.design_record_id)
    elements = design.get("design_elements") or [
        {"element_id": f"E{i + 1}", "kind": "LIMITATION", "text": e.get("limitation", ""), "source_location": e.get("location"), "basis": e.get("basis")}
        for i, e in enumerate(design.get("limitation_evidence") or [])
    ]
    pre = _read(store, state, c.preexam_record_id)
    remaining = {r["element_id"]: r for r in pre.get("remaining_differences") or []}
    no_prior = state.prior_art_label() == LABEL_NONE or not pre
    rows = []
    for e in elements:
        if not e.get("in_independent", True):
            continue
        rd = remaining.get(e["element_id"])
        if no_prior:
            diff, link = NA + "(선행기술 없음)", NA
        elif rd is None:
            diff, link = NA + "(봉인 문헌에 개시)", NA
        else:
            diff = HAVE if (rd.get("why_not_simple") or "NONE") != "NONE" else NONE
            link = rd.get("effect_link") or NA
        row = {
            "scope": "INDEPENDENT", "limitation": f"{e['element_id']} {e.get('text', '')}".strip(), "source_location": e.get("source_location") or "",
            COLUMNS[0]: _basis(e.get("basis")),
            COLUMNS[1]: _basis(e.get("basis")) if e.get("kind") == "SKELETON" else NA,
            COLUMNS[2]: link,
            COLUMNS[3]: diff,
            COLUMNS[4]: _numeric(rd.get("exam_pattern") if rd else None),
        }
        row["명세서에 적을 것"] = _todo(row)
        rows.append(row)
    return rows


def dependent_rows(state: RunState, store) -> list[dict[str, Any]]:
    d = state.dependent
    if d is None or d.current is None:
        return []
    design = _read(store, state, d.design_record_id)
    cands = {x["dc_id"]: x for x in design.get("candidates") or []}
    no_prior = state.prior_art_label() == LABEL_NONE
    rows = []
    for cl in d.current.claims:
        cand = cands.get(cl.get("dc_id") or "", {})
        body = " ".join((cl.get("text") or "").split("\n")[1:])[:160]
        prior = cand.get("PRIOR_ART_CONTRIBUTION_GATE")
        row = {
            "scope": "DEPENDENT", "limitation": f"제{cl['claim_no']}항 ({cl.get('dc_id') or '-'}) {body}".strip(), "source_location": cand.get("exam_pattern_basis") or "",
            COLUMNS[0]: HAVE if cand.get("DEPENDENT_SOURCE_GATE") == "PASS" else PARTIAL,
            COLUMNS[1]: HAVE if cand.get("CAUSAL_CONTRIBUTION_GATE") == "PASS" else PARTIAL,
            COLUMNS[2]: NA,
            COLUMNS[3]: NA + "(선행기술 없음)" if no_prior else (HAVE if prior == "PASS" else PARTIAL),
            COLUMNS[4]: _numeric(cand.get("exam_pattern")),
            "exam_pattern": cand.get("exam_pattern"), "design_change_risk": cand.get("design_change_risk"),
        }
        row["명세서에 적을 것"] = _todo(row)
        rows.append(row)
    return rows


def render_md(rows: list[dict[str, Any]], title: str) -> str:
    if not rows:
        return ""
    lines = [f"## 명세서 작성 인계 — {title}", "",
             "LOCK 조건이 아닌 기록이다. 원자료에 없는 효과·수치는 제안하지 않고 '작성자 확인 필요'로만 남긴다. 종속항 추가를 권하지 않는다.", "",
             "| 한정 | " + " | ".join(COLUMNS) + " | 명세서에 적을 것 |", "|---" * (len(COLUMNS) + 2) + "|"]
    for r in rows:
        todo = "<br>".join(r["명세서에 적을 것"]) or "-"
        lines.append(f"| {r['limitation'].replace('|', '/')} | " + " | ".join(r[c] for c in COLUMNS) + f" | {todo} |")
    return "\n".join(lines) + "\n"


def write_handoff(state: RunState, store, scope: str) -> tuple[str, str]:
    """(relative path, markdown section) of runs/<run_id>/handoff/spec_materials.json after adding `scope` rows."""
    path = store.run_dir(state.run_id) / "handoff" / "spec_materials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = independent_rows(state, store) if scope == "INDEPENDENT" else dependent_rows(state, store)
    data[scope.lower()] = rows
    data["prior_art_set"] = state.prior_art_label()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return "handoff/spec_materials.json", render_md(rows, "독립항" if scope == "INDEPENDENT" else "종속항 세트")
