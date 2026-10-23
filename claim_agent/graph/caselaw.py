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
from collections import Counter
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
    """기록의 인용문이 DB의 그 쪽 텍스트 [quote_start:quote_end]와 글자 단위로 같은지. 쪽 경계를 넘는 인용은 다음 쪽을 줄바꿈으로 이어 본다."""
    pages = (record["page"], record["page"] + 1) if record.get("quote_spans_next_page") else (record["page"],)
    texts = [db.execute("SELECT text FROM pdf_pages WHERE document_id=? AND physical_page=?", (record["document_id"], p)).fetchone()
             for p in pages]
    return all(texts) and "\n".join(t[0] for t in texts)[record["quote_start"]:record["quote_end"]] == record["quote"]


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


def _fact(r: dict) -> str:
    """교훈 기록은 deciding_fact, 특허성·권리행사 기록은 deciding_reason에 법원이 본 사실을 둔다."""
    return r.get("deciding_fact") or r.get("deciding_reason") or ""


def find(lessons: dict | list[dict], text: str, group: str | None = None, k: int = 5, type_code: str | None = None) -> list[tuple[float, dict]]:
    """지적·특징 문장과 청구항 문언·법원이 본 사실의 글자 2-gram 겹침으로 후보를 고른다(형태소 분석기 없이). 관련성 확인은 confirm()."""
    q = _grams(text)
    if not q:
        return []
    keep = GROUPS[group] if group else (lambda r: True)
    scored = []
    for r in lessons["records"] if isinstance(lessons, dict) else lessons:
        if not keep(r) or (type_code and r.get("type_code") != type_code):
            continue
        d = _grams(r["claim_feature"]) | _grams(_fact(r)) | _grams(r.get("other_side", ""))
        hit = len(q & d)
        if hit:
            scored.append((hit / math.sqrt(len(d)), r))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def confirm(finding: str, candidates: list[dict], client, question: str = JEV_QUESTION) -> dict:
    """후보마다 question(기본: 같은 종류의 결함인가)을 Jev noul로 한 요청에 묻는다. 반환은 JevClient.noul_batch 결과."""
    state = {"finding": finding,
             "cases": {f"case_{i}": {"사건": _cite(r), "요건": r["requirement"], "결과": r["outcome"], "청구항 문언": r["claim_feature"],
                                     "상대 구성": r.get("other_side", ""), "법원이 본 사실": _fact(r), "판결 인용": r["quote"]}
                       for i, r in enumerate(candidates)}}
    questions = {f"case_{i}": f"cases.case_{i}에 대해 답한다. " + question for i in range(len(candidates))}
    return client.noul_batch(state, questions)


# ---- 특허성(S12)과 권리행사(S13)

PATENT_SHEET_PATH = "sources/12_판례_특허성_유형기준.md"
ENFORCE_SHEET_PATH = "sources/13_판례_권리행사_작성기준.md"
PATENT_FILE = "docs/caselaw-patentability.json"
ENFORCE_FILE = "docs/caselaw-enforcement.json"

JEV_PATENT = ("finding의 특징과, 이 판결에서 법원이 신규성·진보성을 판단한 특징이 같은 유형이고 법원이 든 이유(결합 동기, 통상의 설계변경, "
              "수치의 임계적 의의, 예측 가능한 효과, 이질적 효과 등)가 finding의 특징에도 같은 방식으로 문제 되는가? "
              "기술 분야가 같다는 것만으로는 아니다.")
JEV_ENFORCE = ("finding이 가리키는 청구항 문언과, 이 판결에서 침해·권리범위 판단을 가른 청구항 문언이 같은 종류의 문언 문제인가? "
               "예: 기능으로만 적힌 구성이 실시예 구조로 좁게 해석됨, 전제부 구성도 구성요소로 봄, 청구항 구성 하나가 없어 문언침해 부정, "
               "수치 범위 밖이라 균등 부정, 여러 주체가 나누어 실시함, 제조방법으로 특정한 물건. 같은 기술 분야라는 것만으로는 아니다.")

