"""`claim-agent graph …` — inspect and build the three graphs outside a run."""
from __future__ import annotations

import json
from pathlib import Path

from ..models.enums import Scope
from ..models.request import MaterialBundle, RunRequest


def register(sub) -> None:
    g = sub.add_parser("graph", help="rule/invention/precedent graphs: pack report, idea sheet, precedent sheet")
    gs = g.add_subparsers(dest="graph_cmd", required=True)

    r = gs.add_parser("rules", help="rule graph: per role×scope pack savings, one pack, or which sections/roles name a term")
    r.add_argument("--role")
    r.add_argument("--scope", choices=[s.value for s in Scope])
    r.add_argument("--impact", metavar="TERM", help="sections and roles that name TERM (e.g. OA_DRAFT_GATE, '조건 4')")
    r.add_argument("--export", metavar="FILE.json", help="write the rule graph as JSON")

    i = gs.add_parser("invention", help="layer 2: extract the invention graph from a request's materials (one model call)")
    i.add_argument("--request-yaml", required=True)
    i.add_argument("--from-json", metavar="RAW.json", help="build from a saved extraction answer instead of calling the model")
    i.add_argument("--out", metavar="DIR", default=None, help="where to write graph.json, rejected.json and ideas.md")
    i.add_argument("--dependent", action="store_true", help="include exam_pattern hints (dependent-side sheet)")
    i.add_argument("--as-role", help="borrow this role's provider and model (default: config graph.invention_role)")

    d = gs.add_parser("ideas", help="print the idea sheet of a saved invention graph")
    d.add_argument("graph_json")
    d.add_argument("--dependent", action="store_true")

    p = gs.add_parser("precedent", help="layer 3: precedent pattern table, or the sheet for a saved invention graph")
    p.add_argument("--path", help="examples.json (default: config graph.precedent_path)")
    p.add_argument("--graph", dest="graph_json", help="invention graph.json to link")
    p.add_argument("--export", metavar="FILE.json")

    c = gs.add_parser("caselaw", help="case-law DB: check, sheets S11-S13, cases for a finding, feature signals, enforcement check")
    cs = c.add_subparsers(dest="caselaw_cmd", required=True)
    ck = cs.add_parser("check", help="read-only DB check (SHA-256, release, row counts)")
    ck.add_argument("--full", action="store_true", help="also run SQLite integrity_check (slow)")
    sh = cs.add_parser("sheet", help="print a sheet built from the records: clarity (S11), patentability (S12), enforcement (S13)")
    sh.add_argument("--kind", choices=["clarity", "patentability", "enforcement"], default="clarity")
    sh.add_argument("--write", action="store_true", help="write it to its sources/ file")
    fd = cs.add_parser("find", help="cases whose court finding matches a finding or feature")
    fd.add_argument("finding", help="the finding text: claim wording + what is unclear/unsupported, or a feature")
    fd.add_argument("--set", dest="record_set", choices=["lessons", "patentability", "enforcement"], default="lessons")
    fd.add_argument("--group", choices=["명확성", "뒷받침", "신규사항", "권리범위"], help="lessons only")
    fd.add_argument("--type", dest="type_code", help="patentability/enforcement type code, e.g. F-NUMERIC, W02")
    fd.add_argument("-k", type=int, default=5, help="candidates to keep before Jev")
    fd.add_argument("--jev", action="store_true", help="confirm each candidate with one Jev request (OpenRouter)")
    fd.add_argument("--json", action="store_true")
    ft = cs.add_parser("features", help="patentability signals per feature (type, court reasons, raw-material questions) — not a verdict")
    ft.add_argument("--feature", action="append", default=[], metavar="TEXT[::TYPE]", help="repeatable; optional ::F-NUMERIC type")
    ft.add_argument("--features-file", help="JSON list of {id, text, type_code?}")
    ft.add_argument("--jev", action="store_true", help="confirm the shown cases with Jev")
    ft.add_argument("--json", action="store_true")
    ef = cs.add_parser("enforce", help="enforcement check of exact claim text: risky wording types with matching cases")
    ef.add_argument("--claim-file", help="claims text (【청구항 N】 markers); default: --text")
    ef.add_argument("--text")
    ef.add_argument("--jev", action="store_true", help="keep only cases Jev confirms for each flag")
    ef.add_argument("--json", action="store_true")
    sw = cs.add_parser("show", help="one case's records with the verified quote")
    sw.add_argument("case_number")
    ex = cs.add_parser("extract", help="extract patentability/enforcement records from the DB (one model call per uncached case)")
    ex.add_argument("--limit", type=int)
    ex.add_argument("--docs", nargs="*", metavar="DOC-ID")
    ex.add_argument("--workers", type=int, default=8)
    ex.add_argument("--extract-provider", dest="extract_provider", default="claude_oauth",
                    help="provider kind for extraction (default claude_oauth: the Claude subscription CLI)")
    ex.add_argument("--extract-model", dest="extract_model", help="default: that provider's default model")
    ex.add_argument("--rebuild", action="store_true", help="call the model again even for cached cases")
    ex.add_argument("--records-only", action="store_true", help="no model calls: rebuild the record files from the cache")
    sg = gs.add_parser("survey-guide", help="rebuild the graph summary section of sources/14 (선행기술조사 특허성 판단 지침) from the case-law and OA graphs")
    sg.add_argument("--write", action="store_true", help="write the file (default: print it)")
    g.set_defaults(func=cmd_graph)


