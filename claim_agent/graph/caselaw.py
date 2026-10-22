"""판례 그래프 (cs2-spec-writer `graph/patent_case_law` 복제본): 읽기 전용 연결 검사, 청구항 주의 시트, 지적별 판례 찾기.

판례는 현재 발명의 원자료가 아니다. 주의 시트(`sources/11_판례_청구항_주의시트.md`)는 architect·drafter가 표현 위험을
피하는 데만 쓰고, 지적별 판례는 syntax·OA 지적에 붙이는 참고 근거다. 어느 쪽도 게이트 값을 바꾸거나 한정을 만들지 않는다.
교훈 기록(`docs/caselaw-lessons.json`)의 인용문은 판례 PDF 쪽 텍스트와 글자 일치를 코드로 확인했지만, 쟁점·결정 사실 요약은
모델이 뽑았고 사람 법리 검토 전(미검토)이다.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# 검증한 배포본의 신뢰 기준(cs2 pipeline/caselaw_db.py와 같은 값). 새 배포를 복제하면 명시적으로 갱신한다.
RELEASE_ID = "case-graph-20260930-v2"
PACK_ID = "pdfkg-f14e7eee8ccf39a8"
DB_SHA256 = "48ac0002320ee831f33765ee5dad5bf2846c6eea29df2e7c9cf7448c73af4d5b"
TABLES = ("metadata", "documents", "pdf_pages")

SHEET_PATH = "sources/11_판례_청구항_주의시트.md"

# 교훈 기록의 tag 분류. C 태그 이름은 기록 내용을 보고 붙인 라벨이다(원본 자료에는 'C1~C8: 명확성 분류'만 있다).
TAG_LABELS = {
    "C1": "용어의 뜻이 한 가지로 정해지지 않음",
    "C2": "기능·결과·용도만으로 구성을 특정",
    "C3": "정도·근사 표현(내외·난용성 등)에 기준 없음",
    "C4": "수치·측정 기준·기준점이 정해지지 않음",
    "C5": "가리키는 대상·선행기재가 불분명",
    "C6": "구성 사이의 작동·결합 관계가 불분명",
    "C7": "법원이 명확하다고 본 표현",
    "C8": "카테고리·말미·나열이 불분명",
    "P2": "청구범위 해석",
    "P4": "기능적 표현",
    "P5": "용어 정의",
}

GROUPS = {
    "명확성": lambda r: r["requirement"] == "명확성",
    "뒷받침": lambda r: r["requirement"] in ("뒷받침", "실시가능", "미완성"),
    "신규사항": lambda r: r["requirement"] in ("보정 신규사항", "정정 범위"),
    "권리범위": lambda r: r["topic"] == "권리범위",
}

# 같은 '조문'이라는 것만으로는 같은 종류가 아니다(cs2 실측: 일반 사례가 0.4~0.7로 통과했다). 빠진 것의 종류를 대조하게 한다.
JEV_QUESTION = ("finding이 지적한 청구항 문언의 결함과, 이 판결에서 법원이 판단한 결함이 같은 종류인가? "
                "같은 종류란 무엇이 빠졌거나 불분명한지가 같은 것이다. 예: 용어의 뜻이 여러 가지로 읽힘, 정도 표현에 기준 없음, "
                "수치의 측정 기준·기준점 없음, 기능·결과만 있고 그것을 이루는 구조 없음, 가리키는 대상이 불분명, "
                "구성 사이의 결합·작동 관계 없음, 청구항이 발명의 설명보다 넓거나 다름, 실시예 형태로 좁게 해석됨. "
                "같은 조문(명확성·뒷받침)이라는 것만으로는 같은 종류가 아니다. "
                "법원이 기재가 충분하다고 본 판결은, 그 판결에서 충분하다고 인정한 바로 그 내용이 finding에서 빠진 경우에만 같은 종류다.")
JEV_THRESHOLD = 0.5
STRONG = 0.7


class CaseLawError(RuntimeError):
    """자료 미설치·잘못된 배포본·손상. '맞는 판례 없음'과는 다르다."""


@dataclass(frozen=True)
class Paths:
    directory: Path
    lessons: Path

    @classmethod
    def from_config(cls, cfg) -> Paths:
        def resolve(p: str) -> Path:
            q = Path(p)
            return q if q.is_absolute() else cfg.project_root / q
        return cls(resolve(cfg.graph.caselaw_dir), resolve(cfg.graph.caselaw_lessons))

    @property
    def db(self) -> Path:
        return self.directory / "graph.sqlite"

    @property
    def manifest(self) -> Path:
        return self.directory / "RELEASE_MANIFEST.json"

    @property
    def pdfs(self) -> Path:
        return self.directory / "pdfs"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _open(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


@lru_cache(maxsize=4)
def _validate(db_path: Path, manifest_path: Path, fingerprint: tuple, full: bool) -> str:
    if _sha256(db_path) != DB_SHA256:
        raise CaseLawError("판례 DB SHA-256 불일치 — cs2-spec-writer의 검증된 배포본에서 다시 복제하세요.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("release_id") != RELEASE_ID or manifest.get("source_pack_id") != PACK_ID:
        raise CaseLawError("판례 release/pack 버전 불일치")
    with closing(_open(db_path)) as db:
        for table in TABLES:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                raise CaseLawError(f"판례 필수 테이블 없음: {table}")
        row = db.execute("SELECT value FROM metadata WHERE key='release'").fetchone()
        if not row or json.loads(row[0]) != manifest:
            raise CaseLawError("판례 manifest와 DB 내부 배포 기록 불일치")
        counts = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in ("documents", "pdf_pages")}
        if counts != {"documents": manifest["counts"]["documents"], "pdf_pages": manifest["pdf_physical_pages"]}:
            raise CaseLawError("판례 행 수와 배포 기록 불일치")
        if full and [r[0] for r in db.execute("PRAGMA integrity_check")] != ["ok"]:
            raise CaseLawError("판례 SQLite 무결성 검사 실패")
    return json.dumps({"status": "PASS", "release_id": RELEASE_ID, "source_pack_id": PACK_ID, "db_sha256": DB_SHA256,
                       "counts": counts, "integrity_checked": full}, ensure_ascii=False)


def _fingerprint(p: Paths) -> tuple:
    out = []
    for f in (p.db, p.manifest):
        if not f.is_file():
            raise CaseLawError(f"판례 파일 없음: {f} (docs/caselaw.md의 복제 절차 참고)")
        st = f.stat()
        out.append((str(f.resolve()), st.st_size, st.st_mtime_ns))
    return tuple(out)


def validate(p: Paths, full: bool = False) -> dict:
    """SHA-256·release·manifest·행 수를 확인한다. full이면 SQLite integrity_check도 한다(수십 초). 법리 검증이 아니다."""
    try:
        result = json.loads(_validate(p.db, p.manifest, _fingerprint(p), full))
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        raise CaseLawError(f"판례 DB 검사 실패: {exc}") from exc
    result["pdf_files_present"] = len(list(p.pdfs.glob("*.pdf"))) if p.pdfs.is_dir() else 0
    return result


def connect(p: Paths) -> sqlite3.Connection:
    """검사를 통과한 DB만 읽기 전용으로 연다. 없는 경로에 빈 DB를 만들지 않는다. 호출자가 닫는다."""
    validate(p)
    return _open(p.db)


# ---- 교훈 기록

def load_lessons(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("release_id") != RELEASE_ID or not isinstance(data.get("records"), list):
        raise CaseLawError(f"교훈 기록의 release가 판례 DB와 다름: {data.get('release_id')}")
    return data


def quote_ok(db: sqlite3.Connection, record: dict) -> bool:
    """기록의 인용문이 DB의 그 쪽 텍스트 [quote_start:quote_end]와 글자 단위로 같은지."""
    row = db.execute("SELECT text FROM pdf_pages WHERE document_id=? AND physical_page=?",
                     (record["document_id"], record["page"])).fetchone()
    return bool(row) and row[0][record["quote_start"]:record["quote_end"]] == record["quote"]


def _cite(r: dict) -> str:
    return f"{r['court']} {r['case_number']}"


def sheet(lessons: dict) -> str:
    """architect·drafter용 주의 시트. 판례 인용문·교훈 문장(명세서 작성용)은 넣지 않고 청구항 문언과 법원이 본 사실만 둔다."""
    R = lessons["records"]
    digest = hashlib.sha256(json.dumps(R, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    bad = lambda r: r["outcome"] in ("위반", "특허권자 불리")  # noqa: E731
    lines = [
        "# 11. 판례 청구항 주의 시트",
        "",
        "> **상태:** 판례 요약 — 사람 법리 검토 전(미검토). 최신 독립항 성공조건 미검증. 판정 기준·게이트 아님. 현재 발명의 원자료 아님.",
        ">",
        f"> **출처:** 판례 DB `{RELEASE_ID}`(문서 1,163건)에서 cs2-spec-writer가 뽑은 교훈 기록 `docs/caselaw-lessons.json`"
        f"(기록 {len(R)}개, 내용 해시 `{digest}`). 이 파일은 `claim-agent graph caselaw sheet --write`로 만들며 손으로 고치지 않는다. "
        "쟁점·결정 사실은 모델 요약이고 인용문만 판례 PDF 쪽 텍스트와 글자 일치를 코드로 확인했다. 원문은 사건번호로 `claim-agent graph caselaw show`에서 본다.",
        ">",
        "> **읽는 역할:** claim-architect(`INDEPENDENT`)와 claim-drafter(`INDEPENDENT`·`DEPENDENT_SET`). claim-success-reviewer는 사용 경계 준수만 감사한다. "
        "그 밖의 역할에는 전달하지 않는다. syntax·OA 지적에 붙는 판례는 이 시트가 아니라 오케스트레이터의 지적별 조회(`claim-agent graph caselaw find`)로 간다.",
        "",
        "---",
        "",
        "## 사용 경계",
        "",
        "- **피할 표현 유형을 알아보는 데만 쓴다.** 아래 사례는 법원이 어떤 청구항 문언을 불명확하다고 보거나, 발명의 설명과 맞지 않는다고 보거나, 좁게 해석했는지를 보여 준다.",
        "- **한정·구성·수치·효과를 만들지 않는다.** 위험을 없애려면 원자료에 없는 기준·구조·수치가 필요하면 지어내지 말고 `근거 미확인 — 작성자 확인 필요`로 남긴다. 사례의 기술 내용은 현재 발명의 근거가 아니다.",
        "- **게이트를 바꾸지 않는다.** DESIGN_GATE, DRAFTER_GATE와 성공조건 판정은 성공조건과 원자료로만 정한다. 이 시트와 맞지 않는다는 이유로 PASS를 REVIEW로 낮추거나 그 반대로 하지 않는다.",
        "- **주골격·최소충분 한정을 좁히지 않는다.** 권리범위 사례는 '발명의 설명이 범위를 좁혔다'는 기록이다. 독립항에 실시예 형태를 더하라는 뜻이 아니다(성공조건 조건 8).",
        "- **용어·문체는 여전히 style 단계가 정한다.** drafter는 이 시트로 표면 용어를 고르지 않는다. 표현 위험이 남으면 의미 초안의 독자 검사에 적어 style adjuster와 success reviewer가 보게 한다.",
        "",
    ]

    def entries(rs, full: bool):
        for r in rs:
            if full:
                lines.append(f"- **{r['claim_feature']}** — {r['deciding_fact']} ({_cite(r)})")
            else:
                lines.append(f"- {r['claim_feature']} ({_cite(r)})")

    lines += ["## 1. 명확성 — 법원이 불명확하다고 본 청구항 문언 (특허법 제42조 제4항 제2호)", ""]
    clar = [r for r in R if GROUPS["명확성"](r)]
    for tag in ("C1", "C2", "C3", "C4", "C5", "C6", "C8"):
        rs = [r for r in clar if r["tag"] == tag and bad(r)]
        if rs:
            lines += [f"### {tag} {TAG_LABELS[tag]}", ""]
            entries(rs, True)
            lines.append("")
    lines += ["### C7 법원이 명확하다고 본 표현 (위 유형이라는 것만으로 불명확하지는 않다)", ""]
    entries([r for r in clar if not bad(r)], False)
    lines += ["", "## 2. 뒷받침 — 청구항이 발명의 설명과 맞지 않거나 더 넓다고 본 사례 (제42조 제4항 제1호)", ""]
    entries([r for r in R if r["requirement"] == "뒷받침" and bad(r)], True)
    lines += ["", "## 3. 권리범위 — 분쟁에서 청구항 문언이 해석된 방식", ""]
    for tag in ("P2", "P4", "P5"):
        rs = [r for r in R if r["tag"] == tag]
        lines += [f"### {tag} {TAG_LABELS[tag]}", "", "특허권자에게 불리하게 해석된 사례:", ""]
        entries([r for r in rs if bad(r)], True)
        good = [r for r in rs if not bad(r)]
        if good:
            lines += ["", "특허권자에게 유리하게 해석된 사례:", ""]
            entries(good, False)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _grams(text: str) -> set[str]:
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def find(lessons: dict, text: str, group: str | None = None, k: int = 5) -> list[tuple[float, dict]]:
    """지적 문장과 청구항 문언·결정 사실의 글자 2-gram 겹침으로 후보를 고른다(형태소 분석기 없이). 관련성 확인은 confirm()."""
    q = _grams(text)
    if not q:
        return []
    keep = GROUPS[group] if group else (lambda r: True)
    scored = []
    for r in lessons["records"]:
        if not keep(r):
            continue
        d = _grams(r["claim_feature"]) | _grams(r["deciding_fact"])
        hit = len(q & d)
        if hit:
            scored.append((hit / math.sqrt(len(d)), r))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def confirm(finding: str, candidates: list[dict], client) -> dict:
    """후보마다 'finding과 같은 종류의 결함인가'를 Jev noul로 한 요청에 묻는다. 반환은 JevClient.noul_batch 결과."""
    state = {"finding": finding,
             "cases": {f"case_{i}": {"사건": _cite(r), "요건": r["requirement"], "결과": r["outcome"],
                                     "청구항 문언": r["claim_feature"], "법원이 본 사실": r["deciding_fact"], "판결 인용": r["quote"]}
                       for i, r in enumerate(candidates)}}
    questions = {f"case_{i}": f"cases.case_{i}에 대해 답한다. " + JEV_QUESTION for i in range(len(candidates))}
    return client.noul_batch(state, questions)


def label(p: float | None) -> str:
    if p is None:
        return "Jev 미확인"
    if p >= STRONG:
        return "Jev 확인"
    return "약한 관련" if p >= JEV_THRESHOLD else "관련 낮음"
