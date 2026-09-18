"""The narrow model settings API; never accepts credentials or executable commands."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import AppConfig, apply_overrides
from .provider.subscription import KINDS, auth_home, connection_status
from .roles.registry import ROLE_NAMES

ROLE_LABELS = ["독립항 설계", "의미 초안 작성", "용어·스타일 조정", "성공조건 검수", "통사·범위 검수", "OA 전략 검수", "블라인드 복원", "도면·기준 비교", "종속항 전략 설계"]
PROVIDERS = {
    "gemini": {"label": "Gemini · API 키", "models": []},
    # CLI가 받는 별칭 프리셋(`claude --model`). 목록에 없는 ID는 모델 ID 직접 입력으로 지정한다.
    "claude_oauth": {"label": "Claude · 구독 OAuth", "models": ["default", "fable", "opus", "sonnet", "haiku"],
                     "install_url": "https://code.claude.com/docs/en/setup"},
    "codex_oauth": {"label": "Codex · 구독 OAuth", "models": ["default"],
                    "install_url": "https://developers.openai.com/codex/cli/"},
}
THINKING = ["MINIMAL", "LOW", "MEDIUM", "HIGH"]


def cached_codex_models() -> list[str]:
    """Only public model metadata; never inspect another CLI's credentials or sessions."""
    for path in (auth_home("codex_oauth") / "models_cache.json", Path.home() / ".codex" / "models_cache.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            models = [model_name(m["slug"]) for m in data.get("models", []) if m.get("visibility") == "list"]
            if models:
                return models
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return []


def model_name(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", value):
        raise ValueError("모델 ID를 확인하세요. 공백 없이 모델 ID 또는 default를 입력하세요.")
    return value


def merge_settings_file(root: Path, overrides: dict) -> Path:
    """Merge dotted overrides into .tui/model-settings.json, the file every runtime reads while loading config."""
    path = root / ".tui" / "model-settings.json"
    stored = {}
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({**stored, **overrides}, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return path


def provider_overrides(cfg: AppConfig, data: dict) -> dict:
    """The chat header switch: provider (and optionally its default model) only, role rows untouched."""
    kind = data.get("provider")
    if not isinstance(kind, str) or kind not in PROVIDERS:
        raise ValueError("제공자를 선택하세요.")
    result = {"provider.kind": kind}
    model = data.get("model")
    if model is not None:
        result["model.default" if kind == "gemini" else f"provider.{kind}.model"] = model_name(model)
    AppConfig.model_validate(apply_overrides(cfg.model_dump(), result))
    return result


def settings_overrides(cfg: AppConfig, data: dict) -> dict:
    kind = data.get("provider")
    if not isinstance(kind, str) or kind not in PROVIDERS:
        raise ValueError("제공자를 선택하세요.")
    defaults = data.get("defaults")
    if not isinstance(defaults, dict) or set(defaults) != set(PROVIDERS):
        raise ValueError("제공자별 기본 모델을 확인하세요.")
    result = {"provider.kind": kind}
    for provider, model in defaults.items():
        key = "model.default" if provider == "gemini" else f"provider.{provider}.model"
        result[key] = model_name(model)
    rows = data.get("roles")
    if not isinstance(rows, dict) or set(rows) != set(ROLE_NAMES):
        raise ValueError("9개 서브에이전트 설정을 모두 전달하세요.")
    for name, row in rows.items():
        if not isinstance(row, dict) or set(row) != {"provider", "model", "thinking_level"}:
            raise ValueError("에이전트에는 제공자·모델·추론 강도만 설정할 수 있습니다.")
        if row["provider"] is not None and (not isinstance(row["provider"], str) or row["provider"] not in PROVIDERS):
            raise ValueError("에이전트 제공자를 확인하세요.")
        if row["thinking_level"] not in THINKING:
            raise ValueError("추론 강도를 확인하세요.")
        for key in ("provider", "model", "thinking_level"):
            result[f"roles.{name}.{key}"] = model_name(row[key]) if key == "model" and row[key] is not None else row[key]
    AppConfig.model_validate(apply_overrides(cfg.model_dump(), result))
    return result


def connection(cfg, kind):
    if kind in KINDS:
        return connection_status(kind, getattr(cfg.provider, kind).executable)
    key = cfg.model.api_key_env
    present = bool(os.environ.get(key) or os.environ.get("GOOGLE_API_KEY"))
    return {"installed": True, "connected": present, "message": "API 키 설정됨" if present else f".env에 {key} 설정 필요"}


def settings_snapshot(cfg):
    providers = {}
    for kind, meta in PROVIDERS.items():
        default = cfg.default_model_for(kind)
        cached = cached_codex_models() if kind == "codex_oauth" else []
        models = list(dict.fromkeys([default, *meta["models"], *cached, *[r.model for r in cfg.roles.values() if r.model and (r.provider or cfg.provider.kind) == kind]]))
        providers[kind] = {**meta, "models": models}
    return {"provider": cfg.provider.kind, "defaults": {p: cfg.default_model_for(p) for p in PROVIDERS}, "providers": providers,
            "thinking_levels": THINKING, "roles": [{"id": name, "label": label, "provider": cfg.role(name).provider,
                "model": cfg.role(name).model, "thinking_level": cfg.role(name).thinking_level,
                "effective_provider": cfg.provider_for(name), "effective_model": cfg.model_for(name)} for name, label in zip(ROLE_NAMES, ROLE_LABELS, strict=True)]}
