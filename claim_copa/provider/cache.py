"""Gemini context-cache management for per-(role, scope) source bundles."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.ids import sha256_text


@dataclass
class CacheEntry:
    key: str
    name: str
    model: str
    role: str
    scope: str
    expires_at: float
    token_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def cache_key(model: str, role: str, scope: str, system_sha: str, sources_sha: str) -> str:
    return sha256_text("|".join([model, role, scope, system_sha, sources_sha]))[:24]


def parse_ttl_seconds(ttl: str) -> int:
    ttl = ttl.strip()
    if ttl.endswith("s"):
        return int(float(ttl[:-1]))
    return int(float(ttl))


class CacheManager:
    """Registry-backed cache creation. ``client`` is a google.genai Client (or None)."""

    def __init__(self, client: Any, registry_path: Path, ttl: str = "3600s", enabled: bool = True):
        self.client = client
        self.registry_path = registry_path
        self.ttl = ttl
        self.enabled = enabled and client is not None
        self.uncacheable_models: set[str] = set()
        self._entries: dict[str, CacheEntry] = {}
        self._load()

    def _load(self) -> None:
        if self.registry_path.exists():
            try:
                data = json.loads(self.registry_path.read_text(encoding="utf-8"))
                for k, v in data.get("entries", {}).items():
                    self._entries[k] = CacheEntry(**v)
                self.uncacheable_models = set(data.get("uncacheable_models", []))
            except (json.JSONDecodeError, TypeError):
                self._entries = {}

    def _save(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(
            json.dumps(
                {"entries": {k: v.as_dict() for k, v in self._entries.items()}, "uncacheable_models": sorted(self.uncacheable_models)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def list(self) -> list[CacheEntry]:
        return list(self._entries.values())

    def purge(self) -> int:
        n = 0
        for entry in list(self._entries.values()):
            try:
                if self.client is not None:
                    self.client.caches.delete(name=entry.name)
            except Exception:  # noqa: BLE001 - best effort
                pass
            n += 1
        self._entries.clear()
        self._save()
        return n

    def get_or_create(self, model: str, role: str, scope: str, system_instruction: str, sources_block: str) -> CacheEntry | None:
        """Return a live cache entry or None when caching is disabled/unsupported."""
        if not self.enabled or not sources_block or model in self.uncacheable_models:
            return None
        key = cache_key(model, role, scope, sha256_text(system_instruction), sha256_text(sources_block))
        entry = self._entries.get(key)
        now = time.time()
        if entry and entry.expires_at - 60 > now:
            return entry
        try:
            from google.genai import types  # local import: optional dependency at import time

            created = self.client.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    system_instruction=system_instruction,
                    contents=[sources_block],
                    ttl=self.ttl,
                    display_name=f"claim-copa:{role}:{scope}:{key[:12]}",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "too small" in msg or "minimum" in msg or "not supported" in msg or "400" in msg:
                self.uncacheable_models.add(model)
                self._save()
            return None
        token_count = 0
        usage = getattr(created, "usage_metadata", None)
        if usage is not None:
            token_count = int(getattr(usage, "total_token_count", 0) or 0)
        entry = CacheEntry(key, created.name, model, role, scope, now + parse_ttl_seconds(self.ttl), token_count)
        self._entries[key] = entry
        self._save()
        return entry

    def invalidate(self, name: str) -> None:
        for k, v in list(self._entries.items()):
            if v.name == name:
                del self._entries[k]
        self._save()
