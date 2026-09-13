"""Human-approved lessons memory (개선 루프 ③ 지속 학습).

Lessons are procedural / expression cautions only; they are never technical
evidence. Only approved lessons are injected, and their hash is part of
source_set_id so every record is traceable to the lesson set it ran under.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from ..models.ids import sha256_text

STATES = ("pending", "approved", "rejected")


@dataclass
class Lesson:
    id: str
    version: int = 1
    status: str = "pending"
    target_roles: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=lambda: ["INDEPENDENT", "DEPENDENT_SET"])
    invention_types: list[str] = field(default_factory=list)
    text_ko: str = ""
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)
    eval_evidence: list[str] = field(default_factory=list)
    created_at: str = ""
    approved_by: str | None = None
    approved_at: str | None = None
    note: str | None = None
    llm_drafted: bool = False

    def to_yaml(self) -> str:
        return yaml.safe_dump(asdict(self), allow_unicode=True, sort_keys=False)

    @classmethod
    def from_yaml(cls, text: str) -> "Lesson":
        data = yaml.safe_load(text) or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class LessonStore:
    def __init__(self, root: Path):
        self.root = root
        for s in STATES:
            (root / s).mkdir(parents=True, exist_ok=True)

    def _path(self, lesson_id: str, status: str) -> Path:
        return self.root / status / f"{lesson_id}.yaml"

    def list(self, status: str | None = None) -> list[Lesson]:
        out: list[Lesson] = []
        for s in STATES if status is None else [status]:
            for p in sorted((self.root / s).glob("*.yaml")):
                out.append(Lesson.from_yaml(p.read_text(encoding="utf-8")))
        return out

    def get(self, lesson_id: str) -> tuple[Lesson, str]:
        for s in STATES:
            p = self._path(lesson_id, s)
            if p.exists():
                return Lesson.from_yaml(p.read_text(encoding="utf-8")), s
        raise FileNotFoundError(lesson_id)

    def next_id(self) -> str:
        n = len(self.list()) + 1
        while any(l.id == f"L-{n:04d}" for l in self.list()):
            n += 1
        return f"L-{n:04d}"

    def save(self, lesson: Lesson) -> Path:
        for s in STATES:
            p = self._path(lesson.id, s)
            if p.exists() and s != lesson.status:
                p.unlink()
        p = self._path(lesson.id, lesson.status)
        p.write_text(lesson.to_yaml(), encoding="utf-8")
        return p

    def propose(self, text_ko: str, target_roles: list[str], rationale: str, evidence: list[str], llm_drafted: bool = False) -> Lesson:
        lesson = Lesson(id=self.next_id(), text_ko=text_ko, target_roles=target_roles, rationale=rationale, evidence=evidence, created_at=time.strftime("%Y-%m-%dT%H:%M:%S"), llm_drafted=llm_drafted)
        self.save(lesson)
        return lesson

    def approve(self, lesson_id: str, by: str = "user", note: str | None = None) -> Lesson:
        lesson, _ = self.get(lesson_id)
        lesson.status = "approved"
        lesson.version += 1 if lesson.approved_at else 0
        lesson.approved_by = by
        lesson.approved_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        lesson.note = note
        self.save(lesson)
        return lesson

    def reject(self, lesson_id: str, note: str | None = None) -> Lesson:
        lesson, _ = self.get(lesson_id)
        lesson.status = "rejected"
        lesson.note = note
        self.save(lesson)
        return lesson

    # ------------------------------------------------------------ injection
    def approved_for_role(self, role: str, include: list[str] | None = None) -> list[Lesson]:
        out = []
        for l in self.list("approved"):
            if include is not None and l.id not in include:
                continue
            if not l.target_roles or role in l.target_roles:
                out.append(l)
        return out

    def injection_text(self, role: str, include: list[str] | None = None) -> str:
        lessons = self.approved_for_role(role, include)
        if not lessons:
            return ""
        return "\n".join(f"- ({l.id} v{l.version}) {l.text_ko.strip()}" for l in lessons)

    def texts_by_role(self, roles: list[str], enabled: bool = True, include: list[str] | None = None) -> dict[str, str]:
        if not enabled:
            return {}
        return {r: t for r in roles if (t := self.injection_text(r, include))}

    def digest(self, enabled: bool = True, include: list[str] | None = None) -> str:
        if not enabled:
            return "none"
        items = [l for l in self.list("approved") if include is None or l.id in include]
        if not items:
            return "none"
        return sha256_text("\n".join(f"{l.id}:{l.version}:{l.text_ko}" for l in sorted(items, key=lambda x: x.id)))


def propose_from_run(store_dir: Path, run_state: dict, lessons: LessonStore) -> list[Lesson]:
    """Draft pending lessons from a halted run's open issues (template-based, no LLM)."""
    out: list[Lesson] = []
    halt = run_state.get("halt") or {}
    role = halt.get("role", "")
    for issue in halt.get("open_issues", []):
        text = issue.get("text", "").strip()
        if not text:
            continue
        lesson = lessons.propose(
            text_ko=f"[{halt.get('stage')}] {issue.get('code','')} — 다음 상황을 미리 점검한다: {text}",
            target_roles=[role] if role and role != "engine" else [],
            rationale=f"run {run_state.get('run_id')} 중지 사유({halt.get('kind')})에서 자동 제안; 사람 승인 전에는 주입되지 않음",
            evidence=[f"{run_state.get('run_id')}:{halt.get('record_id')}"],
        )
        out.append(lesson)
    return out
