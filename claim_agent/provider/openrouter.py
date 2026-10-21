"""OpenRouter chat-completions transport (API key from .env), for the vendor/model IDs OpenRouter routes.

The packet, permitted-tool loop and blind-call guard are SubscriptionProvider's; only `_once` differs. The key is read
from the environment on each call and never written to logs, events or run records.
"""
from __future__ import annotations

import base64
import os
import ssl
import sys

import httpx

from .base import CallSpec, ImagePart, ProviderError
from .subscription import SubscriptionProvider

API = "https://openrouter.ai/api/v1"


def tls_verify() -> ssl.SSLContext | bool:
    if sys.platform != "win32":
        return True
    import truststore

    # Validate TLS with Windows' trusted roots (managed CA roots included), as the Gemini client does.
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def api_key(config) -> str | None:
    return os.environ.get(config.api_key_env) or None


def connection_status(config) -> dict:
    present = bool(api_key(config))
    return {"installed": True, "connected": present, "message": "API 키 설정됨" if present else f".env에 {config.api_key_env} 설정 필요"}


class OpenRouterProvider(SubscriptionProvider):
    transport, billing = "openrouter_api", "api"

    def __init__(self, config, events=None):
        super().__init__("openrouter", config, events)

    def status(self) -> dict:
        return connection_status(self.config)

    def _once(self, spec: CallSpec, packet: str, images: list[ImagePart]) -> tuple[str, dict]:
        key = api_key(self.config)
        if not key:
            raise ProviderError(f".env에 {self.config.api_key_env}를 설정하세요.")
        content = [{"type": "text", "text": packet}]
        for img in images:
            content.extend([{"type": "text", "text": img.label}, {"type": "image_url", "image_url": {
                "url": f"data:{img.mime_type};base64,{base64.b64encode(img.data).decode('ascii')}"}}])
        body = {
            "model": spec.model,
            # cache_control lets models that support it (Anthropic, Gemini …) cache the fixed role instruction.
            "messages": [{"role": "system", "content": [{"type": "text", "text": spec.system_instruction, "cache_control": {"type": "ephemeral"}}]},
                         {"role": "user", "content": content}],
            "temperature": spec.gen.temperature,
            "max_tokens": self.config.max_output_tokens,
            "reasoning": {"effort": {"MINIMAL": "low"}.get(spec.gen.thinking_level, spec.gen.thinking_level.lower())},
            "usage": {"include": True},
        }
        if spec.json_schema:
            body["response_format"] = {"type": "json_object"}
        try:
            response = httpx.post(f"{API}/chat/completions", json=body, timeout=self.config.timeout_s, verify=tls_verify(),
                                  headers={"Authorization": f"Bearer {key}", "X-Title": "Claim-Agent"})
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenRouter 연결 실패: {type(exc).__name__}") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        error = data.get("error") if isinstance(data, dict) else None
        if response.status_code != 200 or error:
            message = error.get("message") if isinstance(error, dict) else error
            raise ProviderError(f"OpenRouter {response.status_code}: {str(message or response.reason_phrase)[:300]}")
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise ProviderError("OpenRouter 출력이 최대 길이에서 잘렸습니다.")
        text = (choice.get("message") or {}).get("content") or ""
        u = data.get("usage") or {}
        inp, out = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
        usage = {"prompt_tokens": inp, "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
                 "output_tokens": out, "thoughts_tokens": 0, "total_tokens": inp + out}
        if u.get("cost") is not None:
            usage["cost_usd"] = float(u["cost"])   # OpenRouter bills in USD; telemetry uses it instead of pricing
        return text, usage
