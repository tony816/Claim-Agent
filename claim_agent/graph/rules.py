"""Layer 1 — rule graph over sources/*.md and the role×scope rule packs built from it.

Extraction is deterministic (no model): every markdown heading becomes a SECTION node with its line span, and the
terms a section names — gates/locks/statuses in UPPER_SNAKE, `조건 N`, role names, other source files — become
TERM nodes linked by MENTIONS (DEFINES when the term is in the heading). Roles link to the files their whitelist
allows (CONSUMES) and to the terms their role file names (USES).

A rule pack for (role, scope) keeps, from the whitelisted files only:
  - each file's preamble and the sections whose heading is a general rule (원칙, 판정 상태, 충돌, 우선순위 …),
  - every section that names a term the role file names (its gates, conditions, statuses, roles),
  - the definition sections of terms those sections name (one hop),
and drops, for INDEPENDENT scope, sections whose heading is about dependent claims.
Small files (≤ `FULL_FILE_CHARS`) are kept whole. `claim-success-reviewer` is exempt by default: it audits the
source-use contract and keeps reading whole files (config `graph.rule_pack_exempt_roles`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models.enums import Scope
from ..models.ids import sha256_text
from ..roles.registry import RoleRegistry
from ..roles.whitelist import SOURCE_WHITELIST, sources_for
from ..sources.registry import SOURCE_FILES, SourceSet
from .store import EXTRACTED, Edge, Graph, Node, Span

FULL_FILE_CHARS = 6000
MIN_FILE_SAVING = 0.10   # a file whose pack would save less than this is sent whole (section markers cost too)

# A role's own working standard is never sliced: its checklist items carry the substance without naming any gate
# (06's CLAR/NOV/INV items, 04's style rules). Packing applies only to the files a role consults secondarily.
PRIMARY: dict[str, set[str]] = {
    "claim-architect": {"SUCCESS", "S11", "S13"},
    "claim-drafter": {"SUCCESS", "S08", "S05", "S11", "S13"},
    "claim-style-adjuster": {"S07", "S04"},
    "claim-success-reviewer": {"README", "SUCCESS", "S07", "S04", "S08", "S05"},
    "syntax-scope-reviewer": {"S04"},
    "oa-strategy-reviewer": {"S06", "S12"},
    "dependent-claim-strategy-architect": {"S08", "S05", "S12", "S13"},
    "prior-art-pre-examiner": {"S09", "S12"},
}
_HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
_UPPER = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?![A-Za-z0-9_])")
_COND = re.compile(r"조건\s*(\d+(?:\s*[·,]\s*\d+)*)")
_ROLE = re.compile(r"\b((?:claim|syntax|oa|picture|blind|dependent)-[a-z-]+(?:reviewer|architect|drafter|adjuster))\b")
_ALWAYS = re.compile(r"원칙|판정 상태|공통|충돌|우선순위|정의|용어|필수 출력|필수 산출물|활성 게이트")
_DEPENDENT_HEADING = re.compile(r"종속항|DEPENDENT|DC-")
_FILE_BY_NAME = {name: key for key, name in SOURCE_FILES.items()}


def terms_in(text: str) -> set[str]:
    out = {m.group(1) for m in _UPPER.finditer(text)}
    for m in _COND.finditer(text):
        out.update(f"조건 {n}" for n in re.split(r"\s*[·,]\s*", m.group(1)))
    out.update(m.group(1) for m in _ROLE.finditer(text))
    for name, key in _FILE_BY_NAME.items():
        if name in text:
            out.add(f"FILE:{key}")
    return out


@dataclass
class Section:
    id: str
    file_key: str
    level: int            # 0 = preamble before the first heading
    heading: str
    line_start: int
    line_end: int
    text: str
    parent: str | None
    terms: set[str] = field(default_factory=set)
    heading_terms: set[str] = field(default_factory=set)


def split_sections(file_key: str, text: str) -> list[Section]:
    lines = text.splitlines()
    marks: list[tuple[int, int, str]] = []  # (line_index, level, heading)
    in_code = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        m = None if in_code else _HEADING.match(line)
        if m:
            marks.append((i, len(m.group(1)), m.group(2)))
    out: list[Section] = []
    first = marks[0][0] if marks else len(lines)
    if first > 0 and "".join(lines[:first]).strip():
        body = "\n".join(lines[:first])
        out.append(Section(f"{file_key}#0", file_key, 0, "(머리말)", 1, first, body, None))
    stack: list[Section] = []
    for n, (i, level, heading) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        body = "\n".join(lines[i:end])
        while stack and stack[-1].level >= level:
            stack.pop()
        sec = Section(f"{file_key}#{n + 1}", file_key, level, heading, i + 1, end, body, stack[-1].id if stack else None)
        out.append(sec)
        stack.append(sec)
    for s in out:
        s.terms = terms_in(s.text)
        s.heading_terms = terms_in(s.heading)
    return out


def role_terms(roles: RoleRegistry, role: str) -> set[str]:
    return {t for t in terms_in(roles.get(role).body) if not t.startswith("FILE:")}


class RuleGraph:
    """The rule graph plus its parsed sections, keyed for pack building."""

    def __init__(self, sources: SourceSet, roles: RoleRegistry):
        self.sources = sources
        self.roles = roles
        self.sections: dict[str, Section] = {}
        self.by_file: dict[str, list[Section]] = {}
        for key, sf in sources.files.items():
            secs = split_sections(key, sf.text)
            self.by_file[key] = secs
            for s in secs:
                self.sections[s.id] = s
        self.graph = self._build()

    def _build(self) -> Graph:
        g = Graph("rules", {"sources_digest": self.sources.digest(), "roles_digest": self.roles.digest()})
        for key, sf in self.sources.files.items():
            g.add_node(Node(f"FILE:{key}", "FILE", sf.relpath, attrs={"sha256": sf.sha256, "chars": len(sf.text)}))
        for s in self.sections.values():
            sf = self.sources.get(s.file_key)
            g.add_node(Node(s.id, "SECTION", s.heading, attrs={"level": s.level, "chars": len(s.text)},
                            spans=[Span(sf.relpath, sf.sha256, s.line_start, s.line_end)]))
            g.add_edge(Edge(s.parent or f"FILE:{s.file_key}", s.id, "CONTAINS"))
            for t in sorted(s.terms):
                if t.startswith("FILE:"):
                    if t != f"FILE:{s.file_key}":
                        g.add_edge(Edge(s.id, t, "REFERENCES"))
                    continue
                g.add_node(Node(t, "TERM", t))
                g.add_edge(Edge(s.id, t, "DEFINES" if t in s.heading_terms else "MENTIONS"))
        for (role, scope), keys in SOURCE_WHITELIST.items():
            rid = f"ROLE:{role}"
            g.add_node(Node(rid, "ROLE", role))
            for k in keys:
                g.add_edge(Edge(rid, f"FILE:{k}", "CONSUMES", attrs={"scope": scope.value}))
            if role in self.roles.roles:
                for t in sorted(role_terms(self.roles, role)):
                    g.add_node(Node(t, "TERM", t))
                    g.add_edge(Edge(rid, t, "USES"))
        for n in g.nodes.values():
            n.status = EXTRACTED
        return g

    # ---- packs

    def descendants(self, sid: str) -> list[Section]:
        s = self.sections[sid]
        out = []
        for c in self.by_file[s.file_key]:
            p = c.parent
            while p and p != sid:
                p = self.sections[p].parent
            if p == sid:
                out.append(c)
        return out

    def pack(self, role: str, scope: Scope) -> RulePack:
        keys = sources_for(role, scope)
        seeds = role_terms(self.roles, role) if role in self.roles.roles else set()
        chosen: dict[str, str] = {}   # section id -> reason

        def skip(s: Section) -> bool:
            return scope == Scope.INDEPENDENT and bool(_DEPENDENT_HEADING.search(s.heading))

        def take(s: Section, reason: str, subtree: bool) -> None:
            if skip(s):
                return
            chosen.setdefault(s.id, reason)
            if subtree:
                for c in self.descendants(s.id):
                    if not skip(c):
                        chosen.setdefault(c.id, f"under:{s.id}")

        primary = PRIMARY.get(role, set())
        for k in keys:
            whole = "primary-file" if k in primary else "small-file" if len(self.sources.get(k).text) <= FULL_FILE_CHARS else None
            if whole:
                for s in self.by_file[k]:
                    chosen[s.id] = whole
                continue
            for s in self.by_file[k]:
                if s.level == 0 or _ALWAYS.search(s.heading):
                    take(s, "general", True)
                elif s.heading_terms & seeds:
                    take(s, "defines:" + ",".join(sorted(s.heading_terms & seeds)[:3]), True)
                elif s.terms & seeds:
                    take(s, "role-term:" + ",".join(sorted(s.terms & seeds)[:3]), False)
        # One hop: sections that define a term the chosen sections name.
        named = {t for sid in chosen for t in self.sections[sid].terms if not t.startswith("FILE:")}
        for k in keys:
            for s in self.by_file[k]:
                hit = s.heading_terms & named
                if hit and s.id not in chosen:
                    take(s, "hop:" + ",".join(sorted(hit)[:3]), True)
        # Parents give the heading path (their own intro text only; a section's text stops at the next heading).
        for sid in list(chosen):
            p = self.sections[sid].parent
            while p:
                chosen.setdefault(p, "parent")
                p = self.sections[p].parent
        for k in keys:
            secs = self.by_file[k]
            kept = sum(len(s.text) for s in secs if s.id in chosen)
            if kept >= (1 - MIN_FILE_SAVING) * len(self.sources.get(k).text):
                for s in secs:
                    chosen.setdefault(s.id, "low-saving-file")
        ordered = [s for k in keys for s in self.by_file[k] if s.id in chosen]
        return RulePack(role, scope, keys, ordered, chosen, seeds, self)


def pack_provider(sources: SourceSet, roles: RoleRegistry, exempt: list[str]):
    """(role, scope) -> RulePack, or None when the role is exempt or its pack equals the whole files."""
    rg = RuleGraph(sources, roles)
    cache: dict[tuple[str, Scope], RulePack | None] = {}

    def get(role: str, scope: Scope) -> RulePack | None:
        if role in exempt:
            return None
        if (role, scope) not in cache:
            p = rg.pack(role, scope)
            cache[(role, scope)] = p if p.text_chars() < p.full_chars() else None
        return cache[(role, scope)]

    return get


@dataclass
class RulePack:
    role: str
    scope: Scope
    file_keys: list[str]
    sections: list[Section]        # chosen sections in file order; each holds only its own text up to the next heading
    reasons: dict[str, str]
    seeds: set[str]
    rg: RuleGraph

    def file_text(self, key: str) -> str:
        secs = [s for s in self.sections if s.file_key == key]
        if len(secs) == len(self.rg.by_file[key]):
            return self.rg.sources.get(key).text
        return "\n\n".join(f"<!-- §{s.id} L{s.line_start}-{s.line_end} -->\n{s.text.rstrip()}" for s in secs)

    def text_chars(self) -> int:
        return sum(len(self.file_text(k)) for k in self.file_keys)

    def full_chars(self) -> int:
        return sum(len(self.rg.sources.get(k).text) for k in self.file_keys)

    @property
    def pack_id(self) -> str:
        return "rp-" + sha256_text("\n".join(f"{k}:{self.rg.sources.get(k).sha256}:{','.join(s.id for s in self.sections if s.file_key == k)}" for k in self.file_keys))[:12]

    def coverage(self) -> dict[str, Any]:
        """Seed terms that some whitelisted section names but no chosen section does (should be empty)."""
        in_scope = [s for k in self.file_keys for s in self.rg.by_file[k]
                    if not (self.scope == Scope.INDEPENDENT and _DEPENDENT_HEADING.search(s.heading))]
        named_anywhere = {t for s in in_scope for t in s.terms}
        named_in_pack = {t for s in self.sections for t in s.terms}
        missing = sorted((self.seeds & named_anywhere) - named_in_pack)
        dropped = [s.id for k in self.file_keys for s in self.rg.by_file[k] if s.id not in self.reasons]
        return {"missing_terms": missing, "dropped_sections": dropped}

    def summary(self) -> dict[str, Any]:
        full, packed = self.full_chars(), self.text_chars()
        return {
            "role": self.role, "scope": self.scope.value, "pack_id": self.pack_id, "files": self.file_keys,
            "full_chars": full, "pack_chars": packed, "saved_pct": round(100 * (1 - packed / full), 1) if full else 0.0,
            **self.coverage(),
        }
