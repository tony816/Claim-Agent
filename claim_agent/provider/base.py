"""Provider abstraction shared by Gemini, replay and scripted providers."""
from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models.ids import sha256_text

_PY_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass
class ImagePart:
    mime_type: str
    data: bytes
    label: str = ""
    sha256: str = ""       # content hash of `data` (Files API registry key)


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
    cache_packet_text: str = ""             # exact shared substring; full packet remains auditable
    cache_images: bool = False
    expected_reuse: int = 1                 # planned uses of the cacheable bundle in this run (cache creation policy)
    history: list[dict[str, Any]] = field(default_factory=list)   # chat turns [{role: user|model, parts: [{text}|{inline_data}]}]

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
    def from_dict(cls, d: dict[str, Any]) -> CallResult:
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


_FILE_HEAD = re.compile(r"^<<<FILE (\S+) sha256=([0-9a-f]+)>>>", re.MULTILINE)
_MATERIAL_HEAD = re.compile(r"^<<<MATERIAL \[[^\]]+\] (.+?) \(sha256=([0-9a-f]+)\)>>>(.*)$", re.MULTILINE)
_REQUEST_SECTION = re.compile(r"^### 현재 요청[^\n]*\n(.*?)(?=^### |\Z)", re.MULTILINE | re.DOTALL)
REQUEST_EXCERPT_MAX = 1200


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n…({len(text) - limit:,}자 생략)"


def request_summary(spec: CallSpec, sources_sent: bool = True) -> dict[str, Any]:
    """What the live log shows for one call: sizes, file headers and the current request — never the full texts.

    The full system instruction, pre-loaded sources and packet stay in the run's `calls/` audit file; repeating them in
    the event log filled the browser's window with source documents. `sources_sent` is False when a provider cache
    served the sources, so the log does not claim to have sent what it did not.
    """
    request = ""
    section = _REQUEST_SECTION.search(spec.packet_text)
    if section:
        request = section.group(1)
        request = request.split("## 현재 요청", 1)[-1]            # after the project instructions, if they lead
    elif not spec.stage:
        request = spec.packet_text                               # a conversation turn: the packet is the message
    materials = [f"{name}({sha[:8]})" for name, sha, rest in _MATERIAL_HEAD.findall(spec.packet_text) if "이미지 파트" not in rest]
    call_file = f"{spec.run_id}/calls/{spec.seq:03d}-{spec.role}-{spec.scope}.json" if spec.run_id and spec.seq and spec.phase == "main" else ""
    return dict(
        seq=spec.seq, record_id=spec.meta.get("record_id"), json=spec.json_schema is not None,
        system_chars=len(spec.system_instruction), sources=[f"{path} ({sha[:8]})" for path, sha in _FILE_HEAD.findall(spec.sources_block)],
        sources_sent=sources_sent, materials=materials, images=[img.label for img in spec.images],
        packet_chars=len(spec.packet_text), call_file=call_file, request=clip(request, REQUEST_EXCERPT_MAX) if request.strip() else "",
    )


class LLMProvider(Protocol):
    name: str

    def generate(self, spec: CallSpec) -> CallResult: ...


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
