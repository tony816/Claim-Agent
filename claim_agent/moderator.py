"""Conversation moderator (CLAUDE.md `대화 모더레이터`): between the user and the gated flow.

Front: the router runs on the moderator's model, and a CONSULT question is answered here from the user's materials,
the sealed records and the previous answer, without calling a claim role. Back: a pipeline or REVIEW_ONLY result gets a
`## 답변` written for the user's own message, above the report the code renders from the sealed records.

The moderator is not a gate role and not a source. It never seals wording: every `【청구항 N】` block it writes that
is not a known text (a sealed revision or the user's own claim) is labelled here, deterministically.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .models.request import IMAGE_EXT
from .provider.base import CallSpec, GenParams, ImagePart, LLMProvider

MODERATOR = "conversation-moderator"
UNSEALED_LABEL = "(미검수 후보 — 게이트를 거치지 않은 문언 · 사용 금지)"
CONSULT_NOTE = "※ 상담 답변 — 청구항 역할·검수를 호출하지 않았고 LOCK이 없습니다. 후보를 고르거나 반영을 지시하면 전체 게이트로 작성합니다."

_RULES = """권한 한계 (CLAUDE.md 대화 모더레이터):
- 기술 사실은 사용자 제공 원자료(첨부 도면·슬라이드·설명·사용자가 준 청구항)에서만 가져오고 위치를 적는다(예: 슬라이드 3의 ~).
  원자료가 답하지 않으면 `근거 미확인`이라고 쓰고 발명자에게 확인할 질문으로 남긴다. 원자료에 없는 구성·효과·수치·재료를 만들지 않는다.
- 앞선 답변·검수 보고서는 원자료가 아니다. 거기서 가져온 판단은 "앞선 검수 의견"처럼 출처를 밝힌다.
- PASS·LOCK·게이트 통과를 판정하지 않고, 어떤 문언도 `최종안`·`확정`이라고 부르지 않는다. 실패·미검증을 숨기지 않는다.
- 청구항 전문(【청구항 N】 블록)을 새로 쓰지 않는다. 표현 후보는 짧은 구절로만 보이고 `(미검수 후보)`를 붙인다.
- 신규성·진보성·등록 가능성을 단정하지 않는다.
- record_id·run_id·게이트 표를 나열하지 않는다. 상태는 평이한 말로 한 번만 적는다.
- 한국어로, 동료 변리사와 대화하듯 짧게 쓴다. 서론, 요청 분류 설명, 게이트 이름으로 시작하지 않는다."""

CONSULT_SYSTEM = f"""너는 Claim-Agent의 대화 모더레이터다. 사용자는 특허 청구항을 함께 다듬는 중이고, 이번 메시지는 문언 산출이 아니라
답·설명·선택지를 원하는 질문이다. 청구항 역할은 호출되지 않았다. 네 답이 이번 턴의 답 전부다.

답 순서:
1. 첫 문단에서 질문에 바로 답한다(있다/없다, 왜, 어느 쪽이 낫다).
2. 근거를 원자료 위치와 함께 적는다.
3. 선택지가 있으면 2~4개로 좁히고, 각각 권리범위 영향(넓어짐·좁아짐·회피 위험·근거 강도)을 한 줄씩 적는다. 추천이 있으면 하나를 고른다.
4. 마지막 줄에 사용자가 정할 다음 한 가지를 질문 하나로 묻는다(예: "이 중 B로 6항을 다시 쓸까요?").
보통 5~20줄이다.

{_RULES}"""

DELIVER_SYSTEM = f"""너는 Claim-Agent의 대화 모더레이터다. 검수 플로우가 방금 끝났다. '실행 결과'를 사용자의 이번 메시지에 맞춘 첫머리 답으로
전달한다. 청구항 문언·최종안 칸·게이트 표·역할 보고서는 네 답 아래에 코드가 그대로 붙인다.

- 첫 문장에서 사용자가 물은 것 또는 요청한 것에 답한다: 무엇을 했고 결과가 어떤지. 요청과 실제 수행이 다르면 그렇다고 말한다.
- LOCK 여부와 중지 여부는 '실행 상태'에 적힌 대로만 말한다. 중지됐으면 왜 멈췄는지 한두 줄로 풀고, 사용자가 정할 것 하나를 질문으로 묻는다.
- 멈춘 이유는 '중지 지적'에 적힌 내용으로만 설명한다. 지적에 없는 이유를 추측하지 않고, 지적이 없으면 없다고 말한다. 지적이 사용자가 준 부모항 문언에서 온 것이면 그렇게 말한다.
- 검토 의견(REVIEW_ONLY)이면 결론, 핵심 이유 2~3개, 고칠 방향 순서로 리뷰어 지적을 사용자 질문의 말로 옮긴다. 리뷰어가 하지 않은 지적을 더하지 않는다.
- 청구항 문언을 다시 쓰거나 인용하지 않는다(아래에 그대로 붙는다). 새 문언 후보도 만들지 않는다.
- 5~10줄.

