"""Prior-art search (PRIOR_*) and pre-exam (PRE_EXAM) stages of PipelineEngine.

docs/선행기술조사_사전심사_설계_2026-10.md. WIPS and Jev calls are engine work, not role calls: they are counted
against `prior_search.max_wips_calls` (not `pipeline.max_calls`) and written as `wips` / `jev` telemetry rows that
never carry query text, keys or tokens. Jev probabilities only pick documents; disclosure is decided from COMPARE
quotes that the engine re-finds in the document text (설계 5.2), and the novelty gate follows 설계 5.3.
"""
from __future__ import annotations

import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..models.contracts import contract_for
from ..models.enums import DisclosureStatus, ExecStatus, NoveltyConclusion, RequestMode, RiskLevel, Scope
from ..models.envelope import RoleEnvelope
from ..models.ids import Identifiers, input_revision_id, record_id
from ..models.prior_art import LABEL_NONE, PriorArtDoc, PriorArtState, PriorSearchOptions, relevance_band
from ..models.request import EXISTING_SET_EDIT, MaterialBundle, RunRequest
from ..models.state import Halt, RecordRef, RunState, Stage
from ..prior_art import wips as wips_mod
from ..prior_art.quotes import find_quote
from ..prior_art.timing import timing
from ..provider.base import ImagePart
from ..store.telemetry import TelemetryRow, TelemetryWriter, now
from . import packets

# Relevance questions, not disclosure questions: on the 2026-10-21 live run (117 WIPS candidates for the clip holder) the
# disclosure wording ("핵심 구성과 그 결합 대부분을 개시하는가") topped out at 0.25 and sealed nothing, because a title and
# abstract almost never show a whole combination. These wordings ranked the cable holders first (19 ≥ 0.5 when screening;
# 0.50/0.44/0.42 against 0.15/0.19 when trimming the top 5). Disclosure itself is decided later from COMPARE quotes.
SCREEN_QUESTION = ("candidates의 {key} 후보는 invention의 신규성·진보성을 따질 때 대비할 가치가 있는 선행문헌인가. "
                   "같은 과제를 풀거나 invention의 핵심 구성 일부를 같은 쓰임으로 갖고 있으면 그렇다. 분야나 낱말만 같으면 아니다.")
TRIM_QUESTION = ("docs의 {key} 문헌의 청구범위·요약에 invention의 핵심 구성(elements) 가운데 둘 이상이 같은 쓰임으로 나와 "
                 "구성 단위로 대비할 가치가 있는가. 분야나 낱말만 같으면 아니다.")
STATUS_KO = {"DISCLOSED": "개시", "PARTIALLY_DISCLOSED": "일부 개시", "NOT_DISCLOSED": "미개시", "UNCONFIRMED": "확인 필요"}
CONCLUSION_KO = {"NEGATED_RISK": "부정 우려", "NOT_NEGATED": "인정 가능", "UNDETERMINED": "판단 보류"}
# Classification fields of 10_심사이력_경험기준 never travel to the architect (설계 14.3).
EXAM_FIELDS = ("exam_pattern", "design_change_risk", "effect_link", "why_not_simple", "numeric_basis")


@dataclass
class PriorArtTools:
    """WIPS client (anything with `call(tool, args)`) and Jev client (`noul_batch`, `noul`)."""

    wips: Any = None
    jev: Any = None


@dataclass
class _DocView:
    doc_id: str
    label: str
    sha256: str
    text: str | None
    image: ImagePart | None
    published: str = ""
    path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class _CountingWips:
    """Counts every WIPS tool call against max_wips_calls and writes one `wips` telemetry row per call."""

    def __init__(self, inner: Any, engine: Any, state: RunState, stage: Stage):
        self.inner, self.engine, self.state, self.stage = inner, engine, state, stage

    def call(self, tool: str, args: dict) -> dict:
        pa = self.state.prior_art
        assert pa is not None
        if pa.wips_calls >= pa.options.max_wips_calls:
            raise wips_mod.WipsError(f"WIPS 호출 한도 도달: {pa.wips_calls}/{pa.options.max_wips_calls} (prior_search.max_wips_calls)")
        t0 = time.monotonic()
        ok, err = True, None
        try:
            return self.inner.call(tool, args)
        except Exception as exc:
            ok, err = False, type(exc).__name__
            raise
        finally:
            pa.wips_calls += 1
            self.engine._aux_telemetry(self.state, self.stage, "wips", {"name": tool, "country": args.get("ctry"), "ok": ok, "error": err},
                                       int((time.monotonic() - t0) * 1000))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "doc"


