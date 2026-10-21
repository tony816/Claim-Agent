from __future__ import annotations

from pathlib import Path

import pytest

from claim_agent.models.request import RunRequest
from claim_agent.runtime import build_runtime

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "eval" / "cases" / "sample-clip-holder"


@pytest.fixture(autouse=True)
def offline_model_catalog(monkeypatch):
    """Settings snapshots must not fetch live Gemini/OpenRouter lists (or write ROOT/.tui) in tests."""
    from claim_agent import model_settings

    monkeypatch.setattr(model_settings, "gemini_models", lambda cfg: [])
    monkeypatch.setattr(model_settings, "openrouter_models", lambda cfg: [])


@pytest.fixture
def project_root() -> Path:
    return ROOT


@pytest.fixture
def rt(tmp_path: Path):
    return build_runtime(ROOT, None, None, {"paths.runs_dir": str(tmp_path / "runs"), "paths.lessons_dir": str(tmp_path / "lessons"), "cache.enabled": False, "pipeline.max_concurrency": 1})


@pytest.fixture
def request_dep() -> RunRequest:
    return RunRequest.from_yaml(CASE / "request.yaml")


@pytest.fixture
def request_indep(request_dep: RunRequest) -> RunRequest:
    r = request_dep.model_copy()
    r.dependent = False
    return r
