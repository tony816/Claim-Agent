"""스타일 참고용 청구항 그래프: cs2-spec-writer의 `jev_graph_lossless`(명세서 병합본 126건·청구항 1,014개·인용관계)를 읽기만 한다.

문체 코퍼스(`sources/청구항_문체학습용_분야별검색최적화본.md`, 20세트·143항)는 같은 `명세서 병합본.docx`에서 엄격선별한
부분집합이다. 그래프는 그 원문 전체라서 표기·띄어쓰기를 몇 항이 쓰는지 비교할 수 있고, 종속항의 부모항을 인용관계로 따라갈 수
있다. 검색은 07 §3과 라우팅 인덱스 계약 그대로 정확 일치뿐이며 분야·카테고리로 고르지 않는다. 판례 그래프가 아니다.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..sources.corpus import CorpusIndex

EXPECTED = {"PatentCase": 126, "PatentClaim": 1014}
MAX_CHAIN = 8              # 부모항 체인에서 여는 항의 수 상한(대상 항 포함)


class ClaimGraphError(RuntimeError):
    """그래프 미설치·손상·다른 배포본. 일치하는 표현이 없다는 뜻이 아니다."""


@dataclass(frozen=True)
class GraphClaim:
    claim_id: str
    case_no: int               # 명세서 병합본 안의 사례 순번
    claim_no: int
    text: str
    parents: tuple[str, ...]   # 인용하는 청구항(REFERS_TO_CLAIM)
    corpus: str | None         # 문체 코퍼스에 수록된 항이면 "EX-01 청구항 6"
    flagged_review: bool       # 코퍼스의 알려진 재검토 대상

    def label(self) -> str:
        return f"사례 {self.case_no} 청구항 {self.claim_no}" + (f", 코퍼스 {self.corpus}" if self.corpus else ", 코퍼스 미수록")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _key(text: str) -> str:
    """코퍼스와 그래프의 같은 항을 맞춘다. 코퍼스 파서가 붙여 오는 끝의 `---`와 공백(no-break space 포함)만 무시한다."""
    return re.sub(r"\s+", "", re.sub(r"\n\s*-{3,}\s*$", "", text or ""))


@lru_cache(maxsize=2)
def _load(db_path: Path, fingerprint: tuple) -> tuple[str, tuple[tuple, ...], tuple[tuple[str, str], ...]]:
    sums = db_path.parent / "SHA256SUMS.txt"
    listed = next((line.split()[0] for line in sums.read_text(encoding="utf-8").splitlines() if line.strip().endswith(" graph.sqlite3")), None) \
        if sums.is_file() else None
    digest = _sha256(db_path)
    if listed is None or listed != digest:
        raise ClaimGraphError("청구항 그래프 graph.sqlite3가 같은 폴더 SHA256SUMS.txt와 다릅니다 — cs2-spec-writer의 jev_graph_lossless를 다시 복사하세요.")
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        counts = dict(db.execute("SELECT label, COUNT(*) FROM nodes WHERE label IN ('PatentCase','PatentClaim') GROUP BY label").fetchall())
        if counts != EXPECTED:
            raise ClaimGraphError(f"청구항 그래프 노드 수가 다릅니다: {counts}")
        cases = {cid: n for n, (cid,) in enumerate(db.execute("SELECT node_id FROM nodes WHERE label='PatentCase' ORDER BY ordinal"), 1)}
        claims = tuple((nid, cases.get(case, 0), int(no), text or "")
                       for nid, case, no, text in db.execute("SELECT node_id, case_id, ordinal, text FROM nodes WHERE label='PatentClaim' ORDER BY source_line"))
        refs = tuple(db.execute("SELECT start_id, end_id FROM relationships WHERE type='REFERS_TO_CLAIM' ORDER BY source_line").fetchall())
    return digest, claims, refs


@dataclass
class ClaimGraph:
    directory: Path
    sha256: str
    claims: dict[str, GraphClaim]

    @classmethod
    def open(cls, directory: Path, corpus_text: str) -> ClaimGraph:
        """검사한 그래프를 읽어 코퍼스 수록 여부를 붙인다. 없거나 다르면 ClaimGraphError."""
        db_path = directory / "graph.sqlite3"
        if not db_path.is_file():
            raise ClaimGraphError(f"청구항 그래프가 없습니다: {db_path} (docs/graph_layers.md)")
        st = db_path.stat()
        try:
            digest, rows, refs = _load(db_path.resolve(), (st.st_size, st.st_mtime_ns))
        except (OSError, sqlite3.Error) as exc:
            raise ClaimGraphError(f"청구항 그래프를 열지 못했습니다: {exc}") from exc
        parents: dict[str, list[str]] = {}
        for child, parent in refs:
            parents.setdefault(child, []).append(parent)
        corpus = CorpusIndex.parse(corpus_text)
        in_corpus = {_key(c.text): c for c in corpus.claims}
        claims = {}
        for nid, case_no, no, text in rows:
            c = in_corpus.get(_key(text))
            claims[nid] = GraphClaim(nid, case_no, no, text, tuple(parents.get(nid, ())), f"{c.ex_id} 청구항 {c.claim_no}" if c else None,
                                     bool(c and c.flagged_review))
        return cls(directory, digest, claims)

    def search(self, query: str, limit: int) -> tuple[int, int, list[tuple[GraphClaim, str]]]:
        """정확히 일치하는 항 수, 그중 코퍼스 수록 항 수, 그리고 조각 limit개.
        코퍼스 수록(재검토 대상 아님) → 미수록 → 재검토 대상 순이고, 같은 순위는 원문 순서다."""
        q = query.strip()
        hits = [c for c in self.claims.values() if q and q in c.text]
        rank = sorted(hits, key=lambda c: 2 if c.flagged_review else 0 if c.corpus else 1)
        out = []
        for c in rank[:limit]:
            i = c.text.find(q)
            out.append((c, c.text[max(0, i - 60):i + len(q) + 60].replace("\n", " ")))
        return len(hits), sum(1 for c in hits if c.corpus), out

    def chain(self, claim_id: str) -> list[GraphClaim]:
        """대상 항과 그 부모항들(인용관계를 끝까지), 청구항 번호순. 선행기재 확인용이다."""
        if claim_id not in self.claims:
            raise KeyError(claim_id)
        seen, todo = [], [claim_id]
        while todo and len(seen) < MAX_CHAIN:
            cid = todo.pop()
            if cid in seen or cid not in self.claims:
                continue
            seen.append(cid)
            todo.extend(self.claims[cid].parents)
        return sorted((self.claims[c] for c in seen), key=lambda c: c.claim_no)

    def describe(self) -> str:
        return f"스타일 참고용 청구항 그래프 `{self.directory.as_posix()}` (graph.sqlite3 sha256 {self.sha256[:12]}…, 126건·1,014항, 코퍼스 수록 {sum(1 for c in self.claims.values() if c.corpus)}항)"


def open_from_config(cfg, corpus_text: str) -> ClaimGraph:
    """`graph.claim_graph_dir`(프로젝트 기준 상대 경로 허용)의 그래프. `graph.claim_style: off`면 ClaimGraphError."""
    if cfg.graph.claim_style != "on":
        raise ClaimGraphError("graph.claim_style: off")
    p = Path(cfg.graph.claim_graph_dir)
    return ClaimGraph.open(p if p.is_absolute() else cfg.project_root / p, corpus_text)
