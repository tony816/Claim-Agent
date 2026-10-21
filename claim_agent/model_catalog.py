"""Live model lists for the settings pickers: Gemini models.list and OpenRouter /models, newest first.

Fetched lists are cached in `.tui/model-catalog.json` for CACHE_TTL_S so opening the settings does not hit the network
every time. A failed fetch falls back to the last cached list, then to nothing (the static picker entries remain).
Only public model metadata is stored, never keys.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx

CACHE_TTL_S = 6 * 3600
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
# Gemini models that answer generateContent but are not text chat models a role can run on.
GEMINI_SKIP = re.compile(r"tts|image|banana|lyria|transcribe|robotics|computer-use|embedding|live|audio|customtools|deep-research|antigravity|aqa")


def _cache_path(root: Path) -> Path:
    return root / ".tui" / "model-catalog.json"


def _cached(root: Path, kind: str, fetch, refresh: bool = False) -> list[str]:
    path = _cache_path(root)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    entry = stored.get(kind) if isinstance(stored, dict) else None
    if entry and not refresh and time.time() - entry.get("fetched_at", 0) < CACHE_TTL_S:
        return entry["models"]
    try:
        models = fetch()
    except Exception:  # noqa: BLE001 - network, auth or SDK errors all mean "use what we had"
        return entry["models"] if entry else []
    if models:
        stored = {**(stored if isinstance(stored, dict) else {}), kind: {"fetched_at": time.time(), "models": models}}
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    return models


def _version(name: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def gemini_models(cfg, refresh: bool = False) -> list[str]:
    def fetch():
        if not (os.environ.get(cfg.model.api_key_env) or os.environ.get("GOOGLE_API_KEY")):
            return []
        from .provider.gemini import make_client

        names = []
        client = make_client(cfg.model.api_key_env)   # keep a reference: a collected client closes mid-pagination
        for m in client.models.list():
            name = str(getattr(m, "name", "")).replace("models/", "")
            if name.startswith("gemini-") and "generateContent" in (getattr(m, "supported_actions", None) or []) and not GEMINI_SKIP.search(name):
                names.append(name)
        # Newest version first; the moving *-latest aliases go after the pinned versions.
        return sorted(names, key=lambda n: ("latest" in n, -_version(n), n))

    return _cached(cfg.project_root, "gemini", fetch, refresh)


def openrouter_models(cfg, refresh: bool = False) -> list[str]:
    def fetch():
        from .provider.openrouter import tls_verify

        response = httpx.get(OPENROUTER_MODELS, timeout=20, verify=tls_verify())
        response.raise_for_status()
        rows = [m for m in response.json()["data"]
                if not m["id"].endswith(":batch") and "text" in ((m.get("architecture") or {}).get("output_modalities") or ["text"])]
        return [m["id"] for m in sorted(rows, key=lambda m: (-(m.get("created") or 0), m["id"]))]

    return _cached(cfg.project_root, "openrouter", fetch, refresh)