def _load_request(path: Path) -> tuple[RunRequest, MaterialBundle]:
    req = RunRequest.from_yaml(path)
    base = path.parent

    def fix(p: str) -> str:
        q = Path(p)
        return str(q if q.is_absolute() or q.exists() else base / q)

    req.invention_sources = [fix(p) for p in req.invention_sources]
    req.drawings = [fix(p) for p in req.drawings]
    req.prior_art = [fix(p) for p in req.prior_art]
    if req.spec_path:
        req.spec_path = fix(req.spec_path)
    return req, MaterialBundle.load(req)


def cmd_graph(args) -> int:
    from ..cli import _rt

    rt = _rt(args)
    if args.graph_cmd == "rules":
        return _rules(rt, args)
    if args.graph_cmd == "invention":
        return _invention(rt, args)
    if args.graph_cmd == "ideas":
        from .invention import idea_sheet
        from .store import Graph

        print(idea_sheet(Graph.load(Path(args.graph_json)), include_exam_patterns=args.dependent))
        return 0
    if args.graph_cmd == "caselaw":
        return _caselaw(rt, args)
    if args.graph_cmd == "survey-guide":
        from ..prior_art import guide

        text = guide.build(rt.cfg)
        if args.write:
            (rt.cfg.project_root / guide.GUIDE_PATH).write_text(text, encoding="utf-8")
            print(f"→ {guide.GUIDE_PATH} ({len(text)}자)")
        else:
            print(text)
        return 0
    return _precedent(rt, args)


def _caselaw(rt, args) -> int:
    from contextlib import closing

    from . import caselaw as cl

    paths = cl.Paths.from_config(rt.cfg)
    try:
        if args.caselaw_cmd == "check":
            print(json.dumps(cl.validate(paths, full=args.full), ensure_ascii=False, indent=1))
            return 0
        if args.caselaw_cmd == "extract":
            return _caselaw_extract(rt, paths, args)
        root = rt.cfg.project_root
        sets = {"lessons": lambda: cl.load_lessons(paths.lessons),
                "patentability": lambda: cl.load_records(root / cl.PATENT_FILE),
                "enforcement": lambda: cl.load_records(root / cl.ENFORCE_FILE)}
        if args.caselaw_cmd == "sheet":
            build, path = {"clarity": (lambda: cl.sheet(sets["lessons"]()), cl.SHEET_PATH),
                           "patentability": (lambda: cl.sheet_patentability(sets["patentability"]()), cl.PATENT_SHEET_PATH),
                           "enforcement": (lambda: cl.sheet_enforcement(sets["enforcement"]()), cl.ENFORCE_SHEET_PATH)}[args.kind]
            text = build()
            if args.write:
                (root / path).write_text(text, encoding="utf-8")
                print(f"→ {path} ({len(text)}자)")
            else:
                print(text)
            return 0
        if args.caselaw_cmd == "show":
            rs = [r for name in sets for r in sets[name]()["records"] if r["case_number"] == args.case_number]
            if not rs:
                print(f"판례 기록에 {args.case_number} 없음")
                return 1
            with closing(cl.connect(paths)) as db:
                for r in rs:
                    print(json.dumps({**r, "quote_matches_db": cl.quote_ok(db, r),
                                      "pdf": str(paths.pdfs / f"{r['document_id']}.pdf")}, ensure_ascii=False, indent=1))
            return 0
        if args.caselaw_cmd == "features":
            return _caselaw_features(cl, sets["patentability"](), args)
        if args.caselaw_cmd == "enforce":
            return _caselaw_enforce(cl, sets["enforcement"](), args)
        return _caselaw_find(cl, paths, sets[args.record_set](), args)
    except (cl.CaseLawError, FileNotFoundError) as exc:
        print(f"판례 DB 사용 불가: {exc}")
        return 2


