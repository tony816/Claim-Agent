"""판례 DB에서 특허성(신규성·진보성)과 권리행사(침해·권리범위) 판단 기록을 뽑는다.

사건마다 관련 쪽만 모델에 한 번 보내고, 답은 `graph/patent_case_law/extract/raw/`에 캐시한다. 기록은 인용문이 DB의 그 쪽
텍스트에서 다시 찾아질 때만 남긴다(공백 차이는 무시하고 찾은 뒤, 저장하는 인용문은 DB 원문 [quote_start:quote_end]).
요약·교훈 필드는 모델 추출이고 사람 법리 검토 전(미검토)이다.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import caselaw as cl

PAT_ISSUES = {"진보성", "신규성", "결합동기·결합가능성", "수치한정발명", "선택발명", "주지관용기술", "파라미터발명", "의약용도발명",
              "사후적 고찰", "결정형발명", "약리효과·약리기전"}
ENF_ISSUES = {"권리범위확인", "균등침해", "특허침해", "문언침해", "간접침해", "청구범위 해석", "기능적 청구항", "전제부·비구성요소 한정",
              "자유실시기술", "확인대상발명 특정", "과제해결원리·치환요건", "제조방법이 기재된 물건발명", "손해배상", "선사용권", "특허권 소진·수리"}
PAT_TERMS = ("진보성", "신규성", "차이점", "통상의 기술자", "용이하게", "결합", "임계적", "현저한 효과")
ENF_TERMS = ("균등", "권리범위", "침해", "확인대상발명", "구성요소", "문언", "자유실시", "간접", "치환")
MAX_CHARS = 36_000

# 특징 유형. 괄호의 06 항목은 같은 유형의 진보성 부정 점검 항목이다(그 항목의 판례 근거로 S12에 연결).
FEATURE_TYPES = {
    "F-SHAPE": "형상·크기·위치·방향·개수 (06 INV-04)",
    "F-MATERIAL": "재질 (06 INV-05)",
    "F-NUMERIC": "수치 범위 (06 INV-07)",
    "F-RELATION": "구성 사이의 결합·작동 관계 (06 INV-01·INV-10)",
    "F-ADD": "공지·일반 구성의 부가 (06 INV-02)",
    "F-SUBST": "구성의 치환·대체 (06 INV-03)",
    "F-MULTI": "복수화·병렬화 (06 INV-06)",
    "F-CONTROL": "제어·센서·자동화·데이터 처리 (06 INV-08)",
    "F-PROCESS": "공정 단계·순서·조건",
    "F-COMPOSITION": "조성·배합·화합물",
    "F-USE": "용도 (의약용도 포함)",
    "F-SELECTION": "선택발명",
    "F-PARAM": "파라미터·특성값",
    "F-OTHER": "기타",
}
PAT_FACTORS = ("결합 동기·암시 있음", "결합 동기 없음·결합 곤란", "반대 교시·저해 요인", "기술분야 상이", "주지관용기술",
               "통상의 창작능력·단순 설계변경", "예측 가능한 효과", "이질적·현저한 효과", "임계적 의의 있음", "임계적 의의 없음",
               "사후적 고찰 배척", "선행문헌에 같은 구성 개시", "실질적 동일", "기술적 과제 상이", "작용 원리 상이")
WORDING_TYPES = {
    "W01": "구성요소 완비 — 청구항 구성 하나가 없으면 문언침해 아님",
    "W02": "기능적 표현의 해석",
    "W03": "용어 해석 — 발명의 설명·도면 참작",
    "W04": "전제부·비구성요소 한정",
    "W05": "수치·범위 한정",
    "W06": "균등 — 과제해결원리",
    "W07": "균등 — 치환 가능성·용이성·작용효과",
    "W08": "의식적 제외 — 출원·정정 경과",
    "W09": "실시 주체 — 복수 주체·사용자 행위",
    "W10": "간접침해 — 그 물건의 생산에만 사용",
    "W11": "자유실시기술 — 공지기술에서 용이",
    "W12": "카테고리·제조방법 기재 물건",
    "W13": "확인대상발명 특정·입증",
    "W14": "기타",
}
REQUIREMENTS = ("신규성", "진보성", "문언침해", "균등침해", "권리범위확인", "간접침해", "자유실시기술", "청구범위 해석", "기타")
OUTCOMES = ("부정", "인정", "특허권자 유리", "특허권자 불리")

SYSTEM = f"""너는 한국 특허 판결문에서 법원의 판단을 기록으로 뽑는 추출기다. 판결문 밖의 지식으로 법리·사실·교훈을 보태지 않는다.

