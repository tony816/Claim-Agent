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


def _pending_stages(state: RunState) -> str:
    """Report absent work explicitly; a model STOP is not pipeline completion."""
    if state.request_mode.value == "REVIEW_ONLY":
        expected = state.request.get("reviewers") or ["syntax-scope-reviewer"]
        seen = {r.role for r in state.records.values() if not r.stale and not r.superseded}
    else:
        expected = ["ARCHITECT", "DRAFT", "STYLE", "SUCCESS", "SYNTAX", "OA", "BLIND", "PICTURE"]
        if state.request.get("dependent"):
            expected += ["DEP_ARCHITECT", "DEP_DRAFT", "DEP_STYLE", "DEP_SUCCESS", "DEP_SYNTAX", "DEP_OA", "DEP_RECON"]
        seen = {r.stage for r in state.records.values() if not r.stale and not r.superseded}
    pending = [stage for stage in expected if stage not in seen]
    if pending:
        return "미실행 필수 단계: " + ", ".join(pending) + ". 앞 단계의 게이트/입력 조건이 충족되어야 진행합니다."
    return "요청 경로의 역할 호출 기록이 있습니다. 각 게이트의 통과 여부와 LOCK 상태는 아래 표를 따릅니다."


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

    out = [f"# Claim-Agent 실행 보고서 — {state.run_id}", ""]
    out.append(f"- request_mode: {state.request_mode.value}")
    out.append(f"- source_set_id: {state.source_set_id} / variant: {state.variant_id}")
    out.append(f"- candidate_id: {c.candidate_id} / revision: {c.revision} / design_revision: {c.design_revision}")
    out.append(f"- 결과: **{state.outcome}**")
    out.append("")
    reviewing = state.request_mode.value == "REVIEW_ONLY"
    out.append("## 검토 의견" if reviewing else "## 최종안")
    out.append("")
    if reviewing:
        out.append("REVIEW_ONLY — 요청된 검수만 수행하며 청구항 작성·수정 또는 LOCK 발급 결과가 아닙니다.")
        for role, report in state.review_reports.items():
            out.extend(["", f"### {role}", "", report])
        if not state.review_reports:
            out.append("검토 보고서가 아직 생성되지 않았습니다.")
        out.append("")
    elif root_text:
        lock_state = "FINAL_CLAIM_LOCK " + c.final_claim_lock if final_root else ("DRAFT_CLAIM_LOCK " + c.draft_claim_lock if c.draft_claim_lock else "LOCK 없음")
        out.append(f"### 독립항 ({lock_state}; {root_label if c.draft_claim_lock or c.final_claim_lock else '미확정 문언'})")
        out.append("")
        out.append(root_text)
        out.append("")
    else:
        out.append("독립항 문언 없음 (설계 단계에서 중지)")
        out.append("")
    if state.halt and state.halt.report_markdown and not reviewing:
        out.extend(["## 중지 단계의 검토 내용", "",
                    "아래는 해당 역할의 원 보고서입니다. 제시된 방향·예시 문언은 후속 검수를 통과한 확정안이 아닙니다.", "",
                    state.halt.report_markdown, ""])
    if dep:
        dl = dep.final_set_lock or dep.draft_set_lock
        label = ("FINAL_DEPENDENT_SET_LOCK " if dep.final_set_lock else "DRAFT_DEPENDENT_SET_LOCK ") + dl if dl else "LOCK 없음"
        out.append(f"### 종속항 세트 ({label}; {'출원용 최종 종속항 세트' if dep.final_set_lock else PROVISIONAL_LABEL})")
        out.append("")
        out.append(dep_text or "종속항 문언 없음")
        out.append("")
    out.append("## 핵심 판단")
    out.append("")
    out.append(_pending_stages(state))
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
        out.append("재개: `claim-agent resume " + state.run_id + " --decide \"<결정>\" [--apply-style-fix|--apply-meaning-fix|--redesign|--add-source PATH]`")
    if not state.spec_present:
        out.append("- OA_FINAL_GATE / DEPENDENT_OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED (정식 명세서 미제공; DRAFT 게이트와 공존)")
    if not state.prior_art_present:
        out.append("- 신규성·진보성: UNVERIFIED — PRIOR_ART_NOT_PROVIDED (등록 가능성을 단정하지 않음)")
    for n in state.notes:
        out.append(f"- {n}")
    out.append("")
    return "\n".join(out)
