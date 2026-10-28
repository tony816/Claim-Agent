"""Free work mode (CLAUDE.md 자유 작업 모드): an ungated general agent for first drafts, and gate roles on demand.

A conversation the user switches into this mode sends every message straight to one general agent: no router, no claim
pipeline. Its answers carry the code's `미검수 초안` label, since no gate ran and nothing is locked. From the tool panel
the user picks gate roles for one message's claim wording and may type a request beside them. The roles run on exactly
that wording through the existing REVIEW_ONLY path (r1, design_revision N/A), with the request as their focus.

Without a request the answer is the review itself. With one, the general agent then carries the request out on the
reviews: it fixes the wording when the request asks for a fix (the answer is the fixed claims and a line or two on what
changed), and otherwise answers it from the findings without rewriting a claim. The reports stay in the full report
and the history, never PASS evidence for a LOCK.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .provider.base import GenParams, LLMProvider
from .store.telemetry import UsageMeter

FREE = "free"
GATED = "gated"
FREE_ROLE = "자유 작업"
REVIEW_ROLES = {
    "claim-style-adjuster": "문체 조정",
    "syntax-scope-reviewer": "통사·범위 검수",
    "oa-strategy-reviewer": "OA 검수",
    "claim-success-reviewer": "성공조건 감사",
    "blind-claim-reconstruction-reviewer": "블라인드 복원",
}
UNREVIEWED_LABEL = ("※ 미검수 초안 — 자유 작업 모드의 일반 에이전트 답변입니다. 생략 단계: 전체 게이트(설계·의미 초안·문체·성공조건·통사·OA·역구성·LOCK). "
                    "최종안·확정안이 아닙니다.")
REVIEW_NOTE = ("※ 선택 검수 — 사용자가 고른 역할만 이 문언에 실행했습니다(REVIEW_ONLY, r1, design_revision N/A). 다른 게이트는 실행하지 않았고 "
               "LOCK이 없으며, 이 결과는 LOCK의 PASS 근거가 아닙니다.")
REVISED_LABEL = ("※ 미검수 초안 — 고른 검수({labels})를 이 문언에 실행한 뒤, 일반 에이전트가 그 결과와 사용자 요청을 함께 반영해 고친 문언입니다. "
                 "고친 문언은 다시 검수하기 전에는 미검수이고 LOCK이 없습니다. 검수 보고서 전문은 '전체 보고서 펼치기'에 있습니다.")
ANSWERED_LABEL = ("※ 선택 검수({labels}) 결과로 사용자 요청에 답했습니다. 청구항은 고치지 않았고 LOCK이 없으며, 이 답은 LOCK의 PASS 근거가 아닙니다. "
                  "검수 보고서 전문은 '전체 보고서 펼치기'에 있습니다.")
BLIND_NOTE = ("※ 블라인드 복원은 역피처 검수의 1단계만입니다. 자유 작업에는 DESIGN_GATE가 없어 기준 비교(picture)는 실행하지 않았고, "
              "종속항은 부모항 체인과 함께 항마다 따로 읽혔습니다.")
STYLE_NOTE = ("※ 문체 조정은 고른 문언을 고쳐 냅니다(스타일가이드·용어표현 출처게이트, 범위 불변). 스타일 조정 문언은 미검수 초안이고 LOCK이 없으며, "
              "함께 고른 다른 검수는 그 문언을 읽었습니다. 이 문언은 다음 검수 대상으로 고를 수 있습니다.")
STYLE_UNCHANGED_NOTE = "※ 문체 조정이 고칠 곳을 찾지 못했거나 문언을 내지 않아, 다른 검수는 고른 문언 그대로를 읽었습니다."
BLIND_NOTE_TEXT = "※ 입력한 요청은 블라인드 복원에 보내지 않았습니다(블라인드는 청구항 문언만 받는 새 context). 다른 검수와 요청을 처리하는 단계에만 반영했습니다."
HALTED_NOTE = "※ 검수가 끝까지 실행되지 않아, 끝난 검수 결과로만 요청을 처리했습니다. 중지 원인은 작업 로그와 전체 보고서에 있습니다."
PROJECT_FILES_HEAD ="## 프로젝트 소스 파일 (프로젝트에 미리 등록되어 매 요청에 자동 첨부 — 사용자 제공 원자료)"
CATEGORY_LABELS = {"invention": "발명 자료", "drawing": "도면", "prior_art": "선행기술", "spec": "정식 명세서"}

# The free agent is the one place a request meets the model without gates, so it must draft at least as well as the bare
# model does: an earlier, stricter prompt (any inference = 근거 미확인, every function clause stripped on a counterexample)
# gave 4–6 weaker claims where the bare model gave 9 on the same slides (docs/도구사용_개선로그.md #13).
FREE_SYSTEM = """너는 Claim-Agent의 자유 작업 에이전트다. 사용자는 한국 특허 청구항을 함께 1차 작업하는 변리사·발명자다.
이 모드에는 설계·문체·성공조건·통사·OA·역구성 게이트가 없다. 경험 많은 변리사가 도면과 메모를 보고 바로 쓰듯, 요청받은 청구항 초안·수정안·대안·의견을 바로 쓴다.

