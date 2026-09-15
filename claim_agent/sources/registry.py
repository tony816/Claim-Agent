"""Source catalog (sources/*.md) and source_set_id computation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models.ids import sha256_text

# Stable keys -> file names under sources/.
SOURCE_FILES: dict[str, str] = {
    "README": "README.md",
    "SUCCESS": "독립항_작성_성공조건.md",
    "S04": "04_청구항_스타일가이드.md",
    "S05": "05_종속항_전개패턴_가이드.md",
    "S06": "06_OA_심사리스크_체크리스트.md",
    "S07": "07_용어표현_출처게이트.md",
    "S08": "08_종속항_기술기여_게이트.md",
    "ROUTING": "청구항_예시검색_라우팅인덱스.md",
    "CORPUS": "청구항_문체학습용_분야별검색최적화본.md",
}

ADAPTER_VERSION = 1


@dataclass(frozen=True)
class SourceFile:
    key: str
    path: Path
    relpath: str
    sha256: str
    text: str


class SourceSet:
    def __init__(self, files: dict[str, SourceFile], sources_dir: Path):
        self.files = files
        self.sources_dir = sources_dir

    @classmethod
    def load(cls, sources_dir: Path, project_root: Path | None = None) -> "SourceSet":
        root = project_root or sources_dir.parent
        files: dict[str, SourceFile] = {}
        missing = []
        for key, name in SOURCE_FILES.items():
            p = sources_dir / name
            if not p.exists():
                missing.append(name)
                continue
            text = p.read_text(encoding="utf-8")
            rel = str(p.relative_to(root)).replace("\\", "/")
            files[key] = SourceFile(key=key, path=p, relpath=rel, sha256=sha256_text(text), text=text)
        if missing:
            raise FileNotFoundError(f"missing source files in {sources_dir}: {missing}")
        return cls(files, sources_dir)

    def get(self, key: str) -> SourceFile:
        return self.files[key]

    def digest(self) -> str:
        joined = "\n".join(f"{k}:{self.files[k].sha256}" for k in sorted(self.files))
        return sha256_text(joined)


def source_set_id(sources_digest: str, roles_digest: str, lessons_digest: str, adapter_version: int = ADAPTER_VERSION) -> str:
    return f"cc-py-{sources_digest[:8]}-{roles_digest[:8]}-{lessons_digest[:8]}-a{adapter_version}"