def _jev(cl, finding: str, candidates: list[dict], question: str) -> tuple[dict, str]:
    """후보마다 question을 Jev로 묻는다. CLI에서 못 부르면 빈 확률과 이유를 돌려준다(오케스트레이터가 커넥터로 묻는다)."""
    from ..prior_art import jev

    if not candidates:
        return {}, "후보 없음"
    ok, why = jev.status()
    if not ok:
        return {}, f"Jev 불가 — {why}"
    res = cl.confirm(finding, candidates, jev.JevClient(), question)
    return res.get("probabilities") or {}, f"Jev {res['status']}" + (f" — {res['error']}" if res.get("error") else "")


def _case_row(cl, r: dict, p: float | None = None) -> dict:
    return {"case": f"{r['court']} {r['case_number']}", "decision_date": r["decision_date"], "requirement": r["requirement"],
            "outcome": r["outcome"], "type": r.get("type_code") or r.get("tag"), "jev": p, "label": cl.label(p),
            "claim_feature": r["claim_feature"], "other_side": r.get("other_side", ""), "deciding": cl._fact(r),
            "evidence_basis": r.get("evidence_basis", ""), "drafting_lesson": r.get("drafting_lesson", ""), "page": r["page"], "quote": r["quote"]}


def _caselaw_features(cl, data: dict, args) -> int:
    items = [{"id": f"F{i + 1}", "text": t.split("::")[0], "type_code": (t.split("::")[1] if "::" in t else None)}
             for i, t in enumerate(args.feature)]
    if args.features_file:
        items += json.loads(Path(args.features_file).read_text(encoding="utf-8"))
    out = []
    for it in items:
        sig = cl.feature_signals(data, it["text"], it.get("type_code"))
        probs, note = _jev(cl, it["text"], sig["cases"], cl.JEV_PATENT) if args.jev else ({}, "Jev 미호출")
        rows = [_case_row(cl, r, probs.get(f"case_{i}")) for i, r in enumerate(sig["cases"])]
        if probs:
            rows = [x for x in rows if x["jev"] is not None and x["jev"] >= cl.JEV_THRESHOLD]
        out.append({**{k: v for k, v in sig.items() if k != "cases"}, "id": it.get("id", ""), "jev": note, "cases": rows})
    if args.json:
        print(json.dumps({"signals": out, "jev_question": cl.JEV_PATENT, "jev_threshold": cl.JEV_THRESHOLD,
                          "note": "판례 신호 — 특허성 판정 아님(선행기술 대비 필요). 미검토.", "reviewed": False}, ensure_ascii=False, indent=1))
        return 0
    print("특허성 판례 신호 (미검토 — 판정·게이트 아님. 신규성·진보성은 선행기술 대비로만 판정)")
    for s in out:
        guess = f" (유형 추정: {s['type_guessed']} — `TEXT::유형`으로 직접 줄 수 있음)" if s["type_guessed"] else ""
        print(f"\n## {s['id']} {s['feature']}\n- 유형: {s['type_code']} {s['type_label']}{guess} · 법원 기록 {s['outcomes']} · {s['jev']}")
        if s["deny_factors"]:
            print("- 부정 이유 상위: " + ", ".join(f"{k} {n}" for k, n in s["deny_factors"]))
        if s["grant_factors"]:
            print("- 인정 이유 상위: " + ", ".join(f"{k} {n}" for k, n in s["grant_factors"]))
        for q in s["questions"]:
            print(f"- 원자료 확인: {q}")
        for x in s["cases"]:
            print(f"  - {x['case']} {x['requirement']} {x['outcome']} {x['label']}: {x['claim_feature'][:70]} — {x['deciding'][:150]}")
    return 0