자료와 근거
- 기술 사실은 사용자가 준 자료(대화, 첨부 도면·슬라이드, 프로젝트 지침·USER_LOCK·파일)에서 가져온다. 도면의 라벨·분해도·화살표·메모가 보여 주는
  구성·배치·적층 순서·연결, 그리고 통상의 기술자가 거기서 명확히 읽어 내는 사항은 근거가 있는 것으로 쓴다. 예: 분해도에서 겹쳐 그려진
  부품들은 그 순서로 적층되고, 과열 시 전원을 끊는 안전 부품이 히터와 같은 모듈에 있으면 히터 급전 경로에 놓인다. 도면에서 읽은 해석은
  청구항 앞 설명에 한 줄로 밝힌다.
- `근거 미확인`은 자료에 없고 명확히 도출되지도 않는 사실(수치, 자료에 없는 재료·목적·결선 방식 등)에만 붙인다. 그런 사실은 지어내지 않고,
  그 사실 없이 쓸 수 있는 가장 넓은 문언으로 쓴 뒤 발명자 확인 질문으로 남긴다. 확인할 것이 있다는 이유로 청구항을 빼거나 약하게 하지 않는다.

청구항 작성
- 종속항을 요청받으면 대상에 대해 자료가 보여 주는 청구 가능한 특징을 빠짐없이 찾는다: 구성 부품, 적층·배치·연결 관계, 재질, 부품 종류,
  개구·관통홀 같은 형상, 작동 상태. 기술적 의미가 큰 항을 앞에 두고, 재질·부품 종류처럼 보정 근거로 남겨 둘 항은 뒤에 둔다. 효과가 자료에
  적혀 있지 않다는 이유만으로 개시된 구조를 빼지 않는다.
- 기능·작용을 구성의 귀결로 적을 때(`…함으로써 …하는`, `…을 구비하여 …을 구속하는`, `…되면 …하는` 등)는 그 기능이 성립하는 데 필요한
  전제(접촉, 간격·치수 관계, 상대 위치, 전기적 연결 등)를 자료에서 찾아 함께 적는다. 예: `마주하는 한 쌍의 측면`만으로는 이동 구속이
  따라 나오지 않으므로 측면 사이 간격과 대상물 폭의 관계를 함께 적는다. 공지 부품을 통상 기능으로 한정할 때(써멀 퓨즈의 과열 차단,
  단열재의 열 차단)는 그 기능이 작동하는 배치(급전 경로 상, 가열 부재와 커버 사이 등)를 자료가 보여 주거나 명확히 도출되는 대로 함께 적는다.
  전제가 자료에 없으면 그 기능 구절 없이 구조로 쓰고 확인 질문을 남긴다.
