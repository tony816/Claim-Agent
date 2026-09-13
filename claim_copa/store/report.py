"""Markdown report rendering (최종안 → 핵심 판단 → 남은 REVIEW/BLOCK/UNVERIFIED)."""
from __future__ import annotations

from ..models.state import RunState
from ..pipeline.locks import PROVISIONAL_LABEL


def _gate_table(state: RunState) -> str:
    rows = ["| 단계 | 역할 | record_id | 상태 | 주요 게이트 | 비고 |", "|---|---|---|---|---|---|"]
    for rid, r in state.records.items():
        gates = ", ".join(f"{k}: {v}" for k, v in r.gates.items()) or "-"
        note = []
        if r.superseded:
            note.append("superseded")
        if r.stale:
            note.append("STALE")
        if not r.issued:
            note.append("미발급")
        rows.append(f"| {r.stage} | {r.role} | `{rid}` | {r.status} | {gates} | {' '.join(note) or '-'} |")
    return "\n".join(rows)


def render_report(state: RunState, claims_only: bool = False) -> str:
    c = state.candidate
    cur = c.current
    root_text = cur.exact_text if cur and cur.exact_text else (cur.meaning_draft_text if cur else None)
    dep = state.dependent
    dep_text = dep.current.exact_text if dep and dep.current and dep.current.exact_text else None
    final_root = bool(c.final_claim_lock)
    root_label = "출원용 최종안" if final_root else PROVISIONAL_LABEL

    if claims_only:
        parts = [f"<!-- {root_label} -->"]
        if root_text:
            parts.append(root_text)
        if dep_text:
            parts.append(dep_text)
        return "\n\n".join(parts) + "\n"

    out = [f"# Claim Copa 실행 보고서 — {state.run_id}", ""]
    out.append(f"- request_mode: {state.request_mode.value}")
    out.append(f"- source_set_id: {state.source_set_id} / variant: {state.variant_id}")
    out.append(f"- candidate_id: {c.candidate_id} / revision: {c.revision} / design_revision: {c.design_revision}")
    out.append(f"- 결과: **{state.outcome}**")
    out.append("")
    out.append("## 최종안")
    out.append("")
    if root_text:
        lock_state = "FINAL_CLAIM_LOCK " + c.final_claim_lock if final_root else ("DRAFT_CLAIM_LOCK " + c.draft_claim_lock if c.draft_claim_lock else "LOCK 없음")
        out.append(f"### 독립항 ({lock_state}; {root_label if c.draft_claim_lock or c.final_claim_lock else '미확정 문언'})")
        out.append("")
        out.append(root_text)
        out.append("")
    else:
        out.append("독립항 문언 없음 (설계 단계에서 중지)")
        out.append("")
    if dep:
        dl = dep.final_set_lock or dep.draft_set_lock
        label = ("FINAL_DEPENDENT_SET_LOCK " if dep.final_set_lock else "DRAFT_DEPENDENT_SET_LOCK ") + dl if dl else "LOCK 없음"
        out.append(f"### 종속항 세트 ({label}; {'출원용 최종 종속항 세트' if dep.final_set_lock else PROVISIONAL_LABEL})")
        out.append("")
        out.append(dep_text or "종속항 문언 없음")
        out.append("")
    out.append("## 핵심 판단")
    out.append("")
    out.append(_gate_table(state))
    out.append("")
    if dep and dep.current and dep.current.targets:
        out.append("### 종속항별 역구성")
        out.append("")
        out.append("| 목표항 | blind | picture | 판정 |")
        out.append("|---|---|---|---|")
        for tid, t in dep.current.targets.items():
            out.append(f"| {tid} | `{t.blind_record_id}` | `{t.picture_record_id}` | {t.picture_status or '-'} |")
        out.append(f"\nDEPENDENT_RECONSTRUCTION_GATE: {dep.current.reconstruction_gate or 'N/A'}")
        out.append("")
    out.append("## 남은 REVIEW/BLOCK/UNVERIFIED")
    out.append("")
    if state.halt:
        h = state.halt
        out.append(f"- **중지**: {h.kind} @ {h.stage} ({h.role}) — {h.reason_code or ''} {h.message}")
        for issue in h.open_issues:
            out.append(f"  - [{issue.get('kind')}] {issue.get('code','')} {issue.get('text','')}" + (f" → {issue.get('return_to')}" if issue.get("return_to") else ""))
        out.append("")
        out.append("재개: `claim-copa resume " + state.run_id + " --decide \"<결정>\" [--apply-style-fix|--apply-meaning-fix|--redesign|--add-source PATH]`")
    if not state.spec_present:
        out.append("- OA_FINAL_GATE / DEPENDENT_OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED (정식 명세서 미제공; DRAFT 게이트와 공존)")
    if not state.prior_art_present:
        out.append("- 신규성·진보성: UNVERIFIED — PRIOR_ART_NOT_PROVIDED (등록 가능성을 단정하지 않음)")
    for n in state.notes:
        out.append(f"- {n}")
    out.append("")
    return "\n".join(out)
