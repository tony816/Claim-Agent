"""Record / replay providers for reproducible offline runs and evals."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .base import CallResult, CallSpec, LLMProvider, ProviderError


def _fixture_name(seq: int, spec: CallSpec) -> str:
    return f"{seq:03d}-{spec.role}-{spec.scope}-{spec.phase}.json"


class RecordingProvider:
    name = "record"

    def __init__(self, inner: LLMProvider, fixtures_dir: Path, sanitize: bool = False):
        self.inner = inner
        self.dir = fixtures_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        self.sanitize = sanitize

    def generate(self, spec: CallSpec) -> CallResult:
        result = self.inner.generate(spec)
        self.seq += 1
        payload: dict[str, Any] = {
            "key": spec.replay_key(),
            "meta": {
                "role": spec.role,
                "scope": spec.scope,
                "model": spec.model,
                "phase": spec.phase,
                "stage": spec.stage,
                "record_id": (spec.meta or {}).get("record_id"),
                "target_claim_id": (spec.meta or {}).get("target_claim_id"),
                "system_sha": spec.system_sha,
                "sources_sha": spec.sources_sha,
                "packet_sha": spec.packet_sha,
                "gen": spec.gen.__dict__,
            },
            "request_preview": "" if self.sanitize else spec.packet_text[:2000],
            "response": result.as_dict(),
        }
        (self.dir / _fixture_name(self.seq, spec)).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result.provider = f"record({result.provider})"
        return result


class ReplayProvider:
    """strict: match on replay_key; loose: next fixture for (role, scope, phase) in file order."""

    name = "replay"

    def __init__(self, fixtures_dir: Path, strict: bool = False):
        self.dir = fixtures_dir
        self.strict = strict
        self.by_key: dict[str, dict[str, Any]] = {}
        self.by_role: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.by_record: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self._cursor: dict[tuple, int] = defaultdict(int)
        self._used: set[str] = set()
        for p in sorted(fixtures_dir.glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            data["_file"] = p.name
            self.by_key[data.get("key", "")] = data
            meta = data.get("meta", {})
            k = (meta.get("role", ""), meta.get("scope", ""), meta.get("phase", "main"))
            self.by_role[k].append(data)
            if meta.get("record_id"):
                self.by_record[k + (meta["record_id"],)].append(data)
        if not self.by_key:
            raise ProviderError(f"no fixtures found in {fixtures_dir}")

    def _next(self, key: tuple, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        i = self._cursor[key]
        while i < len(items) and items[i]["_file"] in self._used:
            i += 1
        if i >= len(items):
            return None
        self._cursor[key] = i + 1
        return items[i]

    def generate(self, spec: CallSpec) -> CallResult:
        if self.strict:
            data = self.by_key.get(spec.replay_key())
            if data is None:
                raise ProviderError(f"no strict fixture for {spec.role}/{spec.scope}/{spec.phase} key={spec.replay_key()[:12]}")
        else:
            # loose: prefer the fixture recorded for the same record_id (order-independent, safe under fan-out),
            # then fall back to the next unused fixture for (role, scope, phase).
            k = (spec.role, spec.scope, spec.phase)
            rid = (spec.meta or {}).get("record_id")
            data = None
            if rid and self.by_record.get(k + (rid,)):
                data = self._next(k + (rid,), self.by_record[k + (rid,)])
            if data is None:
                data = self._next(k, self.by_role.get(k, []))
            if data is None:
                raise ProviderError(f"no loose fixture left for {k} (record_id={rid})")
            self._used.add(data["_file"])
        result = CallResult.from_dict(data["response"])
        result.provider = f"replay:{data['_file']}"
        return result
