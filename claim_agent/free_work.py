"""Free work mode (CLAUDE.md 자유 작업 모드): an ungated general agent for first drafts, and gate roles on demand.

A conversation the user switches into this mode sends every message straight to one general agent: no router, no claim
pipeline. Its answers carry the code's `미검수 초안` label, since no gate ran and nothing is locked. Under an answer that
holds claim wording the user can click one gate role; the click runs the existing REVIEW_ONLY path on exactly that
wording (r1, design_revision N/A), so the report is an opinion on the draft and never PASS evidence for a LOCK. The
review lands in the conversation history, so the next free turn can apply it when the user asks.
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
    "syntax-scope-reviewer": "통사·범위 검수",
    "oa-strategy-reviewer": "OA 검수",
    "claim-success-reviewer": "성공조건 감사",
    "blind-claim-reconstruction-reviewer": "블라인드 복원",
}
UNREVIEWED_LABEL = ("※ 미검수 초안 — 자유 작업 모드의 일반 에이전트 답변입니다. 생략 단계: 전체 게이트(설계·의미 초안·문체·성공조건·통사·OA·역구성·LOCK). "
                    "최종안·확정안이 아닙니다.")
REVIEW_NOTE = ("※ 선택 검수 — 사용자가 고른 역할만 이 문언에 실행했습니다(REVIEW_ONLY, r1, design_revision N/A). 다른 게이트는 실행하지 않았고 "
               "LOCK이 없으며, 이 결과는 LOCK의 PASS 근거가 아닙니다.")
BLIND_NOTE = ("※ 블라인드 복원은 역피처 검수의 1단계만입니다. 자유 작업에는 DESIGN_GATE가 없어 기준 비교(picture)는 실행하지 않았습니다. "
              "독립항 문언만 읽은 복원 결과를 의도한 구조와 직접 대조하세요.")
BLIND_NOTE_TEXT = "※ 추가 요청은 블라인드 복원에 전달하지 않았습니다(블라인드는 청구항 문언만 받는 새 context)."
PROJECT_FILES_HEAD ="## 프로젝트 소스 파일 (프로젝트에 미리 등록되어 매 요청에 자동 첨부 — 사용자 제공 원자료)"
CATEGORY_LABELS = {"invention": "발명 자료", "drawing": "도면", "prior_art": "선행기술", "spec": "정식 명세서"}

FREE_SYSTEM = """너는 Claim-Agent의 자유 작업 에이전트다. 사용자는 한국 특허 청구항을 함께 1차 작업하는 변리사·발명자다.
이 모드에는 설계·문체·성공조건·통사·OA·역구성 게이트가 없다. 사용자가 요청하면 청구항 초안·수정안·대안·의견을 바로 쓴다.
- 기술 사실·구성·수치·재료·효과는 사용자가 준 자료(대화, 첨부, 프로젝트 지침·파일)에서만 가져온다. 자료에 없으면 지어내지 말고
  `근거 미확인`으로 표시한 뒤 발명자에게 확인할 질문으로 남긴다.
- 청구항은 한 항마다 `【청구항 N】` 머리줄로 시작하고, 종속항은 `제N항에 있어서,`로 인용한다. 머리줄을 굵게 하거나 목록 기호를 붙이지 않는다.
  설명은 청구항 블록 앞에 쓰고, 청구항 사이와 마지막 청구항 뒤에는 설명을 섞지 않는다. 사용자는 버튼으로 청구항 블록만 검수 역할에 넘긴다.
- 네 답은 검수를 거치지 않은 초안이다. 게이트 통과·PASS·LOCK을 주장하지 않고, 어떤 문언도 `최종안`·`확정`이라고 부르지 않는다.
  신규성·진보성·등록 가능성을 단정하지 않는다.
- 대화에 앞선 검수 결과(통사·OA·성공조건·블라인드 복원 보고서)가 있으면, 사용자가 반영을 요청할 때 그 지적을 짚어 가며 고친다.
  검수 보고서는 원자료가 아니며, 고친 문언도 다시 검수하기 전에는 미검수다.
