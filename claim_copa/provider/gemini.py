"""Gemini provider (google-genai SDK)."""
from __future__ import annotations

import os
import time
from typing import Any

from .base import CallResult, CallSpec, ProviderError, parse_json_text
from .cache import CacheManager

THINKING_BUDGET_FALLBACK = {"MINIMAL": 512, "LOW": 1024, "MEDIUM": 8192, "HIGH": -1}


def make_client(api_key_env: str = "GEMINI_API_KEY", api_key: str | None = None) -> Any:
    from google import genai

    key = api_key or os.environ.get(api_key_env) or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise ProviderError(f"{api_key_env} is not set")
    return genai.Client(api_key=key)


class GeminiProvider:
    name = "gemini"

    def __init__(self, client: Any, cache: CacheManager | None = None, retry_attempts: int = 3, backoff_s: list[float] | None = None):
        self.client = client
        self.cache = cache
        self.retry_attempts = retry_attempts
        self.backoff_s = backoff_s or [2, 8, 20]
        self._thinking_level_unsupported: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def _thinking_config(self, model: str, level: str) -> Any:
        from google.genai import types

        if model in self._thinking_level_unsupported:
            return types.ThinkingConfig(thinking_budget=THINKING_BUDGET_FALLBACK.get(level.upper(), -1))
        return types.ThinkingConfig(thinking_level=level.upper())

    def _build_contents(self, spec: CallSpec, include_sources: bool) -> list[Any]:
        from google.genai import types

        parts: list[Any] = []
        if include_sources and spec.sources_block:
            parts.append(types.Part.from_text(text=spec.sources_block))
        parts.append(types.Part.from_text(text=spec.packet_text))
        for img in spec.images:
            parts.append(types.Part.from_bytes(data=img.data, mime_type=img.mime_type))
        return [types.Content(role="user", parts=parts)]

    def _config(self, spec: CallSpec, cache_name: str | None, json_mode: bool) -> Any:
        from google.genai import types

        kwargs: dict[str, Any] = {
            "temperature": spec.gen.temperature,
            "max_output_tokens": spec.gen.max_output_tokens,
            "thinking_config": self._thinking_config(spec.model, spec.gen.thinking_level),
        }
        if cache_name:
            kwargs["cached_content"] = cache_name
        else:
            kwargs["system_instruction"] = spec.system_instruction
        if json_mode and spec.json_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_json_schema"] = spec.json_schema
        if spec.tools:
            kwargs["tools"] = spec.tools
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _usage(resp: Any) -> dict[str, int]:
        u = getattr(resp, "usage_metadata", None)
        if u is None:
            return {}
        return {
            "prompt_tokens": int(getattr(u, "prompt_token_count", 0) or 0),
            "cached_tokens": int(getattr(u, "cached_content_token_count", 0) or 0),
            "thoughts_tokens": int(getattr(u, "thoughts_token_count", 0) or 0),
            "output_tokens": int(getattr(u, "candidates_token_count", 0) or 0),
            "total_tokens": int(getattr(u, "total_token_count", 0) or 0),
        }

    @staticmethod
    def _finish_reason(resp: Any) -> str:
        cands = getattr(resp, "candidates", None) or []
        if not cands:
            return ""
        fr = getattr(cands[0], "finish_reason", None)
        return str(getattr(fr, "value", fr) or "")

    @staticmethod
    def _function_calls(resp: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        history = getattr(resp, "automatic_function_calling_history", None) or []
        for content in history:
            for part in getattr(content, "parts", None) or []:
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    out.append({"name": fc.name, "args": dict(fc.args or {})})
                fr = getattr(part, "function_response", None)
                if fr is not None:
                    out.append({"response_of": fr.name, "response": fr.response})
        return out

    # ------------------------------------------------------------------ main
    def generate(self, spec: CallSpec) -> CallResult:
        cache_entry = None
        if spec.use_cache and self.cache is not None and spec.sources_block:
            cache_entry = self.cache.get_or_create(spec.model, spec.role, spec.scope, spec.system_instruction, spec.sources_block)
        json_mode = spec.json_schema is not None and not spec.tools
        last_exc: Exception | None = None
        for attempt in range(self.retry_attempts):
            contents = self._build_contents(spec, include_sources=cache_entry is None)
            config = self._config(spec, cache_entry.name if cache_entry else None, json_mode)
            t0 = time.time()
            try:
                resp = self.client.models.generate_content(model=spec.model, contents=contents, config=config)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                msg = str(exc)
                low = msg.lower()
                if "thinking_level" in low and spec.model not in self._thinking_level_unsupported:
                    self._thinking_level_unsupported.add(spec.model)
                    continue
                if cache_entry is not None and ("cachedcontent" in low or "not found" in low or "404" in low):
                    self.cache.invalidate(cache_entry.name)  # type: ignore[union-attr]
                    cache_entry = None
                    continue
                if any(code in msg for code in ("429", "500", "502", "503", "504")) or "overloaded" in low or "resource_exhausted" in low:
                    if attempt + 1 < self.retry_attempts:
                        time.sleep(self.backoff_s[min(attempt, len(self.backoff_s) - 1)])
                    continue
                raise ProviderError(f"Gemini call failed for {spec.role}: {msg}") from exc
            latency = int((time.time() - t0) * 1000)
            text = resp.text or ""
            parsed = parse_json_text(text) if json_mode or spec.json_schema is not None else None
            return CallResult(
                text=text,
                parsed=parsed,
                usage=self._usage(resp),
                latency_ms=latency,
                cache_hit=cache_entry is not None,
                cache_name=cache_entry.name if cache_entry else None,
                function_calls=self._function_calls(resp),
                finish_reason=self._finish_reason(resp),
                model=spec.model,
                provider=self.name,
            )
        raise ProviderError(f"Gemini call failed after retries for {spec.role}: {last_exc}")

    # ------------------------------------------------------------------ probes
    def list_models(self) -> list[str]:
        names = []
        for m in self.client.models.list():
            names.append(str(getattr(m, "name", "")).replace("models/", ""))
        return names
