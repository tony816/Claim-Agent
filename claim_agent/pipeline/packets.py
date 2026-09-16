"""Input packets for every role call.

Each packet mirrors the role file's 입력 하드 게이트 list: a HANDOFF-style
header, then the required 전문 sections. Reports of upstream roles are passed
verbatim (report_markdown) exactly as the Claude Code orchestrator would.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import EXECUTION_PROFILE, PROTOCOL_VERSION
from ..models.enums import Scope, StyleChangeMode
from ..models.ids import Identifiers
from ..models.request import MaterialBundle
from ..models.state import RunState
from ..provider.base import ImagePart
from .claimtext import flatten


@dataclass
class Packet:
    text: str
    images: list[ImagePart] = field(default_factory=list)
    cache_text: str = ""
    cache_images: bool = False


def _hdr(state: RunState, ids: Identifiers, record_id: str, extra: dict[str, str] | None = None) -> str:
    lines = [
        "## RUN_HEADER",
        f"execution_profile: {EXECUTION_PROFILE}",
        f"protocol_version: {PROTOCOL_VERSION}",
        f"source_set_id: {state.source_set_id}",
        f"run_id: {state.run_id}",
        f"request_mode: {state.request_mode.value}",
        f"input_revision: {state.input_revision}",
        f"candidate_id: {ids.candidate_id}",
        f"revision: {ids.revision}",
        f"design_revision: {ids.design_revision}",
        f"dependent_set_id: {ids.dependent_set_id or 'N/A'}",
        f"dependent_design_revision: {ids.dependent_design_revision or 'N/A'}",
        f"dependent_revision: {ids.dependent_revision or 'N/A'}",
        f"target_claim_id: {ids.target_claim_id or 'N/A'}",
        f"record_id (오케스트레이터 지정, PASS 시 발급): {record_id}",
        f"PRIOR_ART_SET: {'제공됨(아래 원자료 참조)' if state.prior_art_present else 'NONE'}",
        f"정식 명세서 상태: {'PRESENT' if state.spec_present else 'MISSING'}",
    ]
    if state.baseline_set:
        lines.append(f"authoring_scope: EXISTING_SET_EDIT (루트 기준: BASELINE_SET {state.baseline_set.record_id} — LOCK 아님, 이번 run 미검증)")
        lines.append("edit_targets: " + ", ".join(f"제{n}항" for n in state.baseline_set.edit_targets))
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n\n"


def _section(title: str, body: str | None) -> str:
    return f"### {title}\n\n{(body or '').rstrip()}\n\n"


def _user_lock(bundle: MaterialBundle) -> str:
    return _section("USER_LOCK 원문", bundle.user_lock or "없음")


def _materials(bundle: MaterialBundle, cats: tuple[str, ...]) -> tuple[str, list[ImagePart]]:
    items = bundle.by_category(*cats)
    if not items:
        return _section("현재 발명 원자료", "없음 — 제공된 원자료가 없다. 기술내용을 추정하지 않는다."), []
    parts = ["### 현재 발명 원자료 (전문)\n\n"]
    images: list[ImagePart] = []
    for it in items:
        label = f"[{it.category}] {it.name} (sha256={it.sha256[:12]})"
        if it.kind == "text":
            parts.append(f"<<<MATERIAL {label}>>>\n{(it.text or '').rstrip()}\n<<<END MATERIAL>>>\n\n")
        else:
            parts.append(f"<<<MATERIAL {label}>>> (이미지 파트로 첨부)\n\n")
            images.append(ImagePart(it.mime_type or "image/png", it.data or b"", label, it.content_sha256))
    return "".join(parts), images


def _report(state: RunState, store, record_id: str | None, title: str) -> str:
    if not record_id:
        return _section(title, "없음")
    return _section(title + f" ({record_id})", store.read_record_report(state.run_id, record_id))


def _output_contract(extra: str = "") -> str:
    return (
        "### 출력 계약\n\n"
        "제공된 JSON 스키마를 따르는 JSON 객체 하나만 반환한다. `report_markdown`에는 역할 파일의 출력 형식 항목을 어댑터 규칙 5의 압축형(목표 3,000자 이내)으로 담고, "
        "`status`·`gates`·`next_step`·`handoff_ready`·`exact_claim_text`는 보고서와 글자 단위로 일치시킨다. "
        "입력으로 받은 다른 역할의 보고서, 원자료, BASELINE_SET 및 부모항 체인 전문을 출력 보고서에 통째로 재첨부하지 않는다. "
        "필요한 경우 record_id·항 번호·위치로만 참조한다. 이번 역할이 새로 작성한 청구항 문언은 예외다. "
        "이번 역할의 필수 출력 형식·exact 문언·근거표·판정 이유는 빠짐없이 작성한다. "
        + extra
        + "\n"
    )


# --------------------------------------------------------------------------- independent
#
# Packet layout (stable prefix first): `USER_LOCK → 원자료 → run-invariant upstream reports` come before the
# volatile `RUN_HEADER → task sections → per-revision reports → 출력 계약`. A role's repeated calls
# (loops, repair, tool phase) then share an identical prefix, which implicit context caching can reuse;
# nothing in any role contract fixes the header position, and every identifier is still in the packet.


def architect(state: RunState, bundle: MaterialBundle, ids: Identifiers, record_id: str, request_text: str, redesign_goal: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    txt = _user_lock(bundle) + mats
    txt += _hdr(state, ids, record_id)
    txt += _section("현재 요청", request_text)
    if bundle.claim_file_text():
        txt += _section("기존 청구항 — 편집 대상 (새 기술내용의 근거가 아님)", bundle.claim_file_text())
    if redesign_goal:
        txt += _section("설계 변경 목표 (새 design_revision)", redesign_goal)
    txt += _output_contract("`gates.DESIGN_GATE`에 LOCKED/UNLOCKED, `invention_type`에 발명 유형을 적는다.")
    return Packet(txt, imgs)


def drafter_independent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, design_record_id: str, prior_text: str | None, change_goal: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec"))
    txt = _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE: LOCKED 전문 (claim-architect 보고서)")
    txt += _hdr(state, ids, record_id, {"draft_scope": "INDEPENDENT", "meaning_draft_id": record_id, "목표 revision": ids.revision})
    if prior_text:
        txt += _section("직전 확정 revision 청구항 전문", prior_text)
        txt += _section("이번 의미 변경 목표", change_goal or "")
    txt += _output_contract("`exact_claim_text`에 PRE_STYLE 의미 초안 전문을, `gates.DRAFTER_GATE`와 두 PRE_STYLE 검사를 적고, `handoff_ready`는 `claim-style-adjuster 인계 가능` 값이다.")
    return Packet(txt, imgs)


def style_independent(
    state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, design_record_id: str,
    meaning_draft_id: str | None, mode: StyleChangeMode, prior_text: str | None, prior_style_record_id: str | None, feedback: str | None,
) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec"))
    txt = _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE: LOCKED 전문")
    txt += _hdr(state, ids, record_id, {"style_scope": "INDEPENDENT", "style_change_mode": mode.value, "style_record_id": record_id, "목표 revision": ids.revision})
    if mode == StyleChangeMode.STYLE_ONLY_REVISION:
        txt += _section("직전 확정 revision 청구항 전문", prior_text)
        txt += _report(state, store, prior_style_record_id, "직전 style record 전문")
        txt += _section("스타일만 바꾸어야 하는 정확한 피드백·규칙", feedback or "")
    else:
        txt += _report(state, store, meaning_draft_id, "claim-drafter PRE_STYLE 의미 초안 보고서 전문 (revision_status: PRE_STYLE — NOT_GATE_ELIGIBLE)")
        if mode == StyleChangeMode.DRAFTER_REVISION and prior_text:
            txt += _section("직전 확정 revision 청구항 전문", prior_text)
            txt += _section("이번 의미 수정 목표", feedback or "")
    txt += _section("조건부 보조 소스", "`search_style_corpus`/`open_routing_index` 도구가 제공된 경우에만 07 §3 조건에서 사용하고, 사용하지 않았으면 `조건부 보조 소스: NOT_ACTIVATED`로 기록한다.")
    txt += _output_contract("`exact_claim_text`에 스타일 적용 후 최종 청구항 전문을 넣고, `finalized_status`, `TERM_EXPRESSION_GATE`, `CLAIM_STYLE_GATE`, `NON_PATENT_TECHNICAL_READER_GATE`, `GEOMETRIC_OBJECT_GATE`를 채운다.")
    return Packet(txt, imgs)


def success_independent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, design_record_id: str, meaning_draft_id: str | None, style_record_id: str, exact_text: str, corpus_fragments: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    txt = _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE: LOCKED 전문")
    txt += _hdr(state, ids, record_id, {"success_scope": "INDEPENDENT", "success_record_id (예정)": record_id})
    txt += _section("검수 대상 exact 청구항 전문", exact_text)
    txt += _section("평문(줄바꿈·번호 제거)", flatten(exact_text))
    txt += _report(state, store, meaning_draft_id, "PRE_STYLE 의미 초안 보고서 전문")
    txt += _report(state, store, style_record_id, "style record 전문 (변경 대조표·용어·표현 출처표·두 후처리 게이트·독자·기하 게이트)")
    if corpus_fragments:
        txt += _section("style record가 사용한 코퍼스 정확 조각과 라우팅 인덱스", corpus_fragments)
    txt += _output_contract("`handoff_ready`는 `syntax 진행 가능` 값이다. `exact_claim_text`에는 검수 대상 전문을 그대로 echo한다.")
    return Packet(txt, imgs)


def syntax_independent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, design_record_id: str, style_record_id: str, success_record_id: str, exact_text: str, baseline_text: str | None, mode: StyleChangeMode) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec"))
    txt = _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE 전문")
    txt += _hdr(state, ids, record_id, {"review_scope": "INDEPENDENT"})
    txt += _section("검수 대상 청구항 전문", exact_text)
    txt += _section("평문(줄바꿈·번호 제거)", flatten(exact_text))
    txt += _report(state, store, style_record_id, "style record 전문 (CLAIM_STYLE_GATE: PASS)")
    txt += _report(state, store, success_record_id, "claim-success-reviewer success record 전문 (상태: PASS)")
    txt += _section(f"범위 불변 비교 기준 전문 ({mode.value})", baseline_text or "style record의 PRE_STYLE 의미 초안 참조")
    txt += _output_contract("`status`는 종합 판정, `handoff_ready`는 `OA 진행 가능` 값이다.")
    return Packet(txt, imgs)


def oa_independent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, design_record_id: str, style_record_id: str, success_record_id: str, syntax_record_id: str, exact_text: str) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    txt = _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE 전문")
    txt += _hdr(state, ids, record_id, {"review_scope": "INDEPENDENT"})
    txt += _section("검수 대상 청구항 전문", exact_text)
    txt += _report(state, store, style_record_id, "style record 전문")
    txt += _report(state, store, success_record_id, "success record 전문")
    txt += _report(state, store, syntax_record_id, "syntax-scope-reviewer PASS 보고서 전문")
    txt += _output_contract(
        "`gates.OA_DRAFT_GATE`와 `gates.OA_FINAL_GATE`를 반드시 분리해 채우고, 정식 명세서가 없으면 OA_FINAL_GATE는 UNVERIFIED에 `gate_reasons`로 SPEC_NOT_PROVIDED를 적는다. "
        "두 게이트를 하나의 종합 판정으로 합치지 않는다. `handoff_ready`는 `DRAFT 진행 가능(역구성 YES)` 값이다."
    )
    return Packet(txt, imgs)


def picture_independent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, design_record_id: str, style_record_id: str, oa_record_id: str, blind_record_id: str, exact_text: str) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing"))
    txt = mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE 전문")
    txt += _hdr(state, ids, record_id, {"mode": "REFERENCE_COMPARE", "claim_scope": "INDEPENDENT", "blind_snapshot_id": blind_record_id})
    txt += _section("변경 없는 청구항 전문", exact_text)
    txt += _report(state, store, blind_record_id, "봉인된 blind snapshot 전문 (independence: FRESH_CALL_NO_PROJECT_CONTEXT)")
    txt += _report(state, store, style_record_id, "style record 전문 (CLAIM_STYLE_GATE·TERM_EXPRESSION_GATE)")
    txt += _report(state, store, oa_record_id, "OA 보고서 전문")
    txt += _output_contract("`status`는 최종 판정(PASS/PASS-RANGE/REVIEW/BLOCK/UNVERIFIED)이다.")
    return Packet(txt, imgs)


# --------------------------------------------------------------------------- dependent
def _root_label(state: RunState) -> tuple[str, str]:
    """(root record title, root text title). An edit run's root is the user's baseline chain, not a gated LOCK."""
    if state.baseline_set:
        return ("기존 청구항 세트 BASELINE_SET 전문 (LOCK 아님 — 이번 run에서 검증하지 않은 읽기 전용 부모항 체인)",
                "변경 금지 부모항 체인 전문 (BASELINE_SET — 흡수·병합·재작성 금지)")
    return "루트 독립항 LOCK 전문", "변경 없는 루트 독립항 전문"


def _number_contract(state: RunState, target_nos: set[int] | None) -> str:
    if not target_nos:
        return ""
    nos = ", ".join(map(str, sorted(target_nos)))
    text = (f" 요청된 종속항 번호 집합은 [{nos}]이다. `TECHNICAL_SOLUTION_CANDIDATE`인 `candidates[].planned_claim_no` 집합은 이 집합과 정확히 "
            "일치해야 하며 임의 발번·범위 확대·누락은 계약 위반이다. 해당 번호로 낼 기술기여 후보가 없으면 후보를 만들지 말고 `status: REVIEW`로 사유를 보고한다.")
    if state.baseline_set:
        parents = {c.claim_no: c.parent_nos for c in state.baseline_set.claims}
        fixed = "; ".join(f"제{n}항의 부모항 제{', '.join(map(str, parents[n]))}항" for n in sorted(target_nos) if n in parents)
        text += f" EXISTING_SET_EDIT이므로 기존 인용관계({fixed})를 `parent_claim_no`로 유지하고, 부모항 체인을 흡수·병합해 독립항으로 설계하지 않는다."
    return text


def _edit_contract(state: RunState) -> str:
    base = state.baseline_set
    if not base:
        return ""
    nos = ", ".join(f"제{n}항" for n in base.edit_targets)
    return (f" EXISTING_SET_EDIT: `claims[]`와 `exact_claim_text`에는 편집 대상 {nos}만 넣고 기존 세트의 번호·인용관계를 그대로 유지한다. "
            "부모항 체인은 읽기 전용이며 흡수·병합·재작성하지 않는다.")


def _dependent_stable(state: RunState, store, bundle: MaterialBundle, root_lock_id: str, root_design_record_id: str | None, cats: tuple[str, ...]) -> tuple[str, list[ImagePart]]:
    """Run-invariant part shared by every dependent-stage packet: USER_LOCK, 원자료, root lock (+ root design)."""
    mats, imgs = _materials(bundle, cats)
    txt = _user_lock(bundle) + mats
    txt += _report(state, store, root_lock_id, _root_label(state)[0])
    if root_design_record_id:
        txt += _report(state, store, root_design_record_id, "루트 DESIGN_GATE 전문 (기술 개념표)")
    return txt, imgs


def dep_architect(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_design_record_id: str, root_text: str, request_text: str, redesign_goal: str | None,
                  target_nos: set[int] | None = None, scope_repair: str | None = None) -> Packet:
    txt, imgs = _dependent_stable(state, store, bundle, root_lock_id, root_design_record_id, ("invention", "drawing", "spec", "prior_art"))
    txt += _hdr(state, ids, record_id, {"dependent_target_claim_nos": ", ".join(map(str, sorted(target_nos)))} if target_nos else None)
    txt += _section("현재 요청 (종속항 세트)", request_text)
    if redesign_goal:
        txt += _section("종속항 설계 변경 목표", redesign_goal)
    if scope_repair:
        txt += _section("번호 계약 위반 복구 (1회)", f"직전 설계가 번호 계약을 어겨 중지되었다: {scope_repair}\n"
                        "예정 항 번호와 부모항을 아래 출력 계약의 번호 집합·인용관계와 정확히 일치시켜 다시 설계한다.")
    txt += _section(_root_label(state)[1], root_text)
    txt += _output_contract("`gates.DEPENDENT_DESIGN_GATE`, `gates.INVENTIVE_STEP`, `candidates[]`(DC-NN별 분류·세 게이트·예정 항·부모항)를 채운다."
                            + _number_contract(state, target_nos))
    return Packet(txt, imgs)


def drafter_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, prior_set_text: str | None, change_goal: str | None) -> Packet:
    txt, imgs = _dependent_stable(state, store, bundle, root_lock_id, None, ("invention", "drawing", "spec"))
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE: LOCKED 전문")
    txt += _hdr(state, ids, record_id, {"draft_scope": "DEPENDENT_SET", "dependent_meaning_draft_id": record_id})
    txt += _section(_root_label(state)[1], root_text)
    if prior_set_text:
        txt += _section("직전 확정 dependent_revision 세트 전문", prior_set_text)
        txt += _section("이번 의미 변경 목표", change_goal or "")
    txt += _output_contract("`claims[]`에 각 종속항의 claim_no·parent_claim_no·dc_id·text(【청구항 N】 헤더 포함 전문)를, `exact_claim_text`에 세트 전문을 넣는다." + _edit_contract(state))
    return Packet(txt, imgs)


def style_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, dep_meaning_draft_id: str | None, mode: StyleChangeMode, prior_set_text: str | None, prior_style_record_id: str | None, feedback: str | None) -> Packet:
    txt, imgs = _dependent_stable(state, store, bundle, root_lock_id, None, ("invention", "drawing", "spec"))
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE: LOCKED 전문")
    txt += _hdr(state, ids, record_id, {"style_scope": "DEPENDENT_SET", "style_change_mode": mode.value, "dependent_style_record_id": record_id})
    txt += _section(_root_label(state)[1], root_text)
    if mode == StyleChangeMode.STYLE_ONLY_REVISION:
        txt += _section("직전 확정 dependent_revision 세트 전문", prior_set_text)
        txt += _report(state, store, prior_style_record_id, "직전 dependent style record 전문")
        txt += _section("스타일만 바꾸어야 하는 정확한 피드백·규칙", feedback or "")
    else:
        txt += _report(state, store, dep_meaning_draft_id, "drafter 종속항 PRE_STYLE 의미 초안 세트 보고서 전문")
        if mode == StyleChangeMode.DRAFTER_REVISION and prior_set_text:
            txt += _section("직전 확정 dependent_revision 세트 전문", prior_set_text)
            txt += _section("이번 의미 수정 목표", feedback or "")
    txt += _output_contract("`claims[]`에 최종 종속항 세트(항별 전문)를, `exact_claim_text`에 세트 전문을, `per_claim_gates[]`에 목표항별 독자·기하 판정을 넣는다." + _edit_contract(state))
    return Packet(txt, imgs)


def success_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, dep_meaning_draft_id: str | None, dep_style_record_id: str, set_text: str, corpus_fragments: str | None) -> Packet:
    txt, imgs = _dependent_stable(state, store, bundle, root_lock_id, None, ("invention", "drawing", "spec", "prior_art"))
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE: LOCKED 전문")
    txt += _hdr(state, ids, record_id, {"success_scope": "DEPENDENT_SET", "dependent_success_record_id (예정)": record_id})
    txt += _section(_root_label(state)[1], root_text)
    txt += _section("검수 대상 exact 종속항 세트 전문", set_text)
    txt += _report(state, store, dep_meaning_draft_id, "종속항 PRE_STYLE 의미 초안 보고서 전문")
    txt += _report(state, store, dep_style_record_id, "dependent style record 전문")
    if corpus_fragments:
        txt += _section("style record가 사용한 코퍼스 정확 조각과 라우팅 인덱스", corpus_fragments)
    txt += _output_contract("`handoff_ready`는 `syntax 진행 가능` 값이다. `per_claim_gates[]`에 목표항별 판정을 적는다.")
    return Packet(txt, imgs)


def syntax_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, dep_style_record_id: str, dep_success_record_id: str, set_text: str, baseline_text: str | None, mode: StyleChangeMode) -> Packet:
    txt, imgs = _dependent_stable(state, store, bundle, root_lock_id, None, ("invention", "drawing", "spec"))
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE 전문")
    txt += _hdr(state, ids, record_id, {"review_scope": "DEPENDENT_SET"})
    txt += _section(_root_label(state)[1], root_text)
    txt += _section("검수 대상 종속항 세트 전문", set_text)
    txt += _report(state, store, dep_style_record_id, "dependent style record 전문")
    txt += _report(state, store, dep_success_record_id, "dependent success record 전문")
    txt += _section(f"범위 불변 비교 기준 ({mode.value})", baseline_text or "dependent style record의 PRE_STYLE 세트 참조")
    txt += _output_contract("`status`는 종합 판정, `handoff_ready`는 `OA 진행 가능` 값이다.")
    return Packet(txt, imgs)


def oa_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, dep_style_record_id: str, dep_success_record_id: str, dep_syntax_record_id: str, set_text: str) -> Packet:
    txt, imgs = _dependent_stable(state, store, bundle, root_lock_id, None, ("invention", "drawing", "spec", "prior_art"))
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE 전문")
    txt += _hdr(state, ids, record_id, {"review_scope": "DEPENDENT_SET"})
    txt += _section(_root_label(state)[1], root_text)
    txt += _section("검수 대상 종속항 세트 전문", set_text)
    txt += _report(state, store, dep_style_record_id, "dependent style record 전문")
    txt += _report(state, store, dep_success_record_id, "dependent success record 전문")
    txt += _report(state, store, dep_syntax_record_id, "dependent syntax PASS 보고서 전문")
    txt += _output_contract(
        "`gates.DEPENDENT_OA_DRAFT_GATE`와 `gates.DEPENDENT_OA_FINAL_GATE`를 분리해 채우고 정식 명세서가 없으면 FINAL은 UNVERIFIED + SPEC_NOT_PROVIDED다. `handoff_ready`는 `DRAFT 진행 가능(종속항별 역구성 YES)` 값이다."
    )
    return Packet(txt, imgs)


def picture_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_design_record_id: str, dep_design_record_id: str, dep_style_record_id: str, dep_oa_record_id: str, blind_record_id: str, parent_chain_text: str, target_claim_text: str, dc_id: str | None) -> Packet:
    # Shared block first (identical for every target of the set → one explicit cache, parallel jobs share it),
    # then the per-target part.
    mats, imgs = _materials(bundle, ("invention", "drawing"))
    shared = mats
    shared += _report(state, store, root_lock_id, _root_label(state)[0])
    shared += _report(state, store, root_design_record_id, "루트 DESIGN_GATE 전문")
    shared += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE 전문 (목표 DC-NN 포함)")
    shared += _report(state, store, dep_style_record_id, "dependent style record 전문")
    shared += _report(state, store, dep_oa_record_id, "종속항 OA 보고서 전문")
    txt = shared
    txt += _hdr(state, ids, record_id, {"mode": "REFERENCE_COMPARE", "claim_scope": "DEPENDENT_SINGLE", "dependent_blind_snapshot_id": blind_record_id, "목표 DC": dc_id or "미지정(DEPENDENT_DESIGN_GATE에서 대응 확인)"})
    txt += _section("부모항 체인 전문 (blind 입력과 동일)", parent_chain_text)
    txt += _section("목표 종속항 전문 (blind 입력과 동일)", target_claim_text)
    txt += _report(state, store, blind_record_id, "봉인된 dependent blind snapshot 전문")
    txt += _output_contract("`status`는 목표항의 최종 판정이다.")
    return Packet(txt, imgs, cache_text=shared, cache_images=True)


# --------------------------------------------------------------------------- per-claim cost
def text_only(packet: Packet) -> Packet:
    """The packet without image parts, for the text review stages (style·syntax·OA).

    Those stages check sealed wording against the design contract's geometry/space object contract; re-sending every
    drawing made each of their calls tens of thousands of tokens heavier. The packet states what was withheld.
    """
    if not packet.images:
        return packet
    kept = [part for part in packet.text.split("\n\n") if not (part.startswith("<<<MATERIAL ") and part.endswith("(이미지 파트로 첨부)"))]
    note = (f"### 도면 전달 생략\n\n이 문언 검수 단계에는 도면 이미지 {len(packet.images)}장을 전달하지 않는다. 형상·배치 판단은 설계 계약"
            "(DESIGN_GATE·DEPENDENT_DESIGN_GATE)의 형상·공간 객체 계약과 텍스트 원자료로 한다. 도면이 없다는 이유만으로 판정을 낮추지 않고, "
            "설계 계약에 필요한 형상 계약이 없어 판정할 수 없을 때만 그 사유를 REVIEW로 적는다.\n\n")
    return Packet(note + "\n\n".join(kept))


COMBINED_REVIEW_HEADER = (
    "# 통합 검수 호출 (review_mode: COMBINED_SUCCESS_SYNTAX_OA)\n\n"
    "기존 청구항 세트의 한 항 편집(EXISTING_SET_EDIT)에서는 성공조건·통사·OA 검수를 이 한 호출에서 수행한다. 아래 세 역할 파일을 "
    "claim-success-reviewer → syntax-scope-reviewer → oa-strategy-reviewer 순서로 모두 적용하고, 각 역할의 판정 기준을 서로 섞거나 완화하지 않는다."
)


def review_dependent_combined(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str,
                              dep_meaning_draft_id: str | None, dep_style_record_id: str, set_text: str, corpus_fragments: str | None, baseline_text: str | None, mode: StyleChangeMode) -> Packet:
    txt, imgs = _dependent_stable(state, store, bundle, root_lock_id, None, ("invention", "drawing", "spec", "prior_art"))
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE: LOCKED 전문")
    txt += _hdr(state, ids, record_id, {"review_mode": "COMBINED_SUCCESS_SYNTAX_OA", "success_scope": "DEPENDENT_SET", "review_scope": "DEPENDENT_SET",
                                        "dependent_success_record_id (예정)": record_id, "dependent_syntax_record_id (예정)": record_id, "dependent_oa_record_id (예정)": record_id})
    txt += _section(_root_label(state)[1], root_text)
    txt += _section("검수 대상 exact 종속항 세트 전문", set_text)
    txt += _report(state, store, dep_meaning_draft_id, "종속항 PRE_STYLE 의미 초안 보고서 전문")
    txt += _report(state, store, dep_style_record_id, "dependent style record 전문")
    if corpus_fragments:
        txt += _section("style record가 사용한 코퍼스 정확 조각과 라우팅 인덱스", corpus_fragments)
    txt += _section(f"범위 불변 비교 기준 ({mode.value})", baseline_text or "dependent style record의 PRE_STYLE 세트 참조")
    txt += _section("통합 검수 규칙", "`report_markdown`을 `### 1. 성공조건 (claim-success-reviewer)`, `### 2. 통사·범위 (syntax-scope-reviewer)`, "
                    "`### 3. OA (oa-strategy-reviewer)` 세 절로 나누고 각 절에 그 역할의 게이트 줄을 적는다. 뒤 절이 요구하는 앞 역할의 기록 전문"
                    "(success record, syntax PASS 보고서)은 이 보고서의 앞 절 판정으로 대신한다. 앞 절이 PASS가 아니면 뒤 절은 판정하지 않고 역할 파일의 "
                    "upstream 미통과 사유로 UNVERIFIED를 적는다.")
    txt += _output_contract(
        "`gates`에 CLAIM_STYLE_GATE·TERM_EXPRESSION_GATE(성공조건 재검사), NON_PATENT_TECHNICAL_READER_GATE·GEOMETRIC_OBJECT_GATE(통사), "
        "DEPENDENT_OA_DRAFT_GATE·DEPENDENT_OA_FINAL_GATE(OA, 분리)를 모두 채운다. 정식 명세서가 없으면 DEPENDENT_OA_FINAL_GATE는 UNVERIFIED이고 "
        "`gate_reasons`에 SPEC_NOT_PROVIDED를 적는다. `status`는 세 절 중 가장 낮은 판정, `handoff_ready`는 세 절이 모두 다음 단계로 진행 가능할 때만 true다. "
        "`checks[].name`에는 `success:`·`syntax:`·`oa:` 접두어를 붙이고, `per_claim_gates[]`에 목표항별 독자·기하 판정을, `exact_claim_text`에는 검수 대상 전문을 그대로 echo한다."
    )
    return Packet(txt, imgs)


# --------------------------------------------------------------------------- review only
def review_only(state: RunState, bundle: MaterialBundle, ids: Identifiers, record_id: str, role: str, scope: Scope, claim_text: str, request_text: str) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    key = {"syntax-scope-reviewer": "review_scope", "oa-strategy-reviewer": "review_scope", "claim-success-reviewer": "success_scope"}.get(role, "scope")
    txt = _user_lock(bundle) + mats
    txt += _hdr(state, ids, record_id, {key: scope.value, "design_revision": "N/A"})
    txt += _section("검토 요청", request_text)
    txt += _section("제공된 청구항 전문", claim_text or "미제공 — 일반 의견 요청이다. 개별 청구항을 만들거나 검증했다고 하지 않는다.")
    txt += _section("평문(줄바꿈·번호 제거)", flatten(claim_text))
    txt += _section("REVIEW_ONLY 규칙", "요청된 의견/검토 결과를 report_markdown에 답한다. 어떤 방법을 택할지 묻는 요청에는 먼저 기능을 가능하게 하는 관계, 선택지별 근거·불확실성, 현재 자료상 권장 방향과 이유를 설명한다. 선택에 필요한 분석을 하지 않은 채 사용자에게 선택을 되묻지 않는다. 첨부의 다른 미결정 메모나 명칭 문제는 현재 요청과 직접 관련이 있을 때만 다루며 검토를 중지시키지 않는다. 수정 문언을 새로 작성하지 않는다. 제공되지 않은 청구항·설계·원자료·비교 기준·법률 근거가 필요한 시험은 개별 UNVERIFIED로 두고, 이 결과는 DRAFT·FINAL LOCK의 PASS 근거가 아니다.")
    txt += _output_contract()
    return Packet(txt, imgs)