{_RULES}"""


def enabled(cfg) -> bool:
    return cfg.conversation.moderator == "on"


def router_model(cfg, fallback: str) -> tuple[str, str]:
    """(role, model) for the routing call: the moderator reads intent when it is on."""
    return (MODERATOR, cfg.model_for(MODERATOR)) if enabled(cfg) else ("request-router", fallback)


# ---- deterministic guard --------------------------------------------------------------------------------------------

_HEAD = re.compile(r"[【\[]\s*청구항\s*(\d+)\s*[】\]]")
_BLOCK_END = re.compile(r"\n\s*\n\s*\n|\n(?=#)|\n(?=※)")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def known_claims(*texts: str | None) -> set[str]:
    """Normalized claim bodies the answer may quote verbatim: sealed revisions and the user's own claims."""
    out: set[str] = set()
    for text in texts:
        if not text:
            continue
        heads = list(_HEAD.finditer(text))
        for i, m in enumerate(heads):
            body = text[m.end():(heads[i + 1].start() if i + 1 < len(heads) else len(text))]
            if _norm(body):
                out.add(_norm(body))
    return out


def guard(answer: str, known: set[str]) -> str:
    """Label every claim block that is not a known text, and keep `최종안` out of the moderator's own headings."""
    lines = [re.sub(r"최종\s*안", "후보(미검수)", line) if line.lstrip().startswith("#") else line for line in answer.split("\n")]
    text = "\n".join(lines)
    heads = list(_HEAD.finditer(text))
    out, last = [], 0
    for i, m in enumerate(heads):
        stop = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        end = _BLOCK_END.search(text, m.end(), stop)
        body = _norm(text[m.end():end.start() if end else stop])
        sealed = bool(body) and any(body == k or body.startswith(k) for k in known)
        out.append(text[last:m.end()])
        if not sealed and not text[m.end():m.end() + len(UNSEALED_LABEL) + 2].strip().startswith(UNSEALED_LABEL):
            out.append(" " + UNSEALED_LABEL)
        last = m.end()
    out.append(text[last:])
    return "".join(out)


# ---- state summaries --------------------------------------------------------------------------------------------------

def seal_summary(state) -> str:
    """What the conversation's run holds now, in the words the answer may use: text, lock and halt."""
    from .store.report import edited_set_text

    if state is None:
        return "이 대화에 이전 작업 없음."
    c, dep = state.candidate, state.dependent
    out = [f"작업 결과: {state.outcome} · 요청 유형 {state.request_mode.value}"]
    root = "FINAL_CLAIM_LOCK" if c.final_claim_lock else "DRAFT_CLAIM_LOCK(잠정안)" if c.draft_claim_lock else "LOCK 없음"
    if state.baseline_set:
        lock = "DRAFT_DEPENDENT_SET_LOCK(잠정안)" if dep and dep.draft_set_lock else "LOCK 없음"
        nos = ", ".join(f"제{n}항" for n in state.baseline_set.edit_targets)
        out.append(f"사용자가 준 청구항 세트에서 {nos}만 편집 — 편집 항 {lock}, 나머지 항은 사용자 원문(미검증)")
        text = edited_set_text(state)
        if text:
            out += ["", text]
    else:
        if c.current and c.current.exact_text:
            out += [f"독립항 ({root}):", c.current.exact_text]
        if dep and dep.current and dep.current.exact_text:
            dl = "FINAL_DEPENDENT_SET_LOCK" if dep.final_set_lock else "DRAFT_DEPENDENT_SET_LOCK(잠정안)" if dep.draft_set_lock else "LOCK 없음"
            out += [f"종속항 세트 ({dl}):", dep.current.exact_text]
    if state.halt:
        h = state.halt
        out.append(f"중지: {h.stage} {h.kind} — {h.message}".strip())
        # The reviewer's own findings, so the answer explains the stop from them instead of guessing (run replay2-e69bc8-165948).
        issues = h.open_issues or []
        if issues:
            out.append("중지 지적 (검수 역할이 남긴 그대로):")
            out += [f"- [{o.get('code') or o.get('kind')}] {_clip(o.get('text', ''), ISSUE_CHARS)}" for o in issues[:MAX_ISSUES]]
            if len(issues) > MAX_ISSUES:
                out.append(f"- 외 {len(issues) - MAX_ISSUES}건")
    return "\n".join(out)


