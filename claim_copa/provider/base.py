"""Provider abstraction shared by Gemini, replay and scripted providers."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..models.ids import sha256_text


@dataclass
class ImagePart:
    mime_type: str
    data: bytes
    label: str = ""


@dataclass
class GenParams:
    temperature: float = 0.2
    thinking_level: str = "HIGH"
    max_output_tokens: int = 32768


@dataclass
class CallSpec:
    role: str
    scope: str
    model: str
    system_instruction: str
    packet_text: str
    sources_block: str = ""                 # cache candidate; "" when the role has no sources
    images: list[ImagePart] = field(default_factory=list)
    json_schema: dict[str, Any] | None = None
    tools: list[Callable[..., Any]] | None = None    # python callables (automatic function calling)
    gen: GenParams = field(default_factory=GenParams)
    use_cache: bool = True
    phase: str = "main"                     # main | tool_phase | repair
    stage: str = ""
    run_id: str = ""
    seq: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def system_sha(self) -> str:
        return sha256_text(self.system_instruction)

    @property
    def sources_sha(self) -> str:
        return sha256_text(self.sources_block)

    @property
    def packet_sha(self) -> str:
        return sha256_text(self.packet_text)

    def replay_key(self) -> str:
        return sha256_text("|".join([self.role, self.scope, self.model, self.phase, self.system_sha, self.sources_sha, self.packet_sha]))


@dataclass
class CallResult:
    text: str
    parsed: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    cache_hit: bool = False
    cache_name: str | None = None
    function_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    provider: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "parsed": self.parsed,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "cache_hit": self.cache_hit,
            "cache_name": self.cache_name,
            "function_calls": self.function_calls,
            "finish_reason": self.finish_reason,
            "model": self.model,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CallResult":
        return cls(
            text=d.get("text", ""),
            parsed=d.get("parsed"),
            usage=d.get("usage", {}),
            latency_ms=d.get("latency_ms", 0),
            cache_hit=d.get("cache_hit", False),
            cache_name=d.get("cache_name"),
            function_calls=d.get("function_calls", []),
            finish_reason=d.get("finish_reason", ""),
            model=d.get("model", ""),
            provider=d.get("provider", ""),
        )


class LLMProvider(Protocol):
    name: str

    def generate(self, spec: CallSpec) -> CallResult: ...


class ProviderError(RuntimeError):
    pass


def parse_json_text(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction (handles ```json fences)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(t[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None
