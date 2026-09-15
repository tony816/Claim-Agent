"""Cache creation policy, TTL extension, prefix-first packets and the light style tool phase."""
from __future__ import annotations

import time
from types import SimpleNamespace

from claim_agent.provider.cache import CacheManager
from claim_agent.provider.scripted import ScriptedProvider

from . import scripted_roles as R


class Caches:
    def __init__(self):
        self.created, self.updated = [], []

    def create(self, model, config):
        self.created.append((model, config))
        return SimpleNamespace(name=f"cachedContents/{len(self.created)}", usage_metadata=SimpleNamespace(total_token_count=5000))

    def update(self, name, config):
        self.updated.append((name, config))

    def delete(self, name):
        pass


def _client():
    return SimpleNamespace(models=SimpleNamespace(), caches=Caches())


def test_auto_policy_skips_single_use_and_creates_on_second_sighting(tmp_path):
    client = _client()
    cm = CacheManager(client, tmp_path / "reg.json", "3600s", True, warm="auto", min_expected_reuse=2)
    assert cm.get_or_create("m", "oa-strategy-reviewer", "INDEPENDENT", "sys", "SRC" * 100, expected_reuse=1) is None
    assert not client.caches.created and cm.skipped == 1
    # the same bundle requested again within the TTL window (a second run) is worth caching
    assert cm.get_or_create("m", "oa-strategy-reviewer", "INDEPENDENT", "sys", "SRC" * 100, expected_reuse=1) is not None
    assert len(client.caches.created) == 1
    # a bundle with planned reuse in this run is created immediately (dependent picture fan-out)
    assert cm.get_or_create("m", "picture-claim-reconstruction-reviewer", "DEPENDENT_SINGLE", "sys", "SHARED", identity="run-1", expected_reuse=3) is not None
    assert len(client.caches.created) == 2
    # the seen registry survives a restart
    cm2 = CacheManager(client, tmp_path / "reg.json", "3600s", True, warm="auto")
    assert cm2.get_or_create("m", "syntax-scope-reviewer", "INDEPENDENT", "sys", "OTHER") is None
    assert CacheManager(client, tmp_path / "reg.json", "3600s", True, warm="auto").get_or_create("m", "syntax-scope-reviewer", "INDEPENDENT", "sys", "OTHER") is not None


def test_always_and_never_policies(tmp_path):
    client = _client()
    assert CacheManager(client, tmp_path / "a.json", warm="always").get_or_create("m", "r", "s", "sys", "SRC") is not None
    never = CacheManager(client, tmp_path / "b.json", warm="never")
    assert never.get_or_create("m", "r2", "s", "sys", "SRC") is None and len(client.caches.created) == 1


def test_ttl_is_extended_when_a_live_entry_is_reused_late(tmp_path):
    client = _client()
    cm = CacheManager(client, tmp_path / "reg.json", "3600s", True, warm="always")
    entry = cm.get_or_create("m", "r", "s", "sys", "SRC")
    entry.expires_at = time.time() + 600      # less than half the TTL left
    again = cm.get_or_create("m", "r", "s", "sys", "SRC")
    assert again is entry and client.caches.updated and again.expires_at > time.time() + 3000


def test_packets_put_stable_material_before_the_header_and_decisions_before_contract(rt, request_indep):
    prov = ScriptedProvider(R.happy_script(dependent=False))
    engine = rt.engine(prov)
    state = engine.run(engine.start(request_indep, "prefix-01"))
    assert state.outcome == "DRAFT_CLAIM_LOCK"
    for spec in prov.calls:
        if spec.role == "blind-claim-reconstruction-reviewer" or spec.phase == "tool_phase":
            continue
        text = spec.packet_text
        assert text.index("현재 발명 원자료") < text.index("## RUN_HEADER") < text.rindex("### 출력 계약")
    # decisions from a resume land right before the output contract, after the cacheable prefix
    state.notes.append("사용자 결정 (STYLE): 띄어쓰기 유지")
    packet = engine._with_decisions(state, "STABLE\n\n## RUN_HEADER\n\n### 출력 계약\n\nX")
    assert packet.index("STABLE") < packet.index("사용자 결정·메모") < packet.index("### 출력 계약")


def test_style_tool_phase_is_light_and_main_call_declares_reuse(rt, request_dep):
    prov = ScriptedProvider(R.happy_script(dependent=True))
    engine = rt.engine(prov)
    state = engine.run(engine.start(request_dep, "prefix-02"))
    assert state.outcome.endswith("DRAFT_DEPENDENT_SET_LOCK")
    phase_a = [c for c in prov.calls if c.phase == "tool_phase"]
    assert len(phase_a) == 2
    for c in phase_a:
        assert c.images == [] and c.use_cache is False and c.gen.thinking_level == "LOW" and c.gen.max_output_tokens == 2048
        assert "<<<FILE sources/07_용어표현_출처게이트.md" in c.sources_block and "<<<FILE sources/04_청구항_스타일가이드.md" not in c.sources_block
    main_style = [c for c in prov.calls if c.role == "claim-style-adjuster" and c.phase == "main"]
    assert all("<<<FILE sources/04_청구항_스타일가이드.md" in c.sources_block for c in main_style)
    pictures = [c for c in prov.calls if c.role == "picture-claim-reconstruction-reviewer" and c.scope == "DEPENDENT_SINGLE"]
    assert {c.expected_reuse for c in pictures} == {3}
    assert all(c.expected_reuse == 1 for c in prov.calls if c.role == "claim-architect")
