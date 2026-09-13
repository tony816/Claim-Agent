"""Generate offline replay fixtures for eval/cases/sample-clip-holder from the scripted roles.

Usage: python scripts/make_sample_fixtures.py
The fixtures let `claim-copa eval run --case sample-clip-holder` and
`claim-copa run --replay eval/cases/sample-clip-holder/fixtures ...` work without an API key.
Replace them with real recordings via `--record` once a GEMINI_API_KEY is available.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from claim_copa.models.request import RunRequest  # noqa: E402
from claim_copa.provider.replay import RecordingProvider  # noqa: E402
from claim_copa.provider.scripted import ScriptedProvider  # noqa: E402
from claim_copa.runtime import build_runtime  # noqa: E402
import scripted_roles as R  # noqa: E402


def main() -> None:
    case = ROOT / "eval" / "cases" / "sample-clip-holder"
    fixtures = case / "fixtures"
    if fixtures.exists():
        shutil.rmtree(fixtures)
    tmp = Path(tempfile.mkdtemp())
    rt = build_runtime(ROOT, None, None, {"paths.runs_dir": str(tmp / "runs"), "cache.enabled": False, "pipeline.max_concurrency": 1})
    provider = RecordingProvider(ScriptedProvider(R.happy_script(dependent=True)), fixtures)
    engine = rt.engine(provider)
    req = RunRequest.from_yaml(case / "request.yaml")
    state = engine.run(engine.start(req, "fixture-gen"))
    print("outcome:", state.outcome, "fixtures:", len(list(fixtures.glob('*.json'))))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
