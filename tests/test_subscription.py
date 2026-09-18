"""Offline subscription transport, context isolation and mixed-provider regressions."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from claim_agent.config import AppConfig, SubscriptionConfig, load_config
from claim_agent.model_settings import PROVIDERS, provider_overrides, settings_overrides
from claim_agent.provider import subscription as sub
from claim_agent.provider.base import CallResult, CallSpec, ImagePart, ProviderError
from claim_agent.runtime import live_provider
from claim_agent.store.telemetry import estimate_cost
from claim_agent.web import Workspace


def spec(**kwargs):
    return CallSpec(**{**dict(role="claim-drafter", scope="INDEPENDENT", model="default", system_instruction="역할 계약", packet_text="원자료 전문"), **kwargs})


@pytest.fixture
def fake_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(sub, "executable", lambda *_: ["official-cli"])
    monkeypatch.setattr(sub, "connection_status", lambda *_: {"connected": True})

    def run(command, kind, **kw):
        calls.append((command, kind, kw))
        if kind == "claude_oauth":
            output = json.dumps({"type": "system", "subtype": "init"}) + "\n" + json.dumps({"type": "result", "subtype": "success", "result": '{"status":"REVIEW"}', "usage": {"input_tokens": 10, "output_tokens": 4}})
        else:
            Path(command[command.index("--output-last-message") + 1]).write_text('{"status":"REVIEW"}', encoding="utf-8")
            output = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}})
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(sub, "run_cli", run)
    return calls


@pytest.mark.parametrize("kind", sub.KINDS)
def test_transport_preserves_unicode_images_and_schema(kind, fake_cli):
    provider = sub.SubscriptionProvider(kind, SubscriptionConfig())
    result = provider.generate(spec(images=[ImagePart("image/png", b"image bytes", "도면 1")], json_schema={"type": "object"}))
    cmd, _, kw = fake_cli[0]
    assert result.parsed == {"status": "REVIEW"} and result.provider == kind
    assert result.usage["total_tokens"] == 14 and result.usage["subscription_calls"] == 1
    assert "원자료 전문" in kw["stdin"]
    assert not kw["cwd"].exists()  # call-specific material is removed
    assert "원자료 전문" not in " ".join(cmd)  # never sent through command line
    if kind == "claude_oauth":
        message = json.loads(kw["stdin"])["message"]
        assert message["content"][-1]["source"]["type"] == "base64"
        assert cmd[cmd.index("--tools") + 1] == "" and "--bare" not in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd
    else:
        assert "--image" in cmd and "--ignore-user-config" in cmd and "features.shell_tool=false" in cmd
    assert estimate_cost("default", result.usage, {"default": {"input_per_m": 100}}) is None


@pytest.mark.parametrize("field,value", [("sources_block", "SECRET"), ("history", [{"role": "user", "parts": []}]),
    ("images", [ImagePart("image/png", b"image")]), ("cache_packet_text", "SECRET"), ("tools", [print])])
def test_blind_rejects_context_before_any_cli_call(field, value, fake_cli):
    with pytest.raises(ProviderError, match="blind"):
        sub.SubscriptionProvider("codex_oauth", SubscriptionConfig()).generate(spec(role="blind-claim-reconstruction-reviewer", **{field: value}))
    assert not fake_cli


def test_fresh_blind_has_no_history_or_project_path(fake_cli):
    provider = sub.SubscriptionProvider("codex_oauth", SubscriptionConfig())
    provider.generate(spec(role="blind-claim-reconstruction-reviewer"))
    provider.generate(spec(role="blind-claim-reconstruction-reviewer"))
    assert fake_cli[0][2]["cwd"] != fake_cli[1][2]["cwd"]
    assert all("HISTORY" not in call[2]["stdin"] for call in fake_cli)


def test_tool_roundtrip_only_executes_given_callable(monkeypatch):
    monkeypatch.setattr(sub, "connection_status", lambda *_: {"connected": True})
    seen, packets = [], []

    def search_style_corpus(query: str):
        seen.append(query)
        return {"result": "허용된 정확 검색 결과"}

    definitions = sub.tool_definitions([search_style_corpus])
    assert definitions[0]["name"] == "search_style_corpus" and definitions[0]["input_schema"]["required"] == ["query"]
    replies = iter(['{"claim_agent_tool":{"name":"search_style_corpus","arguments":{"query":"조임나사"}}}', "SEARCH_DONE"])
    provider = sub.SubscriptionProvider("claude_oauth", SubscriptionConfig())

    def once(_spec, packet, images):
        packets.append(packet)
        return next(replies), {"prompt_tokens": 10}

    monkeypatch.setattr(provider, "_once", once)
    result = provider.generate(spec(tools=[search_style_corpus], phase="tool_phase"))
    assert seen == ["조임나사"] and "허용된 정확 검색 결과" in packets[1]
    assert result.function_calls[1]["response_of"] == "search_style_corpus"
    assert result.usage["prompt_tokens"] == 20


def test_auth_env_does_not_inherit_keys_settings_or_parent_session(monkeypatch, tmp_path):
    monkeypatch.setattr(sub, "auth_home", lambda kind: tmp_path / kind)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_THREAD_ID", "CLAUDECODE"):
        monkeypatch.setenv(key, "secret-value")
    env = sub.cli_env("claude_oauth")
    assert "secret-value" not in env.values()
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude_oauth")


@pytest.mark.parametrize("kind,stdout,stderr,connected", [
    ("claude_oauth", '{"loggedIn":true,"authMethod":"claude.ai"}', "", True),
    ("claude_oauth", '{"loggedIn":true,"authMethod":"api_key"}', "", False),
    ("codex_oauth", "", "Logged in using ChatGPT", True),
    ("codex_oauth", "Logged in using an API key", "", False),
])
def test_status_accepts_only_subscription_auth(monkeypatch, kind, stdout, stderr, connected):
    monkeypatch.setattr(sub, "executable", lambda *_: ["cli"])
    monkeypatch.setattr(sub, "run_cli", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout, stderr))
    result = sub.connection_status(kind)
    assert result["connected"] is connected and "authMethod" not in str(result)


def test_role_dispatch_repair_and_aux_do_not_need_default_api_key(monkeypatch):
    from claim_agent import runtime
    cfg = AppConfig.model_validate({"roles": {"claim-drafter": {"provider": "codex_oauth", "model": "custom-model"},
                                            "claim-style-adjuster": {"provider": "claude_oauth"}}})
    seen = []

    def factory(config, kind, *args):
        assert kind != "gemini"
        return SimpleNamespace(generate=lambda call: (seen.append((kind, call.phase, call.model)) or CallResult(text="ok")))

    monkeypatch.setattr(runtime, "_live_provider", factory)
    provider = live_provider(cfg)
    for phase in ("main", "repair"):
        provider.generate(spec(model=cfg.model_for("claim-drafter"), phase=phase))
    provider.generate(spec(role="claim-style-adjuster", model=cfg.model_for("claim-style-adjuster"), phase="tool_phase"))
    assert seen == [("codex_oauth", "main", "custom-model"), ("codex_oauth", "repair", "custom-model"), ("claude_oauth", "tool_phase", "sonnet")]


def settings_payload(workspace):
    data = workspace.model_settings()
    return {"provider": "codex_oauth", "defaults": data["defaults"], "roles": {
        r["id"]: {k: r[k] for k in ("provider", "model", "thinking_level")} for r in data["roles"]}}


def test_settings_persist_without_replacing_role_contract(tmp_path):
    workspace = Workspace(tmp_path)
    payload = settings_payload(workspace)
    payload["roles"]["claim-drafter"].update(provider="claude_oauth", model="opus", thinking_level="MEDIUM")
    workspace.save_model_settings(payload)
    cfg = load_config(project_root=tmp_path)
    assert cfg.provider.kind == "codex_oauth" and cfg.model_for("claim-drafter") == "opus"
    assert cfg.role("claim-drafter").thinking_level == "MEDIUM"
    assert cfg.role("claim-drafter").tools is True
    assert cfg.model_for("claim-architect") == "default"
    assert load_config(project_root=tmp_path, overrides={"provider.kind": "gemini"}).provider.kind == "gemini"


def test_settings_reject_invalid_inputs_and_running_save(tmp_path):
    workspace = Workspace(tmp_path)
    payload = settings_payload(workspace)
    payload["defaults"]["codex_oauth"] = 'x; echo token'
    with pytest.raises(ValueError, match="모델 ID"):
        workspace.save_model_settings(payload)
    assert not (tmp_path / ".tui/model-settings.json").exists()
    payload = settings_payload(workspace)
    payload["roles"]["claim-drafter"]["tools"] = False
    with pytest.raises(ValueError):
        settings_overrides(workspace.cfg, payload)
    workspace.jobs["running"] = {"done": False}
    with pytest.raises(ValueError, match="실행 중"):
        workspace.save_model_settings(settings_payload(workspace))


def test_header_switch_changes_provider_mid_chat_and_keeps_role_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("claim_agent.model_settings.connection_status", lambda *a, **k: {"installed": True, "connected": False, "message": "구독 계정 로그인이 필요합니다."})
    workspace = Workspace(tmp_path)
    payload = settings_payload(workspace)
    payload["roles"]["claim-drafter"].update(provider="claude_oauth", model="opus")
    workspace.save_model_settings(payload)

    snapshot = workspace.switch_provider({"provider": "gemini"})
    assert snapshot["provider"] == "gemini" and set(snapshot["connection"]) >= {"connected", "message"}
    cfg = load_config(project_root=tmp_path)
    assert cfg.provider.kind == "gemini" and cfg.model_for("claim-drafter") == "opus"        # 역할별 지정은 그대로
    assert cfg.provider_for("claim-drafter") == "claude_oauth"

    snapshot = workspace.switch_provider({"provider": "claude_oauth", "model": "haiku"})
    assert snapshot["connection"]["connected"] is False and "로그인" in snapshot["connection"]["message"]
    cfg = load_config(project_root=tmp_path)
    assert cfg.provider.kind == "claude_oauth" and cfg.default_model == "haiku" and cfg.role("claim-drafter").model == "opus"

    with pytest.raises(ValueError, match="모델 ID"):
        workspace.switch_provider({"provider": "claude_oauth", "model": "opus; echo token"})
    workspace.jobs["running"] = {"done": False}
    with pytest.raises(ValueError, match="실행 중"):
        workspace.switch_provider({"provider": "gemini"})
    assert load_config(project_root=tmp_path).provider.kind == "claude_oauth"


def test_claude_api_key_provider_is_gone_and_old_configs_move_to_the_cli(tmp_path):
    assert "anthropic" not in PROVIDERS and set(PROVIDERS) == {"gemini", "claude_oauth", "codex_oauth"}
    workspace = Workspace(tmp_path)
    with pytest.raises(ValueError, match="제공자"):
        provider_overrides(workspace.cfg, {"provider": "anthropic"})
    (tmp_path / "claim-agent.yaml").write_text(
        "provider:\n  kind: anthropic\n  anthropic:\n    model: claude-opus-5\n    api_key_env: ANTHROPIC_API_KEY\n"
        "roles:\n  claim-drafter: {provider: anthropic}\n", encoding="utf-8")
    cfg = load_config(project_root=tmp_path)
    assert cfg.provider.kind == "claude_oauth" and cfg.provider_for("claim-drafter") == "claude_oauth"
    assert not hasattr(cfg.provider, "anthropic") and cfg.api_key_env == "GEMINI_API_KEY"


def test_cli_failure_does_not_leak_raw_error(fake_cli, monkeypatch):
    monkeypatch.setattr(sub, "run_cli", lambda *a, **k: subprocess.CompletedProcess([], 1, "token=TOP_SECRET", "TOP_SECRET"))
    with pytest.raises(ProviderError) as exc:
        sub.SubscriptionProvider("codex_oauth", SubscriptionConfig()).generate(spec())
    assert "TOP_SECRET" not in str(exc.value)