- 사용자가 확정한 청구항·용어·문형은 그대로 쓰고, 새 항은 그 용어를 `상기 …`로 이어 받는다. 부모항 체인이 정하지 않은 방향·기준을 새로
  끌어오지 않고, 업계 기초지식이 있는 독자가 한 번 읽고 그릴 수 있게 쓴다.
- 대화의 앞선 답변(게이트 run 결과 포함)은 참고 자료다. 그 항 수나 구성에 묶이지 말고 자료 전체에서 다시 후보를 찾는다.
- 앞선 검수 결과(문체·통사·OA·성공조건·블라인드 복원 보고서)가 있고 사용자가 반영을 요청하면 그 지적을 짚어 가며 고친다. 검수 보고서는
  원자료가 아니다. 지적이 자료와 어긋나면(예: 도면을 받지 않은 검수 역할이 도면에 있는 출처를 못 찾은 경우) 그렇다고 밝히고 따르지 않는다.
  기능 구절의 전제가 빠졌다는 지적은 기능을 지우기보다 자료가 주는 전제를 더해 닫는다.

형식
- 청구항은 한 항마다 `【청구항 N】` 머리줄로 시작하고, 종속항은 `제N항에 있어서,`로 인용한다. 머리줄을 굵게 하거나 목록 기호를 붙이지 않는다.
  설명은 청구항 블록 앞에 쓰고, 청구항 사이와 마지막 청구항 뒤에는 설명을 섞지 않는다. 사용자는 작업 도구로 청구항 블록만 검수 역할에 넘긴다.
- 네 답은 검수를 거치지 않은 초안이다. 게이트 통과·PASS·LOCK을 주장하지 않고, 어떤 문언도 `최종안`·`확정`이라고 부르지 않는다.
  고친 문언도 다시 검수하기 전에는 미검수다. 신규성·진보성·등록 가능성을 단정하지 않는다.
- 한국어로, 동료 변리사와 작업하듯 간결하게 쓴다. 설명은 항별 근거·해석 한 줄과 문언을 바꿀 확인 질문으로 줄인다."""
# Drafting is the hardest call in the mode and the bare model it is measured against thinks at full effort.
FREE_THINKING = "HIGH"

REVIEW_REQUEST = ("자유 작업 모드에서 사용자가 작업 도구 패널에서 고른 검수({labels})다. '제공된 청구항 전문'은 게이트를 거치지 않은 초안이다. "
                  "이 역할의 기준으로 결함을 찾고, 결함마다 문제 구절·이유·고칠 방향을 적는다. 수정 문언 전문은 쓰지 않는다"
                  "(문체 조정만 예외로, 범위 불변으로 고친 문언 전문을 낸다). "
                  "사용자 요청이 있으면 그 요청이 가리키는 관점·대상·범위로 이 역할의 검수를 하고, 요청한 지점을 먼저 판단한다.")
# The general agent's turn after the reviews (only when the user typed a request): the reports are in the history right
# above this message. The request decides whether the wording is rewritten or the findings answer it.
FOLLOW_UP_REQUEST = """방금 실행한 검수({labels})의 결과를 바탕으로 아래 사용자 요청을 처리하라. 검수 보고서는 바로 위 대화에 있다.
먼저 요청이 무엇을 원하는지 정한다. 문언을 고치라는 요청(고쳐 줘·반영해 줘·바꿔 줘·다시 써 줘·다듬어 줘 등)이면 '고칠 때'를,
그 밖의 요청(특정 관점의 판단·정리·설명·우선순위·선택지·위험 평가)이면 '답할 때'를 따른다.

고칠 때:
- 요청이 무엇을 고치라는 것인지 먼저 정하고, 검수 지적 가운데 같은 곳이거나 자료로 고칠 수 있는 것을 함께 반영한다. 요청과 검수 지적이 어긋나면 요청을 따른다.
- 블라인드 복원 결과가 있으면, 복원이 자료의 구조와 다르게 읽거나 정하지 못한 곳을 문언으로 닫는다.
- 고친 문언의 기능·결과 구절마다 적힌 구성만으로 그 기능이 따라 나오는지(반례가 없는지) 다시 본다. 고치다 전제를 빼거나 약하게 하지 않는다.
  전제가 빠졌다는 지적은 자료(도면에서 명확히 읽히는 배치·연결 포함)가 주는 전제를 더해 닫고, 전제가 자료에 없을 때만 그 기능 구절을 구조로 바꾼다.
