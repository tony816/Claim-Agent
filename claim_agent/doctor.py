"""`claim-agent doctor`: environment, sources, roles, schema and (optionally) live API probes."""
from __future__ import annotations

import os
from typing import Any

from .models.envelope import ENVELOPE_JSON_SCHEMA, schema_depth
from .roles.registry import ROLE_NAMES
from .runtime import Runtime

CONTRACT_TOKENS: dict[str, list[str]] = {
    "CLAUDE.md": ["PRE_STYLE — NOT_GATE_ELIGIBLE", "style_record_id", "claim-success-reviewer", "CLAIM_STYLE_GATE", "NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE", "DEPENDENT_RECONSTRUCTION_GATE"],
    ".claude/agents/claim-success-reviewer.md": ["name: claim-success-reviewer", "success_scope: INDEPENDENT | DEPENDENT_SET", "success_record_id", "dependent_success_record_id", "syntax 진행 가능"],
    ".claude/agents/claim-style-adjuster.md": ["name: claim-style-adjuster", "STYLE_ONLY_REVISION", "CLAIM_STYLE_GATE", "FINALIZED_FOR_SUCCESS", "조건부 보조 소스: NOT_ACTIVATED"],
    ".claude/agents/blind-claim-reconstruction-reviewer.md": ["tools: []", "claim_scope: INDEPENDENT | DEPENDENT_SINGLE", "dependent_blind_snapshot_id", "1회독 도식화 가능성"],
    ".claude/agents/picture-claim-reconstruction-reviewer.md": ["claim_scope: INDEPENDENT | DEPENDENT_SINGLE", "NON_PATENT_TECHNICAL_READER_GATE", "GEOMETRIC_OBJECT_GATE"],
    ".claude/agents/oa-strategy-reviewer.md": ["OA_DRAFT_GATE", "OA_FINAL_GATE", "DEPENDENT_OA_DRAFT_GATE", "DEPENDENT_OA_FINAL_GATE"],
    "sources/README.md": ["필수 소비·검증 역할", "claim-success-reviewer", "claim-style-adjuster가 조건부 하드 로딩"],
}


def run_doctor(rt: Runtime, live: bool = False, model: str | None = None, contracts: bool = False) -> list[tuple[str, str, str]]:
    """Return rows of (check, status, detail). status ∈ OK | WARN | FAIL | SKIP."""
    rows: list[tuple[str, str, str]] = []
    cfg = rt.cfg
    rows.append(("config", "OK", f"provider={cfg.provider.kind} model={cfg.default_model} cache={cfg.cache.enabled} runs={cfg.path('runs_dir')}"))
    rows.append(("roles", "OK", f"{len(rt.roles.roles)} role files loaded ({', '.join(ROLE_NAMES[:3])}…)"))
    rows.append(("sources", "OK", f"{len(rt.sources.files)} source files; source_set_id={rt.source_set_id}"))
    rows.append(("lessons", "OK", f"approved={len(rt.lessons.list('approved'))} pending={len(rt.lessons.list('pending'))} hash={rt.lessons_hash[:12]}"))
    depth = schema_depth(ENVELOPE_JSON_SCHEMA)
    rows.append(("envelope schema", "OK" if depth <= 3 else "WARN", f"depth={depth}, properties={len(ENVELOPE_JSON_SCHEMA['properties'])}"))
    key_present = bool(os.environ.get(cfg.api_key_env) or (cfg.provider.kind == "gemini" and os.environ.get("GOOGLE_API_KEY")) or (cfg.provider.kind == "anthropic" and os.environ.get("ANTHROPIC_AUTH_TOKEN")))
    rows.append(("api key", "OK" if key_present else "WARN", f"{cfg.api_key_env} {'set' if key_present else 'not set — live calls impossible; use --replay'}"))

    if contracts:
        root = cfg.project_root
        for rel, tokens in CONTRACT_TOKENS.items():
            p = root / rel
            if not p.exists():
                rows.append((f"contract {rel}", "FAIL", "missing"))
                continue
            text = p.read_text(encoding="utf-8")
            missing = [t for t in tokens if t not in text]
            rows.append((f"contract {rel}", "OK" if not missing else "FAIL", "all tokens present" if not missing else f"missing: {missing}"))

    if live:
        if not key_present:
            rows.append(("live probe", "SKIP", "no API key"))
            return rows
        if cfg.provider.kind == "anthropic":
            try:
                from .runtime import live_provider

                names = live_provider(cfg).list_models()
                target = model or cfg.default_model
                rows.append(("models.list", "OK" if target in names else "WARN", f"{len(names)} models; configured '{target}' {'found' if target in names else 'NOT found'}" + ("" if target in names else suggest_model(target, names))))
                if not cfg.telemetry.pricing.get(target):
                    rows.append(("pricing", "WARN", f"telemetry.pricing has no entry for '{target}'; cost estimates will be empty"))
            except Exception as exc:  # noqa: BLE001
                rows.append(("live probe", "FAIL", str(exc)[:300]))
            return rows
        try:
            from .provider.gemini import GeminiProvider, make_client

            client = make_client(cfg.model.api_key_env)
            gp = GeminiProvider(client)
            names = gp.list_models()
            target = model or cfg.model.default
            rows.append(("models.list", "OK" if target in names else "WARN", f"{len(names)} models; configured '{target}' {'found' if target in names else 'NOT found'}" + ("" if target in names else suggest_model(target, names))))
            if not cfg.telemetry.pricing.get(target):
                rows.append(("pricing", "WARN", f"telemetry.pricing has no entry for '{target}'; cost estimates will be empty (see claim-agent.yaml)"))
            rows.extend(_probe_json(client, target))
            rows.extend(_probe_cache(client, target, cfg.cache.ttl))
        except Exception as exc:  # noqa: BLE001
            rows.append(("live probe", "FAIL", str(exc)[:300]))
    return rows


def suggest_model(target: str, names: list[str]) -> str:
    """Closest available model ids for a configured id that does not exist."""
    import difflib

    close = difflib.get_close_matches(target, names, n=3, cutoff=0.5)
    if not close:
        family = target.split("-")[0]
        close = [n for n in names if n.startswith(family)][:3]
    return f"; closest: {', '.join(close)}" if close else ""


def _probe_json(client: Any, model: str) -> list[tuple[str, str, str]]:
    from google.genai import types

    try:
        resp = client.models.generate_content(
            model=model,
            contents="다음 JSON만 출력: {\"ok\": true, \"lang\": \"ko\"}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}, "lang": {"type": "string"}}, "required": ["ok"]},
                thinking_config=types.ThinkingConfig(thinking_level="LOW"),
            ),
        )
        return [("json+thinking_level probe", "OK", (resp.text or "")[:80])]
    except Exception as exc:  # noqa: BLE001
        return [("json+thinking_level probe", "WARN", str(exc)[:200] + " (provider falls back to thinking_budget)")]


def _probe_cache(client: Any, model: str, ttl: str) -> list[tuple[str, str, str]]:
    from google.genai import types

    try:
        filler = ("청구항 스타일 캐시 프로브. " * 400)
        c = client.caches.create(model=model, config=types.CreateCachedContentConfig(contents=[filler], ttl="120s", display_name="claim-agent-doctor"))
        try:
            client.caches.delete(name=c.name)
        except Exception:  # noqa: BLE001
            pass
        return [("context cache probe", "OK", f"created {c.name} (ttl config {ttl})")]
    except Exception as exc:  # noqa: BLE001
        return [("context cache probe", "WARN", str(exc)[:200] + " (provider falls back to inline sources)")]
