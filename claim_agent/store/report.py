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
        if r.evidence_counts:
            note.append("근거 " + "/".join(f"{k[0]}{v}" for k, v in r.evidence_counts.items() if v))
        rows.append(f"| {r.stage} | {r.role} | `{rid}` | {r.status} | {gates} | {' '.join(note) or '-'} |")
    return "\n".join(rows)


def _pending_list(state: RunState) -> list[str]:
    if state.request_mode.value == "REVIEW_ONLY":
        expected = state.request.get("reviewers") or ["syntax-scope-reviewer"]
        seen = {r.role for r in state.records.values() if not r.stale and not r.superseded}
    else:
        expected = ["ARCHITECT", "DRAFT", "STYLE", "SUCCESS", "SYNTAX", "OA", "BLIND", "PICTURE"]
        if state.request.get("dependent"):
            expected += ["DEP_ARCHITECT", "DEP_DRAFT", "DEP_STYLE", "DEP_SUCCESS", "DEP_SYNTAX", "DEP_OA", "DEP_RECON"]
        seen = {r.stage for r in state.records.values() if not r.stale and not r.superseded}
    return [stage for stage in expected if stage not in seen]


def _pending_stages(state: RunState) -> str:
    """Report absent work explicitly; a model STOP is not pipeline completion."""
    pending = _pending_list(state)
    if pending:
        return "미실행 필수 단계: " + ", ".join(pending) + ". 앞 단계의 게이트/입력 조건이 충족되어야 진행합니다."
    return "요청 경로의 역할 호출 기록이 있습니다. 각 게이트의 통과 여부와 LOCK 상태는 아래 표를 따릅니다."


def _fmt_cost(u: dict[str, float]) -> str:
    if not u.get("calls"):
        return "-"
    if u.get("unpriced_calls"):
        return "N/A" if u.get("unpriced_calls") == u.get("calls") else f"${u.get('cost_usd', 0):.4f} (일부 미산정)"
    return f"${u.get('cost_usd', 0):.4f}"


def render_performance(state: RunState) -> str:
    """성능 요약: 단계별 호출·지연·토큰·캐시 적중·추정 비용 (원문은 포함하지 않는다)."""
    u = state.usage
    if not u.get("calls"):
        return ""
    out = ["## 성능 요약", "", "| 단계 | 호출 | 지연(s) | 입력 토큰 | 캐시 토큰 | 출력+사고 토큰 | 캐시 적중 | 추정 비용 |", "|---|---|---|---|---|---|---|---|"]
    for stage, s in state.stage_usage.items():
        out.append(f"| {stage} | {int(s.get('calls', 0))} | {s.get('latency_ms', 0) / 1000:.1f} | {int(s.get('prompt_tokens', 0)):,} | {int(s.get('cached_tokens', 0)):,} | {int(s.get('output_tokens', 0) + s.get('thoughts_tokens', 0)):,} | {int(s.get('cache_hits', 0))} | {_fmt_cost(s)} |")
    hit = f"{u.get('cache_hits', 0) / u['calls'] * 100:.0f}%"
    out.append(f"| **합계** | {int(u['calls'])} | {u.get('latency_ms', 0) / 1000:.1f} | {int(u.get('prompt_tokens', 0)):,} | {int(u.get('cached_tokens', 0)):,} | {int(u.get('output_tokens', 0) + u.get('thoughts_tokens', 0)):,} | {hit} | {_fmt_cost(u)} |")
    if u.get("unpriced_calls"):
        out.append("")
        out.append("비용은 `claim-agent.yaml`의 `telemetry.pricing`에 사용 모델 단가를 넣어야 산정된다.")
    out.append("")
    return "\n".join(out)