- 기술 사실은 자료(대화, 첨부, 프로젝트 지침·USER_LOCK·파일)와 거기서 명확히 읽히는 사항에서 가져온다. 자료로 고칠 수 없거나 아래 청구항 밖(부모항 등)을 고쳐야 하는 지적은 그 부분을 그대로 둔다.
  도면을 받지 않은 검수 역할이 도면에 있는 출처를 못 찾았다는 지적은 따르지 않고, 못 고친 곳에 그 이유를 적는다.
- 아래 청구항의 항 번호와 인용관계는 그대로 두고, 없는 항을 새로 쓰지 않는다.
- 답 형식: 첫머리에 `- 바꾼 곳: …` 한두 줄(고치지 못한 지적이 있으면 `- 못 고친 곳: … (이유)` 한 줄), 그다음 고친 청구항 전문만. 검수 보고서를 옮겨 적거나 다른 설명을 붙이지 않는다.

답할 때:
- 청구항을 다시 쓰지 않는다. `【청구항 N】` 블록을 쓰지 않고, 표현 후보가 필요하면 짧은 구절로만 보인다.
- 검수 결과 가운데 요청에 해당하는 지적을 문제 구절·이유·고칠 방향과 함께, 요청이 원하는 형태(정리·표·우선순위 등)로 답한다.
- 검수자가 하지 않은 판단을 더하지 않는다. 검수 결과가 요청에 답하지 못하면 그렇다고 쓴다.

사용자 요청: {note}

대상 청구항:
{target}"""

# A claim block starts at a header line (bold or bracketed spellings included) and the last one ends at the first
# markdown heading, note, rule or fence after it — the agent is told to keep its explanation above the claims.
_HEAD = re.compile(r"^[ \t]*(?:\*\*)?(?:【\s*청구항\s*\d+\s*】|\[\s*청구항\s*\d+\s*\])", re.MULTILINE)
_BOLD_HEAD = re.compile(r"\*\*((?:【\s*청구항\s*\d+\s*】|\[\s*청구항\s*\d+\s*\]))\*\*")
_END = re.compile(r"\n[ \t]*(?:#|※|---|\*\*\*|```)|\n[ \t]*\n[ \t]*\n")


def review_target(text: str) -> str | None:
    """The claim wording a review button sends: from the first claim header to the end of the last claim, or None."""
    text = _BOLD_HEAD.sub(r"\1", text.replace("\r\n", "\n"))
    heads = list(_HEAD.finditer(text))
    if not heads:
        return None
    end = _END.search(text, heads[-1].end())
    return text[heads[0].start():end.start() if end else len(text)].strip() or None


def review_scope(target: str) -> str:
    """DEPENDENT_SET when the wording holds a dependent claim, else INDEPENDENT."""
    from .pipeline.claimtext import ClaimParseError, parse_claim_set

    try:
        return "DEPENDENT_SET" if any(not c.is_independent for c in parse_claim_set(target)) else "INDEPENDENT"
    except ClaimParseError:
        return "INDEPENDENT"


def _preset_parts(attachments: list[dict]) -> list[dict]:
    from .chat import user_message

    parts: list[dict] = [{"text": PROJECT_FILES_HEAD}]
    for item in attachments:
        label = CATEGORY_LABELS.get(item.get("category", "invention"), item.get("category", ""))
        parts += user_message(f"[{label}] {item['name']}", "", [item["path"]])["parts"]
    return parts


def free_turn(provider: LLMProvider, cfg, request: dict) -> dict:
    """One turn of the general agent. The history keeps this turn's text and own attachments, not the project files."""
    from .chat import generate_turn, user_message
    from .store.report import usage_note

    started = time.time()
    provider = UsageMeter(provider, cfg.telemetry.pricing)
    message = user_message(request["text"], "", request.get("files", []))
    preset = request.get("project_attachments") or []
    sent = {"role": "user", "parts": [*_preset_parts(preset), *message["parts"]]} if preset else None
    gen = GenParams(temperature=0.5, thinking_level=FREE_THINKING, max_output_tokens=cfg.model.max_output_tokens)
    result = generate_turn(provider, request["model"], request.get("history", []), message, None, request.get("instructions"),
                           sent=sent, system=FREE_SYSTEM, role=FREE_ROLE, gen=gen, user_lock=request.get("user_lock"))
    # The label and footnote are for the reader; the history the agent sees next turn keeps its bare answer.
    answer = UNREVIEWED_LABEL + "\n\n" + result["answer"] + "\n\n" + usage_note(provider.usage, time.time() - started)
    return {**result, "answer": answer, "effective_mode": "FREE", "status": "complete"}


