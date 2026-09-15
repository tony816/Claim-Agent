"""Deterministic claim-text utilities: hashing, flattening, parsing, chains."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models.ids import sha256_text

_CLAIM_HEAD = re.compile(r"【\s*청구항\s*(\d+)\s*】")
_PARENT_SINGLE = re.compile(r"제\s*(\d+)\s*항")
_MULTI_HINTS = ("중 어느 한 항", "중 어느 하나의 항", "또는 제", "내지 제", "항 중")


class ClaimParseError(ValueError):
    pass


class MultiDependentChain(ValueError):
    pass


def exact_sha256(text: str) -> str:
    """Hash of the exact text — one changed space or punctuation mark = new hash."""
    return sha256_text(text)


def flatten(text: str) -> str:
    """평문화: remove claim headers, line breaks, list markers and repeated spaces."""
    t = _CLAIM_HEAD.sub(" ", text)
    t = re.sub(r"^\s*(?:[-*•]|\d+[.)]|\(\d+\))\s+", " ", t, flags=re.MULTILINE)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


@dataclass
class Claim:
    claim_no: int
    text: str                       # full text including header line
    body: str                       # text without the 【청구항 N】 header
    parent_nos: list[int] = field(default_factory=list)
    multi_dependent: bool = False

    @property
    def is_independent(self) -> bool:
        return not self.parent_nos

    @property
    def parent_no(self) -> int | None:
        return self.parent_nos[0] if self.parent_nos else None


def _parents_of(body: str) -> tuple[list[int], bool]:
    head = body[:80]
    if "있어서" not in head and "에 있어서" not in body[:120]:
        # Independent claims have no 제N항 reference in their preamble.
        return [], False
    pre = body.split("있어서", 1)[0]
    nos = [int(n) for n in _PARENT_SINGLE.findall(pre)]
    multi = len(nos) > 1 or any(h in pre for h in _MULTI_HINTS)
    return nos, multi


def parse_claim_set(text: str) -> list[Claim]:
    """Split a 【청구항 N】-formatted document into claims (order preserved)."""
    heads = list(_CLAIM_HEAD.finditer(text))
    if not heads:
        raise ClaimParseError("no 【청구항 N】 headers found")
    claims: list[Claim] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        full = text[m.start():end].rstrip()
        body = text[m.end():end].strip()
        no = int(m.group(1))
        parents, multi = _parents_of(body)
        claims.append(Claim(no, full, body, parents, multi))
    return claims


def validate_parent_refs(claims: list[Claim]) -> list[str]:
    """Return human-readable problems: forward references, missing parents, duplicates."""
    problems: list[str] = []
    seen: set[int] = set()
    for c in claims:
        if c.claim_no in seen:
            problems.append(f"청구항 {c.claim_no} 번호 중복")
        seen.add(c.claim_no)
        for p in c.parent_nos:
            if p >= c.claim_no:
                problems.append(f"청구항 {c.claim_no}이(가) 뒤 번호 또는 자기 자신 제{p}항을 인용")
            elif p not in seen:
                problems.append(f"청구항 {c.claim_no}이(가) 존재하지 않는 제{p}항을 인용")
    return problems


def parent_chain(claims: list[Claim], target_no: int, expand_multi: bool = False) -> list[list[Claim]]:
    """Return one or more chains root→…→direct parent (excluding the target).

    A single chain is returned unless the target or an ancestor is multi-dependent;
    then either raise MultiDependentChain or (expand_multi) return one chain per alternative.
    """
    by_no = {c.claim_no: c for c in claims}
    if target_no not in by_no:
        raise ClaimParseError(f"청구항 {target_no} 없음")

    def chains_for(no: int) -> list[list[Claim]]:
        c = by_no[no]
        if c.is_independent:
            return [[]]
        if c.multi_dependent and not expand_multi:
            raise MultiDependentChain(f"청구항 {no}은(는) 다중 종속 인용이다: {c.parent_nos}")
        out: list[list[Claim]] = []
        for p in c.parent_nos:
            if p not in by_no:
                raise ClaimParseError(f"청구항 {no}의 부모 제{p}항 없음")
            for chain in chains_for(p):
                out.append(chain + [by_no[p]])
        return out

    return chains_for(target_no)


def parent_chain_text(chain: list[Claim]) -> str:
    return "\n\n".join(c.text for c in chain)


def contains_user_lock(claim_text: str, user_lock: str | None) -> bool:
    """USER_LOCK wording must survive verbatim (whitespace-normalised comparison)."""
    if not user_lock:
        return True
    norm = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731
    return norm(user_lock) in norm(claim_text)


def claim_set_text(claims: list[Claim]) -> str:
    return "\n\n".join(c.text for c in claims)
