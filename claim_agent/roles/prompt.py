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

    def _render_sources(self, keys: list[str]) -> tuple[str, list[str]]:
        if not keys:
            return "", []
        blocks = ["## 사전 로딩 소스\n\n"]
        paths = []
        for key in keys:
            sf = self.sources.get(key)
            blocks.append(render_file_block(sf.relpath, sf.sha256, sf.text))
            paths.append(sf.relpath)
        return "".join(blocks), paths

    def sources_block(self, role: RoleSpec, scope: Scope) -> tuple[str, list[str], list[str]]:
        keys = sources_for(role.name, scope)
        block, paths = self._render_sources(keys)
        return block, keys, paths

    def assemble_combined(self, roles: list[RoleSpec], scope: Scope, header: str) -> AssembledPrompt:
        """One call that applies several role files in order (EXISTING_SET_EDIT review); sources are their whitelists' union."""
        parts = [self.preamble, "\n---\n\n", header.rstrip(), "\n"]
        for role in roles:
            parts += ["\n---\n\n", f"## 역할 파일: {role.name}\n\n", role.body]
            lesson = self.lessons.get(role.name)
            if lesson:
                parts += ["\n## 승인된 프로젝트 교훈 (절차·표현 주의사항; 기술내용 근거 아님)\n\n", lesson.rstrip(), "\n"]
        keys = list(dict.fromkeys(k for role in roles for k in sources_for(role.name, scope)))
        block, paths = self._render_sources(keys)
        return AssembledPrompt("".join(parts), block, keys, paths)

    def sources_subset(self, role: RoleSpec, scope: Scope, keys: list[str]) -> str:
        """A smaller pre-loaded block (only `keys`, all of which must be whitelisted for the role/scope)."""
        allowed = sources_for(role.name, scope)
        chosen = [k for k in keys if k in allowed]
        if not chosen:
            return ""
        blocks = ["## 사전 로딩 소스\n\n"]
        for key in chosen:
            sf = self.sources.get(key)
            blocks.append(render_file_block(sf.relpath, sf.sha256, sf.text))
        return "".join(blocks)

    def assemble(self, role: RoleSpec, scope: Scope) -> AssembledPrompt:
        block, keys, paths = self.sources_block(role, scope)
        return AssembledPrompt(self.system_instruction(role), block, keys, paths)
