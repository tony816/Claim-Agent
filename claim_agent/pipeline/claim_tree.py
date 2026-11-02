"""청구항 트리: 최상류 과제 → 독립항(과제 해결의 최상위 기능·구성·효과) → 종속항(기능·구성·효과) → 그 하위 종속항.

Drawn from the sealed records only (the problem record, the architect's `problem` and skeleton `design_elements`, the
dependent design's `candidates[]` with `problem`/`feature`/`effect`/`parent_node`). It is a record for the user and the
lock, never a gate: the hierarchy itself is checked by `models.contracts.claim_tree_gaps` before the design is accepted.
"""
from __future__ import annotations

from typing import Any

from ..models.contracts import set_candidates
from ..models.enums import CandidateClass
from ..models.envelope import CandidateItem, RoleEnvelope
from ..models.state import RunState, nos_label

ROOT_NODE = "ROOT"
NO_PROBLEM = "기록 없음 (problem_finding off 또는 실행 안 됨)"


def _read(store, run_id: str, rid: str | None) -> dict[str, Any]:
    if not rid:
        return {}
    try:
        return store.read_record(run_id, rid)
    except (OSError, ValueError):
        return {}


def _clip(text: str | None, limit: int = 100) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def root_problem(state: RunState, store) -> tuple[str, str]:
    """The top node: the problem the architect adopted, else the user's fixed problem, else the finder's record."""
    c = state.candidate
    design = _read(store, state.run_id, c.design_record_id)
    if (design.get("problem") or "").strip():
        return design["problem"].strip(), f"claim-architect {c.design_record_id}"
    user = str(state.request.get("user_problem") or "").strip()
    if user:
        return user, "USER_PROBLEM (사용자 확정)"
    prob = _read(store, state.run_id, c.problem_record_id)
    if (prob.get("problem") or "").strip():
        return prob["problem"].strip(), f"{c.problem_record_id} ({prob.get('problem_structure') or '구조 미기재'})"
    return NO_PROBLEM, "—"


def _root_no(root_text: str) -> int:
    from .claimtext import ClaimParseError, parse_claim_set

    try:
        for cl in parse_claim_set(root_text or ""):
            if cl.is_independent:
                return cl.claim_no
    except ClaimParseError:
        pass
    return 1


def _node_label(cand: CandidateItem, show_parent_no: bool) -> str:
    aux = cand.classification == CandidateClass.FALLBACK_ONLY
    head = f"【청구항 {cand.planned_claim_no}】 {cand.dc_id}" + (" [보조 축 — 구조만, 효과 미기재]" if aux else "")
    parts = []
    if (cand.problem or "").strip():
        parts.append("과제: " + _clip(cand.problem))
    if (cand.feature or "").strip():
        parts.append("구성: " + _clip(cand.feature))
    if (cand.effect or "").strip():
        parts.append("효과: " + _clip(cand.effect))
    if show_parent_no and cand.parent_claim_no is not None:
        parts.append(f"부모 제{cand.parent_claim_no}항")
    return head + (" — " + " / ".join(parts) if parts else "")


def render_claim_tree(state: RunState, store, env: RoleEnvelope, root_text: str) -> str:
    """Text tree of the accepted dependent design (the picture the user asked for: problem → claim 1 → nodes)."""
    problem, source = root_problem(state, store)
    lines = [f"[최상류 과제] {problem}  (출처: {source})"]
    c = state.candidate
    base = state.baseline_set
    if base:
        lines.append(f"└─ 기준 세트 {nos_label(base.chain_nos)} (BASELINE_SET — 미검증 읽기 전용 체인)")
    else:
        design = _read(store, state.run_id, c.design_record_id)
        lines.append(f"└─ 【청구항 {_root_no(root_text)}】 과제 해결의 최상위 기능·구성·효과")
        for e in design.get("design_elements") or []:
            if e.get("kind") == "SKELETON":
                lines.append(f"     주골격 {e.get('element_id', '')}: {_clip(e.get('text'))}")
    written = sorted(set_candidates(env), key=lambda x: x.planned_claim_no or 0)
    ids = {w.dc_id for w in written}
    children: dict[str, list[CandidateItem]] = {}
    for cand in written:
        node = (cand.parent_node or "").strip()
        key = node if node in ids else ROOT_NODE
        children.setdefault(key, []).append(cand)

    def walk(key: str, prefix: str) -> None:
        kids = children.get(key, [])
        for i, cand in enumerate(kids):
            last = i == len(kids) - 1
            lines.append(prefix + ("└─ " if last else "├─ ") + _node_label(cand, show_parent_no=bool(base) and key == ROOT_NODE))
            walk(cand.dc_id, prefix + ("   " if last else "│  "))

    walk(ROOT_NODE, "   ")
    if not written:
        lines.append("   (잠긴 종속항 후보 없음)")
    return "\n".join(lines)