def review_turn(provider: LLMProvider, cfg, config_path, request: dict, folder: Path) -> dict:
    """Run the chosen gate roles on one message's wording (REVIEW_ONLY, no router, no moderator), focused by the user's
    request. Without a request the reviews are the answer; with one, the general agent carries it out on them.

    `provider` is the live provider the general agent answers on; the reviewers run on the pipeline's own.
    `revised` in the result says whether the answer is fixed wording (it can then be the next review target).
    """
    from .chat import _close, generate_turn, user_message
    from .conversation_pipeline import intake, run_pipeline
    from .routing import RouteDecision
    from .runtime import build_runtime, make_provider
    from .store.report import turn_usage, usage_note
    from .store.telemetry import add_usage

    started = time.time()
    review = request["review"]
    roles = list(dict.fromkeys(review.get("roles") or []))
    if not roles or any(role not in REVIEW_ROLES for role in roles):
        raise ValueError("지원하지 않는 검수 역할입니다.")
    target = str(review.get("claim_text") or "").strip()
    if not target:
        raise ValueError("검수할 청구항 문언이 없습니다.")
    history = request.get("history", [])
    # The blind reader needs each dependent claim's exact parent chain; the conversation's drafts are where it is.
    request = {**request, "review": {**review, "parent_claims": parent_claims_text(history)}}
    blocks, paths = intake(request, folder)
    # The click's request text is the program's, not user material; the reviewed wording is the review target only.
    asked = {b["path"] for b in blocks if b.get("is_request")}
    paths["invention_sources"] = [p for p in paths["invention_sources"] if p not in asked]
    path = folder / "intake" / "review-target.txt"
    path.write_text(target, encoding="utf-8", newline="")
    blocks.append(dict(id="review-target", text=target, origin="assistant_reference", category="claim_file", path=str(path), is_request=False))
    labels = ", ".join(REVIEW_ROLES[role] for role in roles)
    route = RouteDecision(mode="REVIEW_ONLY", reason="자유 작업 모드 — 사용자가 작업 도구에서 고른 검수: " + labels,
                          reviewers=roles, review_scope=review_scope(target), claim_source_id="review-target")
    rt = build_runtime(cfg.project_root, config_path, overrides={cfg.default_model_key: request["model"]})
    pipeline_provider = make_provider(rt)
    try:
        result = run_pipeline(rt, pipeline_provider, request, route, blocks, paths, folder, None, turn={"started_at": started})
    finally:
        _close(pipeline_provider)
    note = str(review.get("note") or "").strip()
    blind = "blind-claim-reconstruction-reviewer" in roles
    # Style hands back restyled wording; the other chosen roles read it, and it can be the next review target.
    styled = result.get("styled_text") if "claim-style-adjuster" in roles else None
    style_notes = [STYLE_NOTE if styled else STYLE_UNCHANGED_NOTE] if "claim-style-adjuster" in roles else []
    reports = "\n".join([REVIEW_NOTE] + style_notes + ([BLIND_NOTE] if blind else [])) + "\n\n" + result["answer"]
    # The history holds what happened, in order: the reviews asked and answered, then the request asked and answered.
    reviewed = [{"role": "user", "parts": [{"text": review_asked_text(roles, note, target)}]}, {"role": "model", "parts": [{"text": reports}]}]
    if not note:
        # Review only: the reports, as the reviewers wrote them, are the answer.
        rt.store.write_chat_report(result["run_id"], reports)
        return {**result, "answer": reports, "history": [*history, *reviewed], "effective_mode": "REVIEW_ONLY", "revised": bool(styled),
                "claim_text": styled}
    provider = UsageMeter(provider, cfg.telemetry.pricing)
    message = user_message(FOLLOW_UP_REQUEST.format(labels=labels, note=note, target=styled or target), "", [])
    preset = request.get("attachments") or []
    sent = {"role": "user", "parts": [*_preset_parts(preset), *message["parts"]]} if preset else None
    gen = GenParams(temperature=0.3, thinking_level=FREE_THINKING, max_output_tokens=cfg.model.max_output_tokens)
    done = generate_turn(provider, request["model"], [*history, *reviewed], message, None, request.get("instructions"),
                         sent=sent, system=FREE_SYSTEM, role=FREE_ROLE, gen=gen, user_lock=request.get("user_lock"))
    # The request decided the form: claim blocks mean fixed wording, anything else an answer from the findings.
    revised = review_target(done["answer"]) is not None
    usage = {k: v for k, v in turn_usage(rt.store.load_state(result["run_id"]))[0].items() if k not in ("started_at", "finished_at")}
    notes = [(REVISED_LABEL if revised else ANSWERED_LABEL).format(labels=labels)] + ([BLIND_NOTE] if blind else []) + ([BLIND_NOTE_TEXT] if blind else [])
    notes += [HALTED_NOTE] if result["status"] != "complete" else []
    body = done["answer"]
    if styled and not revised:
        # Answered from the findings without a rewrite: the restyled wording is still this turn's draft to carry on from.
        notes += style_notes
        body += "\n\n### 스타일 조정 문언 (미검수 초안)\n\n" + styled
    answer = "\n".join(notes) + "\n\n" + body + "\n\n" + usage_note(add_usage(usage, provider.usage), time.time() - started)
    rt.store.write_chat_report(result["run_id"], answer)
    return {**result, "answer": answer, "history": done["history"], "effective_mode": "REVIEW_ONLY", "finish_reason": done["finish_reason"],
            "revised": revised or bool(styled), "claim_text": None if revised else styled}


