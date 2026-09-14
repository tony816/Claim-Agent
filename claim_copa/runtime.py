"""Factory that wires config, roles, sources, lessons, provider and engine."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig, load_config
from .improve.experiments import Variant, compare_envelopes
from .improve.lessons import LessonStore
from .models.envelope import RoleEnvelope
from .pipeline.engine import PipelineEngine
from .provider.base import CallSpec, LLMProvider
from .provider.cache import CacheManager
from .provider.replay import RecordingProvider, ReplayProvider
from .roles.prompt import PromptAssembler
from .roles.registry import ROLE_NAMES, RoleRegistry
from .sources.registry import ADAPTER_VERSION, SourceSet, source_set_id
from .store.runstore import RunStore


@dataclass
class Runtime:
    cfg: AppConfig
    roles: RoleRegistry
    sources: SourceSet
    lessons: LessonStore
    store: RunStore
    assembler: PromptAssembler
    source_set_id: str
    lessons_hash: str
    variant: Variant

    def engine(self, provider: LLMProvider, shadow_hook=None) -> PipelineEngine:
        return PipelineEngine(self.cfg, provider, self.roles, self.sources, self.store, self.assembler, self.source_set_id, self.variant.variant_id, self.lessons_hash, shadow_hook)


def build_runtime(project_root: Path, config_path: Path | None = None, variant: Variant | None = None, overrides: dict[str, Any] | None = None) -> Runtime:
    variant = variant or Variant.default()
    merged = dict(variant.overrides)
    merged.update(overrides or {})
    cfg = load_config(config_path, project_root, merged)
    roles = RoleRegistry.load(cfg.path("roles_dir"))
    sources = SourceSet.load(cfg.path("sources_dir"), cfg.project_root)
    lessons = LessonStore(cfg.path("lessons_dir"))
    inject = cfg.lessons.inject == "approved_only" and variant.lessons_include != []
    lesson_texts = lessons.texts_by_role(ROLE_NAMES, inject, variant.lessons_include)
    for role, suffix in variant.prompt_suffix.items():
        lesson_texts[role] = (lesson_texts.get(role, "") + "\n\n" + suffix).strip()
    lessons_hash = lessons.digest(inject, variant.lessons_include)
    if variant.prompt_suffix:
        from .models.ids import sha256_text

        lessons_hash = sha256_text(lessons_hash + variant.variant_id)
    assembler = PromptAssembler(sources, lesson_texts)
    ssid = source_set_id(sources.digest(), roles.digest(), lessons_hash, ADAPTER_VERSION)
    store = RunStore(cfg.path("runs_dir"))
    return Runtime(cfg, roles, sources, lessons, store, assembler, ssid, lessons_hash, variant)


def make_provider(rt: Runtime, mode: str = "gemini", fixtures: Path | None = None, strict_replay: bool = False, api_key: str | None = None) -> LLMProvider:
    """mode: gemini | replay | record."""
    if mode == "replay":
        if fixtures is None:
            raise ValueError("--replay requires a fixtures directory")
        return ReplayProvider(fixtures, strict=strict_replay)
    from .provider.gemini import GeminiProvider, make_client
    from .live_events import EventWriter

    client = make_client(rt.cfg.model.api_key_env, api_key)
    cache = CacheManager(client, rt.cfg.path("runs_dir") / ".cache-registry.json", rt.cfg.cache.ttl, rt.cfg.cache.enabled)
    gp = GeminiProvider(client, cache, rt.cfg.pipeline.retry.max_attempts, rt.cfg.pipeline.retry.backoff_s,
                        events=EventWriter.from_env(rt.cfg.model.api_key_env))
    if mode == "record":
        if fixtures is None:
            raise ValueError("--record requires a fixtures directory")
        return RecordingProvider(gp, fixtures)
    return gp


def make_shadow_hook(shadow_rt: Runtime, shadow_provider: LLMProvider, diffs: list[dict[str, Any]]):
    """Run the variant on the identical packet after each baseline call; record only."""

    def hook(spec: CallSpec, env: RoleEnvelope, state) -> None:
        role = shadow_rt.roles.get(spec.role)
        from .models.enums import Scope

        prompt = shadow_rt.assembler.assemble(role, Scope(spec.scope))
        rc = shadow_rt.cfg.role(spec.role)
        sspec = copy.copy(spec)
        sspec.system_instruction = prompt.system_instruction
        sspec.sources_block = prompt.sources_block
        sspec.model = shadow_rt.cfg.model_for(spec.role)
        sspec.gen = copy.copy(spec.gen)
        sspec.gen.temperature = rc.temperature
        sspec.gen.thinking_level = rc.thinking_level
        sspec.phase = "shadow"
        res = shadow_provider.generate(sspec)
        senv = None
        if res.parsed:
            try:
                senv = RoleEnvelope.model_validate(res.parsed)
            except Exception:  # noqa: BLE001
                senv = None
        diff = compare_envelopes(env, senv) if senv else {"error": "shadow envelope invalid"}
        entry = {"run_id": state.run_id, "seq": spec.seq, "role": spec.role, "variant_id": shadow_rt.variant.variant_id, "diff": diff, "usage": res.usage, "latency_ms": res.latency_ms}
        diffs.append(entry)
        shadow_rt.store.write_shadow(state.run_id, spec.seq, spec.role, {**entry, "envelope": senv.model_dump(mode="json") if senv else None})

    return hook
