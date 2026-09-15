"""Gemini context-cache management for per-(role, scope) source bundles."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
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

    def __init__(self, client: Any, registry_path: Path, ttl: str = "3600s", enabled: bool = True, warm: str = "always", min_expected_reuse: int = 2):
        self.client = client
        self.registry_path = registry_path
        self.ttl = ttl
        self.enabled = enabled and client is not None
        self.warm = warm
        self.min_expected_reuse = max(1, int(min_expected_reuse))
        self.uncacheable_models: set[str] = set()
        self._lock = threading.RLock()
        self._rejected_keys: set[str] = set()
        self._entries: dict[str, CacheEntry] = {}
        self._seen: dict[str, float] = {}      # key -> last time creation was skipped (second sighting creates)
        self.skipped: int = 0                  # creations skipped by the policy (telemetry/report)
        self._load()

    def _load(self) -> None:
        if self.registry_path.exists():
            try:
                data = json.loads(self.registry_path.read_text(encoding="utf-8"))
                for k, v in data.get("entries", {}).items():
                    self._entries[k] = CacheEntry(**v)
                self._seen = {k: float(v) for k, v in (data.get("seen") or {}).items()}
                # Old registries blacklisted whole models after any 400. Retry
                # those models; only a particular content key may be rejected.
                self.uncacheable_models = set()
            except (json.JSONDecodeError, TypeError):
                self._entries = {}

    def _save(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        horizon = time.time() - parse_ttl_seconds(self.ttl)
        self._seen = {k: v for k, v in self._seen.items() if v > horizon}
        data = json.dumps({"entries": {k: v.as_dict() for k, v in self._entries.items()}, "seen": self._seen}, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.registry_path.parent, delete=False) as fh:
            temporary = fh.name
            fh.write(data)
        try:
            os.replace(temporary, self.registry_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

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

    def get_or_create(self, model: str, role: str, scope: str, system_instruction: str, sources_block: str, *, images=(), identity: str = "", expected_reuse: int = 1) -> CacheEntry | None:
        # Parallel per-claim comparisons must create just one shared cache.
        with self._lock:
            return self._get_or_create(model, role, scope, system_instruction, sources_block, images, identity, expected_reuse)

    def _should_create(self, key: str, expected_reuse: int, now: float) -> bool:
        if self.warm == "never":
            return False
        if self.warm == "always" or expected_reuse >= self.min_expected_reuse:
            return True
        seen = self._seen.get(key)
        if seen is not None and now - seen < parse_ttl_seconds(self.ttl):
            return True     # second sighting within the TTL window: reuse is real
        self._seen[key] = now
        self.skipped += 1
        self._save()
        return False

    def _extend(self, entry: CacheEntry, now: float) -> None:
        """Push the expiry out when a live entry is reused with less than half its TTL left."""
        ttl_s = parse_ttl_seconds(self.ttl)
        if entry.expires_at - now > ttl_s / 2:
            return
        try:
            from google.genai import types

            self.client.caches.update(name=entry.name, config=types.UpdateCachedContentConfig(ttl=self.ttl))
        except Exception:  # noqa: BLE001 - extension is best effort; expiry fallback already exists
            return
        entry.expires_at = now + ttl_s
        self._save()

    def _get_or_create(self, model, role, scope, system_instruction, sources_block, images, identity, expected_reuse=1):
        """Return a live cache entry or None when caching is disabled/unsupported/not worth creating."""
        if not self.enabled or not sources_block or model in self.uncacheable_models:
            return None
        fingerprint = sources_block
        if images or identity:
            fingerprint += "\n" + json.dumps([identity, [(i.mime_type, i.label, hashlib.sha256(i.data).hexdigest()) for i in images]], ensure_ascii=False)
        key = cache_key(model, role, scope, sha256_text(system_instruction), sha256_text(fingerprint))
        if key in self._rejected_keys:
            return None
        entry = self._entries.get(key)
        now = time.time()
        if entry and entry.expires_at - 60 > now:
            self._extend(entry, now)
            return entry
        if not self._should_create(key, expected_reuse, now):
            return None
        try:
            from google.genai import types  # local import: optional dependency at import time

            parts = [types.Part.from_text(text=sources_block)]
            for img in images:
                parts.append(types.Part.from_text(text=img.label))
                parts.append(types.Part.from_bytes(data=img.data, mime_type=img.mime_type))
            created = self.client.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    system_instruction=system_instruction,
                    contents=[types.Content(role="user", parts=parts)],
                    ttl=self.ttl,
                    display_name=f"claim-agent:{role}:{scope}:{key[:12]}",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "too small" in msg or "minimum" in msg or "not supported" in msg or "400" in msg:
                self._rejected_keys.add(key)
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
        with self._lock:
            self._invalidate(name)

    def _invalidate(self, name: str) -> None:
        for k, v in list(self._entries.items()):
            if v.name == name:
                del self._entries[k]
        self._save()
