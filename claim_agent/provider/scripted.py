"""ScriptedProvider: canned envelopes for deterministic state-machine tests."""
from __future__ import annotations

import json
from collections import deque
from typing import Any

from ..models.envelope import RoleEnvelope
from .base import CallResult, CallSpec, ProviderError


class ScriptedProvider:
    """Responses are consumed per role in FIFO order.

    ``script`` is ``{role_name: [envelope_dict | RoleEnvelope | callable(spec)->dict, ...]}``.
    A callable receives the CallSpec and returns an envelope dict; useful to echo
    identifiers or the exact text from the packet.
    """

    name = "scripted"

    def __init__(self, script: dict[str, list[Any]]):
        self.queues = {role: deque(items) for role, items in script.items()}
        self.calls: list[CallSpec] = []

    def generate(self, spec: CallSpec) -> CallResult:
        self.calls.append(spec)
        if spec.phase == "tool_phase":
            # tool phase never consumes a scripted envelope; behave as "no search needed"
            return CallResult(text="SEARCH_DONE", parsed=None, model=spec.model, provider=self.name, finish_reason="STOP")
        q = self.queues.get(spec.role)
        if not q:
            raise ProviderError(f"scripted provider has no response left for role {spec.role} (phase={spec.phase})")
        item = q.popleft()
        if callable(item) and not isinstance(item, (dict, RoleEnvelope)):
            item = item(spec)
        if isinstance(item, RoleEnvelope):
            data = item.model_dump(mode="json")
        elif isinstance(item, str):
            # tool-phase plain text
            return CallResult(text=item, parsed=None, model=spec.model, provider=self.name, finish_reason="STOP")
        else:
            data = item
        text = json.dumps(data, ensure_ascii=False)
        return CallResult(
            text=text,
            parsed=data,
            usage={"prompt_tokens": 1000, "cached_tokens": 0, "thoughts_tokens": 100, "output_tokens": 500, "total_tokens": 1600},
            latency_ms=1,
            model=spec.model,
            provider=self.name,
            finish_reason="STOP",
        )

    def remaining(self) -> dict[str, int]:
        return {r: len(q) for r, q in self.queues.items() if q}
