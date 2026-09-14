"""Project .env loading and API client integration, without network calls."""
import os
from unittest.mock import Mock

import pytest
from google import genai

from claim_agent.config import load_config
from claim_agent.provider.base import ProviderError
from claim_agent.provider.gemini import make_client


@pytest.fixture(autouse=True)
def clean_keys(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "CUSTOM_API_KEY", "PYTHON_DOTENV_DISABLED"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("name", ["GEMINI_API_KEY", "GOOGLE_API_KEY", "CUSTOM_API_KEY"])
def test_project_env_reaches_client(tmp_path, monkeypatch, name):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(f'{name}="test-key"\n', encoding="utf-8-sig")
    # A file in the working directory must not override the chosen project.
    (tmp_path / ".env").write_text(f"{name}=wrong-key\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    factory = Mock()
    monkeypatch.setattr(genai, "Client", factory)

    api_key_env = "CUSTOM_API_KEY" if name == "CUSTOM_API_KEY" else "GEMINI_API_KEY"
    cfg = load_config(project_root=project, overrides={"model.api_key_env": api_key_env})
    make_client(cfg.model.api_key_env)

    factory.assert_called_once()
    assert factory.call_args.kwargs["api_key"] == "test-key"
    assert "test-key" not in cfg.model_dump_json()


def test_existing_environment_takes_precedence(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("GEMINI_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "shell-key")
    load_config(project_root=tmp_path)
    assert os.environ["GEMINI_API_KEY"] == "shell-key"


@pytest.mark.parametrize("content", [None, "GEMINI_API_KEY=\n"])
def test_missing_or_empty_key_allows_config_but_blocks_live_client(tmp_path, content):
    if content is not None:
        (tmp_path / ".env").write_text(content, encoding="utf-8")
    cfg = load_config(project_root=tmp_path)
    with pytest.raises(ProviderError, match="GEMINI_API_KEY is not set"):
        make_client(cfg.model.api_key_env)