class PriorArtSteps:
    """Mixin for PipelineEngine (uses its _call, _decide, _halt, _bump_design, store, cfg)."""

    prior_art_tools: PriorArtTools | None = None

    # ------------------------------------------------------------------ setup
    def _init_prior_art(self, state: RunState, request: RunRequest) -> None:
        """Freeze this run's options (config defaults + request overrides) and decide whether the run starts with a search."""
        opts = self.cfg.prior_search.model_copy()
        if request.prior_search is not None:
            opts = PriorSearchOptions.model_validate({**opts.model_dump(), **request.prior_search.model_dump(exclude_unset=True)})
        eligible = request.request_mode in (RequestMode.AUTHORING_DRAFT, RequestMode.FINALIZATION) and request.authoring_scope != EXISTING_SET_EDIT
        if not (opts.enabled and eligible):
            return
        base, source = (opts.base_date, "REQUEST") if opts.base_date else (date.today().isoformat(), "RUN_DATE")
        state.prior_art = PriorArtState(options=opts, base_date=base, base_date_source=source)
        state.stage = Stage.PRIOR_QUERY

    def _prior_tools(self) -> PriorArtTools:
        if self.prior_art_tools is not None:
            return self.prior_art_tools
        from ..prior_art import fixtures
        from ..prior_art.jev import JevClient
        from ..provider.replay import RecordingProvider, ReplayProvider

        if isinstance(self.provider, ReplayProvider):
            tools = PriorArtTools(fixtures.FixtureWips(Path(self.provider.dir) / "wips"), fixtures.FixtureJev(Path(self.provider.dir) / "jev"))
        elif isinstance(self.provider, RecordingProvider):
            tools = PriorArtTools(fixtures.RecordingWips(wips_mod.WipsClient(), Path(self.provider.dir) / "wips"),
                                  fixtures.RecordingJev(JevClient(), Path(self.provider.dir) / "jev"))
        else:
            tools = PriorArtTools(wips_mod.WipsClient(), JevClient())
        self.prior_art_tools = tools
        return tools

    def _base_date(self, state: RunState) -> str:
        if state.prior_art is not None:
            return state.prior_art.base_date
        return date.fromtimestamp(state.created_at).isoformat()

    def _reload_bundle(self, state: RunState, req: RunRequest) -> MaterialBundle:
        state.request = req.model_dump(mode="json")
        bundle = self._load_bundle(req)
        self.store.save_materials(state.run_id, bundle)
        state.input_revision = input_revision_id(bundle.digest())
        state.material_meta = [i.as_meta() for i in bundle.items]
        state.spec_present = bundle.spec_present
        state.prior_art_present = bundle.prior_art_present
        self._bundle = bundle
        return bundle

    # ------------------------------------------------------------------ telemetry (engine work)
    def _aux_telemetry(self, state: RunState, stage: Stage, phase: str, tool: dict[str, Any], latency_ms: int) -> None:
        c = state.candidate
        row = TelemetryRow(
            ts=now(), run_id=state.run_id, seq=state.call_seq, stage=stage.value, role="engine", scope=phase.upper(), request_mode=state.request_mode.value,
            model="", provider=phase, variant_id=state.variant_id, source_set_id=state.source_set_id, lessons_hash=state.lessons_hash,
            candidate_id=c.candidate_id, revision=c.revision, design_revision=c.design_revision, dependent_set_id=None, dependent_revision=None,
            target_claim_id=None, loop_index=0, retry_count=0, prompt_tokens=0, cached_tokens=0, thoughts_tokens=0, output_tokens=0,
            latency_ms=latency_ms, cache_hit=False, parse_ok=True, repair_used=False, execution_status="RUN", status="PASS" if tool.get("ok", True) else "FAILED",
            reason_code=None, phase=phase, tool=tool,
        )
        TelemetryWriter(self.store.telemetry_path(state.run_id), self.cfg.telemetry.enabled).write(row)

    def _jev_cost(self, state: RunState, stage: Stage, res: dict) -> None:
        """OpenRouter's Jev bill joins cost_usd (pipeline.max_cost_usd); without one it is an unpriced call. Never counted in `calls`."""
        if res.get("status") != "RUN":
            return
        cost = res.get("cost_usd")
        with self._lock:
            state.add_usage(stage.value, {"cost_usd": float(cost)} if cost is not None else {"unpriced_calls": 1})

    def _jev_batch(self, state: RunState, stage: Stage, jev_state: dict, questions: dict[str, str]) -> dict[str, float] | None:
        """Probabilities per question key, or None when Jev did not run (batch, then one request per question)."""
        jev = self._prior_tools().jev
        if jev is None:
            return None
        t0 = time.monotonic()
        res = jev.noul_batch(jev_state, questions)
        self._jev_cost(state, stage, res)
        self._aux_telemetry(state, stage, "jev", {"mode": "batch", "question_count": len(questions), "state_chars": res.get("state_chars"), "status": res.get("status"),
                                                  "ok": res.get("status") == "RUN"}, int((time.monotonic() - t0) * 1000))
        if res.get("status") == "RUN":
            return {k: float(v) for k, v in (res.get("probabilities") or {}).items()}
        if res.get("status") == "NOT_RUN":
            state.prior_art.search_warnings.append(f"Jev 미실행({stage.value}): {res.get('error') or 'state 한도 초과'}")  # type: ignore[union-attr]
            return None
        probs: dict[str, float] = {}
        for key, q in questions.items():
            t1 = time.monotonic()
            one = jev.noul(jev_state, q)
            self._jev_cost(state, stage, one)
            self._aux_telemetry(state, stage, "jev", {"mode": "single", "question_count": 1, "state_chars": one.get("state_chars"), "status": one.get("status"),
                                                      "ok": one.get("status") == "RUN"}, int((time.monotonic() - t1) * 1000))
            if one.get("status") == "RUN" and one.get("probability") is not None:
                probs[key] = float(one["probability"])
        if not probs:
            state.prior_art.search_warnings.append(f"Jev 실패({stage.value}): {res.get('error') or '응답 없음'}")  # type: ignore[union-attr]
            return None
        return probs

    def _invention_state(self, pa: PriorArtState) -> dict[str, Any]:
        return {"problem": pa.problem, "elements": pa.provisional_elements}

    # ------------------------------------------------------------------ PRIOR_* stages
    def _prior_step(self, state: RunState, bundle: MaterialBundle) -> None:
        st = state.stage
        pa = state.prior_art
        assert pa is not None, "prior-art stage without prior_art state"
        req = RunRequest.model_validate(state.request)
        c = state.candidate
        ids = Identifiers(c.candidate_id, c.revision, c.design_revision, input_revision=state.input_revision)

        if st == Stage.PRIOR_QUERY:
            rid = record_id("prior_query", ids)
            pk = packets.prior_query(state, bundle, ids, rid, req.request_text, pa.options.countries)
            env, _ = self._call(state, "prior-art-searcher", Scope.PRIOR_QUERY, st, "prior_query", pk, ids, rid)
            self._decide(state, env, "prior-art-searcher", Scope.PRIOR_QUERY, st, rid, c.loop_counts)
            pa.query_record_id = rid
            pa.queries_ko = [q.strip() for q in env.queries_ko if q.strip()][:3]
            pa.queries_en = [q.strip() for q in env.queries_en if q.strip()][:3]
            pa.provisional_elements = [e.strip() for e in env.provisional_elements if e.strip()]
            pa.problem = (env.problem or "").strip()
            state.stage = Stage.PRIOR_SEARCH
            return

        if st == Stage.PRIOR_SEARCH:
            client = _CountingWips(self._prior_tools().wips, self, state, st)
            try:
                hits, warns = wips_mod.search_all(client, pa.queries_ko, pa.queries_en, pa.options.countries, pa.options.size_per_query)
            except wips_mod.WipsError as exc:
                raise self._halt_exc(state, st, "UNVERIFIED", "PRIOR_SEARCH_UNAVAILABLE",
                                     f"WIPS 선행기술 검색을 할 수 없습니다: {exc}. 검색 없이(PRIOR_ART_SET: NONE) 진행하려면 `--accept-unverified`로 재개하고, "
                                     "검색하려면 .env의 WIPS_API_KEY를 확인한 뒤 같은 run을 재개하세요.") from exc
            pa.hits = hits
            pa.search_warnings.extend(warns)
            state.stage = Stage.PRIOR_SCREEN
            return

        if st == Stage.PRIOR_SCREEN:
            self._screen(state, bundle, ids)
            return

        if st == Stage.PRIOR_FETCH:
            client = _CountingWips(self._prior_tools().wips, self, state, st)
            folder = self.store.run_dir(state.run_id) / "prior_art"
            folder.mkdir(parents=True, exist_ok=True)
            by_skey = {h["skey"]: h for h in pa.hits}
            have = {d.skey for d in pa.docs}
            for skey in pa.selected:
                if skey in have or skey not in by_skey:
                    continue
                hit = by_skey[skey]
                try:
                    text, meta = wips_mod.fetch(client, hit)
                except wips_mod.WipsError as exc:
                    pa.search_warnings.append(f"{wips_mod.number(hit)} 전문 수집 실패: {exc}")
                    continue
                path = folder / f"{_safe(skey)}.md"
                path.write_text(text, encoding="utf-8")
                (folder / f"{_safe(skey)}.claims.md").write_text(meta.get("claims_text") or "", encoding="utf-8")
                scr = pa.screened.get(skey, {})
                pa.docs.append(PriorArtDoc(
                    skey=skey, number=meta.get("number") or wips_mod.number(hit), title=meta.get("title") or hit.get("title", ""),
                    published=meta.get("published") or hit.get("published", ""), filed=meta.get("filed") or hit.get("filed", ""),
                    score=float(hit.get("score") or 0), path=str(path), sha256=_sha(text), p_screen=scr.get("p_screen"),
                    relevance=scr.get("relevance"), warnings=list(meta.get("warnings") or []),
                ))
            state.stage = Stage.PRIOR_TRIM
            return

        if st == Stage.PRIOR_TRIM:
            self._trim_and_seal(state, req, ids)
            return

    def _screen(self, state: RunState, bundle: MaterialBundle, ids: Identifiers) -> None:
        pa = state.prior_art
        assert pa is not None
        st = Stage.PRIOR_SCREEN
        hits = pa.hits
        if not hits:
            pa.screening, pa.selected, pa.selection_rule = "SCORE_ONLY", [], "검색 후보 없음"
            state.stage = Stage.PRIOR_FETCH
            return
        keys = {f"c{i}": h for i, h in enumerate(hits)}
        pa.jev_questions["PRIOR_SCREEN"] = SCREEN_QUESTION.format(key="c{i}")
        jev_state = {"invention": self._invention_state(pa),
                     "candidates": {k: {"number": wips_mod.number(h), "title": h.get("title", ""), "abstract": (h.get("abstract") or "")[:900]} for k, h in keys.items()}}
        probs = self._jev_batch(state, st, jev_state, {k: SCREEN_QUESTION.format(key=k) for k in keys})
        screened: dict[str, dict[str, Any]] = {}
        if probs:
            pa.screening = "JEV"
            for k, h in keys.items():
                if k in probs:
                    screened[h["skey"]] = {"p_screen": round(probs[k], 4), "relevance": relevance_band(probs[k])}
        else:
            screened = self._screen_by_model(state, bundle, ids, hits)
            pa.screening = "MODEL_FALLBACK" if screened else "SCORE_ONLY"
        pa.screened = screened
        rid = record_id("prior_screen", ids)
        rows = self._screen_rows(pa)
        md = self._screen_md(pa, rows)
        self.store.write_record(state.run_id, rid, {"record_id": rid, "kind": "prior_screen", "screening": pa.screening, "rows": rows,
                                                    "question": pa.jev_questions.get("PRIOR_SCREEN"), "warnings": pa.search_warnings}, md)
        state.records[rid] = RecordRef(record_id=rid, kind="prior_screen", role="engine", scope=Scope.PRIOR_SCREEN.value, stage=st.value, status=pa.screening,
                                       ids=ids.as_dict(), source_set_id=state.source_set_id)
        if pa.options.confirm:
            issues = [{"kind": "USER_DECISION", "code": r["skey"], "text": f"{r['number']} {r['title']} — 관련도 {r['relevance'] or '-'}"
                       + (f", Jev {r['p_screen']:.2f}" if r.get("p_screen") is not None else "") + f", 공개일 {r['published'] or '모름'}"} for r in rows[:30]]
            raise self._halt_exc(state, st, "USER_DECISION", "PRIOR_ART_SELECTION",
                                 f"선행기술 후보 {len(rows)}건을 선별했습니다. 대비할 문헌과 기준일을 골라 "
                                 "`claim-agent resume <run_id> --select-prior-art <skey,...> [--base-date YYYY-MM-DD]`로 재개하세요.",
                                 issues, record_id=rid, report_markdown=md)
        self._auto_select(pa)
        state.stage = Stage.PRIOR_FETCH

    def _screen_by_model(self, state: RunState, bundle: MaterialBundle, ids: Identifiers, hits: list[dict]) -> dict[str, dict[str, Any]]:
        rid = record_id("prior_screen", ids, seq=2)
        cands = "\n\n".join(f"#### {h['skey']} — {wips_mod.number(h)} {h.get('title', '')}\n{(h.get('abstract') or '(요약 없음)')[:900]}" for h in hits)
        pk = packets.prior_screen(state, bundle, ids, rid, cands)
        try:
            env, _ = self._call(state, "prior-art-searcher", Scope.PRIOR_SCREEN, Stage.PRIOR_SCREEN, "prior_screen", pk, ids, rid)
        except Exception as exc:  # noqa: BLE001 - the model fallback failing falls back to the WIPS score order
            state.prior_art.search_warnings.append(f"모델 선별 실패: {type(exc).__name__}")  # type: ignore[union-attr]
            return {}
        if not contract_for("prior-art-searcher", Scope.PRIOR_SCREEN).passes(env, state.request_mode):
            return {}
        known = {h["skey"] for h in hits}
        return {x.skey: {"p_screen": None, "relevance": x.relevance, "reason": x.reason} for x in env.screened if x.skey in known}

    def _screen_rows(self, pa: PriorArtState) -> list[dict[str, Any]]:
        rows = []
        for h in pa.hits:
            scr = pa.screened.get(h["skey"], {})
            rows.append({"skey": h["skey"], "number": wips_mod.number(h), "title": h.get("title", ""), "published": h.get("published", ""),
                         "score": h.get("score"), "queries": h.get("queries", []), "p_screen": scr.get("p_screen"), "relevance": scr.get("relevance"),
                         "timing": timing(h.get("published", ""), pa.base_date)})
        order = {"높음": 0, "중간": 1, "낮음": 2}
        rows.sort(key=lambda r: (-(r["p_screen"] if r["p_screen"] is not None else -1), order.get(r["relevance"] or "", 3), -(r["score"] or 0)))
        return rows

    def _auto_select(self, pa: PriorArtState) -> None:
        rows = self._screen_rows(pa)
        late = [r["skey"] for r in rows if r["timing"].startswith(("기준일 이후", "기준일과 같은 날"))]
        pool = [r for r in rows if r["skey"] not in late]
        if pa.screening == "JEV":
            picked = [r for r in pool if (r["p_screen"] or 0) >= pa.options.screen_threshold]
            rule = f"Jev p_screen ≥ {pa.options.screen_threshold} 확률순 최대 {pa.options.max_docs}건"
        elif pa.screening == "MODEL_FALLBACK":
            picked = [r for r in pool if r["relevance"] == "높음"] + [r for r in pool if r["relevance"] == "중간"]
            rule = f"모델 선별(Jev 실패) 관련도 높음→중간 순 최대 {pa.options.max_docs}건"
        else:
            picked = sorted(pool, key=lambda r: -(r["score"] or 0))
            rule = f"WIPS 점수순(Jev·모델 선별 실패) 최대 {pa.options.max_docs}건"
        pa.selected = [r["skey"] for r in picked[: pa.options.max_docs]]
        pa.excluded_by_date = late
        pa.selection_rule = rule + (f"; 기준일 {pa.base_date} 이후·같은 날 공개 {len(late)}건 제외" if late else "")

    def _trim_and_seal(self, state: RunState, req: RunRequest, ids: Identifiers) -> None:
        pa = state.prior_art
        assert pa is not None
        st = Stage.PRIOR_TRIM
        pa.trimmed = []
        if not pa.docs or not pa.options.trim:
            pa.trim = "OFF"
        else:
            folder = self.store.run_dir(state.run_id) / "prior_art"
            keys = {f"d{i}": d for i, d in enumerate(pa.docs)}
            pa.jev_questions["PRIOR_TRIM"] = TRIM_QUESTION.format(key="d{i}")
            docs_state = {}
            for k, d in keys.items():
                p = folder / f"{_safe(d.skey)}.claims.md"
                docs_state[k] = {"number": d.number, "title": d.title, "claims_and_abstract": p.read_text(encoding="utf-8") if p.exists() else ""}
            probs = self._jev_batch(state, st, {"invention": self._invention_state(pa), "docs": docs_state},
                                    {k: TRIM_QUESTION.format(key=k) for k in keys})
            if probs is None:
                pa.trim = "SKIPPED"
            else:
                pa.trim = "JEV"
                for k, d in keys.items():
                    if k in probs:
                        d.p_trim = round(probs[k], 4)
                    if d.p_trim is not None and d.p_trim < pa.options.trim_threshold:
                        pa.trimmed.append({"skey": d.skey, "number": d.number, "p_trim": d.p_trim})
        kept = pa.kept()
        digest = "\n".join(sorted(f"{d.skey}:{d.sha256}" for d in kept))
        pa.set_id = "pas-" + _sha(digest)[:12]
        searched_paths = {d.path for d in pa.docs}
        req.prior_art = [p for p in req.prior_art if p not in searched_paths] + [d.path for d in kept]
        self._reload_bundle(state, req)
        rid = record_id("prior_art_set", ids)
        md = self._set_md(state, pa)
        self.store.write_record(state.run_id, rid, {"record_id": rid, "kind": "prior_art_set", "prior_art_set_id": pa.set_id, "label": state.prior_art_label(),
                                                    "base_date": pa.base_date, "base_date_source": pa.base_date_source, "countries": pa.options.countries,
                                                    "queries_ko": len(pa.queries_ko), "queries_en": len(pa.queries_en), "screening": pa.screening,
                                                    "selection_rule": pa.selection_rule, "trim": pa.trim, "jev_questions": pa.jev_questions,
                                                    "docs": [d.model_dump() for d in pa.docs], "trimmed": pa.trimmed, "excluded_by_date": pa.excluded_by_date,
                                                    "warnings": pa.search_warnings, "wips_calls": pa.wips_calls}, md)
        state.records[rid] = RecordRef(record_id=rid, kind="prior_art_set", role="engine", scope=Scope.PRIOR_SCREEN.value, stage=st.value, status="SEALED",
                                       ids=ids.as_dict(), source_set_id=state.source_set_id)
        pa.set_record_id = rid
        state.notes.append(f"선행기술 집합 봉인 {pa.set_id}: 문헌 {len(kept)}건 ({state.prior_art_label()}, 기준일 {pa.base_date})")
        state.stage = Stage.ARCHITECT

    # ------------------------------------------------------------------ reports
    @staticmethod
    def _screen_md(pa: PriorArtState, rows: list[dict[str, Any]]) -> str:
        lines = ["## 선행기술 후보 선별", "",
                 f"- 선별 방식: {pa.screening} (Jev 확률은 문헌 선택 도구이며 개시 근거가 아님)",
                 f"- 기준일: {pa.base_date} ({'요청' if pa.base_date_source == 'REQUEST' else '실행일 — 출원 예정일 미지정'})",
                 f"- 검색 국가: {', '.join(pa.options.countries)} · 국문 문장 {len(pa.queries_ko)}개 · 영문 문장 {len(pa.queries_en)}개 · 후보 {len(rows)}건", "",
                 "| skey | 번호 | 명칭 | 공개일 | Jev | 관련도 | 공개 시점 |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            p = f"{r['p_screen']:.2f}" if r.get("p_screen") is not None else "-"
            lines.append(f"| {r['skey']} | {r['number']} | {r['title']} | {r['published'] or '-'} | {p} | {r['relevance'] or '-'} | {r['timing'] or '-'} |")
        if pa.search_warnings:
            lines += ["", "경고:"] + [f"- {w}" for w in pa.search_warnings]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _set_md(state: RunState, pa: PriorArtState) -> str:
        kept = pa.kept()
        lines = ["## 선행기술 집합 봉인", "",
                 f"- prior_art_set_id: {pa.set_id}", f"- PRIOR_ART_SET: {state.prior_art_label()} (전수 조사가 아니다)",
                 f"- 기준일: {pa.base_date} ({'요청' if pa.base_date_source == 'REQUEST' else '실행일 — 출원 예정일 미지정, 공개 시점 판단은 실행일 기준'})",
                 f"- 검색 국가: {', '.join(pa.options.countries)} · WIPS 호출 {pa.wips_calls}회",
                 f"- 선별: {pa.screening} · 선택 규칙: {pa.selection_rule or '사용자 선택'}",
                 f"- 거르기: {pa.trim}" + (f" (Jev p_trim < {pa.options.trim_threshold} 제외)" if pa.trim == "JEV" else ""), ""]
        if pa.jev_questions:
            lines += ["Jev 질문 문구:"] + [f"- {k}: {v}" for k, v in pa.jev_questions.items()] + [""]
        lines += ["| 문헌 | 명칭 | 공개일 | Jev 선별 | Jev 거르기 | 결과 |", "|---|---|---|---|---|---|"]
        kept_keys = {d.skey for d in kept}
        for d in pa.docs:
            ps = f"{d.p_screen:.2f}" if d.p_screen is not None else "-"
            pt = f"{d.p_trim:.2f}" if d.p_trim is not None else "-"
            lines.append(f"| {d.number} | {d.title} | {d.published or '-'} | {ps} | {pt} | {'봉인' if d.skey in kept_keys else '제외'} |")
        if not kept:
            best = sorted(pa.docs, key=lambda d: -(d.p_trim or d.p_screen or 0))[:3]
            lines += ["", "검색 후보 중 대비 기준을 넘은 문헌 없음." + (" 최고 확률: " + ", ".join(f"{d.number} {max(d.p_trim or 0, d.p_screen or 0):.2f}" for d in best) if best else "")]
        if pa.excluded_by_date:
            lines += ["", f"기준일 이후·같은 날 공개로 제외: {len(pa.excluded_by_date)}건"]
        if pa.search_warnings:
            lines += ["", "경고:"] + [f"- {w}" for w in pa.search_warnings]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ PRE_EXAM
    def _needs_preexam(self, state: RunState, bundle: MaterialBundle) -> bool:
        if state.baseline_set is not None or state.prior_art_label() == LABEL_NONE:
            return False
        if not bundle.by_category("prior_art"):
            state.notes.append("사전 심사 생략: 봉인된 선행기술 집합에 문헌 0건")
            return False
        return True

    def _design_elements(self, state: RunState) -> list[dict[str, Any]]:
        c = state.candidate
        if not c.design_record_id:
            return []
        rec = self.store.read_record(state.run_id, c.design_record_id)
        elements = [e for e in rec.get("design_elements") or [] if e.get("element_id") and e.get("text")]
        if elements:
            return elements
        # Older or terse designs: their 조건 4 evidence rows are the independent claim's limitations.
        return [{"element_id": f"E{i + 1}", "kind": "LIMITATION", "text": e["limitation"], "in_independent": True,
                 "source_location": e.get("location"), "basis": e.get("basis")} for i, e in enumerate(rec.get("limitation_evidence") or []) if e.get("limitation")]

    def _preexam_docs(self, state: RunState, bundle: MaterialBundle) -> list[_DocView]:
        meta = {d.path: d for d in (state.prior_art.kept() if state.prior_art else [])}
        out = []
        for i, it in enumerate(bundle.by_category("prior_art")):
            d = meta.get(it.path)
            label = f"{d.number} {d.title}".strip() if d else it.name
            image = None if it.kind == "text" else ImagePart(it.mime_type or "image/png", it.data or b"", f"[prior_art] {it.name}", it.content_sha256)
            out.append(_DocView(f"D{i + 1}", label, it.content_sha256, it.text if it.kind == "text" else None, image, d.published if d else "", it.path))
        return out

    def _preexam_step(self, state: RunState, bundle: MaterialBundle) -> None:
        c = state.candidate
        cur = c.current
        assert cur is not None
        st = Stage.PRE_EXAM
        ids = self._ids(state)
        elements = self._design_elements(state)
        if not elements:
            raise self._halt_exc(state, st, "UNVERIFIED", "DESIGN_ELEMENTS_MISSING",
                                 "설계 기록에 대비 단위(design_elements 또는 한정별 근거표)가 없어 사전 심사를 할 수 없습니다. 재설계(--redesign)로 재개하세요.")
        docs = self._preexam_docs(state, bundle)
        base_date = self._base_date(state)
        design_id = c.design_record_id or ""

        def one(doc: _DocView) -> tuple[_DocView, list[dict[str, Any]], str | None, RoleEnvelope | None, str | None]:
            keys = {e["element_id"]: f"{doc.sha256}:{_sha(e['text'])}" for e in elements}
            if all(k in c.preexam_rows for k in keys.values()):
                rows = [dict(c.preexam_rows[keys[e["element_id"]]], element_id=e["element_id"]) for e in elements]
                return doc, rows, doc.published or None, None, None
            dids = Identifiers(ids.candidate_id, ids.revision, ids.design_revision, input_revision=ids.input_revision, extra={"doc_id": doc.doc_id})
            rid = record_id("preexam_compare", dids)
            pk = packets.preexam_compare(state, self.store, bundle, dids, rid, design_id, elements, doc.doc_id, doc.label, doc.text, doc.image, base_date)
            env, _ = self._call(state, "prior-art-pre-examiner", Scope.PREEXAM_COMPARE, st, "preexam_compare", pk, dids, rid, expected_reuse=len(docs))
            if not contract_for("prior-art-pre-examiner", Scope.PREEXAM_COMPARE).passes(env, state.request_mode):
                return doc, [], None, env, rid
            rows = self._verify_rows(doc, elements, env)
            with self._lock:
                for r in rows:
                    c.preexam_rows[keys[r["element_id"]]] = {k: v for k, v in r.items() if k != "element_id"}
            return doc, rows, (doc.published or env.doc_published or ""), env, rid

        if self.cfg.pipeline.max_concurrency > 1 and len(docs) > 1:
            with ThreadPoolExecutor(max_workers=self.cfg.pipeline.max_concurrency) as ex:
                results = list(ex.map(one, docs))
        else:
            results = [one(d) for d in docs]
        compare_ids = [rid for _, _, _, _, rid in results if rid]
        for _, rows, _, env, rid in results:
            if env is not None and not rows:
                raise self._halt_exc(state, st, "REVIEW", env.reason_code or "OTHER",
                                     f"사전 심사 대비(COMPARE)가 완료되지 않았습니다({rid}: {env.status.value}). 입력을 확인한 뒤 재개하세요.",
                                     [o.model_dump() for o in env.open_issues], record_id=rid, report_markdown=env.report_markdown)
        essentials = [e["element_id"] for e in elements if e.get("in_independent", True)] or [e["element_id"] for e in elements]
        tallies = {doc.doc_id: self._tally(rows, essentials, timing(published or "", base_date)) for doc, rows, published, _, _ in results}
        table = self._comparison_table(results, elements, tallies)

        rid = record_id("preexam_judge", ids)
        pk = packets.preexam_judge(state, self.store, bundle, ids, rid, design_id, elements, table, base_date)
        env, ref = self._call(state, "prior-art-pre-examiner", Scope.PREEXAM_JUDGE, st, "preexam_judge", pk, ids, rid)
        if env.execution_status != ExecStatus.RUN or not contract_for("prior-art-pre-examiner", Scope.PREEXAM_JUDGE).passes(env, state.request_mode):
            raise self._halt_exc(state, st, "UNVERIFIED", env.reason_code or "OTHER", "사전 심사 종합 판정(JUDGE)이 실행되지 않았습니다.",
                                 [o.model_dump() for o in env.open_issues], record_id=rid, report_markdown=env.report_markdown)
        gate, code, message, threats = self._novelty_decision(env, tallies, {d.doc_id: d for d, *_ in results})
        risk = env.inventive_risk.value if env.inventive_risk else RiskLevel.UNVERIFIED.value
        ref.gates.update({"PREEXAM_NOVELTY_GATE": gate, "PREEXAM_EVIDENCE_GATE": "PASS", "INVENTIVE_RISK": risk})
        summary = self._judge_md(state, gate, code, message, risk, env, tallies, table)
        self.store.write_record(state.run_id, rid, {**self.store.read_record(state.run_id, rid), "engine_decision": {
            "PREEXAM_NOVELTY_GATE": gate, "reason_code": code, "message": message, "INVENTIVE_RISK": risk, "tallies": tallies,
            "compare_record_ids": compare_ids, "base_date": base_date, "prior_art_label": state.prior_art_label()}}, summary)
        c.preexam_record_id = rid
        cur.records["preexam_judge"] = rid
        for i, x in enumerate(compare_ids):
            cur.records[f"preexam_compare_{i + 1}"] = x
        state.notes.append(f"사전 심사 {rid}: PREEXAM_NOVELTY_GATE {gate}" + (f" ({code})" if code else "") + f", INVENTIVE_RISK {risk}")

        if gate == "PASS":
            if risk == RiskLevel.HIGH.value and state.prior_art is not None and state.prior_art.options.inventive_high == "review":
                raise self._halt_exc(state, st, "REVIEW", "INVENTIVE_RISK_HIGH", "사전 심사에서 진보성 위험이 HIGH입니다(prior_search.inventive_high: review). "
                                     "보고서를 확인하고 그대로 진행하거나 재설계하세요.", record_id=rid, report_markdown=summary)
            state.stage = Stage.DRAFT
            return
        if gate == "BLOCK":
            counts = c.loop_counts
            limit = self.cfg.pipeline.max_return_loops.get("PREEXAM", 2)
            if counts.get("PREEXAM", 0) >= limit:
                raise self._halt_exc(state, st, "LOOP_LIMIT", "PREEXAM", f"사전 심사 재설계 한도 초과({counts.get('PREEXAM', 0)}/{limit}): {message}",
                                     record_id=rid, report_markdown=summary)
            counts["PREEXAM"] = counts.get("PREEXAM", 0) + 1
            self._bump_design(state, self._architect_feedback(env, threats, tallies, results, elements, rid), full_reset=True)
            state.notes.append(f"RETURN_TO_ARCHITECT ← {rid} (사전 심사 N 위협)")
            self.store.save_state(state)
            return
        raise self._halt_exc(state, st, gate, code, message, [{"kind": gate, "code": code or "", "text": message}], record_id=rid, report_markdown=summary)

    @staticmethod
    def _verify_rows(doc: _DocView, elements: list[dict[str, Any]], env: RoleEnvelope) -> list[dict[str, Any]]:
        """Re-find each disclosed quote in the document (설계 5.2-1); an unfound '개시' becomes UNCONFIRMED."""
        by_id: dict[str, dict[str, Any]] = {}
        for r in env.preexam_rows:
            if r.element_id in by_id or r.element_id not in {e["element_id"] for e in elements}:
                continue
            row = {"element_id": r.element_id, "status": r.status.value, "model_status": r.status.value, "quote": r.quote, "where": r.where, "note": r.note, "verified": None}
            if r.status in (DisclosureStatus.DISCLOSED, DisclosureStatus.PARTIALLY_DISCLOSED) and doc.text is not None:
                hit = find_quote(doc.text, r.quote)
                if hit:
                    row["verified"] = True
                    row["where"] = hit.get("where") or r.where
                else:
                    row["verified"] = False
                    row["status"] = DisclosureStatus.UNCONFIRMED.value
            elif r.status in (DisclosureStatus.DISCLOSED, DisclosureStatus.PARTIALLY_DISCLOSED):
                row["status"] = DisclosureStatus.UNCONFIRMED.value     # an image document cannot be re-checked
            by_id[r.element_id] = row
        for e in elements:
            by_id.setdefault(e["element_id"], {"element_id": e["element_id"], "status": DisclosureStatus.UNCONFIRMED.value, "model_status": "", "quote": "",
                                               "where": "", "note": "모델 답에 이 요소가 없음", "verified": None})
        return [by_id[e["element_id"]] for e in elements]

    @staticmethod
    def _tally(rows: list[dict[str, Any]], essentials: list[str], when: str) -> dict[str, Any]:
        st = {r["element_id"]: r["status"] for r in rows}
        missing = [x for x in essentials if st.get(x) in ("NOT_DISCLOSED", "PARTIALLY_DISCLOSED")]
        uncertain = [x for x in essentials if st.get(x) == "UNCONFIRMED"]
        if missing:
            kind = "CLEAR"
        elif uncertain:
            kind = "UNCERTAIN"
        else:
            kind = "ANTICIPATED" if not when else "ANTICIPATED_TIMING"
        return {"kind": kind, "missing": missing, "uncertain": uncertain, "timing": when}

    @staticmethod
    def _novelty_decision(env: RoleEnvelope, tallies: dict[str, dict[str, Any]], docs: dict[str, _DocView]) -> tuple[str, str | None, str, list[str]]:
        """설계 5.3: (gate, reason_code, message, threat doc ids). The model's conclusion alone never passes or returns."""
        concl = {n.doc_id: n.conclusion for n in env.novelty}
        label = {k: v.label for k, v in docs.items()}
        anticipated = [d for d, t in tallies.items() if t["kind"] == "ANTICIPATED"]
        mismatch = [d for d in anticipated if concl.get(d) != NoveltyConclusion.NEGATED_RISK]
        if mismatch:
            return "REVIEW", "PREEXAM_MODEL_CODE_MISMATCH", ("코드 집계로는 " + ", ".join(f"{d} {label[d]}" for d in mismatch)
                                                             + "에 독립항 필수 요소가 모두 개시(인용 확인)되어 있는데 사전 심사자의 결론은 신규성 부정 우려가 아닙니다. 결론을 확인하세요."), []
        if anticipated:
            if not env.remaining_differences:
                return "REVIEW", "NO_SOURCE_DIFFERENTIATOR", (", ".join(f"{d} {label[d]}" for d in anticipated)
                                                              + "에 독립항 필수 요소가 모두 개시되어 있고, 봉인 문헌 어디에도 없는 요소로서 발명 원자료에 근거가 있는 것이 없습니다. "
                                                                "원자료를 보충하거나 방향을 정해 주세요."), anticipated
            return "BLOCK", None, "신규성 위협: " + ", ".join(f"{d} {label[d]}" for d in anticipated) + "이 독립항 필수 요소를 모두 개시(인용 확인)", anticipated
        identity = [d for d, t in tallies.items() if t["kind"] == "CLEAR" and concl.get(d) == NoveltyConclusion.NEGATED_RISK]
        if identity:
            return "REVIEW", "PREEXAM_SUBSTANTIAL_IDENTITY", (", ".join(f"{d} {label[d]}" for d in identity)
                                                              + "은 대비표상 미개시·일부 개시 필수 요소가 있는데 사전 심사자는 신규성 부정 우려로 봤습니다(실질적 동일, 09 P4). 판단을 확인하세요."), []
        unsure = [d for d, t in tallies.items() if t["kind"] in ("UNCERTAIN", "ANTICIPATED_TIMING")]
        if unsure:
            return "UNVERIFIED", "PREEXAM_EVIDENCE_UNCONFIRMED", ("근거를 확인해야 합니다: " + "; ".join(
                f"{d} {label[d]} — " + (f"인용 미확인 요소 {', '.join(tallies[d]['uncertain'])}" if tallies[d]["kind"] == "UNCERTAIN" else tallies[d]["timing"]) for d in unsure)), []
        return "PASS", None, "봉인 문헌마다 독립항 필수 요소 중 미개시·일부 개시가 있음(이 집합 기준)", []

    @staticmethod
    def _comparison_table(results, elements: list[dict[str, Any]], tallies: dict[str, dict[str, Any]]) -> str:
        out = []
        for doc, rows, published, _, _ in results:
            t = tallies[doc.doc_id]
            head = f"#### {doc.doc_id} {doc.label}\n공개일: {published or '(모름)'}" + (f" — {t['timing']}" if t["timing"] else "")
            lines = [head, "", "| 요소 | 상태 | 인용 확인 | 원문 인용 | 위치 | 대응·차이 |", "|---|---|---|---|---|---|"]
            for r in rows:
                ver = {True: "확인됨", False: "못 찾음", None: "-"}[r.get("verified")]
                quote = (r.get("quote") or "")[:200].replace("|", "/").replace("\n", " ")
                lines.append(f"| {r['element_id']} | {STATUS_KO.get(r['status'], r['status'])} | {ver} | {quote} | {r.get('where') or ''} | {(r.get('note') or '').replace('|', '/')} |")
            kind_ko = {"ANTICIPATED": "독립항 필수 요소 모두 개시(인용 확인)", "ANTICIPATED_TIMING": "모두 개시되었으나 공개 시점 확인 필요",
                       "UNCERTAIN": "미개시 없음 — 인용 미확인 요소 " + ", ".join(t["uncertain"]), "CLEAR": "미개시·일부 개시 필수 요소 " + ", ".join(t["missing"])}[t["kind"]]
            lines += ["", f"필수 요소 집계(코드): {kind_ko}"]
            out.append("\n".join(lines))
        return "\n\n".join(out)

    @staticmethod
    def _judge_md(state: RunState, gate: str, code: str | None, message: str, risk: str, env: RoleEnvelope, tallies: dict[str, dict[str, Any]], table: str) -> str:
        lines = ["## 사전 심사 판정 (오케스트레이터)", "",
                 f"- PREEXAM_NOVELTY_GATE: {gate}" + (f" — {code}" if code else ""), f"- 사유: {message}",
                 f"- INVENTIVE_RISK: {risk} (비게이팅)", f"- PRIOR_ART_SET: {state.prior_art_label()} — 결과는 이 집합 기준이며 전수 조사가 아니다",
                 f"- 기준일: {state.prior_art.base_date if state.prior_art else date.fromtimestamp(state.created_at).isoformat()}", "",
                 "### 문헌별 신규성 (사전 심사자 결론 / 코드 집계)", "", "| 문헌 | 결론 | 코드 집계 | 빠진 요소 |", "|---|---|---|---|"]
        concl = {n.doc_id: n for n in env.novelty}
        for d, t in tallies.items():
            n = concl.get(d)
            lines.append(f"| {d} | {CONCLUSION_KO.get(n.conclusion.value, '-') if n else '-'} | {t['kind']} | {n.missing if n else ''} |")
        if env.remaining_differences:
            lines += ["", "### 남는 차이 (발명 원자료 위치)", "", "| 요소 | 원자료 위치 | exam_pattern | 설계변경 위험 | effect_link | why_not_simple | numeric_basis |",
                      "|---|---|---|---|---|---|---|"]
            for r in env.remaining_differences:
                lines.append(f"| {r.element_id} | {r.source_location} | {r.exam_pattern or '-'} | {r.design_change_risk or '-'} | {r.effect_link or '-'} | {r.why_not_simple or '-'} | {r.numeric_basis or '-'} |")
        lines += ["", "### 문헌별 대비표", "", table, "", "## 사전 심사자 보고서", "", env.report_markdown]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _architect_feedback(env: RoleEnvelope, threats: list[str], tallies, results, elements, rid: str) -> str:
        """The redesign goal handed to the architect: disclosure facts and source locations only (설계 14.3 — no exam classification)."""
        text = {e["element_id"]: e["text"] for e in elements}
        lines = [f"사전 심사 되돌림 ({rid}) — 신규성 위협(이 검색 집합 기준). 조건 6의 증거로 쓰고 조건 8로 최소충분 집합을 다시 정한다. "
                 "원자료에 없는 구성으로 회피하지 않는다.", ""]
        rows_by_doc = {doc.doc_id: (doc, rows) for doc, rows, *_ in results}
        for d in threats:
            doc, rows = rows_by_doc[d]
            lines.append(f"### {d} {doc.label}: 독립항 필수 요소 모두 개시(인용 확인)")
            for r in rows:
                if r["status"] == "DISCLOSED":
                    lines.append(f"- {r['element_id']} ({text.get(r['element_id'], '')}): 「{(r.get('quote') or '')[:200]}」 {r.get('where') or ''}")
        lines += ["", "### 봉인 문헌 어디에도 개시되지 않은 요소 (발명 원자료 위치)"]
        for r in env.remaining_differences:
            lines.append(f"- {r.element_id} ({text.get(r.element_id, '')}): {r.source_location}" + (f" — {r.note}" if r.note else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------ halts
    def _halt_exc(self, state: RunState, stage: Stage, kind: str, code: str | None, message: str, issues: list[dict] | None = None,
                  record_id: str | None = None, report_markdown: str | None = None):
        from .engine import PipelineHalt

        return PipelineHalt(Halt(stage=stage.value, role="engine", kind=kind, reason_code=code, message=message, open_issues=issues or [],
                                 record_id=record_id, report_markdown=report_markdown))

    # ------------------------------------------------------------------ resume
    def _resume_prior_art(self, state: RunState, decision: Any, halted_stage: Stage) -> bool:
        """Handle select_prior_art and the PRIOR_SEARCH_UNAVAILABLE acceptance. True when the decision was consumed."""
        pa = state.prior_art
        if decision.action == "accept_unverified" and state.halt and state.halt.reason_code == "PRIOR_SEARCH_UNAVAILABLE":
            assert pa is not None
            pa.skipped = True
            state.notes.append("WIPS 검색 불가를 받아들여 검색 없이 진행(PRIOR_ART_SET: NONE)")
            state.stage = Stage.ARCHITECT
            return True
        if decision.action != "select_prior_art":
            return False
        if pa is None or not pa.hits:
            from ..provider.base import ProviderError

            raise ProviderError("이 run에는 선행기술 검색 후보가 없어 문헌을 고를 수 없습니다(prior_search.enabled 필요).")
        known = {h["skey"] for h in pa.hits}
        chosen = [s for s in (decision.prior_art_selection or []) if s in known]
        unknown = [s for s in (decision.prior_art_selection or []) if s not in known]
        if unknown:
            from ..provider.base import ProviderError

            raise ProviderError(f"검색 후보에 없는 skey: {', '.join(unknown)}")
        if decision.base_date:
            pa.base_date, pa.base_date_source = decision.base_date, "REQUEST"
        sealed = pa.set_id is not None
        pa.selected, pa.selection_rule = chosen, "사용자 선택"
        pa.docs = [d for d in pa.docs if d.skey in chosen]
        pa.trimmed, pa.set_id, pa.set_record_id = [], None, None
        if sealed:
            # A different set is a different evidence base: the design and everything built on it start over (설계 5.5).
            self._bump_design(state, "선행기술 집합 변경(사용자 선택): " + ", ".join(chosen), full_reset=True)
        state.notes.append(f"사용자 선행기술 선택: {', '.join(chosen) or '없음'} (기준일 {pa.base_date})")
        state.stage = Stage.PRIOR_FETCH
        return True


def stage_is_prior(stage: Stage) -> bool:
    return stage in (Stage.PRIOR_QUERY, Stage.PRIOR_SEARCH, Stage.PRIOR_SCREEN, Stage.PRIOR_FETCH, Stage.PRIOR_TRIM)
