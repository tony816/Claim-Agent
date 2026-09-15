"""Build every derived copy of the role files from the single source `.claude/agents/*.md`.

Derivatives:
  * `.codex/agents/<role>.toml`  — Codex agent definitions (name, description,
    model_reasoning_effort, developer_instructions = role body).
  * `dist/<bundle_name>-<bundle_version>/` (+ .zip) — the web project bundle
    (same layout as the former scripts/build-web-bundle.ps1), only with --web.

Run without options to (re)generate the Codex TOML files. Run with --check to
verify that the committed TOML files match the Markdown source; CI fails on
drift so a role edit can never silently diverge between execution surfaces.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / ".claude" / "agents"
CODEX = ROOT / ".codex" / "agents"
WEB = ROOT / "web"

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_BUNDLE_HEADER = re.compile(r"^<!-- claim-agent-bundle:[^\r\n]*-->\r?\n*")


def split_role(text: str) -> tuple[dict[str, str], str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text.strip() + "\n"
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text[m.end():].strip() + "\n"


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_multiline(value: str) -> str:
    # Basic multi-line string: escape backslashes and any run of three quotes.
    body = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return '"""\n' + body + '"""'


def render_codex(meta: dict[str, str], body: str) -> str:
    lines = [
        f"name = {toml_string(meta.get('name', ''))}",
        f"description = {toml_string(meta.get('description', ''))}",
        f"model_reasoning_effort = {toml_string(meta.get('effort', 'high'))}",
        f"developer_instructions = {toml_multiline(body)}",
        "",
    ]
    return "\n".join(lines)


def expected_codex() -> dict[Path, str]:
    out: dict[Path, str] = {}
    for md in sorted(AGENTS.glob("*.md")):
        meta, body = split_role(md.read_text(encoding="utf-8"))
        name = meta.get("name") or md.stem
        out[CODEX / f"{name}.toml"] = render_codex(meta, body)
    return out


def check_codex() -> list[str]:
    problems: list[str] = []
    expected = expected_codex()
    for path, content in expected.items():
        if not path.exists():
            problems.append(f"missing: {path.relative_to(ROOT)}")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"drift: {path.relative_to(ROOT)}")
    for stray in sorted(CODEX.glob("*.toml")):
        if stray not in expected:
            problems.append(f"stray (no source .md): {stray.relative_to(ROOT)}")
    return problems


def write_codex() -> int:
    CODEX.mkdir(parents=True, exist_ok=True)
    n = 0
    for path, content in expected_codex().items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            n += 1
    return n


# ----------------------------------------------------------------------------- web bundle
def read_version() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (WEB / "VERSION").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    for key in ("bundle_name", "bundle_version", "protocol_version", "source_set_id"):
        if not values.get(key):
            raise SystemExit(f"web/VERSION is missing required key: {key}")
    return values


def versioned_markdown(src: Path, canonical: str, bundle_version: str) -> str:
    content = _BUNDLE_HEADER.sub("", src.read_text(encoding="utf-8"))
    return f"<!-- claim-agent-bundle: {bundle_version}; canonical-path: {canonical} -->\n\n{content}"


def build_web_bundle(dist: Path | None = None) -> Path:
    values = read_version()
    version = values["bundle_version"]
    dist = dist or ROOT / "dist"
    bundle = dist / f"{values['bundle_name']}-{version}"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    for name in ("PROJECT_INSTRUCTIONS.md", "README.md", "BLIND_CHAT_PROMPT.md", "HANDOFF_TEMPLATES.md"):
        (bundle / name).write_text(versioned_markdown(WEB / name, f"web/{name}", version), encoding="utf-8")
    shutil.copyfile(WEB / "VERSION", bundle / "VERSION")
    (bundle / "core").mkdir()
    (bundle / "core" / "CLAUDE_CORE.md").write_text(versioned_markdown(ROOT / "CLAUDE.md", "CLAUDE.md", version), encoding="utf-8")
    (bundle / "roles").mkdir()
    for md in sorted(AGENTS.glob("*.md")):
        (bundle / "roles" / md.name).write_text(versioned_markdown(md, f".claude/agents/{md.name}", version), encoding="utf-8")
    (bundle / "sources").mkdir()
    for md in sorted((ROOT / "sources").glob("*.md")):
        (bundle / "sources" / md.name).write_text(versioned_markdown(md, f"sources/{md.name}", version), encoding="utf-8")

    instructions = (bundle / "PROJECT_INSTRUCTIONS.md").read_text(encoding="utf-8")
    for token in (version, values["source_set_id"], "WEB_SINGLE_CHAT", "DRAFT-SELF", "LOCK_MISSING_OR_STALE", "STYLE_PASS", "CLAIM_STYLE_GATE", "SUCCESS_PASS", "claim-success-reviewer", "DEPENDENT_STYLE_PASS", "DEPENDENT_SUCCESS_PASS", "DEPENDENT_DESIGN_GATE", "DEPENDENT_RECONSTRUCTION_GATE", "DRAFT_DEPENDENT_SET_LOCK"):
        if token not in instructions:
            raise SystemExit(f"PROJECT_INSTRUCTIONS is missing required token: {token}")
    style = (bundle / "roles" / "claim-style-adjuster.md").read_text(encoding="utf-8")
    for token in ("sources/README.md", "sources/청구항_예시검색_라우팅인덱스.md", "sources/청구항_문체학습용_분야별검색최적화본.md", "조건부 보조 소스: NOT_ACTIVATED"):
        if token not in style:
            raise SystemExit(f"Bundled style adjuster is missing required source token: {token}")
    success = (bundle / "roles" / "claim-success-reviewer.md").read_text(encoding="utf-8")
    for token in ("success_scope: INDEPENDENT | DEPENDENT_SET", "sources/README.md", "sources/독립항_작성_성공조건.md", "sources/05_종속항_전개패턴_가이드.md", "success_record_id", "dependent_success_record_id"):
        if token not in success:
            raise SystemExit(f"Bundled success reviewer is missing required contract token: {token}")
    if "05_종속항_전개패턴_가이드.md" not in (bundle / "roles" / "syntax-scope-reviewer.md").read_text(encoding="utf-8"):
        raise SystemExit("Bundled syntax reviewer is missing the dependent source 05 contract.")

    files = sorted(p for p in bundle.rglob("*") if p.is_file())
    fingerprint = "\n".join(f"{p.relative_to(bundle).as_posix()}\0{hashlib.sha256(p.read_bytes()).hexdigest()}" for p in files) + "\n"
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    manifest_md = (
        f"<!-- claim-agent-bundle: {version} -->\n\n# Bundle manifest\n\n"
        f"- bundle_name: {values['bundle_name']}\n- bundle_version: {version}\n- protocol_version: {values['protocol_version']}\n"
        f"- source_set_id: {values['source_set_id']}\n- source_manifest_digest: {digest}\n"
        f"- file_count_including_manifests: {len(files) + 2}\n- integrity_file: MANIFEST.sha256\n\n"
        "`MANIFEST.sha256` lists SHA-256 hashes for every packaged file except the hash list itself. "
        "The bundle version header on each Markdown file is the runtime version check used in web projects.\n"
    )
    (bundle / "BUNDLE_MANIFEST.md").write_text(manifest_md, encoding="utf-8")
    files = sorted(p for p in bundle.rglob("*") if p.is_file())
    (bundle / "MANIFEST.sha256").write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(bundle).as_posix()}\n" for p in files), encoding="utf-8")
    zip_path = bundle.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(bundle.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(bundle).as_posix())
    return bundle


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="verify .codex/agents/*.toml match .claude/agents/*.md; exit 1 on drift")
    ap.add_argument("--web", action="store_true", help="also build the web bundle under dist/")
    ap.add_argument("--dist", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.check:
        problems = check_codex()
        for p in problems:
            print(p)
        print("role derivatives: " + ("DRIFT" if problems else "in sync"))
        return 1 if problems else 0
    n = write_codex()
    print(f"codex agents: {n} file(s) written, {len(expected_codex())} total")
    if args.web:
        bundle = build_web_bundle(args.dist)
        print(f"web bundle: {bundle} (+ .zip)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
