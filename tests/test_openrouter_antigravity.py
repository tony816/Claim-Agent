"""Offline OpenRouter and Antigravity transports: request shape, secrets, routing and settings."""
import json
import subprocess

import httpx
import pytest

from claim_agent.config import AppConfig, OpenRouterConfig, SubscriptionConfig
from claim_agent.model_settings import PROVIDERS, connection, settings_overrides, settings_snapshot
from claim_agent.provider import antigravity as agy
from claim_agent.provider import openrouter as orr
from claim_agent.provider.base import CallSpec, ImagePart, ProviderError
from claim_agent.runtime import live_provider
from claim_agent.store.telemetry import estimate_cost


def spec(**kwargs):
    return CallSpec(**{**dict(role="claim-drafter", scope="INDEPENDENT", model="deepseek/deepseek-v4.1-flash-1017",
                              system_instruction="역할 계약", packet_text="원자료 전문"), **kwargs})


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self.data, self.reason_phrase = status, data, "Bad"

    def json(self):
        return self.data


@pytest.fixture
def openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SECRET")
    sent = []

    def post(url, json, timeout, headers, verify):
        sent.append((url, json, headers))
        return FakeResponse(200, {"choices": [{"finish_reason": "stop", "message": {"content": '{"status":"REVIEW"}'}}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 4, "cost": 0.0021, "prompt_tokens_details": {"cached_tokens": 3}}})

    monkeypatch.setattr(orr.httpx, "post", post)
    return sent


def test_openrouter_sends_packet_images_and_reports_usd_cost(openrouter):
    provider = orr.OpenRouterProvider(OpenRouterConfig())
    result = provider.generate(spec(images=[ImagePart("image/png", b"png", "도면 1")], json_schema={"type": "object"}))
    url, body, headers = openrouter[0]
    assert url.endswith("/chat/completions") and headers["Authorization"] == "Bearer sk-or-SECRET"
    assert body["model"] == "deepseek/deepseek-v4.1-flash-1017" and body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"][0]["text"] == "역할 계약"
    user = body["messages"][1]["content"]
    assert "원자료 전문" in user[0]["text"] and user[-1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert result.parsed == {"status": "REVIEW"} and result.provider == "openrouter"
    assert "subscription_calls" not in result.usage and result.usage["cached_tokens"] == 3
    assert estimate_cost(spec().model, result.usage, {}) == 0.0021


def test_openrouter_errors_never_carry_the_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SECRET")
    monkeypatch.setattr(orr.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("sk-or-SECRET")))
    with pytest.raises(ProviderError) as exc:
        orr.OpenRouterProvider(OpenRouterConfig()).generate(spec())
    assert "SECRET" not in str(exc.value)
    monkeypatch.setattr(orr.httpx, "post", lambda *a, **k: FakeResponse(402, {"error": {"message": "Insufficient credits"}}))
    with pytest.raises(ProviderError, match="402: Insufficient credits"):
        orr.OpenRouterProvider(OpenRouterConfig()).generate(spec())


