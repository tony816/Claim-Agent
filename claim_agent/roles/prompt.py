"""System-instruction and source-block assembly."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models.enums import Scope
from ..models.ids import sha256_text
from ..sources.registry import SourceSet
from .registry import RoleSpec
from .whitelist import sources_for

_HERE = Path(__file__).parent


def _load_preamble(name: str) -> str:
    return (_HERE / name).read_text(encoding="utf-8").strip() + "\n"


@dataclass
class AssembledPrompt:
    system_instruction: str
    sources_block: str          # may be empty
    source_keys: list[str]
    source_paths: list[str]

    @property
    def system_sha(self) -> str:
        return sha256_text(self.system_instruction)

    @property
    def sources_sha(self) -> str:
        return sha256_text(self.sources_block)


def render_file_block(relpath: str, sha: str, text: str) -> str:
    return f"<<<FILE {relpath} sha256={sha}>>>\n{text.rstrip()}\n<<<END FILE>>>\n"


class PromptAssembler:
    def __init__(self, sources: SourceSet, lessons_text_by_role: dict[str, str] | None = None):
        self.sources = sources
        self.lessons = lessons_text_by_role or {}
        self.preamble = _load_preamble("adapter_preamble_ko.md")
        self.preamble_blind = _load_preamble("adapter_preamble_blind_ko.md")

    def system_instruction(self, role: RoleSpec) -> str:
        parts = [self.preamble_blind if role.is_blind else self.preamble, "\n---\n\n", role.body]
        lesson = None if role.is_blind else self.lessons.get(role.name)
        if lesson:
            parts += ["\n---\n\n## 승인된 프로젝트 교훈 (절차·표현 주의사항; 기술내용 근거 아님)\n\n", lesson.rstrip(), "\n"]
        return "".join(parts)

    def sources_block(self, role: RoleSpec, scope: Scope) -> tuple[str, list[str], list[str]]:
        keys = sources_for(role.name, scope)
        if not keys:
            return "", [], []
        blocks = ["## 사전 로딩 소스\n\n"]
        paths = []
        for key in keys:
            sf = self.sources.get(key)
            blocks.append(render_file_block(sf.relpath, sf.sha256, sf.text))
            paths.append(sf.relpath)
        return "".join(blocks), keys, paths

    def assemble(self, role: RoleSpec, scope: Scope) -> AssembledPrompt:
        block, keys, paths = self.sources_block(role, scope)
        return AssembledPrompt(self.system_instruction(role), block, keys, paths)
