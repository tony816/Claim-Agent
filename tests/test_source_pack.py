"""Source packs must carry every document in full and never feed the blind reviewer."""
from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_source_pack", ROOT / "scripts" / "build_source_pack.py")
pack = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(pack)


def test_pack_round_trips_every_file_and_fits_one_read(tmp_path):
    extra = tmp_path / "src.md"
    extra.write_bytes("발명 원자료\r\n둘째 줄".encode())
    assert pack.main(["--role", "claim-success-reviewer", "--scope", "DEPENDENT_SET", "--extra", str(extra), "--out", str(tmp_path / "p")]) == 0
    parts = sorted(tmp_path.glob("p.part*.md"), key=lambda p: int(re.search(r"part(\d+)", p.name).group(1)))
    assert len(parts) > 1
    for p in parts:
        text = p.read_text(encoding="utf-8")
        assert len(text.splitlines()) <= pack.MAX_PART_LINES + 40 and len(text.encode("utf-8")) <= pack.MAX_PART_BYTES + 4000
    joined = "".join(p.read_text(encoding="utf-8").split("\n", 1)[1] for p in parts)
    joined = re.sub(r"## manifest\n(?:- .*\n)+\n", "", joined, count=1)
    joined = re.sub(r"=== CONTINUED: .* ===\n", "", joined)
    found = re.findall(r"=== SOURCE: (.*?) \| sha256 (\w+) \| \d+ lines ===\n(.*?)=== END: \1 ===\n", joined, re.S)
    expected = pack.pack_files("claim-success-reviewer", "DEPENDENT_SET", [extra])
    assert len(found) == len(expected)
    for (_label, digest, body), path in zip(found, expected, strict=True):
        original = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        original = original if original.endswith("\n") else original + "\n"
        assert body == original and digest == hashlib.sha256(original.encode("utf-8")).hexdigest()


def test_dependent_documents_only_for_dependent_scope():
    indep = {p.name for p in pack.pack_files("syntax-scope-reviewer", "INDEPENDENT", [])}
    dep = {p.name for p in pack.pack_files("syntax-scope-reviewer", "DEPENDENT_SET", [])}
    assert pack.S08 not in indep and {pack.S08, pack.S05} <= dep


def test_blind_reviewer_never_gets_a_pack(tmp_path):
    with pytest.raises(SystemExit):
        pack.main(["--role", "blind-claim-reconstruction-reviewer", "--out", str(tmp_path / "b")])