def test_openrouter_without_key_is_disconnected(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    status = orr.connection_status(OpenRouterConfig())
    assert not status["connected"] and "OPENROUTER_API_KEY" in status["message"]
    with pytest.raises(ProviderError, match="OPENROUTER_API_KEY"):
        orr.OpenRouterProvider(OpenRouterConfig()).generate(spec())


@pytest.fixture
def fake_agy(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(agy, "WORKSPACE", tmp_path / "agy_ws")
    monkeypatch.setattr(agy, "executable", lambda *_: "agy")
    monkeypatch.setattr(agy, "connection_status", lambda *_: {"connected": True})

    def run(command, kind, **kw):
        files = {p.name: p.read_text(encoding="utf-8") if p.suffix in {".md", ".json"} else p.read_bytes() for p in kw["cwd"].iterdir()}
        calls.append((command, kind, kw, files))
        if "--json-schema" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps({"status": "SUCCESS", "structured_output": {"status": "REVIEW"},
                                                                       "usage": {"input_tokens": 10, "output_tokens": 4}}), "")
        return subprocess.CompletedProcess(command, 0, "평문 답변", "")

    monkeypatch.setattr(agy, "run_cli", run)
    return calls


def test_antigravity_file_mode_passes_packet_and_drawings_as_files(fake_agy, monkeypatch):
    monkeypatch.setattr(agy, "file_mode", lambda: True)
    provider = agy.AntigravityProvider(SubscriptionConfig(model="gemini-3.1-pro-high"))
    result = provider.generate(spec(model="gemini-3.1-pro-high", images=[ImagePart("image/png", b"png", "도면 1")], json_schema={"type": "object"}))
    cmd, kind, kw, files = fake_agy[0]
    assert kind == "antigravity" and result.parsed == {"status": "REVIEW"} and result.usage["subscription_calls"] == 1
    assert cmd[cmd.index("--mode") + 1] == "plan" and cmd[cmd.index("--model") + 1] == "gemini-3.1-pro-high"
    assert "원자료 전문" in files["task.md"] and "역할 계약" in files["task.md"] and files["image-1.png"] == b"png"
    assert "원자료 전문" not in " ".join(cmd)            # the packet goes through task.md, not the command line
    assert "GEMINI_API_KEY" not in kw["env"] and not kw["cwd"].exists()


def test_antigravity_without_read_rule_refuses_drawings_and_long_packets(fake_agy, monkeypatch):
    monkeypatch.setattr(agy, "file_mode", lambda: False)
    provider = agy.AntigravityProvider(SubscriptionConfig(model="gemini-3.1-pro-high"))
    with pytest.raises(ProviderError, match="읽기 허용 규칙"):
        provider.generate(spec(images=[ImagePart("image/png", b"png", "도면 1")]))
    with pytest.raises(ProviderError, match="명령줄 한도"):
        provider.generate(spec(packet_text="가" * 40000))
    assert provider.generate(spec()).text == "평문 답변"     # a short text call runs inline
    assert not fake_agy[0][0][-1].startswith("-p=/")


def test_antigravity_signed_out_is_reported_without_the_login_url(monkeypatch):
    monkeypatch.setattr(agy, "executable", lambda *_: "agy")
    monkeypatch.setattr(agy, "settings", lambda: {})
    monkeypatch.setattr(agy, "run_cli", lambda *a, **k: subprocess.CompletedProcess([], 1, "Error: Please sign in https://accounts.google.com/o/oauth2/auth?x", ""))
    status = agy.connection_status()
    assert not status["connected"] and "accounts.google.com" not in status["message"]


def test_new_providers_route_per_role_and_save_from_settings(tmp_path):
    cfg = AppConfig.model_validate({"roles": {"claim-drafter": {"provider": "openrouter"}, "claim-architect": {"provider": "antigravity"},
                                              "claim-style-adjuster": {"provider": "claude_oauth", "model": "claude-fable-5-1"}}})
    assert cfg.model_for("claim-drafter") == "deepseek/deepseek-v4.1-flash-1017"
    assert cfg.model_for("claim-architect") == "gemini-3.1-pro-high" and cfg.model_for("claim-style-adjuster") == "claude-fable-5-1"
    router = live_provider(cfg)
    assert type(router.factory("openrouter")).__name__ == "OpenRouterProvider"
    assert type(router.factory("antigravity")).__name__ == "AntigravityProvider"
    snapshot = settings_snapshot(cfg)
    assert {"claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"} <= set(snapshot["providers"]["claude_oauth"]["models"])
    payload = {"provider": "openrouter", "defaults": snapshot["defaults"],
               "roles": {r["id"]: {"provider": r["provider"], "model": r["model"], "thinking_level": r["thinking_level"]} for r in snapshot["roles"]}}
    payload["defaults"]["openrouter"] = "anthropic/claude-opus-5.5"
    result = settings_overrides(cfg, payload)
    assert result["provider.kind"] == "openrouter" and result["provider.openrouter.model"] == "anthropic/claude-opus-5.5"
    assert set(PROVIDERS) >= {"antigravity", "openrouter"} and connection(cfg, "openrouter")["installed"]
