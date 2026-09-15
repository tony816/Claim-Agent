"""Anthropic (Claude) provider: same CallSpec/CallResult contract as GeminiProvider.

Mapping notes (see docs/python_runtime.md → 프로바이더):
  * system instruction → `system` text block with `cache_control` (prompt caching, prefix match);
    the pre-loaded sources block is the first user text block, also cache-marked, so the stable
    prefix (role file + sources) is cached and only the packet varies.
  * thinking_level HIGH/MEDIUM/LOW → adaptive thinking + `output_config.effort`; `temperature` is not
    sent by default (removed on Claude Opus 5 / Fable 5 family; 400 if present).
  * json_schema → `output_config.format` (structured outputs); on a schema rejection the call is retried
    with a prompt-level JSON instruction and parsed by the same repair path as Gemini.
  * python tool callables → tool definitions + a manual tool loop (tool_use → tool_result).
  * server-side refusal fallbacks (`fallbacks: "default"`) are on by default; a `refusal` stop reason
    that survives the chain raises ProviderError with the category.
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import time
import uuid
from typing import Any

from ..live_events import EventWriter
from .base import CallResult, CallSpec, ImagePart, ProviderError, parse_json_text

EFFORT = {"MINIMAL": "low", "LOW": "low", "MEDIUM": "medium", "HIGH": "high", "XHIGH": "xhigh", "MAX": "max"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_PY_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


def make_anthropic_client(api_key_env: str = "ANTHROPIC_API_KEY", api_key: str | None = None) -> Any:
    import anthropic

    key = api_key or os.environ.get(api_key_env)
    if key:
        return anthropic.Anthropic(api_key=key)
    return anthropic.Anthropic()   # ANTHROPIC_AUTH_TOKEN / `ant auth login` profile resolution


def tool_definitions(funcs: list[Any]) -> list[dict[str, Any]]:
    """Tool definitions from python callables (name, docstring, typed parameters)."""
    import typing

    out = []
    for fn in funcs:
        props: dict[str, Any] = {}
        required: list[str] = []
        try:
            hints = typing.get_type_hints(fn)
        except Exception:  # noqa: BLE001 - unresolvable forward references default to string
            hints = {}
        for name, param in inspect.signature(fn).parameters.items():
            ann = hints.get(name, param.annotation)
            props[name] = {"type": _PY_JSON.get(ann, "string")}
            if param.default is inspect.Parameter.empty:
                required.append(name)
        doc = (fn.__doc__ or fn.__name__).strip()
        out.append({"name": fn.__name__, "description": doc, "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False}})
    return out


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: Any, *, events: EventWriter | None = None, thinking: str = "adaptive", fallbacks: str = "default",
                 structured_outputs: bool = True, send_temperature: bool = False, retry_attempts: int = 3, backoff_s: list[float] | None = None, max_tool_rounds: int = 6):
        self.client = client
        self.events = events
        self.thinking = thinking
        self.fallbacks = fallbacks
        self.structured_outputs = structured_outputs
        self.send_temperature = send_temperature
        self.retry_attempts = retry_attempts
        self.backoff_s = backoff_s or [2, 8, 20]
        self.max_tool_rounds = max_tool_rounds
        self._schema_rejected: set[str] = set()     # models where output_config.format was refused
        self._fallbacks_rejected = False

    # ------------------------------------------------------------------ helpers
    def _emit(self, kind: str, spec: CallSpec, call_id: str, **data: Any) -> None:
        if self.events:
            self.events.emit(kind, call_id=call_id, role=spec.role, scope=spec.scope, phase=spec.phase, target=spec.meta.get("target_claim_id"), **data)

    @staticmethod
    def _image_block(img: ImagePart) -> dict[str, Any]:
        return {"type": "image", "source": {"type": "base64", "media_type": img.mime_type, "data": base64.standard_b64encode(img.data).decode("ascii")}}

    @staticmethod
    def _history_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Chat history in the runtime's Gemini-style layout → Messages API turns."""
        out = []
        for entry in history:
            role = "assistant" if entry.get("role") in ("model", "assistant") else "user"
            blocks: list[dict[str, Any]] = []
            for part in entry.get("parts", []):
                if "text" in part:
                    blocks.append({"type": "text", "text": part["text"]})
                elif "inline_data" in part:
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": part["inline_data"]["mime_type"], "data": part["inline_data"]["data"]}})
            if blocks:
                out.append({"role": role, "content": blocks})
        return out

    def _messages(self, spec: CallSpec, json_instruction: bool) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if spec.sources_block:
            block: dict[str, Any] = {"type": "text", "text": spec.sources_block}
            if spec.use_cache:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        packet = spec.packet_text
        if json_instruction and spec.json_schema is not None:
            packet += "\n\n### 출력 형식\n\n다음 JSON 스키마를 따르는 JSON 객체 하나만, 코드펜스 없이 출력한다.\n" + json.dumps(spec.json_schema, ensure_ascii=False)
        blocks.append({"type": "text", "text": packet})
        blocks += [self._image_block(img) for img in spec.images]
        return self._history_messages(spec.history) + [{"role": "user", "content": blocks}]

    def _params(self, spec: CallSpec, json_instruction: bool, tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        system_block: dict[str, Any] = {"type": "text", "text": spec.system_instruction}
        if spec.use_cache:
            system_block["cache_control"] = {"type": "ephemeral"}
        params: dict[str, Any] = {
            "model": spec.model, "max_tokens": spec.gen.max_output_tokens, "system": [system_block],
            "messages": self._messages(spec, json_instruction),
            "output_config": {"effort": EFFORT.get(spec.gen.thinking_level.upper(), "high")},
        }
        if self.thinking == "adaptive":
            params["thinking"] = {"type": "adaptive"}
        elif self.thinking == "off":
            params["thinking"] = {"type": "disabled"}
        if self.send_temperature:
            params["temperature"] = spec.gen.temperature
        if spec.json_schema is not None and not json_instruction:
            params["output_config"]["format"] = {"type": "json_schema", "schema": spec.json_schema}
        if tools:
            params["tools"] = tools
        return params

    def _create(self, params: dict[str, Any], spec: CallSpec, call_id: str) -> Any:
        """One Messages API call (streamed, so long outputs never hit HTTP timeouts)."""
        use_fallbacks = self.fallbacks == "default" and not self._fallbacks_rejected
        if use_fallbacks:
            api = self.client.beta.messages
            params = {**params, "betas": [FALLBACK_BETA], "fallbacks": "default"}
        else:
            api = self.client.messages
        try:
            with api.stream(**params) as stream:
                for event in stream:
                    if getattr(event, "type", "") == "content_block_delta" and getattr(getattr(event, "delta", None), "type", "") == "text_delta":
                        self._emit("delta", spec, call_id, text=event.delta.text)
                return stream.get_final_message()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if use_fallbacks and ("fallback" in msg or "betas" in msg or "beta" in msg):
                self._fallbacks_rejected = True
                return self._create({k: v for k, v in params.items() if k not in ("betas", "fallbacks")}, spec, call_id)
            raise

    @staticmethod
    def _usage(msg: Any) -> dict[str, int]:
        u = getattr(msg, "usage", None)
        if u is None:
            return {}
        cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        inp = int(getattr(u, "input_tokens", 0) or 0)
        out = int(getattr(u, "output_tokens", 0) or 0)
        return {"prompt_tokens": inp + cache_read + cache_write, "cached_tokens": cache_read, "thoughts_tokens": 0, "output_tokens": out, "total_tokens": inp + cache_read + cache_write + out}

    @staticmethod
    def _text(msg: Any) -> str:
        return "".join(getattr(b, "text", "") for b in (getattr(msg, "content", None) or []) if getattr(b, "type", "") == "text")

    # ------------------------------------------------------------------ main
    def generate(self, spec: CallSpec) -> CallResult:
        blind = spec.role == "blind-claim-reconstruction-reviewer"
        if spec.cache_packet_text and (blind or spec.tools or spec.packet_text.count(spec.cache_packet_text) != 1):
            raise ProviderError("Shared context must occur exactly once and cannot enter blind/tool calls")
        tools = tool_definitions(spec.tools) if spec.tools else None
        fn_by_name = {fn.__name__: fn for fn in (spec.tools or [])}
        json_instruction = spec.json_schema is not None and (not self.structured_outputs or spec.model in self._schema_rejected)
        last_exc: Exception | None = None
        for attempt in range(self.retry_attempts):
            call_id = uuid.uuid4().hex
            self._emit("request", spec, call_id, attempt=attempt + 1, text=spec.packet_text, system=spec.system_instruction, sources=spec.sources_block, images=[i.label for i in spec.images])
            params = self._params(spec, json_instruction, tools)
            t0 = time.time()
            try:
                msg = self._create(params, spec, call_id)
                function_calls: list[dict[str, Any]] = []
                rounds = 0
                while tools and getattr(msg, "stop_reason", "") == "tool_use" and rounds < self.max_tool_rounds:
                    rounds += 1
                    uses = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
                    results = []
                    for use in uses:
                        fn = fn_by_name.get(use.name)
                        args = dict(use.input or {})
                        try:
                            value = fn(**args) if fn else {"error": f"unknown tool {use.name}"}
                            results.append({"type": "tool_result", "tool_use_id": use.id, "content": json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value})
                            function_calls += [{"name": use.name, "args": args}, {"response_of": use.name, "response": value}]
                        except Exception as exc:  # noqa: BLE001
                            results.append({"type": "tool_result", "tool_use_id": use.id, "content": f"Error: {exc}", "is_error": True})
                            function_calls += [{"name": use.name, "args": args}, {"response_of": use.name, "response": {"error": str(exc)}}]
                    params["messages"] = params["messages"] + [{"role": "assistant", "content": msg.content}, {"role": "user", "content": results}]
                    msg = self._create(params, spec, call_id)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                low = str(exc).lower()
                self._emit("error", spec, call_id, text=str(exc))
                if spec.json_schema is not None and not json_instruction and ("output_config" in low or "schema" in low or "format" in low):
                    self._schema_rejected.add(spec.model)
                    json_instruction = True
                    continue
                if any(code in str(exc) for code in ("429", "500", "502", "503", "504", "529")) or "overloaded" in low or "rate" in low:
                    if attempt + 1 < self.retry_attempts:
                        time.sleep(self.backoff_s[min(attempt, len(self.backoff_s) - 1)])
                    continue
                raise ProviderError(f"Anthropic call failed for {spec.role}: {exc}") from exc
            latency = int((time.time() - t0) * 1000)
            stop = str(getattr(msg, "stop_reason", "") or "")
            if stop == "refusal":
                details = getattr(msg, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                raise ProviderError(f"Anthropic refused the request for {spec.role} (category={category})")
            text = self._text(msg)
            self._emit("response_end", spec, call_id, finish_reason=stop, function_calls=function_calls)
            usage = self._usage(msg)
            return CallResult(
                text=text, parsed=parse_json_text(text) if spec.json_schema is not None else None, usage=usage, latency_ms=latency,
                cache_hit=usage.get("cached_tokens", 0) > 0, cache_name=None, function_calls=function_calls,
                finish_reason={"end_turn": "STOP", "max_tokens": "MAX_TOKENS"}.get(stop, stop.upper()), model=str(getattr(msg, "model", spec.model) or spec.model),
                provider=self.name, raw={"image_transport": "inline" if spec.images else "none", "inline_image_bytes": sum(len(i.data) for i in spec.images), "structured_outputs": not json_instruction},
            )
        raise ProviderError(f"Anthropic call failed after retries for {spec.role}: {last_exc}")

    def list_models(self) -> list[str]:
        return [str(getattr(m, "id", "")) for m in self.client.models.list()]