def parent_claims_text(history: list[dict]) -> str:
    """The claims the conversation holds, one per number from the newest message that has it, as one claim set."""
    from .pipeline.claimtext import numbered_claims

    found: dict[int, str] = {}
    for turn in reversed(history):
        for part in turn.get("parts", []):
            text = part.get("text") or ""
            if text.startswith(REVIEW_NOTE):
                continue                   # a review report quotes the wording it read; it is not a draft
            for no, claim in numbered_claims(review_target(text) or "").items():
                found.setdefault(no, claim.text)
    return "\n\n".join(found[no] for no in sorted(found))


def review_request_text(roles: list[str], note: str = "") -> str:
    """The reviewers' request: the program's review instruction, then the request the user typed beside the chosen tools."""
    text = REVIEW_REQUEST.format(labels=", ".join(REVIEW_ROLES[role] for role in roles))
    return text + ("\n\n사용자 요청: " + note if note.strip() else "")


def review_asked_text(roles: list[str], note: str, target: str) -> str:
    """The user's turn as the conversation shows it and the general agent reads it next."""
    head = f"[선택 검수 · {', '.join(REVIEW_ROLES[role] for role in roles)}]"
    return head + (f" {note.strip()}" if note.strip() else " 아래 문언을 검수해 줘.") + "\n\n" + target
