"""개선 루프 ② 실험: variant definitions and shadow comparison."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..models.envelope import RoleEnvelope
from ..models.ids import sha256_text


@dataclass
class Variant:
    name: str
    description: str = ""
    overrides: dict[str, Any] = field(default_factory=dict)          # dotted config keys
    prompt_suffix: dict[str, str] = field(default_factory=dict)       # role -> extra text appended to system instruction
    lessons_include: list[str] | None = None                          # None = all approved; [] = none
    path: Path | None = None

    @property
    def variant_id(self) -> str:
        blob = yaml.safe_dump({"o": self.overrides, "p": self.prompt_suffix, "l": self.lessons_include}, sort_keys=True)
        return f"{self.name}-{sha256_text(blob)[:8]}"

    @classmethod
    def load(cls, path: Path) -> "Variant":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        suffix = {}
        for role, val in (data.get("prompt_suffix") or {}).items():
            p = Path(val)
            if not p.is_absolute():
                p = path.parent / p
            suffix[role] = p.read_text(encoding="utf-8") if p.exists() else str(val)
        li = data.get("lessons", {}) or {}
        include = None
        if li.get("inject") == "none":
            include = []
        elif li.get("include"):
            include = list(li["include"])
        return cls(name=data.get("name") or path.stem, description=data.get("description", ""), overrides=data.get("overrides") or {}, prompt_suffix=suffix, lessons_include=include, path=path)

    @classmethod
    def default(cls) -> "Variant":
        return cls(name="default")


def compare_envelopes(base: RoleEnvelope, other: RoleEnvelope) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    if base.status != other.status:
        diff["status"] = [base.status.value, other.status.value]
    bg, og = base.gates.present(), other.gates.present()
    for g in sorted(set(bg) | set(og)):
        if bg.get(g) != og.get(g):
            diff[g] = [bg.get(g), og.get(g)]
    if base.next_step != other.next_step:
        diff["next_step"] = [base.next_step.value, other.next_step.value]
    if (base.exact_claim_text or "") != (other.exact_claim_text or ""):
        diff["exact_claim_text"] = "differs"
    return diff
