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


_PROPOSED_HEAD = re.compile(r"^[ \t]*(?:【\s*청구항\s*(\d+)\s*】|\[\s*청구항\s*(\d+)\s*\])", re.MULTILINE)
# "제8항에 있어서," (or a 내지/또는 range) followed by a claim body on the same line — not a note that merely names the phrase.
_PROPOSED_DEPENDENT = re.compile(r"제\s*\d+\s*항(?:\s*(?:내지|또는|및|~|-|–|,)\s*제?\s*\d+\s*항)*(?:\s*중\s*어느\s*(?:한|하나의)\s*항)?\s*에\s*있어서\s*,[^\n]{40,}")


def pasted_claims(text: str) -> dict[int, Claim]:
    """Claims pasted into a note, by header. Text before the first header (the note itself, a headerless fragment) is ignored."""
    heads = list(_PROPOSED_HEAD.finditer(text))
    out: dict[int, Claim] = {}
    for i, m in enumerate(heads):
        body = text[m.end():(heads[i + 1].start() if i + 1 < len(heads) else len(text))].strip()
        no = int(m.group(1) or m.group(2))
        parents, multi = parse_parent_refs(body)
        out[no] = Claim(no, f"【청구항 {no}】\n{body}", body, parents, multi)
    return out


def claim_wording(text: str) -> str | None:
    """Whether a user's note pastes claim wording: DEPENDENT, INDEPENDENT or None (a note about the wording).

    A proposal is a claim header line (【청구항 N】 / [청구항 N]) or a "제N항에 있어서," preamble with a body. It is
    DEPENDENT when every headed claim cites a parent (or there is no header but a dependent preamble), INDEPENDENT when
    some headed claim has no citation. Deterministic, so a resume can be routed without a model call.
    """
    claims = pasted_claims(text)
    if claims:
        return "DEPENDENT" if all("있어서" in c.body[:_PREAMBLE_WINDOW] for c in claims.values()) else "INDEPENDENT"
    return "DEPENDENT" if _PROPOSED_DEPENDENT.search(text) else None


def _cites(nos: list[int]) -> str:
    return ("제" + ", ".join(map(str, nos)) + "항") if nos else "없음(독립항)"


def proposal_outside_targets(baseline: list[dict], targets: list[int], text: str) -> list[str]:
    """What a pasted proposal changes that an edit of `targets` cannot take: other claims' wording, new claims, any citation.

    `baseline` rows are {claim_no, parent_nos, text}. An EXISTING_SET_EDIT keeps the user's set read-only apart from its
    targets and their citations, so these changes would be dropped silently if the run went ahead.
    """
    base = {int(c["claim_no"]): c for c in baseline}
    out: list[str] = []
    for no, claim in sorted(pasted_claims(text).items()):
        old = base.get(no)
        if old is None:
            out.append(f"제{no}항: 기존 세트에 없는 항")
            continue
        cite = f"인용 {_cites(list(old['parent_nos']))} → {_cites(claim.parent_nos)}" if claim.parent_nos != list(old["parent_nos"]) else ""
        if no in targets:
            if cite:
                out.append(f"제{no}항(편집 대상): {cite}")
        elif flatten(claim.text) != flatten(old["text"]):
            out.append(f"제{no}항: 문언 변경" + (f" · {cite}" if cite else ""))
    return out


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


def ordered_claims(*texts: str | None) -> list[Claim]:
    """Parse several claim texts into one list ordered by claim number (stable; duplicates stay for validation).

    A baseline parent chain and edited targets interleave by number (1, 5, 8 + 9, or 1, 8 + 5, 9), so they are merged
    by number rather than concatenated.
    """
    claims = [c for t in texts if t and t.strip() for c in parse_claim_set(t)]
    return sorted(claims, key=lambda c: c.claim_no)


def ancestor_nos(claims: list[Claim], target_no: int) -> set[int]:
    """Every claim the target cites directly or indirectly, over all alternatives of multi-dependent claims."""
    return {c.claim_no for chain in parent_chain(claims, target_no, expand_multi=True) for c in chain}


def baseline_digest(text: str) -> str:
    """Identity of a claim set independent of header spelling and line endings."""
    return exact_sha256(claim_set_text(parse_claim_set(text.replace("\r\n", "\n"))))


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
