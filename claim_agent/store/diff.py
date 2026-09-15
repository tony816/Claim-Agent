"""Revision history and diffs in the CLAUDE.md change-proposal layout.

Every revision transition is rendered as 기존 문언 / 제안 문언 / 변경 이유 / 권리범위 영향 / 근거:
the wording comes from the run state, the reason from the revision's change_goal, the scope impact
from the style record's own 변경 대조표 (quoted, never re-judged here), and the evidence from the
record ids and verdicts that were actually issued for that revision.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

from ..models.state import RunState

_SCOPE_KEYS = ("변경 대조표", "범위 불변", "권리범위")
_RECORD_KINDS_ROOT = ("meaning_draft", "style", "success", "syntax", "oa", "blind", "reference_compare", "draft_claim_lock", "final_claim_lock")
_RECORD_KINDS_DEP = ("dependent_meaning_draft", "dependent_style", "dependent_success", "dependent_syntax", "dependent_oa", "draft_dependent_set_lock", "final_dependent_set_lock")


@dataclass
class RevisionRow:
    scope: str                 # INDEPENDENT | DEPENDENT_SET
    revision: str
    design_revision: str
    mode: str
    change_goal: str | None
    text: str | None
    records: dict[str, str]
    lock: str | None
    superseded: bool


def revision_rows(state: RunState) -> list[RevisionRow]:
    rows: list[RevisionRow] = []
    c = state.candidate
    for rs, superseded in [(h, True) for h in c.history] + ([(c.current, False)] if c.current else []):
        lock = rs.records.get("final_claim_lock") or rs.records.get("draft_claim_lock")
        rows.append(RevisionRow("INDEPENDENT", rs.revision, rs.design_revision, rs.style_change_mode.value, rs.change_goal, rs.exact_text or rs.meaning_draft_text, dict(rs.records), lock, superseded))
    d = state.dependent
    if d:
        for rs, superseded in [(h, True) for h in d.history] + ([(d.current, False)] if d.current else []):
            lock = rs.records.get("final_dependent_set_lock") or rs.records.get("draft_dependent_set_lock")
            rows.append(RevisionRow("DEPENDENT_SET", rs.dependent_revision, rs.dependent_design_revision, rs.style_change_mode.value, rs.change_goal, rs.exact_text or rs.meaning_draft_text, dict(rs.records), lock, superseded))
    return rows


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.;])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def sentence_diff(old: str, new: str) -> list[str]:
    """Sentence-level unified diff lines ('-' removed, '+' added); empty when identical."""
    a, b = _sentences(old), _sentences(new)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        out += [f"- {s}" for s in a[i1:i2]]
        out += [f"+ {s}" for s in b[j1:j2]]
    return out


def _scope_excerpt(report: str, limit: int = 1200) -> str:
    """Quote the style record's own scope-invariance / change table section."""
    if not report:
        return "style record 없음"
    lines = report.splitlines()
    start = next((i for i, ln in enumerate(lines) if any(k in ln for k in _SCOPE_KEYS)), None)
    if start is None:
        return "style record에 변경 대조표·범위 불변 항목이 없음 (record 전문 참조)"
    excerpt = "\n".join(lines[start:start + 40]).strip()
    return excerpt[:limit] + ("…" if len(excerpt) > limit else "")


def _evidence(state: RunState, row: RevisionRow) -> list[str]:
    kinds = _RECORD_KINDS_ROOT if row.scope == "INDEPENDENT" else _RECORD_KINDS_DEP
    out = []
    for kind in kinds:
        rid = row.records.get(kind)
        ref = state.record(rid)
        if ref:
            gates = ", ".join(f"{k}: {v}" for k, v in ref.gates.items())
            out.append(f"{kind}: `{rid}` {ref.status}" + (f" ({gates})" if gates else "") + ("" if ref.issued else " 미발급"))
    return out


def render_revision_history(state: RunState) -> str:
    rows = revision_rows(state)
    if len(rows) <= 1 and not any(r.change_goal for r in rows):
        return ""
    out = ["## 리비전 이력", "", "| 범위 | revision | design | 변경 종류 | 변경 이유 | LOCK | 상태 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        goal = (r.change_goal or "-").splitlines()[0][:80]
        out.append(f"| {r.scope} | {r.revision} | {r.design_revision} | {r.mode} | {goal} | {r.lock or '-'} | {'superseded' if r.superseded else 'current'} |")
    out.append("")
    out.append("리비전 간 문언 대조: `claim-agent runs diff <run_id>`")
    out.append("")
    return "\n".join(out)


def render_revision_diff(store: Any, state: RunState, from_rev: str | None = None, to_rev: str | None = None, scope: str = "INDEPENDENT") -> str:
    rows = [r for r in revision_rows(state) if r.scope == scope]
    if not rows:
        return f"{scope} 리비전이 없습니다."
    by_rev = {r.revision: r for r in rows}
    if to_rev is None:
        to_rev = rows[-1].revision
    if from_rev is None:
        idx = [r.revision for r in rows].index(to_rev)
        from_rev = rows[idx - 1].revision if idx > 0 else None
    if to_rev not in by_rev or (from_rev is not None and from_rev not in by_rev):
        return f"리비전을 찾을 수 없습니다: {from_rev} → {to_rev} (있는 것: {', '.join(by_rev)})"
    new = by_rev[to_rev]
    old = by_rev[from_rev] if from_rev else None
    out = [f"# 리비전 대조 — {state.run_id} {scope} {from_rev or '(없음)'} → {to_rev}", ""]
    out += ["## 기존 문언", "", (old.text if old and old.text else "없음 (첫 리비전)"), ""]
    out += ["## 제안 문언", "", new.text or "없음", ""]
    if old and old.text and new.text:
        lines = sentence_diff(old.text, new.text)
        out += ["## 문장 단위 변경", "", "```diff"] + (lines or ["(동일)"]) + ["```", ""]
    out += ["## 변경 이유", "", f"- 변경 종류: {new.mode} (design {old.design_revision if old else '-'} → {new.design_revision})"]
    out.append(f"- 변경 목표: {new.change_goal or '기록 없음'}")
    out.append("")
    style_kind = "style" if scope == "INDEPENDENT" else "dependent_style"
    style_id = new.records.get(style_kind)
    report = ""
    if style_id:
        try:
            report = store.read_record_report(state.run_id, style_id)
        except FileNotFoundError:
            report = ""
    out += ["## 권리범위 영향 (style record 인용)", "", f"style record: `{style_id or '없음'}`", "", _scope_excerpt(report), ""]
    out += ["## 근거", ""]
    ev = _evidence(state, new)
    out += [f"- {e}" for e in ev] or ["- 발급된 기록 없음"]
    out.append("")
    return "\n".join(out)