def _failed_records(state: RunState) -> list[str]:
    """Live records that did not come back PASS — the only gate rows worth repeating outside the panel."""
    out = []
    for r in state.records.values():
        if r.superseded or r.stale or r.status not in ("REVIEW", "BLOCK", "UNVERIFIED"):
            continue        # PASS, PASS-RANGE and the engine's own LOCKED rows are not open items
        gates = ", ".join(f"{k}: {v}" for k, v in r.gates.items() if v not in ("PASS", "NOT_APPLICABLE"))
        out.append(f"{r.stage} {r.status}" + (f"({gates})" if gates else ""))
    return out


SUMMARY_LIMIT = 500


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _lock_phrase(state: RunState) -> str:
    c, dep = state.candidate, state.dependent
    out = "독립항 " + ("FINAL_CLAIM_LOCK" if c.final_claim_lock else "DRAFT_CLAIM_LOCK(잠정안)" if c.draft_claim_lock else "LOCK 없음")
    if dep:
        out += " · 종속항 " + ("FINAL_DEPENDENT_SET_LOCK" if dep.final_set_lock else "DRAFT_DEPENDENT_SET_LOCK(잠정안)" if dep.draft_set_lock else "LOCK 없음")
    return out


def summary_comment(state: RunState, limit: int = SUMMARY_LIMIT) -> str:
    """The report part of the answer, at most `limit` characters including the list markers.

    It is assembled from what the gates recorded, not written by another model call, so it cannot say more than the
    run did. Lines are in priority order; whatever no longer fits is cut, and the full report keeps everything.
    """
    c = state.candidate
    reviewing = state.request_mode.value == "REVIEW_ONLY"
    failed = _failed_records(state)
    head = (f"REVIEW_ONLY 검토 결과 {state.outcome} — 청구항 작성·LOCK 발급 없음" if reviewing
            else f"결과 {state.outcome} ({c.revision}/{c.design_revision}) — {_lock_phrase(state)}")
    lines = [head + ("; 실행된 모든 단계 PASS" if not failed and not state.halt else "")]
    h = state.halt
    if h:
        lines.append(_clip(f"중지: {h.kind} @ {h.stage}({h.role}) {h.reason_code or ''} — {h.message}", 160))
    if failed:
        lines.append(_clip("미통과: " + "; ".join(failed), 120))
    pending = _pending_list(state)
    if pending:
        lines.append("미실행: " + ", ".join(pending))
    if h:
        lines.append(f"재개: 작업 상태 패널 또는 claim-agent resume {state.run_id}")
    unverified = ([] if state.spec_present else ["정식 명세서 미제공(OA_FINAL_GATE)"]) + \
                 ([] if state.prior_art_present else ["선행기술 미제공(신규성·진보성, 등록 가능성 단정 안 함)"])
    if unverified:
        lines.append("미검증: " + " · ".join(unverified))
    if h and h.open_issues:         # the least essential line: it takes whatever room is left
        lines.append("쟁점: " + "; ".join(_clip(f"{i.get('code', '')} {i.get('text', '')}", 70) for i in h.open_issues[:3]))
    out: list[str] = []
    for line in lines:
        room = limit - len("\n".join(out)) - (1 if out else 0)
        if room < 16:
            break
        out.append(_clip("- " + line, room))
    return "\n".join(out)


def _duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}초"
    if s < 3600:
        return f"{s // 60}분 {s % 60}초"
    return f"{s // 3600}시간 {s % 3600 // 60}분"


