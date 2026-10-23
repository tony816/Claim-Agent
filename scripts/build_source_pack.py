"""Bundle the documents one role reads into a few pack files it can open in a single turn.

A role call used to spend 10-30 tool turns opening `sources/*.md`, the invention
sources and the upstream records one by one. The orchestrator now runs this
script once per role call and passes the part paths; the role reads every part
in one parallel turn. Each file is copied in full (never summarized) under a
header with its path and sha256, so the pack is the same text the role would
have read itself (CLAUDE.md "컨텍스트 전달과 요청 범위").

    python scripts/build_source_pack.py --role claim-success-reviewer --scope DEPENDENT_SET \
        --extra run/src.md run/dd4_design_gate.md --out run/packs/success-dr4
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"

S_README = "README.md"
S_SUCCESS = "독립항_작성_성공조건.md"
S04, S05, S06, S07, S08 = ("04_청구항_스타일가이드.md", "05_종속항_전개패턴_가이드.md", "06_OA_심사리스크_체크리스트.md",
                           "07_용어표현_출처게이트.md", "08_종속항_기술기여_게이트.md")
S09, S10, S11, S12, S13 = ("09_사전심사_기준.md", "10_심사이력_경험기준.md", "11_판례_청구항_주의시트.md",
                           "12_판례_특허성_유형기준.md", "13_판례_권리행사_작성기준.md")

# Policy documents each role must read: (always, DEPENDENT_SET only). Conditional reads
# (example routing index, style corpus) stay with the role and are not packed.
ROLE_SOURCES: dict[str, tuple[list[str], list[str]]] = {
    "claim-architect": ([S_README, S_SUCCESS, S11, S13], []),
    "dependent-claim-strategy-architect": ([S_README, S_SUCCESS, S08, S05, S07, S10, S12, S13], []),
    "claim-drafter": ([S_SUCCESS, S11, S13], [S08, S05]),
    "claim-style-adjuster": ([S_README, S07, S04], []),
    "claim-success-reviewer": ([S_README, S_SUCCESS, S07, S04, S11, S13], [S08, S05]),
    "syntax-scope-reviewer": ([S07, S04], [S08, S05]),
    "oa-strategy-reviewer": ([S06, S12], [S08]),
    "picture-claim-reconstruction-reviewer": ([], []),
    "invention-problem-finder": ([], []),
    "prior-art-pre-examiner": ([S_README, S_SUCCESS, S09, S10, S12], []),
    "prior-art-searcher": ([], []),
}
NO_PACK = {"blind-claim-reconstruction-reviewer"}   # blind sees only claim text; never pack for it

MAX_PART_LINES = 1500
MAX_PART_BYTES = 60_000


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack_files(role: str, scope: str, extras: list[Path]) -> list[Path]:
    if role in NO_PACK:
        raise SystemExit(f"{role}: blind input must stay claim text only; no source pack")
    if role not in ROLE_SOURCES:
        raise SystemExit(f"unknown role: {role}")
    always, dependent = ROLE_SOURCES[role]
    names = always + (dependent if scope == "DEPENDENT_SET" else [])
    return [SOURCES / n for n in names] + extras


def blocks(files: list[Path]) -> list[tuple[str, str, str]]:
    out = []
    for f in files:
        text = f.read_text(encoding="utf-8").replace("\r\n", "\n")
        try:
            label = f.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            label = f.resolve().as_posix()
        text = text if text.endswith("\n") else text + "\n"
        out.append((label, sha256(text), text))
    return out


def split_parts(entries: list[tuple[str, str, str]]) -> list[str]:
    """Cut on line boundaries so every part fits one Read call; a file may span parts."""
    parts, cur, cur_lines, cur_bytes = [], [], 0, 0

    def flush():
        nonlocal cur, cur_lines, cur_bytes
        if cur:
            parts.append("".join(cur))
        cur, cur_lines, cur_bytes = [], 0, 0

    for label, digest, text in entries:
        lines = text.splitlines(keepends=True)
        header = f"=== SOURCE: {label} | sha256 {digest} | {len(lines)} lines ===\n"
        pending = [header] + lines + [f"=== END: {label} ===\n"]
        for i, line in enumerate(pending):
            size = len(line.encode("utf-8"))
            if cur and (cur_lines + 1 > MAX_PART_LINES or cur_bytes + size > MAX_PART_BYTES):
                flush()
                if 0 < i < len(pending) - 1:
                    cur.append(f"=== CONTINUED: {label} ===\n")
                    cur_lines, cur_bytes = 1, len(cur[0].encode("utf-8"))
            cur.append(line)
            cur_lines += 1
            cur_bytes += size
    flush()
    return parts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--role", required=True)
    ap.add_argument("--scope", choices=["INDEPENDENT", "DEPENDENT_SET"], default="INDEPENDENT")
    ap.add_argument("--extra", nargs="*", default=[], type=Path, help="invention sources and upstream records, copied in full")
    ap.add_argument("--out", required=True, type=Path, help="output prefix; parts are <out>.partN.md")
    args = ap.parse_args(argv)

    entries = blocks(pack_files(args.role, args.scope, args.extra))
    manifest = "".join(f"- {label} | sha256 {digest}\n" for label, digest, _ in entries)
    pack_id = sha256(manifest)[:16]
    parts = split_parts(entries)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, body in enumerate(parts, 1):
        head = f"# SOURCE_PACK {pack_id} | role {args.role} | scope {args.scope} | part {n}/{len(parts)}\n"
        if n == 1:
            head += "## manifest\n" + manifest + "\n"
        p = args.out.parent / f"{args.out.name}.part{n}.md"
        p.write_text(head + body, encoding="utf-8")
        paths.append(p)
    print(f"source_pack_id: {pack_id}")
    for p in paths:
        print(p.as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