# 법원이 진보성·신규성을 인정할 때 든 요인 → 원자료에서 확인할 것. 판단 자체는 선행기술이 있어야 한다.
FACTOR_QUESTIONS = {
    "임계적 의의 있음": "수치 범위 안과 밖의 효과 차이를 보여 주는 실험·비교가 원자료에 있는가",
    "이질적·현저한 효과": "선행기술에서 예측되지 않는 효과(종류가 다르거나 정도가 현저한 효과)를 원자료가 그 특징과 연결해 적는가",
    "결합 동기 없음·결합 곤란": "구성들을 함께 쓰기 어렵게 하는 사정(서로 다른 과제·작동 원리, 결합하면 기능을 잃음)을 원자료가 설명하는가",
    "반대 교시·저해 요인": "선행기술이 그 구성을 피하라고 가르치는 사정이 있는가(선행기술이 있어야 확인)",
    "기술적 과제 상이": "그 특징이 푸는 과제를 원자료가 구체적으로 적는가",
    "작용 원리 상이": "그 특징이 어떻게 작동해 효과를 내는지 원자료가 설명하는가",
    "기술분야 상이": "결합하려는 선행기술이 다른 기술분야인지(선행기술이 있어야 확인)",
    "사후적 고찰 배척": "발명의 해결 수단을 알고 난 뒤에야 보이는 결합인지(선행기술이 있어야 확인)",
}