- 한국어로, 동료 변리사와 작업하듯 간결하게 쓴다."""

REVIEW_REQUEST = ("자유 작업 모드에서 사용자가 작업 도구 패널에서 고른 검수({labels})다. '제공된 청구항 전문'은 게이트를 거치지 않은 초안이다. "
                  "이 역할의 기준으로 결함을 찾고, 결함마다 문제 구절·이유·고칠 방향을 적는다. 수정 문언 전문은 쓰지 않는다. "
                  "사용자 추가 요청이 있으면 이 역할의 기준 안에서 그 요청에 먼저 답한다.")

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
    gen = GenParams(temperature=0.5, thinking_level="MEDIUM", max_output_tokens=cfg.model.max_output_tokens)
    result = generate_turn(provider, request["model"], request.get("history", []), message, None, request.get("instructions"),
                           sent=sent, system=FREE_SYSTEM, role=FREE_ROLE, gen=gen)
    # The label and footnote are for the reader; the history the agent sees next turn keeps its bare answer.
    answer = UNREVIEWED_LABEL + "\n\n" + result["answer"] + "\n\n" + usage_note(provider.usage, time.time() - started)
    return {**result, "answer": answer, "effective_mode": "FREE", "status": "complete"}


def review_turn(cfg, config_path, request: dict, folder: Path) -> dict:
    """Run the clicked gate roles on one message's wording through the REVIEW_ONLY path (no router, no moderator)."""
    from .chat import _close
    from .conversation_pipeline import intake, run_pipeline
    from .routing import RouteDecision
    from .runtime import build_runtime, make_provider

    started = time.time()
    review = request["review"]
    roles = list(dict.fromkeys(review.get("roles") or []))
    if not roles or any(role not in REVIEW_ROLES for role in roles):
        raise ValueError("지원하지 않는 검수 역할입니다.")
    target = str(review.get("claim_text") or "").strip()
    if not target:
        raise ValueError("검수할 청구항 문언이 없습니다.")
    blocks, paths = intake(request, folder)
    # The click's request text is the program's, not user material; the reviewed wording is the review target only.
    asked = {b["path"] for b in blocks if b.get("is_request")}
    paths["invention_sources"] = [p for p in paths["invention_sources"] if p not in asked]
    path = folder / "intake" / "review-target.txt"
    path.write_text(target, encoding="utf-8", newline="")
    blocks.append(dict(id="review-target", text=target, origin="assistant_reference", category="claim_file", path=str(path), is_request=False))
    labels = ", ".join(REVIEW_ROLES[role] for role in roles)
    route = RouteDecision(mode="REVIEW_ONLY", reason="자유 작업 모드 — 사용자가 버튼으로 고른 검수: " + labels,
                          reviewers=roles, review_scope=review_scope(target), claim_source_id="review-target")
    rt = build_runtime(cfg.project_root, config_path, overrides={cfg.default_model_key: request["model"]})
    provider = make_provider(rt)
    try:
        result = run_pipeline(rt, provider, request, route, blocks, paths, folder, None, turn={"started_at": started})
    finally:
        _close(provider)
    note = str(review.get("note") or "").strip()
    blind = "blind-claim-reconstruction-reviewer" in roles
    notes = [REVIEW_NOTE] + ([BLIND_NOTE] if blind else []) + ([BLIND_NOTE_TEXT] if blind and note else [])
    answer = "\n".join(notes) + "\n\n" + result["answer"]
    rt.store.write_chat_report(result["run_id"], answer)
    asked_text = review_asked_text(roles, note, target)
    history = [*request.get("history", []), {"role": "user", "parts": [{"text": asked_text}]}, {"role": "model", "parts": [{"text": answer}]}]
    return {**result, "answer": answer, "history": history, "effective_mode": "REVIEW_ONLY"}


def review_request_text(roles: list[str], note: str = "") -> str:
    """The reviewers' request: the program's review instruction, then what the user typed beside the chosen tools."""
    text = REVIEW_REQUEST.format(labels=", ".join(REVIEW_ROLES[role] for role in roles))
    return text + ("\n\n사용자 추가 요청: " + note if note.strip() else "")


def review_asked_text(roles: list[str], note: str, target: str) -> str:
    """The user's turn as the conversation shows it and the general agent reads it next."""
    head = f"[선택 검수 · {', '.join(REVIEW_ROLES[role] for role in roles)}]"
    return head + (f" {note.strip()}" if note.strip() else " 아래 문언을 검수해 줘.") + "\n\n" + target
