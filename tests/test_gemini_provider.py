"""GeminiProvider wiring tests with a fake google.genai client (no network)."""
from __future__ import annotations

import json
import ssl
import sys
from types import SimpleNamespace

import pytest

from claim_agent.provider.base import CallSpec, GenParams
from claim_agent.provider.cache import CacheManager
from claim_agent.provider.gemini import GeminiProvider


@pytest.mark.skipif(sys.platform != "win32", reason="Windows certificate store")
def test_make_client_uses_verified_windows_trust_store(monkeypatch):
    from google import genai
    from claim_agent.provider.gemini import make_client

    captured = {}
    monkeypatch.setattr(genai, "Client", lambda **kwargs: captured.update(kwargs))
    make_client(api_key="test-key")
    context = captured["http_options"]["client_args"]["verify"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert captured["api_key"] == "test-key"


class FakeModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        text = json.dumps({"status": "PASS", "report_markdown": "ok"})
        usage = SimpleNamespace(prompt_token_count=10, cached_content_token_count=5, thoughts_token_count=1, candidates_token_count=3, total_token_count=19)
        return SimpleNamespace(text=text, usage_metadata=usage, candidates=[SimpleNamespace(finish_reason="STOP")], automatic_function_calling_history=[])

    def list(self):
        return [SimpleNamespace(name="models/gemini-3.8-flash"), SimpleNamespace(name="models/gemini-3.8-pro")]


class FakeCaches:
    def __init__(self):
        self.created = []

    def create(self, model, config):
        self.created.append((model, config))
        return SimpleNamespace(name=f"cachedContents/{len(self.created)}", usage_metadata=SimpleNamespace(total_token_count=1234))

    def delete(self, name):
        pass


def _client():
    return SimpleNamespace(models=FakeModels(), caches=FakeCaches())


def _spec(**kw):
    base = dict(role="claim-architect", scope="INDEPENDENT", model="gemini-3.8-flash", system_instruction="SYS", packet_text="PKT", sources_block="SRC" * 10, json_schema={"type": "object"}, gen=GenParams(0.2, "HIGH", 1000))
    base.update(kw)
    return CallSpec(**base)


def test_generate_uses_cache_and_json_schema(tmp_path):
    client = _client()
    cm = CacheManager(client, tmp_path / "reg.json", "3600s", True)
    gp = GeminiProvider(client, cm)
    res = gp.generate(_spec())
    assert res.parsed["status"] == "PASS" and res.cache_hit and res.cache_name == "cachedContents/1"
    model, contents, config = client.models.calls[0]
    assert config.cached_content == "cachedContents/1" and config.system_instruction is None
    assert config.response_mime_type == "application/json" and config.response_json_schema == {"type": "object"}
    assert config.thinking_config.thinking_level.value == "HIGH"
    # sources are NOT re-sent inline when cached
    assert all("SRC" not in (p.text or "") for p in contents[0].parts)
    assert res.usage["cached_tokens"] == 5
    # second call reuses the registry entry
    gp.generate(_spec())
    assert len(client.caches.created) == 1


def test_generate_inline_without_cache_and_blind_has_no_sources():
    client = _client()
    gp = GeminiProvider(client, None)
    gp.generate(_spec(use_cache=False))
    model, contents, config = client.models.calls[0]
    assert config.system_instruction == "SYS" and config.cached_content is None
    assert any("SRC" in (p.text or "") for p in contents[0].parts)
    gp.generate(_spec(role="blind-claim-reconstruction-reviewer", sources_block="", use_cache=False))
    _, contents2, _ = client.models.calls[1]
    assert len(contents2[0].parts) == 1


def test_thinking_level_fallback_to_budget():
    client = _client()
    calls = {"n": 0}
    orig = client.models.generate_content

    def flaky(model, contents, config):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("400 INVALID_ARGUMENT: thinking_level is not supported")
        return orig(model, contents, config)

    client.models.generate_content = flaky
    gp = GeminiProvider(client, None)
    res = gp.generate(_spec(use_cache=False))
    assert res.parsed["status"] == "PASS" and "gemini-3.8-flash" in gp._thinking_level_unsupported


def test_list_models():
    gp = GeminiProvider(_client(), None)
    assert gp.list_models() == ["gemini-3.8-flash", "gemini-3.8-pro"]
