"""Shared context is reused losslessly; gates and fresh blind inputs stay intact."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from claim_agent.provider.base import ImagePart, ProviderError
from claim_agent.provider.cache import CacheManager
from claim_agent.provider.gemini import GeminiProvider
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.pipeline import packets
from tests.test_gemini_provider import _client, _spec
from tests import scripted_roles as R


def shared_spec(**overrides):
    return _spec(role="picture-claim-reconstruction-reviewer", scope="DEPENDENT_SINGLE",
                 sources_block="", packet_text="target-2\nSHARED\ncontract", cache_packet_text="SHARED",
                 cache_images=True, images=[ImagePart("image/png", b"drawing", "figure 1")],
                 run_id="run-a", **overrides)


def test_parallel_targets_reuse_exact_common_text_and_images(tmp_path):
    client = _client()
    cache = CacheManager(client, tmp_path / "cache.json")
    provider = GeminiProvider(client, cache)
    specs = [replace(shared_spec(), packet_text=f"target-{n}\nSHARED\ncontract") for n in range(2, 9)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(provider.generate, specs))
    assert all(r.cache_hit for r in results)
    assert len(client.caches.created) == 1
    cache_parts = client.caches.created[0][1].contents[0].parts
    assert "SHARED" in cache_parts[0].text
    assert cache_parts[-1].inline_data.data == b"drawing"
    for _, contents, _ in client.models.calls:
        assert len(contents[0].parts) == 1
        assert "SHARED" not in contents[0].parts[0].text
        assert "target-" in contents[0].parts[0].text
    assert all("SHARED" in s.packet_text for s in specs)  # Audit input untouched.


def test_shared_cache_changes_with_material_run_and_revision(tmp_path):
    client = _client()
    provider = GeminiProvider(client, CacheManager(client, tmp_path / "cache.json"))
    spec = shared_spec()
    for changed in [spec, replace(spec, run_id="run-b"),
                    replace(spec, images=[ImagePart("image/png", b"changed", "figure 1")]),
                    replace(spec, packet_text="target\nREVISION-2", cache_packet_text="REVISION-2")]:
        provider.generate(changed)
    assert len(client.caches.created) == 4


@pytest.mark.parametrize("failure", ["creation", "expired", "disabled"])
def test_cache_fallback_retains_every_required_input(tmp_path, failure):
    client = _client()
    if failure == "creation":
        client.caches.create = lambda **kw: (_ for _ in ()).throw(RuntimeError("400 too small"))
    if failure == "expired":
        original = client.models.generate_content
        def generate(model, contents, config):
            if config.cached_content:
                raise RuntimeError("404 cachedContent not found")
            return original(model, contents, config)
        client.models.generate_content = generate
    provider = GeminiProvider(client, CacheManager(client, tmp_path / "cache.json", enabled=failure != "disabled"))
    result = provider.generate(shared_spec())
    assert not result.cache_hit
    parts = client.models.calls[-1][1][0].parts
    assert parts[0].text == shared_spec().packet_text
    assert parts[-1].inline_data.data == b"drawing"


def test_blind_rejects_shared_context(tmp_path):
    client = _client()
    provider = GeminiProvider(client, CacheManager(client, tmp_path / "cache.json"))
    with pytest.raises(ProviderError, match="blind"):
        provider.generate(replace(shared_spec(), role="blind-claim-reconstruction-reviewer"))
    assert not client.models.calls and not client.caches.created


def test_one_rejected_cache_does_not_disable_other_roles(tmp_path):
    client = _client()
    original = client.caches.create
    def create(model, config):
        if "short" in config.contents[0].parts[0].text:
            raise RuntimeError("400 minimum token count")
        return original(model, config)
    client.caches.create = create
    cache = CacheManager(client, tmp_path / "cache.json")
    assert cache.get_or_create("model", "role-a", "scope", "sys", "short") is None
    assert cache.get_or_create("model", "role-b", "scope", "sys", "long-enough") is not None


def test_packet_sharing_preserves_gate_reports_and_excludes_blind(rt, request_dep):
    provider = ScriptedProvider(R.happy_script())
    engine = rt.engine(provider)
    state = engine.run(engine.start(request_dep, "transport-test"))
    assert state.dependent.draft_set_lock
    comparisons = [s for s in provider.calls if s.role == "picture-claim-reconstruction-reviewer" and s.scope == "DEPENDENT_SINGLE"]
    assert len(comparisons) == 3
    assert len({s.cache_packet_text for s in comparisons}) == 1
    for spec in comparisons:
        assert spec.cache_packet_text
        assert spec.packet_text.count(spec.cache_packet_text) == 1
        assert "DEPENDENT_DESIGN_GATE" in spec.cache_packet_text
        assert "종속항 OA 보고서 전문" in spec.cache_packet_text
        assert "봉인된 dependent blind snapshot 전문" not in spec.cache_packet_text
    for spec in provider.calls:
        if spec.role == "blind-claim-reconstruction-reviewer":
            assert not spec.cache_packet_text and not spec.cache_images and not spec.images and not spec.use_cache
