"""AnthropicProvider against a fake SDK client: request shape, caching marks, structured outputs, tools, refusal, dispatch."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from claim_agent.chat import generate_turn, user_message
from claim_agent.config import load_config
from claim_agent.live_events import EventReader, EventWriter
from claim_agent.provider.anthropic_provider import AnthropicProvider, tool_definitions
from claim_agent.provider.base import CallSpec, GenParams, ImagePart, ProviderError
from claim_agent.runtime import live_provider

ROOT = Path(__file__).resolve().parents[1]


def _msg(text: str = "", stop: str = "end_turn", blocks=None, cache_read=0, model="claude-opus-5"):
    content = blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
    usage = SimpleNamespace(input_tokens=100, output_tokens=40, cache_read_input_tokens=cache_read, cache_creation_input_tokens=0)
    return SimpleNamespace(content=content, usage=usage, stop_reason=stop, model=model, stop_details=None)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __iter__(self):
        return iter([SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=getattr(b, "text", ""))) for b in self.msg.content if getattr(b, "type", "") == "text"])

    def get_final_message(self):
        return self.msg


class FakeMessages:
    """Records params; serves queued final messages; streams one text delta per final text block."""

    def __init__(self, queue, fail_first: Exception | None = None):
        self.queue, self.calls, self.fail_first = list(queue), [], fail_first

    @contextmanager
    def stream(self, **params):
        self.calls.append(params)
        if self.fail_first is not None:
            exc, self.fail_first = self.fail_first, None
            raise exc
        yield FakeStream(self.queue.pop(0))


def _client(queue, beta_queue=None, beta_fail=None):
    return SimpleNamespace(messages=FakeMessages(queue), beta=SimpleNamespace(messages=FakeMessages(beta_queue or [], beta_fail)), models=SimpleNamespace(list=lambda: [SimpleNamespace(id="claude-opus-5")]))


def _spec(**kw):
    base = dict(role="claim-architect", scope="INDEPENDENT", model="claude-opus-5", system_instruction="SYS", packet_text="PKT", sources_block="SRC" * 50,
                json_schema={"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}, gen=GenParams(0.2, "HIGH", 4000), use_cache=True)
    base.update(kw)
    return CallSpec(**base)


def test_request_shape_caching_and_structured_output(tmp_path):
    client = _client([], beta_queue=[_msg(json.dumps({"status": "PASS", "report_markdown": "ok"}), cache_read=300)])
    gp = AnthropicProvider(client, events=EventWriter(tmp_path / "ev.jsonl"))
    res = gp.generate(_spec(images=[ImagePart("image/png", b"img", "도1", "sha")]))
    params = client.beta.messages.calls[0]
    assert params["betas"] == ["server-side-fallback-2026-07-01"] and params["fallbacks"] == "default"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"} and params["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"][0]["content"][1]["text"] == "PKT" and params["messages"][0]["content"][2]["type"] == "image"
    assert params["thinking"] == {"type": "adaptive"} and params["output_config"]["effort"] == "high" and "temperature" not in params
    assert params["output_config"]["format"]["type"] == "json_schema" and params["max_tokens"] == 4000
    assert res.parsed["status"] == "PASS" and res.cache_hit and res.usage == {"prompt_tokens": 400, "cached_tokens": 300, "thoughts_tokens": 0, "output_tokens": 40, "total_tokens": 440}
    assert res.finish_reason == "STOP" and res.provider == "anthropic" and res.raw["structured_outputs"] is True
    kinds = [e["kind"] for e in EventReader(tmp_path / "ev.jsonl").read()]
    assert kinds == ["request", "delta", "response_end"]


def test_fallback_beta_rejection_downgrades_to_plain_messages():
    client = _client([_msg('{"status": "PASS"}')], beta_fail=RuntimeError("400 unknown parameter: fallbacks"))
    gp = AnthropicProvider(client)
    res = gp.generate(_spec())
    assert res.parsed == {"status": "PASS"} and gp._fallbacks_rejected and "fallbacks" not in client.messages.calls[0]


def test_schema_rejection_falls_back_to_prompt_json():
    client = _client([_msg('{"status": "REVIEW"}')], beta_fail=RuntimeError("400 output_config.format: unsupported schema"))
    gp = AnthropicProvider(client, fallbacks="none")
    client.messages.fail_first = RuntimeError("400 output_config.format: unsupported schema")
    res = gp.generate(_spec())
    params = client.messages.calls[-1]
    assert "format" not in params["output_config"] and "출력 형식" in params["messages"][0]["content"][1]["text"]
    assert res.parsed == {"status": "REVIEW"} and res.raw["structured_outputs"] is False and "claude-opus-5" in gp._schema_rejected


def test_tool_loop_executes_python_callables():
    log = []

    def search_style_corpus(mode: str, query: str, max_fragments: int = 2) -> dict:
        """검색"""
        log.append((mode, query))
        return {"results": [], "note": "예시 없음"}

    defs = tool_definitions([search_style_corpus])
    assert defs[0]["name"] == "search_style_corpus" and defs[0]["input_schema"]["required"] == ["mode", "query"] and defs[0]["input_schema"]["properties"]["max_fragments"]["type"] == "integer"
    use = SimpleNamespace(type="tool_use", id="tu1", name="search_style_corpus", input={"mode": "TERM_EXACT", "query": "조임 나사"})
    client = _client([_msg(blocks=[use], stop="tool_use"), _msg("SEARCH_DONE")])
    gp = AnthropicProvider(client, fallbacks="none")
    res = gp.generate(_spec(json_schema=None, tools=[search_style_corpus], phase="tool_phase"))
    assert res.text == "SEARCH_DONE" and log == [("TERM_EXACT", "조임 나사")]
    second = client.messages.calls[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-1]["content"][0]["type"] == "tool_result" and second[-1]["content"][0]["tool_use_id"] == "tu1"
    assert [c["name"] for c in res.function_calls if "name" in c] == ["search_style_corpus"]


def test_refusal_raises_and_blind_rejects_shared_context():
    client = _client([SimpleNamespace(content=[], usage=None, stop_reason="refusal", model="m", stop_details=SimpleNamespace(category="cyber"))])
    gp = AnthropicProvider(client, fallbacks="none")
    with pytest.raises(ProviderError, match="refused"):
        gp.generate(_spec())
    with pytest.raises(ProviderError, match="blind"):
        gp.generate(_spec(role="blind-claim-reconstruction-reviewer", cache_packet_text="X", packet_text="X"))


def test_config_dispatch_and_chat_history(monkeypatch, tmp_path):
    cfg = load_config(None, ROOT, {"provider.kind": "anthropic", "roles.claim-drafter.model": "claude-sonnet-5"})
    assert cfg.default_model == "claude-opus-5" and cfg.model_for("claim-drafter") == "claude-sonnet-5" and cfg.api_key_env == "ANTHROPIC_API_KEY"
    import claim_agent.provider.anthropic_provider as ap
    client = _client([_msg("안녕하세요")])
    monkeypatch.setattr(ap, "make_anthropic_client", lambda env, key=None: client)
    provider = live_provider(cfg)
    assert isinstance(provider, AnthropicProvider) and provider.fallbacks == "default"
    provider.fallbacks = "none"
    events = EventWriter(tmp_path / "ev.jsonl")
    history = [{"role": "user", "parts": [{"text": "이전 질문"}]}, {"role": "model", "parts": [{"text": "이전 답"}]}]
    result = generate_turn(provider, "claude-opus-5", history, user_message("이어서", "", []), events)
    msgs = client.messages.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"] and msgs[2]["content"][0]["text"] == "이어서"
    assert result["answer"] == "안녕하세요" and len(result["history"]) == 4
    with pytest.raises(ValueError, match="provider.kind"):
        live_provider(load_config(None, ROOT, {"provider.kind": "bogus"}))
