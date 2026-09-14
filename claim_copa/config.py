"""Configuration loading (claim-agent.yaml) with dotted-key overrides."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

DEFAULT_CONFIG_NAME = "claim-agent.yaml"


class ModelConfig(BaseModel):
    default: str = "gemini-3.8-flash"
    api_key_env: str = "GEMINI_API_KEY"
    max_output_tokens: int = 32768


class RoleConfig(BaseModel):
    model: str | None = None
    thinking_level: str = "HIGH"      # MINIMAL | LOW | MEDIUM | HIGH
    temperature: float = 0.2
    cache: bool = True
    tools: bool = True
    aux_mode: str = "two_phase"       # style adjuster only: two_phase | prefetch | off
    max_output_tokens: int | None = None


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl: str = "3600s"
    min_expected_reuse: int = 2


class RetryConfig(BaseModel):
    max_attempts: int = 3
    backoff_s: list[float] = Field(default_factory=lambda: [2, 8, 20])


class PipelineConfig(BaseModel):
    max_return_loops: dict[str, int] = Field(
        default_factory=lambda: {"STYLE_ONLY": 2, "DRAFTER": 2, "ARCHITECT": 1, "DEPENDENT_ARCHITECT": 1}
    )
    max_concurrency: int = 3
    expand_multi_dependent: bool = False
    retry: RetryConfig = Field(default_factory=RetryConfig)


class PathsConfig(BaseModel):
    roles_dir: str = ".claude/agents"
    sources_dir: str = "sources"
    runs_dir: str = "runs"
    lessons_dir: str = "lessons"
    eval_dir: str = "eval"
    experiments_dir: str = "experiments"


class LessonsConfig(BaseModel):
    inject: str = "approved_only"     # approved_only | none


class TelemetryConfig(BaseModel):
    enabled: bool = True
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)


class AppConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    lessons: LessonsConfig = Field(default_factory=LessonsConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    project_root: Path = Field(default_factory=Path.cwd, exclude=True)

    def role(self, name: str) -> RoleConfig:
        return self.roles.get(name, RoleConfig())

    def model_for(self, role_name: str) -> str:
        return self.role(role_name).model or self.model.default

    def path(self, key: str) -> Path:
        return (self.project_root / getattr(self.paths, key)).resolve()


def _set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
        if not isinstance(cur, dict):
            raise ValueError(f"cannot override non-mapping key {dotted}")
    cur[parts[-1]] = value


def apply_overrides(raw: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Return a deep copy of ``raw`` with ``{"a.b.c": v}`` overrides applied."""
    data = copy.deepcopy(raw)
    for key, value in (overrides or {}).items():
        _set_dotted(data, key, value)
    return data


def load_raw_config(path: Path | None, project_root: Path) -> dict[str, Any]:
    cfg_path = path or (project_root / DEFAULT_CONFIG_NAME)
    if path is None and not cfg_path.exists():
        cfg_path = project_root / "claim-copa.yaml"  # Existing installations.
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    return {}


def load_config(
    path: Path | None = None,
    project_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    root = (project_root or Path.cwd()).resolve()
    # Load only this project's file, preserving explicitly set environment values.
    # utf-8-sig also supports .env files saved with a BOM by Windows editors.
    load_dotenv(root / ".env", override=False, encoding="utf-8-sig")
    raw = apply_overrides(load_raw_config(path, root), overrides)
    cfg = AppConfig.model_validate(raw)
    cfg.project_root = root
    return cfg
