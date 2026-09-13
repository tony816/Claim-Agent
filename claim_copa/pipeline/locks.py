"""Lock assembly (field order follows web/HANDOFF_TEMPLATES.md)."""
from __future__ import annotations

from typing import Any

from .. import EXECUTION_PROFILE, PROTOCOL_VERSION
from ..models.state import RunState

PROVISIONAL_LABEL = "명세서 뒷받침·실시가능성 미검증 잠정안"
FINAL_LABEL = "출원용 최종안"
LOCK_CLASS = "DRAFT-ISOLATED-PY"


def _g(state: RunState, rid: str | None, gate: str) -> str:
    r = state.record(rid)
    return (r.gates.get(gate) if r else None) or "N/A"


def build_claim_lock(state: RunState, lock_id: str, final: bool, root_text: str, material_names: list[str]) -> tuple[dict[str, Any], str]:
    c = state.candidate
    cur = c.current
    assert cur is not None
    rec = cur.records
    style = rec.get("style")
    oa = rec.get("oa")
    picture = rec.get("reference_compare")
    lock: dict[str, Any] = {
        "record_type": "FINAL_CLAIM_LOCK" if final else "DRAFT_CLAIM_LOCK",
        "lock_id": lock_id,
        "lock_class": "FINAL-ISOLATED-PY" if final else LOCK_CLASS,
        "execution_profile": EXECUTION_PROFILE,
        "protocol_version": PROTOCOL_VERSION,
        "source_set_id": state.source_set_id,
        "run_id": state.run_id,
        "input_revision": state.input_revision,
        "candidate_id": c.candidate_id,
        "revision": cur.revision,
        "design_revision": cur.design_revision,
        "USER_LOCK": state.request.get("user_lock") or "없음",
        "root_claim_text": root_text,
        "root_text_sha256": cur.exact_sha256,
        "invention_sources": material_names,
        "design_record_id": c.design_record_id,
        "meaning_draft_id": rec.get("meaning_draft"),
        "style_record_id": style,
        "claim_style_gate": _g(state, style, "CLAIM_STYLE_GATE"),
        "term_expression_gate": _g(state, style, "TERM_EXPRESSION_GATE"),
        "non_patent_technical_reader_gate": _g(state, style, "NON_PATENT_TECHNICAL_READER_GATE"),
        "geometric_object_gate": _g(state, style, "GEOMETRIC_OBJECT_GATE"),
        "success_record_id": rec.get("success"),
        "syntax_record_id": rec.get("syntax"),
        "syntax_decision": (state.record(rec.get("syntax")).status if state.record(rec.get("syntax")) else "N/A"),
        "oa_record_id": oa,
        "oa_draft_gate": _g(state, oa, "OA_DRAFT_GATE"),
        "oa_final_gate": _g(state, oa, "OA_FINAL_GATE"),
        "blind_snapshot_id": rec.get("blind"),
        "independence": "FRESH_CALL_NO_PROJECT_CONTEXT",
        "reference_compare_record_id": picture,
        "reference_compare_decision": (state.record(picture).status if state.record(picture) else "N/A"),
        "assurance_note": "Python 오케스트레이터가 별도 Gemini 호출로 각 역할을 실행; blind는 소스·설계 정보 없는 fresh 호출",
        "unverified": _unverified(state, oa),
        "status_label": FINAL_LABEL if final else PROVISIONAL_LABEL,
    }
    return lock, render_lock_md(lock)


def _unverified(state: RunState, oa_id: str | None) -> list[str]:
    out = []
    if not state.spec_present:
        out.append("OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED")
    if not state.prior_art_present:
        out.append("신규성·진보성: UNVERIFIED — PRIOR_ART_NOT_PROVIDED")
    return out