def _caselaw_enforce(cl, data: dict, args) -> int:
    text = Path(args.claim_file).read_text(encoding="utf-8") if args.claim_file else (args.text or "")
    report = cl.enforcement_report(data, text, k=4 if args.jev else 2)
    if args.jev:
        for f in report:
            probs, f["jev"] = _jev(cl, f"{f['snippet']} — {f['question']}", f["cases"], cl.JEV_ENFORCE)
            if probs:
                f["cases"] = [r for i, r in enumerate(f["cases"]) if (probs.get(f"case_{i}") or 0) >= cl.JEV_THRESHOLD][:2]
    if args.json:
        print(json.dumps({"flags": [{**f, "cases": [_case_row(cl, r) for r in f["cases"]]} for f in report],
                          "jev_question": cl.JEV_ENFORCE, "jev_threshold": cl.JEV_THRESHOLD, "reviewed": False}, ensure_ascii=False, indent=1))
        return 0
    print(cl.enforcement_markdown(report), end="")
    return 0


def _caselaw_extract(rt, paths, args) -> int:
    from . import caselaw_extract as cx

    # 추출은 프로젝트 기본 프로바이더와 상관없이 정한 프로바이더로 한다(기본 claude_oauth, docs/caselaw.md).
    kind = args.extract_provider
    model = args.extract_model or rt.cfg.default_model_for(kind)
    out_dir = rt.cfg.project_root / "docs"
    if args.records_only:
        with cx.closing_db(paths) as db:
            meta = cx.cases(db)
        summary = cx.write_records(paths, meta, out_dir)
    else:
        from ..runtime import _live_provider

        print(f"provider {kind} · model {model} · workers {args.workers}")
        summary = cx.extract(paths, _live_provider(rt.cfg, kind), model, out_dir, limit=args.limit, docs=args.docs,
                             workers=args.workers, rebuild=args.rebuild)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def _caselaw_find(cl, paths, data: dict, args) -> int:
    from contextlib import closing

    question = {"lessons": cl.JEV_QUESTION, "patentability": cl.JEV_PATENT, "enforcement": cl.JEV_ENFORCE}[args.record_set]
    hits = cl.find(data, args.finding, args.group if args.record_set == "lessons" else None, args.k, args.type_code)
    probs, jev_note = _jev(cl, args.finding, [r for _, r in hits], question) if args.jev else ({}, "Jev 미호출")
    with closing(cl.connect(paths)) as db:
        rows = []
        for i, (score, r) in enumerate(hits):
            rows.append({**_case_row(cl, r, probs.get(f"case_{i}")), "score": round(score, 3), "quote_matches_db": cl.quote_ok(db, r)})
    if args.jev and probs:
        rows = [x for x in rows if x["jev"] is not None and x["jev"] >= cl.JEV_THRESHOLD]
    if args.json:
        # jev_question: CLI에서 Jev를 못 부를 때 오케스트레이터가 Jev 커넥터로 후보마다 같은 질문을 묻는다(docs/caselaw.md).
        print(json.dumps({"finding": args.finding, "set": args.record_set, "jev": jev_note, "jev_question": question,
                          "jev_threshold": cl.JEV_THRESHOLD, "cases": rows, "reviewed": False}, ensure_ascii=False, indent=1))
        return 0
    print(f"판례 참고 (미검토 — 게이트·근거 아님) · {args.record_set} · {jev_note} · {len(rows)}건")
    for x in rows:
        print(f"\n- {x['case']} ({x['decision_date']}) {x['requirement']} {x['outcome']} [{x['type']}] {x['label']}"
              + (f" p={x['jev']:.2f}" if x["jev"] is not None else f" 겹침={x['score']}"))
        print(f"  청구항 문언: {x['claim_feature']}\n  법원이 본 사실: {x['deciding']}")
        print(f"  인용(PDF {x['page']}쪽, DB 대조 {'일치' if x['quote_matches_db'] else '불일치'}): {x['quote'][:300]}")
    return 0


