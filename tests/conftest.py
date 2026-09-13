from __future__ import annotations

from pathlib import Path

import pytest

from claim_copa.models.request import RunRequest
from claim_copa.runtime import build_runtime

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "eval" / "cases" / "sample-clip-holder"


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