MAX_ISSUES = 6
ISSUE_CHARS = 600


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def sealed_texts(state) -> list[str]:
    """Texts that hold a LOCK or are the user's own set; only these may be quoted without the unsealed label."""
    if state is None:
        return []
    c, dep = state.candidate, state.dependent
    out = []
    if c.current and c.current.exact_text and (c.draft_claim_lock or c.final_claim_lock):
        out.append(c.current.exact_text)
    if dep and dep.current and dep.current.exact_text and (dep.draft_set_lock or dep.final_set_lock):
        out.append(dep.current.exact_text)
    if state.baseline_set:
        out += [cl.text for cl in state.baseline_set.claims if cl.claim_no not in state.baseline_set.edit_targets]
    return out


# ---- calls ------------------------------------------------------------------------------------------------------------

def _images(paths: list[str]) -> list[ImagePart]:
    out = []
    for p in paths:
        path = Path(p)
        mime = IMAGE_EXT.get(path.suffix.lower())
        if mime and path.exists():
            data = path.read_bytes()
            out.append(ImagePart(mime, data, path.name, hashlib.sha256(data).hexdigest()))
    return out


def _spec(cfg, system: str, packet: str, images: list[ImagePart], history: list[dict], stage: str, thinking: str | None = None) -> CallSpec:
    rc = cfg.role(MODERATOR)
    return CallSpec(role=MODERATOR, scope="CHAT", model=cfg.model_for(MODERATOR), system_instruction=system, packet_text=packet,
                    images=images, history=history, use_cache=False, phase="chat", stage=stage,
                    gen=GenParams(temperature=rc.temperature, thinking_level=thinking or rc.thinking_level,
                                  max_output_tokens=rc.max_output_tokens or 8192))


def consult(provider: LLMProvider, cfg, request: dict, blocks: list[dict], paths: dict, previous) -> str:
    """Front: answer a CONSULT question directly. Raises on failure (the turn has no other answer to give)."""
    from .conversation_pipeline import previous_answer

    user = [b for b in blocks if b["origin"] == "user" and not b.get("is_request")]
    material = "\n\n".join(f"### 원자료 {b['id']} ({b['category']})\n{b['text']}" for b in user) or "없음"
    packet = "\n\n".join([
        "## 이번 질문\n" + request["text"],
        "## 사용자 제공 원자료 (기술 사실의 유일한 근거 · 첨부 도면은 이미지로 함께 전달)\n" + material,
        "## 이 대화의 작업 상태 (봉인 기록)\n" + seal_summary(previous),
        "## 앞선 답변 (참고 — 원자료 아님)\n" + (previous_answer(request) or "없음"),
    ])
    result = provider.generate(_spec(cfg, CONSULT_SYSTEM, packet, _images(paths.get("drawings") or []), request.get("history", []), "CONSULT"))
    if not result.text.strip():
        raise RuntimeError("모더레이터가 답을 반환하지 않았습니다.")
    known = known_claims(*sealed_texts(previous), *(b["text"] for b in user))
    return guard(result.text.strip(), known)


def deliver(provider: LLMProvider, cfg, question: str, state, report: str, route_reason: str = "") -> str:
    """Back: the `## 답변` over a pipeline result, then the report verbatim. A failed call leaves the report as it is."""
    if not enabled(cfg):
        return report
    packet = "\n\n".join([
        "## 사용자의 이번 메시지\n" + question,
        "## 요청 분류 근거\n" + (route_reason or "없음"),
        "## 실행 상태\n" + seal_summary(state),
        "## 실행 결과 (사용자에게 이 아래로 그대로 붙는 보고서)\n" + report,
    ])
    try:
        result = provider.generate(_spec(cfg, DELIVER_SYSTEM, packet, [], [], "DELIVER", "MEDIUM"))
        intro = result.text.strip()
    except Exception as exc:  # noqa: BLE001 - delivery must never lose the result it delivers
        return report + f"\n\n※ 모더레이터 답변 생성 실패 — 결과를 그대로 보입니다 ({type(exc).__name__})."
    if not intro:
        return report
    return "## 답변\n\n" + guard(intro, known_claims(*sealed_texts(state))) + "\n\n---\n\n" + report
