"""Role registry: loads `.claude/agents/*.md` as the single source of truth."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..models.ids import sha256_text

ROLE_NAMES = [
    "claim-architect",
    "claim-drafter",
    "claim-style-adjuster",
    "claim-success-reviewer",
    "syntax-scope-reviewer",
    "oa-strategy-reviewer",
    "blind-claim-reconstruction-reviewer",
    "picture-claim-reconstruction-reviewer",
    "dependent-claim-strategy-architect",
]

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class RoleSpec:
    name: str
    path: Path
    frontmatter: dict
    body: str
    sha256: str

    @property
    def is_blind(self) -> bool:
        return self.name == "blind-claim-reconstruction-reviewer"


def strip_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, text[m.end():]


def load_role(path: Path) -> RoleSpec:
    text = path.read_text(encoding="utf-8")
    meta, body = strip_frontmatter(text)
    name = str(meta.get("name") or path.stem)
    return RoleSpec(name=name, path=path, frontmatter=meta, body=body.strip() + "\n", sha256=sha256_text(text))


class RoleRegistry:
    def __init__(self, roles: dict[str, RoleSpec]):
        self.roles = roles

    @classmethod
    def load(cls, roles_dir: Path) -> RoleRegistry:
        roles: dict[str, RoleSpec] = {}
        for p in sorted(roles_dir.glob("*.md")):
            spec = load_role(p)
            roles[spec.name] = spec
        missing = [r for r in ROLE_NAMES if r not in roles]
        if missing:
            raise FileNotFoundError(f"missing role files in {roles_dir}: {missing}")
        return cls(roles)

    def get(self, name: str) -> RoleSpec:
        return self.roles[name]

    def digest(self) -> str:
        joined = "\n".join(f"{n}:{self.roles[n].sha256}" for n in sorted(self.roles))
        return sha256_text(joined)
