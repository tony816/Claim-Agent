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

    c = gs.add_parser("caselaw", help="case-law DB: check, claim caution sheet (S11), cases for a review finding")
    cs = c.add_subparsers(dest="caselaw_cmd", required=True)
    ck = cs.add_parser("check", help="read-only DB check (SHA-256, release, row counts)")
    ck.add_argument("--full", action="store_true", help="also run SQLite integrity_check (slow)")
    sh = cs.add_parser("sheet", help="print the claim caution sheet built from the lesson records")
    sh.add_argument("--write", action="store_true", help=f"write it to {caselaw_sheet_path()}")
    fd = cs.add_parser("find", help="cases whose court finding matches a syntax/OA finding")
    fd.add_argument("finding", help="the finding text: claim wording + what is unclear/unsupported")
    fd.add_argument("--group", choices=["명확성", "뒷받침", "신규사항", "권리범위"])
    fd.add_argument("-k", type=int, default=5, help="candidates to keep before Jev")
    fd.add_argument("--jev", action="store_true", help="confirm each candidate with one Jev request (OpenRouter)")
    fd.add_argument("--json", action="store_true")
    sw = cs.add_parser("show", help="one case's records with the verified quote")
    sw.add_argument("case_number")
    g.set_defaults(func=cmd_graph)


def caselaw_sheet_path() -> str:
    from .caselaw import SHEET_PATH

    return SHEET_PATH


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
    return _precedent(rt, args)


def _caselaw(rt, args) -> int:
    from contextlib import closing

    from . import caselaw as cl

    paths = cl.Paths.from_config(rt.cfg)
    try:
        if args.caselaw_cmd == "check":
            print(json.dumps(cl.validate(paths, full=args.full), ensure_ascii=False, indent=1))
            return 0
        lessons = cl.load_lessons(paths.lessons)
        if args.caselaw_cmd == "sheet":
            text = cl.sheet(lessons)
            if args.write:
                (rt.cfg.project_root / cl.SHEET_PATH).write_text(text, encoding="utf-8")
                print(f"→ {cl.SHEET_PATH} ({len(text)}자)")
            else:
                print(text)
            return 0
        if args.caselaw_cmd == "show":
            rs = [r for r in lessons["records"] if r["case_number"] == args.case_number]
            if not rs:
                print(f"교훈 기록에 {args.case_number} 없음")
                return 1
            with closing(cl.connect(paths)) as db:
                for r in rs:
                    print(json.dumps({**r, "quote_matches_db": cl.quote_ok(db, r),
                                      "pdf": str(paths.pdfs / f"{r['document_id']}.pdf")}, ensure_ascii=False, indent=1))
            return 0
        return _caselaw_find(cl, paths, lessons, args)
    except cl.CaseLawError as exc:
        print(f"판례 DB 사용 불가: {exc}")
        return 2


def _caselaw_find(cl, paths, lessons, args) -> int:
    from contextlib import closing

    from ..prior_art import jev

    hits = cl.find(lessons, args.finding, args.group, args.k)
    probs, jev_note = {}, "Jev 미호출"
    if args.jev and hits:
        ok, why = jev.status()
        if ok:
            res = cl.confirm(args.finding, [r for _, r in hits], jev.JevClient())
            probs = res.get("probabilities") or {}
            jev_note = f"Jev {res['status']}" + (f" — {res['error']}" if res.get("error") else "")
        else:
            jev_note = f"Jev 불가 — {why}"
    with closing(cl.connect(paths)) as db:
        rows = []
        for i, (score, r) in enumerate(hits):
            p = probs.get(f"case_{i}")
            rows.append({"case": f"{r['court']} {r['case_number']}", "decision_date": r["decision_date"], "requirement": r["requirement"],
                         "outcome": r["outcome"], "tag": r["tag"], "score": round(score, 3), "jev": p, "label": cl.label(p),
                         "claim_feature": r["claim_feature"], "deciding_fact": r["deciding_fact"], "page": r["page"],
                         "quote": r["quote"], "quote_matches_db": cl.quote_ok(db, r), "drive_link": r["drive_link"]})
    if args.jev and probs:
        rows = [x for x in rows if x["jev"] is not None and x["jev"] >= cl.JEV_THRESHOLD]
    if args.json:
        # jev_question: CLI에서 Jev를 못 부를 때 오케스트레이터가 Jev 커넥터로 후보마다 같은 질문을 묻는다(docs/caselaw.md).
        print(json.dumps({"finding": args.finding, "jev": jev_note, "jev_question": cl.JEV_QUESTION, "jev_threshold": cl.JEV_THRESHOLD,
                          "cases": rows, "reviewed": False}, ensure_ascii=False, indent=1))
        return 0
    print(f"판례 참고 (미검토 — 게이트·근거 아님) · {jev_note} · {len(rows)}건")
    for x in rows:
        print(f"\n- {x['case']} ({x['decision_date']}) {x['requirement']} {x['outcome']} [{x['tag']}] {x['label']}"
              + (f" p={x['jev']:.2f}" if x["jev"] is not None else f" 겹침={x['score']}"))
        print(f"  청구항 문언: {x['claim_feature']}\n  법원이 본 사실: {x['deciding_fact']}")
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