뽑는 것 두 가지:
1. kind=patentability: 법원이 청구항의 특정 구성(대비에서 나온 차이점 등)에 대해 신규성 또는 진보성을 판단한 곳. 차이점·특징마다 한 기록.
   - requirement: 신규성 또는 진보성. outcome: 부정(그 특징으로는 신규성·진보성이 부정됨) 또는 인정.
   - type_code: 그 특징의 유형 하나. {', '.join(f'{k}={v}' for k, v in FEATURE_TYPES.items())}
   - reason_factors: 법원이 든 이유에 해당하는 것만(없으면 빈 배열). 선택지: {', '.join(PAT_FACTORS)}
   - other_side: 법원이 본 선행발명의 대응 구성·개시 내용.
   - evidence_basis: 법원이 근거로 삼은 명세서 기재 또는 그 부재(예: '명세서에 수치 범위 안팎의 효과 비교가 없다'). 없으면 빈 문자열.
2. kind=enforcement: 권리범위확인·침해 사건에서 청구항 문언(구성요소, 용어, 기능적 표현, 전제부, 수치, 실시 주체, 카테고리 등) 때문에 침해·권리범위 속함 여부가 갈린 곳.
   - requirement: 문언침해, 균등침해, 권리범위확인, 간접침해, 자유실시기술, 청구범위 해석, 기타 중 하나. outcome: 특허권자 유리 또는 특허권자 불리.
   - type_code: {', '.join(f'{k}={v}' for k, v in WORDING_TYPES.items())}
   - other_side: 확인대상발명·침해제품의 대응 구성.
   - drafting_lesson: 청구항 작성자가 이 판단에서 배울 점 한 문장. 판결의 이유에서만 끌어내고, 명세서 작성 지시가 아니라 청구항 문언에 대한 것으로 쓴다.

