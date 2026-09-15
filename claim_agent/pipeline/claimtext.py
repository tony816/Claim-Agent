"""Deterministic claim-text utilities: hashing, flattening, parsing, chains."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models.ids import sha256_text

_CLAIM_HEAD = re.compile(r"【\s*청구항\s*(\d+)\s*】")
# Header spellings accepted on input and normalised to the canonical 【청구항 N】 form.
# Each alternative must start at a line beginning and be followed by the claim body (same or next line).
_HEAD_VARIANTS = re.compile(
    r"^[ \t]*(?:"
    r"【\s*청구항\s*(?P<a>\d+)\s*】"                    # 【청구항 1】 (canonical)
    r"|\[\s*청구항\s*(?P<b>\d+)\s*\]"                  # [청구항 1]
    r"|청구항\s*(?P<c>\d+)\s*[.:：)]?(?=[ \t]*$)"           # 청구항 1 / 청구항 1. / 청구항 1: (line alone)
    r"|제\s*(?P<d>\d+)\s*항\s*[.:：]?(?=[ \t]*$)"        # 제1항 (line alone)
    r"|(?:Claim|CLAIM)\s*(?P<e>\d+)\s*[.:]?(?=[ \t]*$)"   # Claim 1
    r")[ \t]*",
    re.MULTILINE,
)
_PARENT_SINGLE = re.compile(r"제\s*(\d+)\s*항")
_PARENT_RANGE = re.compile(r"제\s*(\d+)\s*항\s*(?:내지|부터|~|-|–)\s*제?\s*(\d+)\s*항")
_MULTI_HINTS = ("중 어느 한 항", "중 어느 하나의 항", "중 어느 하나", "또는 제", "내지 제", "항 중", "및 제")
_PREAMBLE_MARKERS = ("에 있어서", "있어서", "에 기재된", "에 따른", "에 의한", "의 ")
_PREAMBLE_WINDOW = 160


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


def normalize_headers(text: str) -> tuple[str, bool, str]:
    """Rewrite accepted header spellings to the canonical 【청구항 N】 line.

    Returns (text, changed, style) where style is the spelling that was found
    (canonical | bracket | plain | je-hang | english | none).
    """
    styles: list[str] = []

    def repl(m: re.Match[str]) -> str:
        for key, style in (("a", "canonical"), ("b", "bracket"), ("c", "plain"), ("d", "je-hang"), ("e", "english")):
            if m.group(key):
                styles.append(style)
                return f"【청구항 {int(m.group(key))}】\n"
        return m.group(0)  # pragma: no cover

    out = _HEAD_VARIANTS.sub(repl, text)
    out = re.sub(r"【청구항 (\d+)】\n[ \t]*\n", r"【청구항 \1】\n", out)  # header + blank line → header line
    non_canonical = [s for s in styles if s != "canonical"]
    style = non_canonical[0] if non_canonical else ("canonical" if styles else "none")
    return out, bool(non_canonical), style


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


def parse_parent_refs(body: str) -> tuple[list[int], bool]:
    """Explicit rules for the dependent-claim preamble (제N항, 또는, 및, 내지 … 중 어느 한 항)."""
    head = body[:_PREAMBLE_WINDOW]
    cut = -1
    for marker in _PREAMBLE_MARKERS:
        i = head.find(marker)
        if i >= 0 and (cut < 0 or i < cut):
            cut = i
    if cut < 0:
        return [], False
    pre = head[:cut]
    if not _PARENT_SINGLE.search(pre):
        return [], False
    nos: set[int] = set()
    for a, b in _PARENT_RANGE.findall(pre):
        lo, hi = int(a), int(b)
        if hi >= lo:
            nos.update(range(lo, hi + 1))
    nos.update(int(n) for n in _PARENT_SINGLE.findall(pre))
    ordered = sorted(nos)
    multi = len(ordered) > 1 or any(h in pre for h in _MULTI_HINTS)
    return ordered, multi


def _parents_of(body: str) -> tuple[list[int], bool]:
    return parse_parent_refs(body)


def parse_claim_set(text: str) -> list[Claim]:
    """Split a claim document into claims (order preserved).

    Accepts 【청구항 N】, [청구항 N], `청구항 N.`, a lone `제N항` line and `Claim N`; non-canonical
    spellings are normalised first, so `Claim.text` always starts with the canonical header.
    """
    text, _, _ = normalize_headers(text)
    heads = list(_CLAIM_HEAD.finditer(text))
    if not heads:
        raise ClaimParseError("no 【청구항 N】 headers found (accepted spellings: 【청구항 N】, [청구항 N], 청구항 N., 제N항, Claim N)")
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
