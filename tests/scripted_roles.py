"""Canned role envelopes for scripted pipeline tests.

Each factory returns a callable(spec) -> envelope dict that echoes the
identifiers written in the packet header, so tests never hard-code ids.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from claim_copa.provider.base import CallSpec

ROOT_CLAIM = """【청구항 1】
책상 가장자리에 걸리는 클램프부를 갖는 베이스;
상기 클램프부의 하판을 관통하여 끝단이 책상 하면을 누르는 조임 나사; 및
상기 클램프부의 상판의 상면에 결합되고, 위쪽으로 개방된 복수의 슬롯이 폭 방향으로 나란히 형성된 홀더 본체를 포함하고,
상기 복수의 슬롯 각각은 입구 폭이 바닥 폭보다 좁게 형성되는 케이블 클립 홀더."""

DEP_CLAIMS = [
    (2, 1, "DC-01", "【청구항 2】\n제1항에 있어서,\n상기 홀더 본체는 상기 복수의 슬롯 각각의 입구 양측에 형성되어 상기 슬롯의 내측으로 돌출하는 탄성 리브를 더 포함하는 케이블 클립 홀더."),
    (3, 1, "DC-02", "【청구항 3】\n제1항에 있어서,\n상기 클램프부는 상기 상판과 상기 하판을 잇는 연결판을 포함하는 케이블 클립 홀더."),
    (4, 3, "DC-03", "【청구항 4】\n제3항에 있어서,\n상기 조임 나사의 끝단이 상기 책상 하면을 누를 때 상기 상판이 책상 상면에 밀착되는 케이블 클립 홀더."),
]
DEP_SET_TEXT = "\n\n".join(t for _, _, _, t in DEP_CLAIMS)


def _hdr(spec: CallSpec, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}(?: \(.*?\))?:\s*(.+)$", spec.packet_text, re.MULTILINE)
    if not m:
        return None
    v = m.group(1).strip()
    return None if v in ("N/A", "") else v


def ids_from(spec: CallSpec) -> dict[str, Any]:
    return {
        "candidate_id": _hdr(spec, "candidate_id") or "",
        "revision": _hdr(spec, "revision") or "",
        "design_revision": _hdr(spec, "design_revision") or "",
        "dependent_set_id": _hdr(spec, "dependent_set_id"),
        "dependent_design_revision": _hdr(spec, "dependent_design_revision"),
        "dependent_revision": _hdr(spec, "dependent_revision"),
        "target_claim_id": _hdr(spec, "target_claim_id"),
        "record_id": (spec.meta or {}).get("record_id") or _hdr(spec, "record_id") or _hdr(spec, "record_id (오케스트레이터 지정, PASS 시 발급)") or "",
    }


def _base(spec: CallSpec, status: str = "PASS", gates: dict[str, str] | None = None, report_lines: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    gates = gates or {}
    lines = [f"- {k}: {v}" for k, v in gates.items()] + [f"- 상태: {status}"] + (report_lines or []) + ["- 검수 대상 exact 청구항 전문: (생략 없이 아래)", "- 소스 로딩 기록: 사전 로딩 블록", "- 문언 직접 수정 여부: 없음"]
    env: dict[str, Any] = {
        "identifiers": ids_from(spec),
        "execution_status": "RUN",
        "status": status,
        "reason_code": None,
        "gates": gates,
        "gate_reasons": [],
        "checks": [],
        "next_step": "PROCEED",
        "handoff_ready": True,
        "finalized_status": None,
        "exact_claim_text": None,
        "claims": [],
        "per_claim_gates": [],
        "invention_type": {"primary": "PHYSICAL", "secondary": [], "hybrid": False},
        "candidates": [],
        "sketchability": None,
        "open_issues": [],
        "sources_read": [],
        "aux_source_usage": None,
        "report_markdown": "\n".join(lines) + "\n\n" + ("역할 보고서 본문 " * 5),
    }
    env.update(extra)
    return env


def architect(locked: bool = True) -> Callable[[CallSpec], dict]:
    def f(spec: CallSpec) -> dict:
        if locked:
            return _base(spec, "PASS", {"DESIGN_GATE": "LOCKED"}, ["- 주골격 3~5개: 베이스/조임 나사/홀더 본체/슬롯 입구 폭"])
        return _base(spec, "REVIEW", {"DESIGN_GATE": "UNLOCKED"}, ["- LOCK 미해결 사항: 슬롯 형상 술어 주체 미확정"], next_step="USER_DECISION",
                     open_issues=[{"kind": "REVIEW", "code": "GEOMETRY", "text": "오목 형상의 귀속 주체를 원자료로 확정해야 한다", "return_to": None}])
    return f


def drafter(scope: str = "INDEPENDENT", return_to: str | None = None) -> Callable[[CallSpec], dict]:
    def f(spec: CallSpec) -> dict:
        gates = {"DRAFTER_GATE": "PASS", "PRE_STYLE_NON_PATENT_TECHNICAL_READER_CHECK": "PASS", "PRE_STYLE_GEOMETRIC_OBJECT_CHECK": "PASS"}
        if return_to:
            gates["DRAFTER_GATE"] = return_to
            return _base(spec, "BLOCK", gates, next_step=return_to, handoff_ready=False, open_issues=[{"kind": "BLOCK", "code": "DESIGN", "text": "계층 재확정 필요"}])
        if scope == "INDEPENDENT":
            return _base(spec, "PASS", gates, ["- claim-style-adjuster 인계 가능: YES"], exact_claim_text=ROOT_CLAIM.replace("홀더 본체", "[C3: 홀더 본체]"))
        claims = [{"claim_no": n, "parent_claim_no": p, "dc_id": dc, "text": t} for n, p, dc, t in DEP_CLAIMS]
        return _base(spec, "PASS", gates, ["- claim-style-adjuster 인계 가능: YES"], exact_claim_text=DEP_SET_TEXT, claims=claims)
    return f


def style(scope: str = "INDEPENDENT", return_to: str | None = None, text: str | None = None) -> Callable[[CallSpec], dict]:
    def f(spec: CallSpec) -> dict:
        gates = {"TERM_EXPRESSION_GATE": "PASS", "CLAIM_STYLE_GATE": "PASS", "NON_PATENT_TECHNICAL_READER_GATE": "PASS", "GEOMETRIC_OBJECT_GATE": "PASS"}
        if return_to:
            gates["CLAIM_STYLE_GATE"] = return_to
            return _base(spec, "BLOCK", gates, next_step=return_to, handoff_ready=False, finalized_status="NOT_FINALIZED",
                         open_issues=[{"kind": "BLOCK", "code": "CLAUSE", "text": "절 결속 재작성 필요: 슬롯 각각의 귀속", "return_to": return_to}])
        if scope == "INDEPENDENT":
            return _base(spec, "PASS", gates, ["- 확정 revision 상태: FINALIZED_FOR_SUCCESS"], finalized_status="FINALIZED_FOR_SUCCESS", exact_claim_text=text or ROOT_CLAIM, aux_source_usage="NOT_ACTIVATED")
        claims = [{"claim_no": n, "parent_claim_no": p, "dc_id": dc, "text": t} for n, p, dc, t in DEP_CLAIMS]
        pcg = [{"claim_no": n, "verdict": "PASS", "NON_PATENT_TECHNICAL_READER_GATE": "PASS", "GEOMETRIC_OBJECT_GATE": "PASS"} for n, _, _, _ in DEP_CLAIMS]
        return _base(spec, "PASS", gates, ["- 확정 revision 상태: FINALIZED_FOR_SUCCESS"], finalized_status="FINALIZED_FOR_SUCCESS", exact_claim_text=text or DEP_SET_TEXT, claims=claims, per_claim_gates=pcg, aux_source_usage="NOT_ACTIVATED")
    return f


def success(return_to: str | None = None) -> Callable[[CallSpec], dict]:
    def f(spec: CallSpec) -> dict:
        gates = {"CLAIM_STYLE_GATE": "PASS", "TERM_EXPRESSION_GATE": "PASS", "NON_PATENT_TECHNICAL_READER_GATE": "PASS", "GEOMETRIC_OBJECT_GATE": "PASS"}
        if return_to:
            return _base(spec, "BLOCK", gates, ["- syntax 진행 가능: NO"], next_step=return_to, handoff_ready=False, open_issues=[{"kind": "BLOCK", "code": "COND9", "text": "조건 9 분배 불명확", "return_to": return_to}])
        return _base(spec, "PASS", gates, ["- syntax 진행 가능: YES", "- pipeline_eligible: YES"])
    return f


def syntax() -> Callable[[CallSpec], dict]:
    def f(spec: CallSpec) -> dict:
        return _base(spec, "PASS", {"NON_PATENT_TECHNICAL_READER_GATE": "PASS", "GEOMETRIC_OBJECT_GATE": "PASS"}, ["- 종합 판정: PASS", "- OA 진행 가능: YES"])
    return f


def oa(scope: str = "INDEPENDENT", final_pass: bool = False, merged: bool = False) -> Callable[[CallSpec], dict]:
    def f(spec: CallSpec) -> dict:
        d, fn = ("OA_DRAFT_GATE", "OA_FINAL_GATE") if scope == "INDEPENDENT" else ("DEPENDENT_OA_DRAFT_GATE", "DEPENDENT_OA_FINAL_GATE")
        gates = {d: "PASS", fn: "PASS" if final_pass else "UNVERIFIED", "NON_PATENT_TECHNICAL_READER_GATE": "PASS", "GEOMETRIC_OBJECT_GATE": "PASS"}
        lines = [f"- {fn}: UNVERIFIED — SPEC_NOT_PROVIDED" if not final_pass else f"- {fn}: PASS", "- DRAFT 진행 가능: 역구성 YES"]
        env = _base(spec, "PASS", gates, lines, gate_reasons=[] if final_pass else [{"gate": fn, "reason_code": "SPEC_NOT_PROVIDED"}])
        if merged:  # contract violation: only a single 종합 판정 line
            env["report_markdown"] = "- 종합 판정: PASS\n\n" + "본문 " * 20
        return env
    return f


def blind(complete: bool = True) -> Callable[[CallSpec], dict]:
    def f(spec: CallSpec) -> dict:
        env = _base(spec, "PASS" if complete else "UNVERIFIED", {}, ["- 실행 상태: BLIND_COMPLETE", "- 1회독 도식화 가능성: YES"], sketchability="YES")
        env["execution_status"] = "BLIND_COMPLETE" if complete else "UNVERIFIED"
        env["reason_code"] = None if complete else "INPUT_MISSING"
        return env
    return f


def picture(status: str = "PASS", return_to: str | None = None) -> Callable[[CallSpec], dict]:
    def f(spec: CallSpec) -> dict:
        gates = {"NON_PATENT_TECHNICAL_READER_GATE": "PASS", "GEOMETRIC_OBJECT_GATE": "PASS"}
        if return_to:
            return _base(spec, "BLOCK", {**gates, "GEOMETRIC_OBJECT_GATE": "BLOCK"}, ["- 최종 판정: BLOCK"], next_step=return_to, open_issues=[{"kind": "BLOCK", "code": "GEO", "text": "관찰 단면과 실제 면 혼동", "return_to": return_to}])
        return _base(spec, status, gates, [f"- 최종 판정: {status}"])
    return f


def dep_architect(locked: bool = True, tsc: bool = True) -> Callable[[CallSpec], dict]:
    def f(spec: CallSpec) -> dict:
        cands = [{"dc_id": dc, "classification": "TECHNICAL_SOLUTION_CANDIDATE" if tsc else "DRAWING_ONLY", "DEPENDENT_SOURCE_GATE": "PASS", "CAUSAL_CONTRIBUTION_GATE": "PASS", "CLAIMABILITY_GATE": "PASS", "PRIOR_ART_CONTRIBUTION_GATE": "UNVERIFIED", "planned_claim_no": n, "parent_claim_no": p} for n, p, dc, _ in DEP_CLAIMS]
        if not locked or not tsc:
            return _base(spec, "BLOCK", {"DEPENDENT_DESIGN_GATE": "UNLOCKED", "INVENTIVE_STEP": "UNVERIFIED"}, ["- DEPENDENT_DESIGN_GATE: UNLOCKED — NO_TECHNICAL_SOLUTION_CANDIDATE"], reason_code="NO_TECHNICAL_SOLUTION_CANDIDATE", candidates=cands, next_step="STOP")
        return _base(spec, "PASS", {"DEPENDENT_DESIGN_GATE": "LOCKED", "INVENTIVE_STEP": "UNVERIFIED"}, ["- INVENTIVE_STEP: UNVERIFIED — PRIOR_ART_NOT_PROVIDED"], candidates=cands)
    return f


def happy_script(dependent: bool = True, n_targets: int = 3) -> dict[str, list]:
    s = {
        "claim-architect": [architect()],
        "claim-drafter": [drafter("INDEPENDENT")] + ([drafter("DEPENDENT_SET")] if dependent else []),
        "claim-style-adjuster": [style("INDEPENDENT")] + ([style("DEPENDENT_SET")] if dependent else []),
        "claim-success-reviewer": [success()] + ([success()] if dependent else []),
        "syntax-scope-reviewer": [syntax()] + ([syntax()] if dependent else []),
        "oa-strategy-reviewer": [oa("INDEPENDENT")] + ([oa("DEPENDENT_SET")] if dependent else []),
        "blind-claim-reconstruction-reviewer": [blind()] + ([blind()] * n_targets if dependent else []),
        "picture-claim-reconstruction-reviewer": [picture()] + ([picture(), picture("PASS-RANGE"), picture()][:n_targets] if dependent else []),
        "dependent-claim-strategy-architect": [dep_architect()] if dependent else [],
    }
    return s