공통 규칙:
- 법원이 직접 판단한 부분만 기록한다. 당사자 주장, 심결 요지, 다른 판결 요약은 기록하지 않는다.
- claim_feature: 판단 대상인 청구항 구성·문언을 판결문 표현대로 짧게.
- deciding_reason: 법원이 그렇게 판단한 이유를 판결문의 사실로 1~2문장.
- page: 인용문이 있는 쪽 번호([p.N]의 N). quote: 그 판단을 담은 법원 문장을 그 쪽 텍스트에서 글자 그대로 복사(40~400자). 고치거나 줄이거나 이어 붙이지 않는다.
- 각 kind마다 문서당 최대 4개. 해당하는 판단이 없으면 records를 빈 배열로 둔다.
- 해당 없는 필드는 빈 문자열로 둔다."""

SCHEMA = {
    "type": "object",
    "properties": {"records": {"type": "array", "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["patentability", "enforcement"]},
        "requirement": {"type": "string", "enum": list(REQUIREMENTS)},
        "outcome": {"type": "string", "enum": list(OUTCOMES)},
        "type_code": {"type": "string", "enum": [*FEATURE_TYPES, *WORDING_TYPES]},
        "reason_factors": {"type": "array", "items": {"type": "string", "enum": list(PAT_FACTORS)}},
        "claim_feature": {"type": "string"}, "other_side": {"type": "string"}, "deciding_reason": {"type": "string"},
        "evidence_basis": {"type": "string"}, "drafting_lesson": {"type": "string"},
        "page": {"type": "integer"}, "quote": {"type": "string"},
    }, "required": ["kind", "requirement", "outcome", "type_code", "claim_feature", "deciding_reason", "page", "quote"]}}},
    "required": ["records"],
}
VERSION = "1"


def cases(db: sqlite3.Connection) -> dict[str, dict]:
    """document_id → {case metadata, kinds}. 특허성·권리행사 쟁점이 있는 문서만."""
    out: dict[str, dict] = {}
    for (raw,) in db.execute("SELECT record_json FROM second_brain_cases"):
        j = json.loads(raw)
        labels = {i["label"] for i in j.get("issues") or []}
        kinds = (["patentability"] if labels & PAT_ISSUES else []) + (["enforcement"] if labels & ENF_ISSUES else [])
        if not kinds:
            continue
        doc = out.setdefault(j["document_id"], {"document_id": j["document_id"], "case_number": j.get("case_number") or "",
                                                "court": j.get("court") or "", "decision_date": j.get("decision_date") or "",
                                                "case_type": j.get("case_type") or "", "kinds": []})
        doc["kinds"] = sorted(set(doc["kinds"]) | set(kinds))
    return out


def pages(db: sqlite3.Connection, doc: str, kinds: list[str]) -> list[tuple[int, str]]:
    """관련어가 나오는 쪽과 그다음 쪽. 길면 관련어가 많은 쪽부터 MAX_CHARS까지(쪽 순서는 유지)."""
    rows = db.execute("SELECT physical_page, text FROM pdf_pages WHERE document_id=? ORDER BY physical_page", (doc,)).fetchall()
    terms = (PAT_TERMS if "patentability" in kinds else ()) + (ENF_TERMS if "enforcement" in kinds else ())
    hits = {p: sum(t.count(w) for w in terms) for p, t in rows}
    keep = {p for p, n in hits.items() if n} | {p + 1 for p, n in hits.items() if n}
    chosen = [(p, t) for p, t in rows if p in keep and t.strip()]
    if sum(len(t) for _, t in chosen) > MAX_CHARS:
        budget, picked = 0, set()
        for p, t in sorted(chosen, key=lambda x: -hits.get(x[0], 0)):
            if budget + len(t) <= MAX_CHARS:
                picked.add(p)
                budget += len(t)
        chosen = [(p, t) for p, t in chosen if p in picked]
    return chosen


def packet(meta: dict, chosen: list[tuple[int, str]]) -> str:
    want = " · ".join({"patentability": "특허성(신규성·진보성)", "enforcement": "권리행사(침해·권리범위)"}[k] for k in meta["kinds"])
    head = f"사건: {meta['court']} {meta['decision_date']} {meta['case_number']} [{meta['case_type']}]\n뽑을 판단: {want}\n\n"
    return head + "\n\n".join(f"[p.{p}]\n{t}" for p, t in chosen) + "\n\n스키마에 맞는 JSON 하나로 답한다."


_LETTER = re.compile(r"[가-힣]")


def locate(text: str, quote: str) -> tuple[int, int] | None:
    """한글만 비교해 quote를 text에서 찾고 원문 오프셋을 돌려준다.

    PDF 텍스트층은 숫자·문장부호·각주 번호·영문 약어를 엉뚱한 자리로 옮겨 두고('선행발명 4를' → '를.4', 'SMT 부품' → '부품SMT'),
    모델은 인용할 때 그것을 바로잡는다. 그래서 한글 글자열로만 찾는다. 저장하는 인용문은 언제나 DB 원문의 그 구간이다."""
    q = "".join(_LETTER.findall(quote))
    if len(q) < 15:
        return None
    idx = [i for i, ch in enumerate(text) if _LETTER.match(ch)]
    flat = "".join(text[i] for i in idx)
    at = flat.find(q)
    if at < 0:
        return None
    return idx[at], idx[at + len(q) - 1] + 1


def build_records(db: sqlite3.Connection, meta: dict, raw: dict) -> tuple[list[dict], list[dict]]:
    kept, rejected = [], []
    page_text = dict(db.execute("SELECT physical_page, text FROM pdf_pages WHERE document_id=?", (meta["document_id"],)).fetchall())
    for r in raw.get("records") or []:
        why = None
        pat = r.get("kind") == "patentability"
        if pat and (r.get("requirement") not in ("신규성", "진보성") or r.get("outcome") not in ("부정", "인정")
                    or r.get("type_code") not in FEATURE_TYPES):
            why = "특허성 기록의 requirement·outcome·type_code가 맞지 않음"
        elif not pat and (r.get("requirement") in ("신규성", "진보성") or r.get("outcome") not in ("특허권자 유리", "특허권자 불리")
                          or r.get("type_code") not in WORDING_TYPES):
            why = "권리행사 기록의 requirement·outcome·type_code가 맞지 않음"
        hit = None
        if not why:
            page = r.get("page")
            near = [page, page - 1, page + 1] if isinstance(page, int) else []
            order = [p for p in [*near, *sorted(set(page_text) - set(near))] if p in page_text]
            hit = next(((p, *span) for p in order if (span := locate(page_text[p], r.get("quote") or ""))), None)
            if not hit:  # 쪽 경계를 넘는 인용: p와 p+1을 이어 찾는다
                for p in order:
                    if p + 1 in page_text and (span := locate(page_text[p] + "\n" + page_text[p + 1], r.get("quote") or "")):
                        hit = (p, *span)
                        break
            if not hit:
                why = "인용문을 이 사건 원문에서 찾지 못함"
        if why:
            rejected.append({"document_id": meta["document_id"], "reason": why, "record": r})
            continue
        p, s, e = hit
        joined = page_text[p] + ("\n" + page_text[p + 1] if e > len(page_text[p]) else "")
        quote = joined[s:e]
        kept.append({**{k: meta[k] for k in ("document_id", "case_number", "court", "decision_date", "case_type")},
                     "kind": r["kind"], "requirement": r["requirement"], "outcome": r["outcome"], "type_code": r["type_code"],
                     "reason_factors": [f for f in r.get("reason_factors") or [] if f in PAT_FACTORS] if pat else [],
                     "claim_feature": r.get("claim_feature", ""), "other_side": r.get("other_side", ""),
                     "deciding_reason": r.get("deciding_reason", ""), "evidence_basis": r.get("evidence_basis", "") if pat else "",
                     "drafting_lesson": "" if pat else r.get("drafting_lesson", ""),
                     "page": p, "quote": quote, "quote_start": s, "quote_end": e, "quote_spans_next_page": e > len(page_text[p]),
                     "quote_is_argument": bool(re.search(r"주장한다|주장하나|주장하므로|라고 주장", quote)), "reviewed": False})
    return kept, rejected


def extract(paths: cl.Paths, provider, model: str, out_dir: Path, *, limit: int | None = None, docs: list[str] | None = None,
            workers: int = 8, rebuild: bool = False, log=print) -> dict:
    """모델 호출(캐시 없는 문서만) → 인용 재확인 → docs/caselaw-patentability.json·caselaw-enforcement.json."""
    from ..provider.base import CallSpec, GenParams, parse_json_text

    raw_dir = paths.directory / "extract" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with closing_db(paths) as db:
        meta = cases(db)
    todo = sorted(docs or meta)
    if limit:
        todo = todo[:limit]
    usage, failed = Counter(), []

    def one(doc: str) -> str:
        cache = raw_dir / f"{doc}.json"
        if cache.is_file() and not rebuild:
            return "cached"
        with closing_db(paths) as db:
            chosen = pages(db, doc, meta[doc]["kinds"])
        if not chosen:
            cache.write_text(json.dumps({"version": VERSION, "model": model, "records": [], "note": "관련 쪽 없음"}, ensure_ascii=False), encoding="utf-8")
            return "empty"
        spec = CallSpec(role="caselaw-extract", scope="INDEPENDENT", model=model, system_instruction=SYSTEM,
                        packet_text=packet(meta[doc], chosen), json_schema=SCHEMA,
                        gen=GenParams(temperature=0.0, thinking_level="LOW", max_output_tokens=8192), use_cache=False, phase="graph")
        for attempt in range(3):
            try:
                result = provider.generate(spec)
                break
            except Exception as exc:  # noqa: BLE001 - retry, then record the failure and move on
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
                log(f"  재시도 {doc}: {exc}")
        raw = result.parsed or parse_json_text(result.text)
        if raw is None:  # 원인을 볼 수 있게 답을 남기고 캐시는 만들지 않는다(다음 실행에서 다시 부른다)
            failed_dir = raw_dir.parent / "failed"
            failed_dir.mkdir(exist_ok=True)
            (failed_dir / f"{doc}.txt").write_text(result.text or "", encoding="utf-8")
            raise ValueError("JSON을 읽지 못함")
        for k, v in (result.usage or {}).items():
            usage[k] += int(v or 0)
        cache.write_text(json.dumps({"version": VERSION, "model": model, "pages": [p for p, _ in chosen], "usage": result.usage,
                                     "records": raw.get("records") or []}, ensure_ascii=False), encoding="utf-8")
        return "called"

    status = Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, d): d for d in todo}
        for n, f in enumerate(as_completed(futures), 1):
            d = futures[f]
            try:
                status[f.result()] += 1
            except Exception as exc:  # noqa: BLE001
                failed.append({"document_id": d, "error": f"{type(exc).__name__}: {exc}"[:300]})
                status["failed"] += 1
            if n % 25 == 0 or n == len(todo):
                log(f"  {n}/{len(todo)} {dict(status)} tokens {dict(usage)}")
    return {**write_records(paths, meta, out_dir), "status": dict(status), "failed": failed, "usage_this_run": dict(usage)}


def write_records(paths: cl.Paths, meta: dict, out_dir: Path) -> dict:
    """캐시의 모든 사건으로 기록 파일을 다시 쓴다. 사건마다 그 답을 만든 모델을 기록에 남긴다(여러 모델이 섞일 수 있다)."""
    raw_dir = paths.directory / "extract" / "raw"
    kept, rejected, models = [], [], Counter()
    with closing_db(paths) as db:
        for f in sorted(raw_dir.glob("*.json")):
            doc = f.stem
            if doc not in meta:
                continue
            raw = json.loads(f.read_text(encoding="utf-8"))
            models[raw.get("model") or "?"] += 1
            k, r = build_records(db, meta[doc], raw)
            kept += [{**x, "model": raw.get("model") or "?"} for x in k]
            rejected += r
    base = {"release_id": cl.RELEASE_ID, "extract_version": VERSION, "models": dict(models),
            "quote_check": "pdf_pages 원문에서 한글·영문자만 비교해 다시 찾고, 저장한 인용문은 원문 [quote_start:quote_end](코드 검사)",
            "reviewed": "요약·교훈은 모델 추출, 사람 법리 검토 전"}
    out = {}
    for kind, name, labels in (("patentability", "caselaw-patentability.json", FEATURE_TYPES),
                               ("enforcement", "caselaw-enforcement.json", WORDING_TYPES)):
        recs = [r for r in kept if r["kind"] == kind]
        (out_dir / name).write_text(json.dumps({**base, "type_labels": labels, "factors": list(PAT_FACTORS) if kind == "patentability" else [],
                                                "records": recs}, ensure_ascii=False, indent=1), encoding="utf-8")
        out[kind] = {"records": len(recs), "cases": len({r["document_id"] for r in recs})}
    (paths.directory / "extract" / "rejected.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=1), encoding="utf-8")
    out["rejected"] = len(rejected)
    out["raw_docs"] = len(list(raw_dir.glob("*.json")))
    return out


def closing_db(paths: cl.Paths):
    from contextlib import closing

    return closing(cl.connect(paths))
