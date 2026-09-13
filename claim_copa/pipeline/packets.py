"""Input packets for every role call.

Each packet mirrors the role file's 입력 하드 게이트 list: a HANDOFF-style
header, then the required 전문 sections. Reports of upstream roles are passed
verbatim (report_markdown) exactly as the Claude Code orchestrator would.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import EXECUTION_PROFILE, PROTOCOL_VERSION
from ..models.enums import RequestMode, Scope, StyleChangeMode
from ..models.ids import Identifiers
from ..models.request import MaterialBundle, MaterialItem
from ..models.state import RunState
from ..provider.base import ImagePart
from .claimtext import flatten


@dataclass
class Packet:
    text: str
    images: list[ImagePart] = field(default_factory=list)


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
            images.append(ImagePart(it.mime_type or "image/png", it.data or b"", label))
    return "".join(parts), images


def _report(state: RunState, store, record_id: str | None, title: str) -> str:
    if not record_id:
        return _section(title, "없음")
    return _section(title + f" ({record_id})", store.read_record_report(state.run_id, record_id))


def _output_contract(extra: str = "") -> str:
    return (
        "### 출력 계약\n\n"
        "제공된 JSON 스키마를 따르는 JSON 객체 하나만 반환한다. `report_markdown`에는 역할 파일의 출력 형식 전문을 담고, "
        "`status`·`gates`·`next_step`·`handoff_ready`·`exact_claim_text`는 보고서와 글자 단위로 일치시킨다. "
        + extra
        + "\n"
    )


# --------------------------------------------------------------------------- independent
def architect(state: RunState, bundle: MaterialBundle, ids: Identifiers, record_id: str, request_text: str, redesign_goal: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    txt = _hdr(state, ids, record_id)
    txt += _section("현재 요청", request_text)
    if redesign_goal:
        txt += _section("설계 변경 목표 (새 design_revision)", redesign_goal)
    txt += _user_lock(bundle) + mats
    txt += _output_contract("`gates.DESIGN_GATE`에 LOCKED/UNLOCKED, `invention_type`에 발명 유형을 적는다.")
    return Packet(txt, imgs)


def drafter_independent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, design_record_id: str, prior_text: str | None, change_goal: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec"))
    txt = _hdr(state, ids, record_id, {"draft_scope": "INDEPENDENT", "meaning_draft_id": record_id, "목표 revision": ids.revision})
    txt += _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE: LOCKED 전문 (claim-architect 보고서)")
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
    txt = _hdr(state, ids, record_id, {"style_scope": "INDEPENDENT", "style_change_mode": mode.value, "style_record_id": record_id, "목표 revision": ids.revision})
    txt += _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE: LOCKED 전문")
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
    txt = _hdr(state, ids, record_id, {"success_scope": "INDEPENDENT", "success_record_id (예정)": record_id})
    txt += _section("검수 대상 exact 청구항 전문", exact_text)
    txt += _section("평문(줄바꿈·번호 제거)", flatten(exact_text))
    txt += _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE: LOCKED 전문")
    txt += _report(state, store, meaning_draft_id, "PRE_STYLE 의미 초안 보고서 전문")
    txt += _report(state, store, style_record_id, "style record 전문 (변경 대조표·용어·표현 출처표·두 후처리 게이트·독자·기하 게이트)")
    if corpus_fragments:
        txt += _section("style record가 사용한 코퍼스 정확 조각과 라우팅 인덱스", corpus_fragments)
    txt += _output_contract("`handoff_ready`는 `syntax 진행 가능` 값이다. `exact_claim_text`에는 검수 대상 전문을 그대로 echo한다.")
    return Packet(txt, imgs)


def syntax_independent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, design_record_id: str, style_record_id: str, success_record_id: str, exact_text: str, baseline_text: str | None, mode: StyleChangeMode) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec"))
    txt = _hdr(state, ids, record_id, {"review_scope": "INDEPENDENT"})
    txt += _section("검수 대상 청구항 전문", exact_text)
    txt += _section("평문(줄바꿈·번호 제거)", flatten(exact_text))
    txt += _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE 전문")
    txt += _report(state, store, style_record_id, "style record 전문 (CLAIM_STYLE_GATE: PASS)")
    txt += _report(state, store, success_record_id, "claim-success-reviewer success record 전문 (상태: PASS)")
    txt += _section(f"범위 불변 비교 기준 전문 ({mode.value})", baseline_text or "style record의 PRE_STYLE 의미 초안 참조")
    txt += _output_contract("`status`는 종합 판정, `handoff_ready`는 `OA 진행 가능` 값이다.")
    return Packet(txt, imgs)


def oa_independent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, design_record_id: str, style_record_id: str, success_record_id: str, syntax_record_id: str, exact_text: str) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    txt = _hdr(state, ids, record_id, {"review_scope": "INDEPENDENT"})
    txt += _section("검수 대상 청구항 전문", exact_text)
    txt += _user_lock(bundle) + mats
    txt += _report(state, store, design_record_id, "DESIGN_GATE 전문")
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
    txt = _hdr(state, ids, record_id, {"mode": "REFERENCE_COMPARE", "claim_scope": "INDEPENDENT", "blind_snapshot_id": blind_record_id})
    txt += _section("변경 없는 청구항 전문", exact_text)
    txt += _report(state, store, blind_record_id, "봉인된 blind snapshot 전문 (independence: FRESH_CALL_NO_PROJECT_CONTEXT)")
    txt += _report(state, store, design_record_id, "DESIGN_GATE 전문")
    txt += _report(state, store, style_record_id, "style record 전문 (CLAIM_STYLE_GATE·TERM_EXPRESSION_GATE)")
    txt += _report(state, store, oa_record_id, "OA 보고서 전문")
    txt += mats
    txt += _output_contract("`status`는 최종 판정(PASS/PASS-RANGE/REVIEW/BLOCK/UNVERIFIED)이다.")
    return Packet(txt, imgs)


# --------------------------------------------------------------------------- dependent
def dep_architect(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_design_record_id: str, root_text: str, request_text: str, redesign_goal: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    txt = _hdr(state, ids, record_id)
    txt += _section("현재 요청 (종속항 세트)", request_text)
    if redesign_goal:
        txt += _section("종속항 설계 변경 목표", redesign_goal)
    txt += _section("변경 없는 루트 독립항 전문", root_text)
    txt += _report(state, store, root_lock_id, "루트 독립항 LOCK 전문")
    txt += _report(state, store, root_design_record_id, "루트 DESIGN_GATE 전문 (기술 개념표)")
    txt += _user_lock(bundle) + mats
    txt += _output_contract("`gates.DEPENDENT_DESIGN_GATE`, `gates.INVENTIVE_STEP`, `candidates[]`(DC-NN별 분류·세 게이트·예정 항·부모항)를 채운다.")
    return Packet(txt, imgs)


def drafter_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, prior_set_text: str | None, change_goal: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec"))
    txt = _hdr(state, ids, record_id, {"draft_scope": "DEPENDENT_SET", "dependent_meaning_draft_id": record_id})
    txt += _section("변경 없는 루트 독립항 전문", root_text)
    txt += _report(state, store, root_lock_id, "루트 독립항 LOCK 전문")
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE: LOCKED 전문")
    txt += _user_lock(bundle) + mats
    if prior_set_text:
        txt += _section("직전 확정 dependent_revision 세트 전문", prior_set_text)
        txt += _section("이번 의미 변경 목표", change_goal or "")
    txt += _output_contract("`claims[]`에 각 종속항의 claim_no·parent_claim_no·dc_id·text(【청구항 N】 헤더 포함 전문)를, `exact_claim_text`에 세트 전문을 넣는다.")
    return Packet(txt, imgs)


def style_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, dep_meaning_draft_id: str | None, mode: StyleChangeMode, prior_set_text: str | None, prior_style_record_id: str | None, feedback: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec"))
    txt = _hdr(state, ids, record_id, {"style_scope": "DEPENDENT_SET", "style_change_mode": mode.value, "dependent_style_record_id": record_id})
    txt += _section("변경 없는 루트 독립항 전문", root_text)
    txt += _report(state, store, root_lock_id, "루트 독립항 LOCK 전문")
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE: LOCKED 전문")
    txt += _user_lock(bundle) + mats
    if mode == StyleChangeMode.STYLE_ONLY_REVISION:
        txt += _section("직전 확정 dependent_revision 세트 전문", prior_set_text)
        txt += _report(state, store, prior_style_record_id, "직전 dependent style record 전문")
        txt += _section("스타일만 바꾸어야 하는 정확한 피드백·규칙", feedback or "")
    else:
        txt += _report(state, store, dep_meaning_draft_id, "drafter 종속항 PRE_STYLE 의미 초안 세트 보고서 전문")
        if mode == StyleChangeMode.DRAFTER_REVISION and prior_set_text:
            txt += _section("직전 확정 dependent_revision 세트 전문", prior_set_text)
            txt += _section("이번 의미 수정 목표", feedback or "")
    txt += _output_contract("`claims[]`에 최종 종속항 세트(항별 전문)를, `exact_claim_text`에 세트 전문을, `per_claim_gates[]`에 목표항별 독자·기하 판정을 넣는다.")
    return Packet(txt, imgs)


def success_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, dep_meaning_draft_id: str | None, dep_style_record_id: str, set_text: str, corpus_fragments: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    txt = _hdr(state, ids, record_id, {"success_scope": "DEPENDENT_SET", "dependent_success_record_id (예정)": record_id})
    txt += _section("루트 독립항 전문", root_text)
    txt += _section("검수 대상 exact 종속항 세트 전문", set_text)
    txt += _report(state, store, root_lock_id, "루트 독립항 LOCK 전문")
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE: LOCKED 전문")
    txt += _report(state, store, dep_meaning_draft_id, "종속항 PRE_STYLE 의미 초안 보고서 전문")
    txt += _report(state, store, dep_style_record_id, "dependent style record 전문")
    if corpus_fragments:
        txt += _section("style record가 사용한 코퍼스 정확 조각과 라우팅 인덱스", corpus_fragments)
    txt += _user_lock(bundle) + mats
    txt += _output_contract("`handoff_ready`는 `syntax 진행 가능` 값이다. `per_claim_gates[]`에 목표항별 판정을 적는다.")
    return Packet(txt, imgs)


def syntax_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, dep_style_record_id: str, dep_success_record_id: str, set_text: str, baseline_text: str | None, mode: StyleChangeMode) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec"))
    txt = _hdr(state, ids, record_id, {"review_scope": "DEPENDENT_SET"})
    txt += _section("루트 독립항 전문", root_text)
    txt += _section("검수 대상 종속항 세트 전문", set_text)
    txt += _report(state, store, root_lock_id, "루트 독립항 LOCK 전문")
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE 전문")
    txt += _report(state, store, dep_style_record_id, "dependent style record 전문")
    txt += _report(state, store, dep_success_record_id, "dependent success record 전문")
    txt += _section(f"범위 불변 비교 기준 ({mode.value})", baseline_text or "dependent style record의 PRE_STYLE 세트 참조")
    txt += _user_lock(bundle) + mats
    txt += _output_contract("`status`는 종합 판정, `handoff_ready`는 `OA 진행 가능` 값이다.")
    return Packet(txt, imgs)


def oa_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_text: str, dep_design_record_id: str, dep_style_record_id: str, dep_success_record_id: str, dep_syntax_record_id: str, set_text: str) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    txt = _hdr(state, ids, record_id, {"review_scope": "DEPENDENT_SET"})
    txt += _section("루트 독립항 전문", root_text)
    txt += _section("검수 대상 종속항 세트 전문", set_text)
    txt += _report(state, store, root_lock_id, "루트 독립항 LOCK 전문")
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE 전문")
    txt += _report(state, store, dep_style_record_id, "dependent style record 전문")
    txt += _report(state, store, dep_success_record_id, "dependent success record 전문")
    txt += _report(state, store, dep_syntax_record_id, "dependent syntax PASS 보고서 전문")
    txt += _user_lock(bundle) + mats
    txt += _output_contract(
        "`gates.DEPENDENT_OA_DRAFT_GATE`와 `gates.DEPENDENT_OA_FINAL_GATE`를 분리해 채우고 정식 명세서가 없으면 FINAL은 UNVERIFIED + SPEC_NOT_PROVIDED다. `handoff_ready`는 `DRAFT 진행 가능(종속항별 역구성 YES)` 값이다."
    )
    return Packet(txt, imgs)


def picture_dependent(state: RunState, store, bundle: MaterialBundle, ids: Identifiers, record_id: str, root_lock_id: str, root_design_record_id: str, dep_design_record_id: str, dep_style_record_id: str, dep_oa_record_id: str, blind_record_id: str, parent_chain_text: str, target_claim_text: str, dc_id: str | None) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing"))
    txt = _hdr(state, ids, record_id, {"mode": "REFERENCE_COMPARE", "claim_scope": "DEPENDENT_SINGLE", "dependent_blind_snapshot_id": blind_record_id, "목표 DC": dc_id or "미지정(DEPENDENT_DESIGN_GATE에서 대응 확인)"})
    txt += _section("부모항 체인 전문 (blind 입력과 동일)", parent_chain_text)
    txt += _section("목표 종속항 전문 (blind 입력과 동일)", target_claim_text)
    txt += _report(state, store, blind_record_id, "봉인된 dependent blind snapshot 전문")
    txt += _report(state, store, root_lock_id, "루트 독립항 LOCK 전문")
    txt += _report(state, store, root_design_record_id, "루트 DESIGN_GATE 전문")
    txt += _report(state, store, dep_design_record_id, "DEPENDENT_DESIGN_GATE 전문 (목표 DC-NN 포함)")
    txt += _report(state, store, dep_style_record_id, "dependent style record 전문")
    txt += _report(state, store, dep_oa_record_id, "종속항 OA 보고서 전문")
    txt += mats
    txt += _output_contract("`status`는 목표항의 최종 판정이다.")
    return Packet(txt, imgs)


# --------------------------------------------------------------------------- review only
def review_only(state: RunState, bundle: MaterialBundle, ids: Identifiers, record_id: str, role: str, scope: Scope, claim_text: str, request_text: str) -> Packet:
    mats, imgs = _materials(bundle, ("invention", "drawing", "spec", "prior_art"))
    key = {"syntax-scope-reviewer": "review_scope", "oa-strategy-reviewer": "review_scope", "claim-success-reviewer": "success_scope"}.get(role, "scope")
    txt = _hdr(state, ids, record_id, {key: scope.value, "design_revision": "N/A"})
    txt += _section("검토 요청", request_text)
    txt += _section("제공된 청구항 전문", claim_text)
    txt += _section("평문(줄바꿈·번호 제거)", flatten(claim_text))
    txt += _user_lock(bundle) + mats
    txt += _section("REVIEW_ONLY 규칙", "제공되지 않은 설계·원자료·비교 기준이 필요한 시험은 개별 UNVERIFIED로 두고, 이 결과는 DRAFT·FINAL LOCK의 PASS 근거가 아니다.")
    txt += _output_contract()
    return Packet(txt, imgs)
