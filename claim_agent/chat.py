"""Conversation entry point: route first, then chat or the gated claim pipeline."""
from __future__ import annotations

import argparse
import base64
import json
import uuid
from pathlib import Path

from .config import load_config
from .live_events import EventWriter, visible_text
from .models.request import IMAGE_EXT
from .provider.gemini import make_client
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
            parts.append({"text": f"첨부 파일: {p.name}\n" + p.read_text(encoding="utf-8-sig")})
    return {"role": "user", "parts": parts}


def generate_turn(client, model: str, history: list[dict], message: dict, events: EventWriter) -> dict:
    from google.genai import types
    messages = [*history, message]
    contents = []
    for entry in messages:
        parts = []
        for part in entry["parts"]:
            if "text" in part:
                parts.append(types.Part.from_text(text=part["text"]))
            else:
                image = part["inline_data"]
                parts.append(types.Part.from_bytes(data=base64.b64decode(image["data"]), mime_type=image["mime_type"]))
        contents.append(types.Content(role=entry["role"], parts=parts))
    call_id = uuid.uuid4().hex
    # Log text history, preserving roles; binary attachments are identified, not dumped.
    transcript = "\n\n".join(e["role"] + ":\n" + "\n".join(p.get("text", "[첨부 이미지]") for p in e["parts"]) for e in messages)
    events.emit("request", call_id=call_id, role="대화", text=transcript, system=SYSTEM)
    result = []
    finish = ""
    try:
        for chunk in client.models.generate_content_stream(
            model=model, contents=contents,
            config=types.GenerateContentConfig(system_instruction=SYSTEM, max_output_tokens=8192,
                                              automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                                              thinking_config=types.ThinkingConfig(thinking_level="LOW")),
        ):
            text = visible_text(chunk)
            if text:
                result.append(text)
                events.emit("delta", call_id=call_id, role="대화", text=text)
            for candidate in chunk.candidates or []:
                if candidate.finish_reason:
                    finish = str(getattr(candidate.finish_reason, "value", candidate.finish_reason))
        if not result:
            raise RuntimeError("모델이 텍스트 응답을 반환하지 않았습니다.")
        answer = "".join(result)
        events.emit("response_end", call_id=call_id, role="대화", finish_reason=finish)
        return {"answer": answer, "history": [*messages, {"role": "model", "parts": [{"text": answer}]}], "finish_reason": finish}
    except Exception as exc:
        events.emit("error", call_id=call_id, role="대화", text=str(exc))
        raise


def routed_turn(client, cfg, config_path, request: dict, folder: Path, events: EventWriter) -> dict:
    from .conversation_pipeline import intake, previous_state, run_pipeline
    from .provider.gemini import GeminiProvider
    from .routing import classify_request
    from .runtime import build_runtime, make_provider

    previous = previous_state(cfg, request)
    blocks, paths = intake(request, folder, previous)
    from .provider.cache import CacheManager

    router_cache = CacheManager(client, cfg.path("runs_dir") / ".cache-registry.json", cfg.cache.ttl, cfg.cache.enabled, warm=cfg.cache.warm, min_expected_reuse=cfg.cache.min_expected_reuse)
    router = GeminiProvider(client, router_cache, events=events)
    route = classify_request(router, cfg.project_root, request["model"], request["text"], blocks, folder.name,
                             request.get("ui_hints"))
    (folder / "route.json").write_text(route.model_dump_json(indent=2), encoding="utf-8")
    planned_id = previous.run_id if previous and previous.request_mode.value == route.mode and route.mode in {"AUTHORING_DRAFT", "FINALIZATION"} else folder.name
    events.emit("route", role="요청 분류", run_id=planned_id, **route.model_dump())
    message = user_message(request["text"], request.get("material", ""), request.get("files", []))
    history = request.get("history", [])
    if route.mode in {"CHAT", "META"}:
        # A prior report is conversational context, not a user-authored source.
        context = history
        if request.get("reference"):
            context = [*history, {"role": "model", "parts": [{"text": request["reference"]}]}]
        result = generate_turn(client, request["model"], context, message, events)
        return {**result, "effective_mode": route.mode, "route": route.model_dump()}
    rt = build_runtime(cfg.project_root, config_path, overrides={"model.default": request["model"]})
    provider = make_provider(rt)
    try:
        result = run_pipeline(rt, provider, request, route, blocks, paths, folder, previous)
    finally:
        if getattr(provider, "client", None):
            provider.client.close()
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
    events = EventWriter.from_env(cfg.model.api_key_env)
    if events is None:
        raise ValueError("실시간 로그 경로가 필요합니다.")
    client = make_client(cfg.model.api_key_env)
    try:
        result = routed_turn(client, cfg, args.config, request, folder, events)
        (folder / "response.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        (folder / "report.md").write_text(result["answer"], encoding="utf-8")
        return 0
    except Exception as exc:
        # Details are redacted by the event writer and never printed with a raw key.
        events.emit("error", role="요청 처리", text=str(exc))
        print("요청 분류 또는 하네스 실행이 완료되지 않았습니다. 일반 답변으로 우회하지 않습니다. 로그를 확인하세요.")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
