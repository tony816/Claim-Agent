"""Configuration loading (claim-agent.yaml) with dotted-key overrides."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

DEFAULT_CONFIG_NAME = "claim-agent.yaml"
ProviderKind = Literal["gemini", "anthropic", "claude_oauth", "codex_oauth"]


class ModelConfig(BaseModel):
    default: str = "gemini-3.8-flash"
    api_key_env: str = "GEMINI_API_KEY"
    max_output_tokens: int = 32768


class AnthropicConfig(BaseModel):
    model: str = "claude-opus-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    thinking: str = "adaptive"        # adaptive | off (Claude 4.6+ models; effort comes from the role's thinking_level)
    fallbacks: str = "default"        # default | none — server-side refusal fallbacks (beta)
    structured_outputs: bool = True   # output_config.format; falls back to a prompt JSON instruction if the schema is refused
    send_temperature: bool = False    # sampling params are rejected on Claude Opus 5 / Fable 5


class SubscriptionConfig(BaseModel):
    model: str = "default"            # default = CLI account default
    executable: str | None = None     # executable path, never a shell command
    timeout_s: int = Field(default=600, ge=10, le=3600)


class ProviderConfig(BaseModel):
    kind: ProviderKind = "gemini"
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    claude_oauth: SubscriptionConfig = Field(default_factory=lambda: SubscriptionConfig(model="sonnet"))
    codex_oauth: SubscriptionConfig = Field(default_factory=lambda: SubscriptionConfig(model="default"))


class RoleConfig(BaseModel):
    provider: ProviderKind | None = None
    model: str | None = None
    thinking_level: str = "HIGH"      # MINIMAL | LOW | MEDIUM | HIGH
    temperature: float = 0.2
    cache: bool = True
    tools: bool = True
    aux_mode: str = "two_phase"       # style adjuster only: two_phase | off
    aux_thinking_level: str = "LOW"   # tool phase (corpus search decision) runs light: no images, 07+README only
    aux_max_output_tokens: int = 2048
    max_output_tokens: int | None = None


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl: str = "3600s"
    # Creation policy. "auto": create a context cache only when the same role×scope bundle is expected to be
    # reused at least `min_expected_reuse` times in this run, or when the same bundle was requested once before
    # within the TTL window (second sighting = proven reuse). "always": create on first use (batch/eval sessions).
    # "never": never create; existing live entries are still used.
    warm: str = "auto"                 # auto | always | never
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
    # Budget guard: a run halts with HALTED_BUDGET_LIMIT when any limit is exceeded (None = unlimited).
    max_calls: int | None = None
    max_total_tokens: int | None = None       # prompt + output + thoughts, cached tokens included
    max_cost_usd: float | None = None         # requires telemetry.pricing for the model


class PathsConfig(BaseModel):
    roles_dir: str = ".claude/agents"
    sources_dir: str = "sources"
    runs_dir: str = "runs"
    lessons_dir: str = "lessons"
    eval_dir: str = "eval"
    experiments_dir: str = "experiments"


class RetentionConfig(BaseModel):
    days: float | None = None         # when set, `claim-agent runs purge` (and the web launcher) delete older runs/requests
    keep_locks: bool = True           # never auto-delete runs that reached a DRAFT/FINAL lock


class MaterialsConfig(BaseModel):
    max_image_side: int = 2048        # drawings are resized once at intake to this longest side (0 = never)
    files_api: bool = True            # upload each drawing once per content hash and reference it by URI


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
    materials: MaterialsConfig = Field(default_factory=MaterialsConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    project_root: Path = Field(default_factory=Path.cwd, exclude=True)

    def role(self, name: str) -> RoleConfig:
        return self.roles.get(name, RoleConfig())

    def model_for(self, role_name: str) -> str:
        return self.role(role_name).model or self.default_model_for(self.provider_for(role_name))

    def provider_for(self, role_name: str) -> str:
        return self.role(role_name).provider or self.provider.kind

    def default_model_for(self, kind: str) -> str:
        return self.model.default if kind == "gemini" else getattr(self.provider, kind).model

    @property
    def default_model_key(self) -> str:
        return "model.default" if self.provider.kind == "gemini" else f"provider.{self.provider.kind}.model"

    @property
    def default_model(self) -> str:
        return self.default_model_for(self.provider.kind)

    @property
    def api_key_env(self) -> str:
        return self.provider.anthropic.api_key_env if self.provider.kind == "anthropic" else self.model.api_key_env

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
    raw = load_raw_config(path, root)
    settings = root / ".tui" / "model-settings.json"
    if settings.exists():
        raw = apply_overrides(raw, json.loads(settings.read_text(encoding="utf-8")))
    raw = apply_overrides(raw, overrides)
    cfg = AppConfig.model_validate(raw)
    cfg.project_root = root
    return cfg
