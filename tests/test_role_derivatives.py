"""The Codex/web copies of the role files must be generated from .claude/agents/*.md."""
from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_role_derivatives", ROOT / "scripts" / "build_role_derivatives.py")
build = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(build)


def test_codex_toml_matches_markdown_source():
    assert build.check_codex() == []
    for md in sorted((ROOT / ".claude" / "agents").glob("*.md")):
        body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", md.read_text(encoding="utf-8"), flags=re.S).strip() + "\n"
        data = tomllib.loads((ROOT / ".codex" / "agents" / f"{md.stem}.toml").read_text(encoding="utf-8"))
        assert data["developer_instructions"] == body
        assert data["name"] == md.stem and data["model_reasoning_effort"] == "high"


def test_toml_escaping_round_trips_quotes_and_backslashes():
    body = 'a "quoted" line\\with backslash and """triple""" quotes\n'
    text = build.render_codex({"name": "x", "description": 'd "q"', "effort": "low"}, body)
    data = tomllib.loads(text)
    assert data["developer_instructions"] == body and data["description"] == 'd "q"' and data["model_reasoning_effort"] == "low"


def test_web_bundle_builds_with_manifest(tmp_path):
    bundle = build.build_web_bundle(tmp_path)
    manifest = (bundle / "MANIFEST.sha256").read_text(encoding="utf-8")
    assert "roles/claim-architect.md" in manifest and "core/CLAUDE_CORE.md" in manifest and "sources/README.md" in manifest
    assert (bundle / "BUNDLE_MANIFEST.md").read_text(encoding="utf-8").count("source_manifest_digest") == 1
    assert bundle.with_suffix(".zip").exists()
    head = (bundle / "roles" / "claim-drafter.md").read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith("<!-- claim-agent-bundle:") and "canonical-path: .claude/agents/claim-drafter.md" in head
