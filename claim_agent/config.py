"""Configuration loading (claim-agent.yaml) with dotted-key overrides."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .models.prior_art import PriorSearchOptions

DEFAULT_CONFIG_NAME = "claim-agent.yaml"
ProviderKind = Literal["gemini", "claude_oauth", "codex_oauth", "openrouter", "antigravity"]


class ModelConfig(BaseModel):
    default: str = "gemini-3.8-flash"
    api_key_env: str = "GEMINI_API_KEY"
    max_output_tokens: int = 32768


class SubscriptionConfig(BaseModel):
    model: str = "default"            # default = CLI account default
    executable: str | None = None     # executable path, never a shell command
    timeout_s: int = Field(default=600, ge=10, le=3600)


class OpenRouterConfig(BaseModel):
    model: str = "deepseek/deepseek-v4.1-flash-1017"
    api_key_env: str = "OPENROUTER_API_KEY"
    max_output_tokens: int = 32000
    timeout_s: int = Field(default=900, ge=10, le=3600)


class ProviderConfig(BaseModel):
    # Claude and Codex run through their official CLI on a subscription login; there is no API-key transport.
    kind: ProviderKind = "gemini"
    claude_oauth: SubscriptionConfig = Field(default_factory=lambda: SubscriptionConfig(model="sonnet"))
    codex_oauth: SubscriptionConfig = Field(default_factory=lambda: SubscriptionConfig(model="default"))
    # Antigravity runs its official `agy` CLI on the account login that `agy` itself stores.
    antigravity: SubscriptionConfig = Field(default_factory=lambda: SubscriptionConfig(model="gemini-3.1-pro-high", timeout_s=900))
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)


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
        default_factory=lambda: {"STYLE_ONLY": 2, "DRAFTER": 2, "ARCHITECT": 1, "DEPENDENT_ARCHITECT": 1, "PREEXAM": 2}
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
    improve_dir: str = "improve"          # ACE failure records and the approval audit log


class RetentionConfig(BaseModel):
    days: float | None = None         # when set, `claim-agent runs purge` (and the web launcher) delete older runs/requests
    keep_locks: bool = True           # never auto-delete runs that reached a DRAFT/FINAL lock


class MaterialsConfig(BaseModel):
    max_image_side: int = 2048        # drawings are resized once at intake to this longest side (0 = never)
    files_api: bool = True            # upload each drawing once per content hash and reference it by URI


class LessonsConfig(BaseModel):
    inject: str = "approved_only"     # approved_only | none


class ImproveConfig(BaseModel):
    """ACE 개선 루프. 수집·제안만 자동이고 승인·주입은 사람이 한다.

    루프는 `claim-agent improve mine`이나 웹 Approval Inbox에서 부를 때만 돈다. 파이프라인
    종료 훅으로 자동 실행하지 않는다: 회귀 게이트의 live 실행이 실제 API 호출을 쓰고, 신호가
    대부분 여러 run에 걸친 반복이라 run마다 돌릴 실익이 없으며, engine을 건드리지 않기 위해서다.
    주기 실행이 필요하면 engine 훅 대신 바깥에서 이 명령을 예약한다.
    """

    reflect_llm: bool = False         # 회고에 모델 한 문단을 덧붙일지 (기본은 규칙 기반)
    min_repeat: int = 2               # 이 횟수 이상 반복해야 일반화 가능한 failure mode로 본다
    regression_mode: str = "replay"   # replay | live — regression gate 기본 실행 모드
    regression_cases: list[str] = Field(default_factory=list)   # 핵심 회귀 세트 (비우면 전체)


class TelemetryConfig(BaseModel):
    enabled: bool = True
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)


class GraphConfig(BaseModel):
    """claim_agent/graph: rule packs, invention graph idea sheet, precedent sheet. All off by default."""
    rule_pack: str = "off"            # off | on — secondary source files sliced to the role's sections (primary files stay whole)
    rule_pack_exempt_roles: list[str] = Field(default_factory=lambda: ["claim-success-reviewer"])
    invention: str = "off"            # off | on — one extraction call before ARCHITECT; idea sheet to both architects
    invention_role: str = "claim-architect"   # the extraction call borrows this role's provider and model
    invention_model: str | None = None        # None = that role's model
    precedent: str = "off"            # off | on — precedent sheet to dependent-claim-strategy-architect only (설계 §13 #10)
    precedent_path: str = "../cs2-spec-writer/graph/prosecution_v2/examples.json"
    # 판례 DB(cs2에서 복제, git 제외)와 교훈 기록. 주의 시트(S11)는 이 기록에서 만든다 — docs/caselaw.md
    caselaw_dir: str = "graph/patent_case_law"
    caselaw_lessons: str = "docs/caselaw-lessons.json"


class AppConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    lessons: LessonsConfig = Field(default_factory=LessonsConfig)
    improve: ImproveConfig = Field(default_factory=ImproveConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    # WIPS search + Jev screening before the design (docs/선행기술조사_사전심사_설계_2026-10.md); off unless a request enables it.
    prior_search: PriorSearchOptions = Field(default_factory=PriorSearchOptions)
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
        return self.model.api_key_env

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


def migrate_removed_providers(raw: dict[str, Any]) -> dict[str, Any]:
    """Configs written before the API-key Claude transport was removed keep working on the official CLI."""
    provider = raw.get("provider")
    if isinstance(provider, dict):
        provider.pop("anthropic", None)
        if provider.get("kind") == "anthropic":
            provider["kind"] = "claude_oauth"
    for role in (raw.get("roles") or {}).values():
        if isinstance(role, dict) and role.get("provider") == "anthropic":
            role["provider"] = "claude_oauth"
    return raw


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
    cfg = AppConfig.model_validate(migrate_removed_providers(raw))
    cfg.project_root = root
    return cfg
