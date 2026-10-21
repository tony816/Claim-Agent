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


def _price(per_token) -> str:
    try:
        value = float(per_token) * 1e6
    except (TypeError, ValueError):
        return "?"
    return "0" if value == 0 else f"{value:.2f}".rstrip("0").rstrip(".")


def _context(tokens) -> str:
    if not isinstance(tokens, int) or tokens <= 0:
        return ""
    return (f"{round(tokens / 1_000_000, 1):g}M" if tokens >= 1_000_000 else f"{tokens // 1000}K") + " ctx"


def openrouter_label(model: dict) -> str:
    """'DeepSeek V4.1 Flash 1017 · $0.15/$0.6 per 1M · 1M ctx' — name without the vendor prefix, input/output price."""
    name = str(model.get("name") or model["id"]).split(": ", 1)[-1]
    pricing = model.get("pricing") or {}
    prompt, completion = _price(pricing.get("prompt")), _price(pricing.get("completion"))
    price = "무료" if prompt == completion == "0" else "" if prompt == completion == "?" else f"${prompt}/${completion} per 1M"
    return " · ".join(part for part in (name, price, _context(model.get("context_length"))) if part)


def openrouter_catalog(cfg, refresh: bool = False) -> list[list[str]]:
    """[[id, label], …] newest first. The label carries the display name, price and context for the picker's search."""
    def fetch():
        from .provider.openrouter import tls_verify

        response = httpx.get(OPENROUTER_MODELS, timeout=20, verify=tls_verify())
        response.raise_for_status()
        rows = [m for m in response.json()["data"]
                if not m["id"].endswith(":batch") and "text" in ((m.get("architecture") or {}).get("output_modalities") or ["text"])]
        # Raw fields, not the label: a later label format applies to a list cached before it.
        return [{"id": m["id"], "name": m.get("name"), "pricing": {k: (m.get("pricing") or {}).get(k) for k in ("prompt", "completion")},
                 "context_length": m.get("context_length")} for m in sorted(rows, key=lambda m: (-(m.get("created") or 0), m["id"]))]

    # Lists cached by earlier versions hold bare IDs or preformatted [id, label] pairs.
    rows = _cached(cfg.project_root, "openrouter", fetch, refresh)
    return [[row["id"], openrouter_label(row)] if isinstance(row, dict) else row if isinstance(row, list) else [row, ""] for row in rows]


def openrouter_models(cfg, refresh: bool = False) -> list[str]:
    return [model_id for model_id, _ in openrouter_catalog(cfg, refresh)]