def build_dependent_set_lock(state: RunState, lock_id: str, final: bool, root_text: str, set_text: str, material_names: list[str]) -> tuple[dict[str, Any], str]:
    d = state.dependent
    assert d is not None and d.current is not None
    cur = d.current
    rec = cur.records
    style = rec.get("dependent_style")
    oa = rec.get("dependent_oa")
    snapshots = []
    for tid, t in cur.targets.items():
        snapshots.append(
            {
                "target_claim_id": tid,
                "snapshot_id": t.blind_record_id,
                "independence": "FRESH_CALL_NO_PROJECT_CONTEXT",
                "reference_compare_record_id": t.picture_record_id,
                "reference_compare_decision": t.picture_status,
            }
        )
    design = state.record(d.design_record_id)
    lock: dict[str, Any] = {
        "record_type": "FINAL_DEPENDENT_SET_LOCK" if final else "DRAFT_DEPENDENT_SET_LOCK",
        "lock_id": lock_id,
        "lock_class": "FINAL-ISOLATED-PY" if final else LOCK_CLASS,
        "execution_profile": EXECUTION_PROFILE,
        "protocol_version": PROTOCOL_VERSION,
        "source_set_id": state.source_set_id,
        "run_id": state.run_id,
        "input_revision": state.input_revision,
        "root_lock_id": d.root_lock_id,
        "candidate_id": d.root_candidate_id,
        "revision": d.root_revision,
        "design_revision": d.root_design_revision,
        "dependent_set_id": d.dependent_set_id,
        "dependent_design_revision": cur.dependent_design_revision,
        "dependent_revision": cur.dependent_revision,
        "USER_LOCK": state.request.get("user_lock") or "없음",
        "root_claim_text": root_text,
        "dependent_claim_set_text": set_text,
        "set_text_sha256": cur.exact_sha256,
        "invention_sources": material_names,
        "PRIOR_ART_SET": "제공됨" if state.prior_art_present else "NONE",
        "dependent_design_record_id": d.design_record_id,
        "dependent_design_gate": (design.gates.get("DEPENDENT_DESIGN_GATE") if design else "N/A"),
        "dependent_meaning_draft_id": rec.get("dependent_meaning_draft"),
        "dependent_style_record_id": style,
        "claim_style_gate": _g(state, style, "CLAIM_STYLE_GATE"),
        "term_expression_gate": _g(state, style, "TERM_EXPRESSION_GATE"),
        "non_patent_technical_reader_gate": _g(state, style, "NON_PATENT_TECHNICAL_READER_GATE"),
        "geometric_object_gate": _g(state, style, "GEOMETRIC_OBJECT_GATE"),
        "dependent_success_record_id": rec.get("dependent_success"),
        "dependent_syntax_record_id": rec.get("dependent_syntax"),
        "dependent_oa_record_id": oa,
        "dependent_oa_draft_gate": _g(state, oa, "DEPENDENT_OA_DRAFT_GATE"),
        "dependent_oa_final_gate": _g(state, oa, "DEPENDENT_OA_FINAL_GATE"),
        "dependent_snapshots": snapshots,
        "dependent_reconstruction_gate": cur.reconstruction_gate,
        "inventive_step": (design.gates.get("INVENTIVE_STEP") if design else "UNVERIFIED") or "UNVERIFIED",
        "assurance_note": "각 종속항은 부모항 체인+목표항만 받은 fresh blind 호출과 별도 picture 비교를 거침",
        "unverified": _unverified(state, oa),
        "status_label": "출원용 최종 종속항 세트" if final else PROVISIONAL_LABEL,
    }
    return lock, render_lock_md(lock)


def render_lock_md(lock: dict[str, Any]) -> str:
    lines = [f"# {lock['record_type']} {lock['lock_id']}", "", "```text"]
    for k, v in lock.items():
        if isinstance(v, str) and "\n" in v:
            lines.append(f"{k}: |")
            lines += ["  " + ln for ln in v.splitlines()]
        elif isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                if isinstance(item, dict):
                    lines.append("  - " + "; ".join(f"{a}: {b}" for a, b in item.items()))
                else:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("```")
    return "\n".join(lines) + "\n"