def usage_note(usage: dict[str, float], seconds: float | None) -> str:
    """The answer's footnote: elapsed time, tokens, and the API cost computed from `telemetry.pricing`."""
    calls = int(usage.get("calls", 0))
    parts = ["작업 소요 " + (_duration(seconds) if seconds is not None else "미기록")]
    if not calls:
        parts.append("API 호출 없음 (토큰 0 · 비용 $0)")
    else:
        prompt, cached = int(usage.get("prompt_tokens", 0)), int(usage.get("cached_tokens", 0))
        output = int(usage.get("output_tokens", 0) + usage.get("thoughts_tokens", 0))
        parts.append(f"토큰 {prompt + output:,} (입력 {prompt:,}, 그중 캐시 {cached:,} · 출력 {output:,})")
        unpriced, cost = int(usage.get("unpriced_calls", 0)), usage.get("cost_usd", 0)
        if unpriced >= calls:
            parts.append(f"API 비용 산출 불가 (호출 {calls}회 — telemetry.pricing에 사용 모델 단가 없음)")
        elif unpriced:
            parts.append(f"API 비용 ${cost:.4f} 이상 (호출 {calls}회 중 {unpriced}회 단가 없음)")
        else:
            parts.append(f"API 비용 ${cost:.4f} (호출 {calls}회, telemetry.pricing 단가로 산출한 추정치)")
    return "※ " + " · ".join(parts)


def turn_usage(state: RunState) -> tuple[dict[str, float], float | None]:
    """This turn's usage and seconds; a state saved before per-turn accounting falls back to the run's totals."""
    t = state.turn
    if t.get("finished_at"):
        return t, t["finished_at"] - t.get("started_at", t["finished_at"])
    return state.usage, (state.updated_at - state.created_at) if state.usage else None


def render_chat_report(state: RunState) -> str:
    """The conversation answer: the deliverable first, then a summary of at most 500 characters and a usage footnote.

    The deliverable is the claim text, or for REVIEW_ONLY the reviewers' opinion, which has no claim to stand in for
    it. Gate tables, other roles' reports and the revision diff stay in the full report (and the run panel).
    """
    c = state.candidate
    cur = c.current
    dep = state.dependent
    out: list[str] = []
    if state.request_mode.value == "REVIEW_ONLY":
        out += ["## 검토 의견", "", "REVIEW_ONLY — 요청된 검수만 수행하며 청구항 작성·수정 또는 LOCK 발급 결과가 아닙니다.", ""]
        for role, report in state.review_reports.items():
            out += [f"### {role}", "", report, ""]
        if not state.review_reports:
            out += ["검토 의견 없음 (검토 역할이 보고서를 남기기 전에 중지)", ""]
    else:
        root_text = cur.exact_text if cur and cur.exact_text else (cur.meaning_draft_text if cur else None)
        final_root = bool(c.final_claim_lock)
        out += ["## 최종안", ""]
        if root_text:
            lock = "FINAL_CLAIM_LOCK " + c.final_claim_lock if final_root else ("DRAFT_CLAIM_LOCK " + c.draft_claim_lock if c.draft_claim_lock else "LOCK 없음")
            label = "출원용 최종안" if final_root else (PROVISIONAL_LABEL if c.draft_claim_lock else "미확정 문언")
            out += [f"### 독립항 ({lock}; {label})", "", root_text, ""]
        else:
            out += ["독립항 문언 없음 (설계 단계에서 중지)", ""]
        if dep:
            dl = dep.final_set_lock or dep.draft_set_lock
            lock = ("FINAL_DEPENDENT_SET_LOCK " if dep.final_set_lock else "DRAFT_DEPENDENT_SET_LOCK ") + dl if dl else "LOCK 없음"
            label = "출원용 최종 종속항 세트" if dep.final_set_lock else PROVISIONAL_LABEL
            out += [f"### 종속항 세트 ({lock}; {label})", "",
                    (dep.current.exact_text if dep.current and dep.current.exact_text else None) or "종속항 문언 없음", ""]
    out += ["## 요약 코멘트", "", summary_comment(state), ""]
    usage, seconds = turn_usage(state)
    out += [usage_note(usage, seconds),
            f"※ 게이트 표·역할별 원 보고서 전문은 전체 보고서(`runs/{state.run_id}/report.md`)에 있습니다.", ""]
    return "\n".join(out)


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
    perf = render_performance(state)
    if perf:
        out.append(perf)
    from .diff import render_revision_history

    history = render_revision_history(state)
    if history:
        out.append(history)
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
