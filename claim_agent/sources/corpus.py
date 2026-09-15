"""Restricted access to the historical style corpus.

Contract (sources/07 §3 and 라우팅인덱스): only exact surface-expression lookups
for already-locked concepts, or 1–2 fragments for a precisely identified local
syntax problem. Field / architecture / category metadata must never be used
for retrieval, so the index is keyed by claim text only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_EX = re.compile(r"^##\s+(EX-\d{2})\b(.*)$")
_CLAIM = re.compile(r"^####\s+【청구항\s*(\d+)】")
_H1 = re.compile(r"^#\s+")

# Known review-flagged fragments listed in the corpus itself (EX-08 c3, EX-10 c5, EX-11 c6).
FLAGGED_REVIEW = {("EX-08", 3), ("EX-10", 5), ("EX-11", 6)}

MAX_FRAGMENTS = 2


@dataclass
class CorpusClaim:
    ex_id: str
    claim_no: int
    text: str
    line_start: int
    line_end: int

    @property
    def flagged_review(self) -> bool:
        return (self.ex_id, self.claim_no) in FLAGGED_REVIEW


@dataclass
class Fragment:
    ex_id: str
    claim_no: int
    fragment: str
    context: str
    flagged_review: bool

    def as_dict(self) -> dict:
        return {
            "ex_id": self.ex_id,
            "claim_no": self.claim_no,
            "fragment": self.fragment,
            "context": self.context,
            "flagged_review": self.flagged_review,
        }


@dataclass
class CorpusIndex:
    claims: list[CorpusClaim] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> CorpusIndex:
        lines = text.splitlines()
        claims: list[CorpusClaim] = []
        ex_id: str | None = None
        cur_no: int | None = None
        cur_start = 0
        buf: list[str] = []

        def flush(end: int) -> None:
            nonlocal buf, cur_no
            if ex_id and cur_no is not None:
                body = "\n".join(buf).strip()
                if body:
                    claims.append(CorpusClaim(ex_id, cur_no, body, cur_start, end))
            buf = []
            cur_no = None

        for i, line in enumerate(lines, start=1):
            m_ex = _EX.match(line)
            if m_ex:
                flush(i - 1)
                ex_id = m_ex.group(1)
                continue
            if _H1.match(line) and not line.startswith("##"):
                flush(i - 1)
                ex_id = None
                continue
            m_cl = _CLAIM.match(line)
            if m_cl:
                flush(i - 1)
                cur_no = int(m_cl.group(1))
                cur_start = i
                continue
            if line.startswith("###") or line.startswith("## "):
                flush(i - 1)
                continue
            if cur_no is not None:
                buf.append(line)
        flush(len(lines))
        return cls(claims)

    def _search(self, query: str, max_fragments: int, window: int = 60) -> list[Fragment]:
        q = query.strip()
        if not q:
            return []
        out: list[Fragment] = []
        for c in self.claims:
            idx = c.text.find(q)
            if idx < 0:
                continue
            lo = max(0, idx - window)
            hi = min(len(c.text), idx + len(q) + window)
            out.append(Fragment(c.ex_id, c.claim_no, q, c.text[lo:hi].replace("\n", " "), c.flagged_review))
            if len(out) >= max_fragments:
                break
        return out

    def search_exact_term(self, term: str, max_fragments: int = MAX_FRAGMENTS) -> list[Fragment]:
        """07 §3: surface name / spacing / shape predicate of a locked concept."""
        return self._search(term, min(max_fragments, MAX_FRAGMENTS))

    def search_local_syntax(self, phrase: str, max_fragments: int = MAX_FRAGMENTS) -> list[Fragment]:
        """라우팅인덱스: at most 2 fragments for one specified local syntax problem."""
        return self._search(phrase, min(max_fragments, MAX_FRAGMENTS))

    def get_claim(self, ex_id: str, claim_no: int) -> CorpusClaim | None:
        for c in self.claims:
            if c.ex_id == ex_id and c.claim_no == claim_no:
                return c
        return None


FORBIDDEN_QUERY_HINTS = ("분야", "아키텍처", "카테고리", "TF_", "ARCH_", "CAT_", "DENS_", "SUB_")


def is_forbidden_query(query: str) -> bool:
    return any(h in query for h in FORBIDDEN_QUERY_HINTS)
