"""Conversation entry point: route first, then chat or the gated claim pipeline."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .live_events import EventWriter
from .models.request import IMAGE_EXT
from .provider.base import CallSpec, GenParams, ImagePart, LLMProvider
from .runtime import live_provider
from .sources.extract import read_text_any
from .store.telemetry import UsageMeter
from .tui_support import validate_attachment

SYSTEM = "한국어로 명확하고 자연스럽게 대화하는 도우미입니다. 이 경로는 일반 대화와 프로그램 설정 설명 전용입니다. 청구항 출력물·수정안·특허적인 의견은 작성하지 말고 전문 파이프라인 분류가 필요하다고 알리세요. 첨부 자료는 참고 자료입니다. 실제로 수행하지 않은 청구항 검수나 게이트 통과, LOCK 발급을 주장하지 마세요."


def user_message(text: str, material: str, files: list[str]) -> dict:
    parts = [{"text": text}]
    if material.strip():
        parts.append({"text": "사용자 제공 자료:\n" + material})
    for name in files:
        p = validate_attachment(Path(name)).path
        if p.suffix.lower() in IMAGE_EXT:
            parts.append({"inline_data": {"mime_type": IMAGE_EXT[p.suffix.lower()], "data": base64.b64encode(p.read_bytes()).decode("ascii")}})
        else:
            parts.append({"text": f"첨부 파일: {p.name}\n" + read_text_any(p)})
    return {"role": "user", "parts": parts}


PROJECT_INSTRUCTIONS_HEADER = "## 프로젝트 지침 (사용자가 프로젝트 폴더에 미리 설정한 지시)"


def system_with_instructions(base: str, instructions: str | None) -> str:
    """Project instructions are standing user directives for the conversation, never invention facts or gate verdicts."""
    text = (instructions or "").strip()
    if not text:
        return base
    return (base + "\n\n" + PROJECT_INSTRUCTIONS_HEADER + "\n" + text
            + "\n\n위 프로젝트 지침은 답변 방식·범위·우선순위에 대한 사용자 지시이자, 사용자가 직접 적은 기술 설명·청구항 자료다. 지침에 적힌 기술 내용은 사용자 제공 자료로 인용할 수 있지만, 지침을 근거로 검수·게이트·LOCK을 통과한 것처럼 말하지 않는다.")


CHAT_GEN = GenParams(temperature=0.7, thinking_level="LOW", max_output_tokens=8192)


def _chat_spec(model: str, history: list[dict], message: dict, instructions: str | None = None,
               system: str = SYSTEM, role: str = "대화", gen: GenParams = CHAT_GEN) -> CallSpec:
    """The current turn as a provider-neutral CallSpec: text parts become the packet, images ride along."""
    texts, images = [], []
    for part in message["parts"]:
        if "text" in part:
            texts.append(part["text"])
        elif "inline_data" in part:
            data = base64.b64decode(part["inline_data"]["data"])
            images.append(ImagePart(part["inline_data"]["mime_type"], data, "첨부 이미지", hashlib.sha256(data).hexdigest()))
    return CallSpec(
        role=role, scope="CHAT", model=model, system_instruction=system_with_instructions(system, instructions), packet_text="\n\n".join(texts), sources_block="",
        images=images, json_schema=None, tools=None, gen=gen,
        use_cache=False, phase="chat", stage="CHAT", run_id="", history=history,
    )


def generate_turn(provider: LLMProvider, model: str, history: list[dict], message: dict, events: EventWriter, instructions: str | None = None,
                  sent: dict | None = None, **spec) -> dict:
    """One conversational turn through the configured provider (Gemini or Anthropic); events are emitted by the provider.

    `sent` is what the model receives this turn when it differs from the `message` kept in the history: the free work
    mode sends a project's preset files every turn without storing a copy of them in each turn of the history.
    """
    result = provider.generate(_chat_spec(model, history, sent or message, instructions, **spec))
    answer = result.text
    if not answer:
        raise RuntimeError("모델이 텍스트 응답을 반환하지 않았습니다.")
    return {"answer": answer, "history": [*history, message, {"role": "model", "parts": [{"text": answer}]}], "finish_reason": result.finish_reason}


# Token cap for pipeline runs started from a conversation (web) when claim-agent.yaml leaves pipeline.max_total_tokens
# unset: a request that went the wrong way stops near this total instead of spending ~1M tokens with no claim text.
# The halt is resumable; a resume runs through the CLI, which uses the configured (unset) cap.
CONVERSATION_MAX_TOTAL_TOKENS = 1_000_000


def routed_turn(provider: LLMProvider, cfg, config_path, request: dict, folder: Path, events: EventWriter) -> dict:
    """`provider` is the configured live provider (routing + plain chat); the pipeline builds its own from the runtime."""
    from . import moderator
    from .conversation_pipeline import asks_other_edit, intake, previous_state, rerouted_request, run_pipeline
    from .routing import RouteDecision, classify_request
    from .runtime import build_runtime, make_provider
    from .store.report import usage_note

    started = time.time()
    provider = UsageMeter(provider, cfg.telemetry.pricing)     # the routing call is part of this answer's cost
    previous = previous_state(cfg, request)
    blocks, paths = intake(request, folder, previous)
    # Front of the moderator: it reads the intent (CLAUDE.md 대화 모더레이터), on its own model when it is on.
    role, model = moderator.router_model(cfg, request["model"])
    route = classify_request(provider, cfg.project_root, model, request["text"], blocks, folder.name,
                             request.get("ui_hints"), request.get("instructions"), role, request.get("user_lock"))
    halted = bool((request.get("ui_hints") or {}).get("halted")) and previous is not None and previous.halt is not None
    if route.resumes_halt and not halted:
        route.resumes_halt = False                 # only a run that is actually waiting can take the message as its decision
    if route.resumes_halt and asks_other_edit(route, previous, request["text"]):
        route.resumes_halt = False                 # its sealed edit targets are not what this message asks for: a new run
    if halted and route.mode in {"AUTHORING_DRAFT", "FINALIZATION"}:
        # Whether the router took the message as the halt's decision or as a new run (run web-9d3c7750: it read CLAUDE.md
        # and said new run, the text stayed '재개'), a run that cannot reach the parents the request names gets the request.
        held = [c.claim_no for c in previous.baseline_set.claims] if previous.baseline_set else []
        asked = rerouted_request(request["text"], request.get("prior_requests") or [], previous.request, held)
        if asked:
            route.resumes_halt = False
            request = {**request, "text": asked, "continues_request": asked}
    if route.mode == "CONSULT" and not moderator.enabled(cfg):
        # Without the moderator no one answers a question directly; the reviewers answer it as before.
        route = RouteDecision(mode="REVIEW_ONLY", reason=route.reason, reviewers=["syntax-scope-reviewer", "oa-strategy-reviewer"],
                              claim_source_id=route.claim_source_id)
    (folder / "route.json").write_text(route.model_dump_json(indent=2), encoding="utf-8")
    planned_id = previous.run_id if previous and (route.resumes_halt or previous.request_mode.value == route.mode and route.mode in {"AUTHORING_DRAFT", "FINALIZATION"}) else folder.name
    events.emit("route", role="요청 분류", run_id=planned_id, **route.model_dump())
    message = user_message(request["text"], request.get("material", ""), request.get("files", []))
    history = request.get("history", [])
    if route.resumes_halt:
        # The halted run's own resume path (same run, same stage, its scope rules) takes the message; the web runs it.
        return {"answer": "", "history": history, "resume_halted": True, "effective_mode": route.mode,
                "route": route.model_dump(), "run_id": previous.run_id, "finish_reason": "RESUME_HALTED"}
    if route.mode == "CONSULT":
        answer = moderator.consult(provider, cfg, request, blocks, paths, previous)
        footnote = moderator.CONSULT_NOTE + "\n" + usage_note(provider.usage, time.time() - started)
        return {"answer": answer + "\n\n" + footnote, "effective_mode": "CONSULT", "route": route.model_dump(), "finish_reason": "STOP",
                "history": [*history, message, {"role": "model", "parts": [{"text": answer}]}]}
    if route.mode in {"CHAT", "META"}:
        # A prior report is conversational context, not a user-authored source.
        context = history
        if request.get("reference"):
            context = [*history, {"role": "model", "parts": [{"text": request["reference"]}]}]
        result = generate_turn(provider, request["model"], context, message, events, request.get("instructions"))
        # The footnote is for the reader only; the history the model sees next turn keeps the bare answer.
        answer = result["answer"] + "\n\n" + usage_note(provider.usage, time.time() - started)
        return {**result, "answer": answer, "effective_mode": route.mode, "route": route.model_dump()}
    rt = build_runtime(cfg.project_root, config_path, overrides={cfg.default_model_key: request["model"]})
    pipeline_provider = make_provider(rt)
    try:
        result = run_pipeline(rt, pipeline_provider, request, route, blocks, paths, folder, previous,
                              turn={"started_at": started, **provider.usage})
    finally:
        _close(pipeline_provider)
    # Back of the moderator: the answer to this message over the report; the report itself stays as rendered.
    state = rt.store.load_state(result["run_id"])
    result["answer"] = moderator.deliver(provider, cfg, request["text"], state, result["answer"], route.reason)
    rt.store.write_chat_report(state.run_id, result["answer"])
    result["history"] = [*history, message, {"role": "model", "parts": [{"text": result["answer"]}]}]
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config, args.project_root)
    request = json.loads(args.request.read_text(encoding="utf-8"))
    folder = args.request.parent
    events = EventWriter.from_env(cfg.api_key_env)
    if events is None:
        raise ValueError("실시간 로그 경로가 필요합니다.")
    provider = live_provider(cfg, events)
    try:
        from . import free_work

        # Free work mode (CLAUDE.md 자유 작업 모드): the general agent answers without routing, and the tool panel runs the
        # chosen gate roles on one message's wording before the agent fixes it. Every other turn is classified first.
        if request.get("review"):
            result = free_work.review_turn(provider, cfg, args.config, request, folder)
        elif request.get("work_mode") == free_work.FREE:
            result = free_work.free_turn(provider, cfg, request)
        else:
            result = routed_turn(provider, cfg, args.config, request, folder, events)
        (folder / "response.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        (folder / "report.md").write_text(result["answer"], encoding="utf-8")
        return 0
    except Exception as exc:
        # Details are redacted by the event writer and never printed with a raw key.
        events.emit("error", role="요청 처리", text=str(exc))
        print("요청 분류 또는 하네스 실행이 완료되지 않았습니다. 일반 답변으로 우회하지 않습니다. 로그를 확인하세요.")
        return 1
    finally:
        _close(provider)


def _close(provider: Any) -> None:
    client = getattr(provider, "client", None)
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - best effort
            pass


if __name__ == "__main__":
    raise SystemExit(main())