def load_records(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("release_id") != RELEASE_ID or not isinstance(data.get("records"), list):
        raise CaseLawError(f"판례 기록의 release가 판례 DB와 다름: {path.name}")
    return data


def _digest(records: list[dict]) -> str:
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _cut(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _pick(rs: list[dict], n: int) -> list[dict]:
    """한 사건에서 하나씩, 당사자 주장처럼 보이는 인용은 빼고, 짧은 기록부터."""
    seen, out = set(), []
    for r in sorted(rs, key=lambda r: (r.get("quote_is_argument", False), len(r["claim_feature"]) + len(_fact(r)), r["case_number"])):
        if r["case_number"] in seen:
            continue
        seen.add(r["case_number"])
        out.append(r)
        if len(out) == n:
            break
    return out


def _counts(rs: list[dict], key=lambda r: r["outcome"]) -> Counter:
    return Counter(key(r) for r in rs)


def _source_line(data: dict, path: str) -> str:
    R = data["records"]
    models = ", ".join(f"`{m}` {n}건" for m, n in sorted((data.get("models") or {}).items()))
    return (f"> **출처:** 판례 DB `{RELEASE_ID}`에서 `claim-agent graph caselaw extract`가 뽑은 `{path}`(기록 {len(R)}개, 사건 "
            f"{len({r['document_id'] for r in R})}건, 추출 모델별 판결문 수 {models}, 내용 해시 `{_digest(R)}`). 이 파일은 "
            "`claim-agent graph caselaw sheet --kind ... --write`로 만들며 손으로 고치지 않는다. 요약·교훈은 모델 추출이고, 인용문만 판례 원문과 "
            "코드로 대조했다. 기록 전체는 `claim-agent graph caselaw find --set ...`로 찾는다.")


def sheet_patentability(data: dict) -> str:
    R = data["records"]
    lines = [
        "# 12. 판례 특허성 유형 기준", "",
        "> **상태:** 판례 기록 요약 — 사람 법리 검토 전(미검토). 최신 독립항 성공조건 미검증. 판정 기준·게이트 아님. 현재 발명의 원자료 아님.", ">",
        _source_line(data, PATENT_FILE), ">",
        "> **읽는 역할:** prior-art-pre-examiner(`PREEXAM_JUDGE`), dependent-claim-strategy-architect(`DEPENDENT_SET`), oa-strategy-reviewer. "
        "claim-architect·claim-drafter·claim-style-adjuster에는 전달하지 않는다(10과 같은 이유: 독립항이 처음부터 좁아지는 쪽으로 기울어 조건 8과 충돌).",
        "", "---", "", "## 사용 경계", "",
        "- **판례만으로 특허성을 판정하지 않는다.** 신규성·진보성은 봉인된 선행기술과의 대비로만 판정한다. 선행기술이 없으면 `UNVERIFIED`를 유지한다.",
        "- **분포는 법원에 온 사건의 분포다.** 다툼이 된 사건만 모였으므로 유형별 부정·인정 수는 등록 가능성 통계가 아니다.",
        "- **한정·효과·수치를 만들지 않는다.** 인정 요인을 원자료가 뒷받침하지 않으면 지어내지 않고 `원자료에 없음 — 작성자 확인 필요`로 남긴다.",
        "- **게이트를 바꾸지 않는다.** `PREEXAM_NOVELTY_GATE`·사전 심사 결정표·08의 세 게이트·`OA_DRAFT_GATE`는 각자의 기준으로만 정한다.",
        "- **쓰는 곳:** JUDGE는 `INVENTIVE_RISK`와 남는 차이의 이유에, 종속항 설계자는 후보 순위 신호와 근거 공백 질문에, OA는 06 INV 항목 지적의 판례 근거에 쓴다.",
        "", "## 인정 요인별 원자료 확인 질문", "",
        *[f"- **{k}** — {v}?" for k, v in FACTOR_QUESTIONS.items()], "",
        "## 유형별 판례", "",
    ]
    types = data.get("type_labels") or {}
    for code, label in types.items():
        rs = [r for r in R if r["type_code"] == code]
        if not rs:
            continue
        c = _counts(rs)
        lines += [f"### {code} {label} — 기록 {len(rs)}개 (부정 {c['부정']} · 인정 {c['인정']})", ""]
        for outcome, title in (("부정", "부정 이유"), ("인정", "인정 이유")):
            f = Counter(x for r in rs if r["outcome"] == outcome for x in r["reason_factors"])
            if f:
                lines.append(f"- {title} 상위: " + ", ".join(f"{k} {n}" for k, n in f.most_common(4)))
        for outcome, title, n in (("인정", "인정된 특징", 3), ("부정", "부정된 특징", 3)):
            picked = _pick([r for r in rs if r["outcome"] == outcome], n)
            if picked:
                lines += ["", f"{title}:", ""]
                for r in picked:
                    basis = f" 명세서 근거: {_cut(r['evidence_basis'], 110)}" if r.get("evidence_basis") else ""
                    lines.append(f"- **{_cut(r['claim_feature'], 80)}** — {_cut(_fact(r), 170)}{basis} ({_cite(r)}, {r['requirement']})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def sheet_enforcement(data: dict) -> str:
    R = data["records"]
    lines = [
        "# 13. 판례 권리행사 작성 기준", "",
        "> **상태:** 판례 기록 요약 — 사람 법리 검토 전(미검토). 최신 독립항 성공조건 미검증. 판정 기준·게이트 아님. 현재 발명의 원자료 아님.", ">",
        _source_line(data, ENFORCE_FILE), ">",
        "> **읽는 역할:** claim-architect(`INDEPENDENT`), claim-drafter(`INDEPENDENT`·`DEPENDENT_SET`), dependent-claim-strategy-architect. "
        "claim-success-reviewer는 사용 경계 준수만 감사한다. OA는 이 시트 대신 exact 문언의 점검표(`claim-agent graph caselaw enforce`)를 받는다.",
        "", "---", "", "## 사용 경계", "",
        "- **문언을 고르는 데 쓴다.** 같은 기술 개념을 어떤 문언으로 쓸지(구조·관계로 쓸지, 기능으로만 쓸지, 한 주체가 실시하는 단계로 쓸지)를 정할 때 본다.",
        "- **주골격·최소충분 한정을 바꾸지 않는다.** 시트를 이유로 한정을 더하거나 빼야 하면 architect 단계에서 성공조건과 원자료 근거로 다시 정한다(조건 8). drafter·style은 범위를 바꾸지 않는다.",
        "- **원자료에 없는 구조·주체·카테고리를 만들지 않는다.** 필요한 구조가 원자료에 없으면 `근거 미확인 — 작성자 확인 필요`로 남긴다.",
        "- **게이트를 바꾸지 않는다.** 권리행사 위험은 `회피설계 위험`·배치 후보·OA 지적의 참고이며 PASS·REVIEW를 정하지 않는다.",
        "- W13(확인대상발명 특정)은 심판 청구 실무라 이 시트에 넣지 않는다.",
        "", "## 유형별 판례", "",
    ]
    for code, label in (data.get("type_labels") or {}).items():
        rs = [r for r in R if r["type_code"] == code]
        if not rs or code == "W13":
            continue
        c = _counts(rs)
        lines += [f"### {code} {label} — 기록 {len(rs)}개 (특허권자 불리 {c['특허권자 불리']} · 유리 {c['특허권자 유리']})", ""]
        for outcome, title, n in (("특허권자 불리", "특허권자에게 불리했던 문언", 3), ("특허권자 유리", "특허권자에게 유리했던 문언", 2)):
            picked = _pick([r for r in rs if r["outcome"] == outcome], n)
            if picked:
                lines += [f"{title}:", ""]
                for r in picked:
                    other = f" 상대: {_cut(r['other_side'], 70)}." if r.get("other_side") else ""
                    lesson = f" → {_cut(r['drafting_lesson'], 130)}" if r.get("drafting_lesson") else ""
                    lines.append(f"- **{_cut(r['claim_feature'], 80)}**{other} {_cut(_fact(r), 150)}{lesson} ({_cite(r)})")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# 특징 문장의 낱말로 유형을 추정한다. 맞는 낱말이 없으면 비슷한 기록의 다수 유형을 쓴다. 호출자가 유형을 주는 쪽이 정확하다.
TYPE_WORDS = {
    "F-USE": r"용도|치료|예방|의약|투여",
    "F-COMPOSITION": r"조성|함량|중량\s*%|wt%|배합|화합물|혼합물",
    "F-PARAM": r"파라미터|특성값|지수",
    "F-NUMERIC": r"\d|수치|범위|이상|이하|미만|초과|비율",
    "F-MATERIAL": r"재질|소재|재료|합금|수지|고무|금속|세라믹",
    "F-CONTROL": r"제어|센서|감지|자동|알고리즘|프로그램|데이터|신호|산출",
    "F-PROCESS": r"단계|공정|순서",
    "F-MULTI": r"복수|다수|병렬|여러\s*개",
    "F-RELATION": r"결합|연결|연동|맞물|체결|지지|사이에",
    "F-SHAPE": r"형상|모양|각도|방향|위치|크기|길이|폭|곡률|단면|높이",
}


def guess_type(data: dict, text: str) -> tuple[str, str]:
    """(유형, 근거). 낱말 규칙에서 가장 많이 맞은 유형, 없으면 비슷한 기록 8개의 다수 유형."""
    hits = {code: len(re.findall(pat, text)) for code, pat in TYPE_WORDS.items()}
    best = max(hits.values())
    if best:
        return next(code for code, n in hits.items() if n == best), "낱말 규칙"
    votes = Counter(r["type_code"] for _, r in find(data["records"], text, k=8))
    return (votes.most_common(1)[0][0], "비슷한 기록") if votes else ("F-OTHER", "근거 없음")


def feature_signals(data: dict, text: str, type_code: str | None = None, k: int = 4) -> dict:
    """특징 하나의 판례 신호. 유형을 주지 않으면 guess_type()으로 추정한다. 판정이 아니다."""
    R = data["records"]
    guessed = type_code is None
    if guessed:
        type_code, how = guess_type(data, text)
    rs = [r for r in R if r["type_code"] == type_code]
    won = Counter(x for r in rs if r["outcome"] == "인정" for x in r["reason_factors"])
    lost = Counter(x for r in rs if r["outcome"] == "부정" for x in r["reason_factors"])
    return {"feature": text, "type_code": type_code, "type_label": (data.get("type_labels") or {}).get(type_code, ""),
            "type_guessed": how if guessed else "", "outcomes": dict(_counts(rs)), "deny_factors": lost.most_common(4), "grant_factors": won.most_common(4),
            "questions": [f"{f}: {FACTOR_QUESTIONS[f]}?" for f, _ in won.most_common(4) if f in FACTOR_QUESTIONS],
            "cases": [r for _, r in find(rs, text, k=k)]}


# 권리행사 점검: exact 청구항 문언에서 유형별 위험 문언을 코드로 찾는다. 질문일 뿐 판정이 아니다.
_ACTORS = ("사용자", "이용자", "고객", "관리자", "운영자", "판매자", "구매자", "서버", "단말", "클라이언트", "의사", "환자")
ENFORCE_CHECKS: list[tuple[str, re.Pattern, str]] = [
    ("W02", re.compile(r"[가-힣]+(?:는|기\s*위한|도록\s*하는)\s*[가-힣]*수단|[가-힣]+(?:기\s*위한|도록\s*구성된)\s*[가-힣]+부(?=[\s,;.은는이가을를의와과에])"),
     "기능으로 적힌 구성이다. 그 기능을 이루는 구조·관계가 청구항에 있는가? 없으면 발명의 설명의 구조로 좁게 해석될 수 있다."),
    ("W05", re.compile(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|㎛|μm|nm|%|중량\s*%|wt%|℃|°C|도|kPa|MPa|rpm|Hz|초|분|배)?\s*(?:이상|이하|미만|초과|내지|~)"),
     "수치 범위다. 범위 밖 실시에 균등이 부정된 판례가 있다. 그 범위가 발명에 꼭 필요하고 원자료가 근거를 주는가?"),
    ("W12", re.compile(r"(?:방법|공정)(?:으)?로\s*(?:제조|제작|생산)된|에\s*의(?:해|하여)\s*(?:제조|제작|얻어)"),
     "제조방법으로 특정한 물건이다. 구조·성질로 특정할 수 있는가? 판례 기록을 확인한다."),
    ("W01", re.compile(r"[가-힣]+만(?:으로|을|이)\s|오직|하지 않는|없이"),
     "배타·부정 한정이다. 청구항의 구성은 모두 구성요소로 보므로, 상대가 그 조건을 벗어나면 문언침해가 성립하지 않는다. 꼭 필요한 한정인가?"),
]


def split_claims(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"【\s*청구항\s*(\d+)\s*】", text)
    if len(parts) < 3:
        return [("1", text.strip())]
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]


def _snippet(text: str, s: int, e: int, pad: int = 25) -> str:
    return re.sub(r"\s+", " ", text[max(0, s - pad):e + pad]).strip()


def enforcement_flags(claims_text: str) -> list[dict]:
    flags = []
    for no, body in split_claims(claims_text):
        dependent = bool(re.match(r"\s*제\s*\d+\s*항", body))
        m = re.search(r"에\s*있어서\s*,", body[:400])
        # 전제부가 구성을 나열할 때만(…를 포함하는 장치에 있어서). 이름만 있는 '…장치에 있어서,'는 구성요소가 없다.
        if not dependent and m and re.search(r"포함|구비|갖는|가지는|;|및", body[:m.start()]):
            flags.append({"claim": no, "type_code": "W04", "snippet": _snippet(body, 0, m.end(), 0),
                          "question": "전제부(…에 있어서)의 구성도 구성요소로 본다. 전제부에 꼭 필요한 구성만 있는가?"})
        for code, pat, q in ENFORCE_CHECKS:
            spans = [(m.start(), m.end()) for m in pat.finditer(body)]
            if spans:  # 한 항·한 유형에 한 번. 이웃한 일치는 하나의 조각으로 합친다
                merged = []
                for s, e in spans:
                    if merged and s <= merged[-1][1] + 30:
                        merged[-1] = (merged[-1][0], e)
                    else:
                        merged.append((s, e))
                flags.append({"claim": no, "type_code": code, "question": q,
                              "snippet": " / ".join(_snippet(body, s, e) for s, e in merged[:3])})
        actors = sorted({a for a in _ACTORS if re.search(a + r"(?:가|이|는|에서|로부터|의 단말)", body)})
        if len(actors) >= 2 or any(re.search(a + r"(?:가|이)\s", body) for a in ("사용자", "이용자", "고객")):
            flags.append({"claim": no, "type_code": "W09", "snippet": ", ".join(actors) or "사용자 행위",
                          "question": "여러 주체(" + ", ".join(actors) + ")의 행위가 구성요소다. 한 주체가 모든 구성·단계를 실시하는 문언으로 쓸 수 있는가?"})
    return flags


def enforcement_report(data: dict, claims_text: str, k: int = 2) -> list[dict]:
    R = data["records"]
    out = []
    for f in enforcement_flags(claims_text):
        rs = [r for r in R if r["type_code"] == f["type_code"]]
        hits = [r for _, r in find(rs, f["snippet"] + " " + f["question"], k=k)] or _pick(rs, k)
        out.append({**f, "type_label": (data.get("type_labels") or {}).get(f["type_code"], ""), "cases": hits})
    return out


def enforcement_markdown(report: list[dict]) -> str:
    if not report:
        return "권리행사 판례 점검: 코드 점검 유형(W01·W02·W04·W05·W09·W12)에 해당하는 문언 없음.\n"
    lines = ["권리행사 판례 점검표 (미검토 — 게이트·근거 아님. 질문에 답할 근거는 원자료·설계 계약에서 찾는다)", ""]
    for f in report:
        lines.append(f"- 청구항 {f['claim']} · {f['type_code']} {f['type_label']} · `{f['snippet']}` — {f['question']}")
        for r in f["cases"]:
            lines.append(f"  - {_cite(r)} {r['outcome']}: {_cut(r['claim_feature'], 60)} — {_cut(_fact(r), 140)}")
    return "\n".join(lines) + "\n"


def label(p: float | None) -> str:
    if p is None:
        return "Jev 미확인"
    if p >= STRONG:
        return "Jev 확인"
    return "약한 관련" if p >= JEV_THRESHOLD else "관련 낮음"
