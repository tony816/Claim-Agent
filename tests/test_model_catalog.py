"""Offline model pickers: live Gemini/OpenRouter lists, the freshest Codex cache, the newest CLI and .env reloads."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from claim_agent import model_catalog as catalog
from claim_agent import model_settings as ms
from claim_agent.config import AppConfig
from claim_agent.provider import subscription as sub


def cfg_at(root: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.project_root = root
    return cfg


class Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [
            {"id": "old/model", "created": 100, "architecture": {"output_modalities": ["text"]}},
            {"id": "new/model", "created": 300, "architecture": {"output_modalities": ["text"]}},
            {"id": "new/model:batch", "created": 400},
            {"id": "image/only", "created": 500, "architecture": {"output_modalities": ["image"]}},
        ]}


def test_openrouter_list_is_newest_first_cached_and_survives_outages(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(catalog.httpx, "get", lambda *a, **k: calls.append(1) or Response())
    cfg = cfg_at(tmp_path)
    assert catalog.openrouter_models(cfg) == ["new/model", "old/model"]
    assert catalog.openrouter_models(cfg) == ["new/model", "old/model"] and len(calls) == 1   # served from the cache
    stored = json.loads((tmp_path / ".tui" / "model-catalog.json").read_text(encoding="utf-8"))
    stored["openrouter"]["fetched_at"] = time.time() - catalog.CACHE_TTL_S - 1
    (tmp_path / ".tui" / "model-catalog.json").write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(catalog.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert catalog.openrouter_models(cfg) == ["new/model", "old/model"]                      # stale beats empty
    assert catalog.openrouter_models(cfg_at(tmp_path / "fresh")) == []


def test_openrouter_labels_carry_name_price_and_context_for_search(tmp_path, monkeypatch):
    assert catalog.openrouter_label({"id": "deepseek/v4", "name": "DeepSeek: V4 Flash", "context_length": 1048576,
                                     "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}}) == "V4 Flash · $0.15/$0.6 per 1M · 1M ctx"
    assert catalog.openrouter_label({"id": "x/free", "context_length": 262144, "pricing": {"prompt": "0", "completion": "0"}}) == "x/free · 무료 · 262K ctx"
    assert catalog.openrouter_label({"id": "x/bare"}) == "x/bare"
    # Lists cached by earlier versions (bare IDs, preformatted pairs) still load.
    (tmp_path / ".tui").mkdir()
    (tmp_path / ".tui" / "model-catalog.json").write_text(json.dumps({"openrouter": {"fetched_at": time.time(), "models": [
        "old/id", ["pair/id", "Pair"], {"id": "raw/id", "name": "V: Raw", "pricing": {"prompt": "0", "completion": "0"}}]}}), encoding="utf-8")
    cfg = cfg_at(tmp_path)
    assert catalog.openrouter_catalog(cfg) == [["old/id", ""], ["pair/id", "Pair"], ["raw/id", "Raw · 무료"]]
    monkeypatch.setattr(ms, "openrouter_catalog", catalog.openrouter_catalog)
    assert ms.openrouter_labels(cfg) == {"pair/id": "Pair", "raw/id": "Raw · 무료"}


def test_gemini_list_keeps_text_models_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "offline")
    rows = [SimpleNamespace(name=f"models/{n}", supported_actions=a) for n, a in [
        ("gemini-2.5-pro", ["generateContent"]), ("gemini-4-argon", ["generateContent"]), ("gemini-pro-latest", ["generateContent"]),
        ("gemini-3.8-flash", ["generateContent"]), ("gemini-3.8-flash-tts", ["generateContent"]), ("gemini-embedding-2", ["embedContent"]),
        ("gemini-3.8-live", ["bidiGenerateContent"]), ("veo-3.1-generate-preview", ["predictLongRunning"])]]
    import claim_agent.provider.gemini as gemini

    monkeypatch.setattr(gemini, "make_client", lambda *a: SimpleNamespace(models=SimpleNamespace(list=lambda: rows)))
    assert catalog.gemini_models(cfg_at(tmp_path)) == ["gemini-4-argon", "gemini-3.8-flash", "gemini-2.5-pro", "gemini-pro-latest"]
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert catalog.gemini_models(cfg_at(tmp_path / "nokey")) == []


def test_freshest_codex_cache_wins(tmp_path, monkeypatch):
    def cache(path: Path, fetched: str, slugs: list[str]):
        path.mkdir(parents=True, exist_ok=True)
        rows = [{"slug": s, "visibility": "list", "priority": i} for i, s in enumerate(slugs)] + [{"slug": "hidden", "visibility": "hide"}]
        (path / "models_cache.json").write_text(json.dumps({"fetched_at": fetched, "models": rows}), encoding="utf-8")

    monkeypatch.setattr(ms, "auth_home", lambda kind: tmp_path / "app")
    monkeypatch.setattr(ms.Path, "home", lambda: tmp_path / "home")
    cache(tmp_path / "app", "2026-09-18T00:00:00Z", ["gpt-6-astra"])
    cache(tmp_path / "home" / ".codex", "2026-10-21T00:00:00Z", ["gpt-6.1-sol", "gpt-6-astra"])
    assert ms.cached_codex_models() == ["gpt-6.1-sol", "gpt-6-astra"]


def test_newest_discovered_cli_is_used_and_a_configured_path_is_kept(tmp_path, monkeypatch):
    old, new = tmp_path / "old" / "codex.exe", tmp_path / "Local" / "OpenAI" / "Codex" / "bin" / "abc" / "codex.exe"
    for path in (old, new):
        path.parent.mkdir(parents=True)
        path.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(sub.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(sub.shutil, "which", lambda name: str(old) if name in {"codex", str(old)} else None)
    monkeypatch.setattr(sub, "cli_version", lambda command: (0, 163, 0) if command[0] == str(new) else (0, 154, 0))
    assert sub.executable("codex_oauth") == [str(new)]
    assert sub.executable("codex_oauth", str(old)) == [str(old)]


def test_connection_reads_a_key_added_to_env_after_start(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.delenv("OPENROUTER_API_KEY")       # registered, so the key load_dotenv sets is removed after the test
    cfg = cfg_at(tmp_path)
    assert not ms.connection(cfg, "openrouter")["connected"]
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-later\n", encoding="utf-8")
    assert ms.connection(cfg, "openrouter")["connected"]


@pytest.mark.parametrize("value, ok", [("~anthropic/claude-opus-latest", True), ("minimax/minimax-m3.1-flash:free", True),
                                       ("~", False), ("x; echo token", False), ("-rf", False)])
def test_model_ids_accept_openrouter_aliases_only(value, ok):
    if ok:
        assert ms.model_name(value) == value
    else:
        with pytest.raises(ValueError):
            ms.model_name(value)