def _rules(rt, args) -> int:
    from ..roles.whitelist import SOURCE_WHITELIST
    from .rules import RuleGraph

    rg = RuleGraph(rt.sources, rt.roles)
    if args.export:
        rg.graph.save(Path(args.export))
        print(f"rule graph → {args.export} ({rg.graph.stats()})")
    if args.impact:
        t = args.impact
        node = rg.graph.nodes.get(t)
        if node is None:
            print(f"'{t}'를 이름으로 쓰는 절이나 역할이 없다.")
            return 1
        for e in sorted(rg.graph.in_edges(t), key=lambda e: e.src):
            src = rg.graph.nodes[e.src]
            where = src.spans[0].label() if src.spans else ""
            print(f"  {e.type:9s} {src.label} {where}")
        return 0
    if args.role and args.scope:
        pack = rg.pack(args.role, Scope(args.scope))
        print(json.dumps(pack.summary(), ensure_ascii=False, indent=1))
        for s in pack.sections:
            print(f"  + {s.id:10s} {pack.reasons[s.id]:28s} {s.heading[:60]}")
        return 0
    exempt = set(rt.cfg.graph.rule_pack_exempt_roles)
    print(f"graph.rule_pack: {rt.cfg.graph.rule_pack} · exempt: {', '.join(sorted(exempt)) or '-'}")
    print(f"{'role':38s} {'scope':16s} {'full':>7s} {'pack':>7s} {'saved':>6s}  missing_terms")
    for (role, scope), keys in SOURCE_WHITELIST.items():
        if not keys:
            continue
        sm = rg.pack(role, scope).summary()
        flag = " (exempt)" if role in exempt else ""
        print(f"{role:38s} {scope.value:16s} {sm['full_chars']:7d} {sm['pack_chars']:7d} {sm['saved_pct']:5.1f}%  {', '.join(sm['missing_terms']) or '-'}{flag}")
    return 0


def _invention(rt, args) -> int:
    from ..provider.base import CallSpec, GenParams, ImagePart, parse_json_text
    from . import invention as inv

    req, bundle = _load_request(Path(args.request_yaml))
    if args.from_json:
        raw = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        from ..runtime import live_provider

        if args.as_role:
            rt.cfg.graph.invention_role = args.as_role
        model = rt.cfg.graph.invention_model or rt.cfg.model_for(rt.cfg.graph.invention_role)
        images = [ImagePart(it.mime_type or "image/png", it.data or b"", it.name, it.content_sha256)
                  for it in bundle.by_category(*inv.CATEGORIES) if it.kind == "image"]
        spec = CallSpec(role=rt.cfg.graph.invention_role, scope="INDEPENDENT", model=model, system_instruction=inv.EXTRACTION_SYSTEM,
                        packet_text=inv.extraction_packet(bundle), images=images, json_schema=inv.EXTRACTION_SCHEMA,
                        gen=GenParams(temperature=0.0, thinking_level="LOW", max_output_tokens=16384), use_cache=False, phase="graph")
        result = live_provider(rt.cfg).generate(spec)
        raw = result.parsed or parse_json_text(result.text)
        print(f"model {model} · usage {result.usage}")
        if raw is None:
            print("추출 응답을 JSON으로 읽지 못했다:\n" + result.text[:2000])
            return 1
    built = inv.build(raw, bundle)
    sheet = inv.idea_sheet(built.graph, include_exam_patterns=args.dependent)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        built.graph.save(out / "graph.json")
        (out / "rejected.json").write_text(json.dumps(built.rejected, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "ideas.md").write_text(sheet, encoding="utf-8")
        print(f"→ {out}")
    print(sheet)
    if built.rejected:
        print(f"\n원문 불일치로 버린 항목 {len(built.rejected)}개:")
        for r in built.rejected[:20]:
            item = r["item"]
            print(f"  - {r['reason']}: {item.get('label') or item.get('type')} {item.get('id') or (item.get('src'), item.get('dst'))}")
    return 0


def _precedent(rt, args) -> int:
    from .invention import causal_chains
    from .precedent import PrecedentGraph, precedent_sheet
    from .store import Graph

    p = Path(args.path or rt.cfg.graph.precedent_path)
    pg = PrecedentGraph.load(p if p.is_absolute() else rt.cfg.project_root / p)
    if args.export:
        pg.graph.save(Path(args.export))
    if args.graph_json:
        g = Graph.load(Path(args.graph_json))
        print(precedent_sheet(pg, g, causal_chains(g)))
        return 0
    print(json.dumps({"graph": pg.graph.stats(), "patterns": pg.pattern_stats()}, ensure_ascii=False, indent=1))
    return 0
