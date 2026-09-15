"""claim-agent command line interface."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .improve.evalharness import EvalCase, EvalResult, evaluate, summarize, write_results
from .improve.experiments import Variant
from .improve.feedback import build_feedback
from .improve.lessons import extract_llm_materials, propose_from_feedback, propose_from_run, propose_with_llm
from .improve.rca import build_rca
from .models.enums import RequestMode
from .models.request import RunRequest
from .pipeline.engine import Decision
from .runtime import Runtime, build_runtime, make_provider, make_shadow_hook
from .store.report import render_report
from .store.telemetry import read_telemetry

EXIT = {"DRAFT_CLAIM_LOCK": 0, "FINAL_CLAIM_LOCK": 0, "REVIEW_ONLY_DONE": 0, "HALTED_REVIEW": 2, "HALTED_USER_DECISION": 2, "HALTED_BLOCK": 3, "HALTED_UNVERIFIED": 3, "HALTED_LOOP_LIMIT": 4, "HALTED_BUDGET_LIMIT": 4, "HALTED_ENVELOPE_INVALID": 5, "HALTED_ENVELOPE_REPORT_MISMATCH": 5, "HALTED_ERROR": 1}


def _exit_code(outcome: str) -> int:
    if outcome in EXIT:
        return EXIT[outcome]
    return 0 if outcome.endswith("LOCK") else 1


def _rt(args, variant_path: str | None = None) -> Runtime:
    root = Path(args.project_root).resolve()
    variant = Variant.load(Path(variant_path)) if variant_path else None
    overrides = {}
    if getattr(args, "model", None):
        overrides["model.default"] = args.model
    if getattr(args, "no_cache", False):
        overrides["cache.enabled"] = False
    for flag, key in (("max_calls", "pipeline.max_calls"), ("max_total_tokens", "pipeline.max_total_tokens"), ("max_cost_usd", "pipeline.max_cost_usd")):
        if getattr(args, flag, None) is not None:
            overrides[key] = getattr(args, flag)
    if getattr(args, "expand_multi", False):
        overrides["pipeline.expand_multi_dependent"] = True
    return build_runtime(root, Path(args.config) if args.config else None, variant, overrides)


def _provider(rt: Runtime, args):
    mode = "replay" if getattr(args, "replay", None) else ("record" if getattr(args, "record", None) else "gemini")
    fixtures = Path(args.replay) if getattr(args, "replay", None) else (Path(args.record) if getattr(args, "record", None) else None)
    return make_provider(rt, mode, fixtures, strict_replay=getattr(args, "strict_replay", False))


def _request_from_args(args) -> RunRequest:
    if args.request_yaml:
        req = RunRequest.from_yaml(Path(args.request_yaml))
        if args.mode:
            req.request_mode = RequestMode(args.mode)
        return req
    text = Path(args.request_file).read_text(encoding="utf-8") if args.request_file else (args.request or "")
    if not text:
        raise SystemExit("--request, --request-file 또는 --request-yaml 중 하나가 필요하다")
    user_lock = Path(args.user_lock_file).read_text(encoding="utf-8").strip() if args.user_lock_file else args.user_lock
    prior = [] if (not args.prior_art or args.prior_art == ["NONE"]) else args.prior_art
    return RunRequest(
        request_mode=RequestMode(args.mode or "AUTHORING_DRAFT"), request_text=text, candidate_id=args.candidate_id or "cand-01", user_lock=user_lock,
        invention_sources=args.source or [], spec_path=args.spec, drawings=args.drawing or [], prior_art=prior, dependent=bool(args.dependent),
        dependent_target=args.dependent_target, dependent_set_id=args.dependent_set_id, claim_file=args.claim_file,
    )


def _print_summary(state) -> None:
    print(f"[{state.run_id}] outcome={state.outcome} stage={state.stage.value} revision={state.candidate.revision} design={state.candidate.design_revision}")
    if state.halt:
        h = state.halt
        print(f"  halt: {h.kind} @ {h.stage} ({h.role}) {h.reason_code or ''} {h.message[:200]}")
        for o in h.open_issues[:5]:
            print(f"   - [{o.get('kind')}] {o.get('text','')[:160]}")
    u = state.usage
    if u.get("calls"):
        cost = "N/A" if u.get("unpriced_calls") else f"${u.get('cost_usd', 0):.4f}"
        print(f"  usage: {int(u['calls'])} calls, {u.get('latency_ms', 0) / 1000:.0f}s, in {int(u.get('prompt_tokens', 0)):,} (cached {int(u.get('cached_tokens', 0)):,}) / out {int(u.get('output_tokens', 0) + u.get('thoughts_tokens', 0)):,} tokens, cost {cost}")
    print(f"  report: runs/{state.run_id}/report.md")


# ----------------------------------------------------------------------------- commands
def cmd_run(args) -> int:
    rt = _rt(args, args.variant)
    provider = _provider(rt, args)
    diffs: list[dict] = []
    hook = None
    if args.shadow:
        srt = build_runtime(Path(args.project_root).resolve(), Path(args.config) if args.config else None, Variant.load(Path(args.shadow)))
        hook = make_shadow_hook(srt, _provider(srt, args), diffs)
    engine = rt.engine(provider, hook)
    req = _request_from_args(args)
    state = engine.start(req, args.run_id)
    state = engine.run(state)
    if args.claims_only:
        print(render_report(state, claims_only=True))
    else:
        _print_summary(state)
    if diffs:
        print(f"  shadow diffs: {sum(1 for d in diffs if d['diff'])}/{len(diffs)} calls differ (runs/{state.run_id}/shadow/)")
    return _exit_code(state.outcome)


def cmd_resume(args) -> int:
    rt = _rt(args, args.variant)
    engine = rt.engine(_provider(rt, args))
    action = "none"
    for flag, name in (("apply_style_fix", "style_fix"), ("apply_meaning_fix", "meaning_fix"), ("redesign", "redesign"), ("accept_unverified", "accept_unverified")):
        if getattr(args, flag, False):
            action = name
    text = Path(args.decision_file).read_text(encoding="utf-8") if args.decision_file else (args.decide or "")
    decision = Decision(text=text, action=action, add_sources=args.add_source or None, restart_from=args.restart_from, scope=args.scope)
    state = engine.resume(args.run_id, decision)
    _print_summary(state)
    return _exit_code(state.outcome)


def cmd_revise(args) -> int:
    rt = _rt(args, args.variant)
    engine = rt.engine(_provider(rt, args))
    if args.style_only:
        d = Decision(text=args.style_only, action="style_fix", scope=args.scope)
    elif args.meaning:
        d = Decision(text=args.meaning, action="meaning_fix", scope=args.scope)
    else:
        d = Decision(text=args.design or "", action="redesign", scope=args.scope)
    state = engine.resume(args.run_id, d)
    _print_summary(state)
    return _exit_code(state.outcome)


def cmd_review(args) -> int:
    rt = _rt(args, None)
    engine = rt.engine(_provider(rt, args))
    req = RunRequest(
        request_mode=RequestMode.REVIEW_ONLY, request_text=args.request or "제공된 청구항의 요청 검토", candidate_id=args.candidate_id or "review-01",
        user_lock=Path(args.user_lock_file).read_text(encoding="utf-8").strip() if args.user_lock_file else None, invention_sources=args.source or [],
        claim_file=args.claim_file, reviewers=[r.strip() for r in args.reviewers.split(",")], review_scope=args.scope,
    )
    state = engine.run(engine.start(req, args.run_id))
    _print_summary(state)
    return _exit_code(state.outcome)


def cmd_runs(args) -> int:
    rt = _rt(args)
    if args.sub == "list":
        for r in rt.store.list_runs():
            st = rt.store.load_state(r)
            print(f"{r}\t{st.outcome}\t{st.request_mode.value}\t{st.candidate.candidate_id} {st.candidate.revision}/{st.candidate.design_revision}\t{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.updated_at))}")
    elif args.sub == "show":
        st = rt.store.load_state(args.run_id)
        if st.source_set_id != rt.source_set_id:
            print(f"WARNING: source_set_id differs (run {st.source_set_id} vs now {rt.source_set_id}) — records are STALE for continuation")
        print(json.dumps(st.model_dump(mode="json", exclude={"records"}), ensure_ascii=False, indent=2)[:6000])
        for rid, r in st.records.items():
            print(f"  {r.stage:14s} {r.role:38s} {rid:45s} {r.status:10s} {r.gates}")
    elif args.sub == "report":
        st = rt.store.load_state(args.run_id)
        print(render_report(st, claims_only=args.claims_only))
    elif args.sub == "rca":
        rep = build_rca(rt.store, args.run_id)
        md = rep.render_md()
        out = Path(args.out) if args.out else rt.store.run_dir(args.run_id) / "rca.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(md)
        print(f"written: {out}")
    return 0


def cmd_doctor(args) -> int:
    from .doctor import run_doctor

    rt = _rt(args)
    rows = run_doctor(rt, live=args.live, model=args.model, contracts=args.contracts)
    worst = 0
    for check, status, detail in rows:
        print(f"[{status:4s}] {check}: {detail}")
        worst = max(worst, {"OK": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}[status])
    return 0 if worst < 2 else 1


def cmd_models(args) -> int:
    rt = _rt(args)
    from .provider.gemini import GeminiProvider, make_client

    gp = GeminiProvider(make_client(rt.cfg.model.api_key_env))
    for n in gp.list_models():
        mark = " <- configured" if n == rt.cfg.model.default else ""
        print(n + mark)
    return 0


def cmd_sources(args) -> int:
    rt = _rt(args)
    from .roles.whitelist import SOURCE_WHITELIST

    print(f"source_set_id: {rt.source_set_id}")
    for k, sf in rt.sources.files.items():
        print(f"  {k:8s} {sf.sha256[:12]} {sf.relpath} ({len(sf.text)} chars)")
    print("whitelist:")
    for (role, scope), keys in SOURCE_WHITELIST.items():
        print(f"  {role:38s} {scope.value:16s} {keys or '(none)'}")
    return 0


def cmd_cache(args) -> int:
    rt = _rt(args)
    from .provider.cache import CacheManager

    cm = CacheManager(None, rt.cfg.path("runs_dir") / ".cache-registry.json", rt.cfg.cache.ttl, False)
    if args.sub == "list":
        for e in cm.list():
            print(f"{e.name}\t{e.role}/{e.scope}\t{e.model}\texpires {time.strftime('%H:%M:%S', time.localtime(e.expires_at))}\t{e.token_count} tok")
        if not cm.list():
            print("(no cache entries)")
    else:
        try:
            from .provider.gemini import make_client

            cm.client = make_client(rt.cfg.model.api_key_env)
        except Exception:  # noqa: BLE001
            pass
        print(f"purged {cm.purge()} entries")
    return 0


def cmd_feedback(args) -> int:
    rt = _rt(args)
    rep = build_feedback(rt.cfg.path("runs_dir"), args.since, args.window, args.threshold)
    out = Path(args.out) if args.out else rt.cfg.project_root / "reports" / f"feedback-{time.strftime('%Y%m%d')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rep.render_md(), encoding="utf-8")
    print(rep.render_md())
    print(f"written: {out}")
    return 0


def _eval_live(rt: Runtime, args) -> int:
    """Golden-set live run: show the call/cost estimate first, then run with the real provider (optionally recording)."""
    from .improve.estimate import estimate_run

    eval_dir = rt.cfg.path("eval_dir")
    cases = [EvalCase.load(p.parent) for p in sorted((eval_dir / "cases").glob("*/request.yaml")) if args.all or p.parent.name == args.case]
    if not cases:
        raise SystemExit("no eval case matched (--case <id> 또는 --all)")
    rows = []
    for run in rt.store.list_runs():
        rows.extend(read_telemetry(rt.store.telemetry_path(run)))
    model = args.model or rt.cfg.model.default
    total_calls, total_cost = 0, 0.0
    for case in cases:
        est = estimate_run(case.request, model, rt.cfg.telemetry.pricing, rows)
        total_calls += est.calls
        total_cost += est.cost or 0.0
        print(f"{case.case_id}: {est.render()}")
    cost_text = f"${total_cost:.4f}" if rt.cfg.telemetry.pricing.get(model) else "N/A"
    print(f"합계: 호출 {total_calls}회, 추정 비용 {cost_text} (모델 {model})")
    if not args.yes:
        answer = input("실제 API를 호출합니다. 진행할까요? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("취소했습니다.")
            return 0
    args.replay = None
    args.replay_only = False
    args.sub = "run"
    return cmd_eval(args)


def cmd_eval(args) -> int:
    rt = _rt(args)
    eval_dir = rt.cfg.path("eval_dir")
    if args.sub == "new":
        from .improve.evalharness import scaffold_case

        d = scaffold_case(eval_dir / "cases", args.case_id)
        print(f"created {d}\n  1) sources/invention.md에 발명 설명, 도면은 sources/에 넣고 request.yaml에 등록\n  2) expected.yaml의 기대값 채우기\n  3) claim-agent eval live --case {args.case_id} --record {d / 'fixtures'} 로 1회 녹화 후 claim-agent fixtures sanitize {d / 'fixtures'}")
        return 0
    if args.sub == "live":
        return _eval_live(rt, args)
    if args.sub == "list":
        for p in sorted((eval_dir / "cases").glob("*/request.yaml")):
            print(p.parent.name)
        return 0
    if args.sub == "compare":
        a = json.loads(Path(args.a).read_text(encoding="utf-8"))
        b = json.loads(Path(args.b).read_text(encoding="utf-8"))
        for ra, rb in zip(a["results"], b["results"], strict=False):
            print(f"{ra['case_id']}: {ra['variant_id']} passed={ra['passed']} tokens={ra['tokens']} | {rb['variant_id']} passed={rb['passed']} tokens={rb['tokens']}")
        return 0
    cases = [EvalCase.load(p.parent) for p in sorted((eval_dir / "cases").glob("*/request.yaml")) if args.all or p.parent.name == args.case]
    if getattr(args, "replay_only", False) and not args.replay and not args.record:
        for case in [c for c in cases if not c.fixtures_dir]:
            print(f"{case.case_id}: SKIP (no fixtures; --replay-only)")
        cases = [c for c in cases if c.fixtures_dir]
    if not cases:
        raise SystemExit("no eval case matched")
    variants = [Variant.default()]
    if args.variant:
        variants = ([Variant.default()] if args.baseline else []) + [Variant.load(Path(args.variant))]
    results: list[EvalResult] = []
    diffs: list[dict] = []
    for case in cases:
        for variant in variants:
            vrt = build_runtime(rt.cfg.project_root, Path(args.config) if args.config else None, variant)
            fixtures = Path(args.replay) if args.replay else (case.fixtures_dir if (case.fixtures_dir and not args.record) else None)
            mode = "record" if args.record else ("replay" if fixtures else "gemini")
            provider = make_provider(vrt, mode, Path(args.record) if args.record else fixtures)
            hook = None
            if args.shadow and variant.name == "default" and args.variant:
                srt = build_runtime(rt.cfg.project_root, Path(args.config) if args.config else None, Variant.load(Path(args.variant)))
                hook = make_shadow_hook(srt, make_provider(srt, mode, Path(args.record) if args.record else fixtures), diffs)
            engine = vrt.engine(provider, hook)
            run_id = f"eval-{case.case_id}-{variant.name}-{time.strftime('%Y%m%d%H%M%S')}"
            state = engine.run(engine.start(case.request, run_id))
            cur = state.candidate.current
            final_text = (cur.exact_text if cur else None) or ""
            if state.dependent and state.dependent.current and state.dependent.current.exact_text:
                final_text += "\n" + state.dependent.current.exact_text
            tokens, calls = summarize(state, read_telemetry(vrt.store.telemetry_path(run_id)))
            checks = evaluate(state, case.expected, final_text, tokens, calls)
            results.append(EvalResult(case.case_id, variant.variant_id, run_id, state.outcome, checks, tokens, calls, state.total_loops))
            print(f"{case.case_id} / {variant.variant_id}: {state.outcome} passed={results[-1].passed} calls={calls}")
    out = write_results(Path(args.out) if args.out else eval_dir / "results", results, diffs)
    print(f"written: {out}")
    return 0 if all(r.passed for r in results) else 1


def cmd_lessons(args) -> int:
    rt = _rt(args)
    ls = rt.lessons
    if args.sub == "list":
        for l in ls.list(args.status):
            print(f"{l.id}\tv{l.version}\t{l.status}\t{','.join(l.target_roles) or '*'}\t{l.text_ko[:80]}")
    elif args.sub == "show":
        l, s = ls.get(args.id)
        print(l.to_yaml())
    elif args.sub == "approve":
        l = ls.approve(args.id, note=args.note)
        print(f"approved {l.id} v{l.version}; new lessons hash {ls.digest()[:12]} (source_set_id changes for future runs)")
    elif args.sub == "reject":
        l = ls.reject(args.id, note=args.note)
        print(f"rejected {l.id}")
    elif args.sub == "propose":
        if args.text:
            l = ls.propose(args.text, args.roles.split(",") if args.roles else [], args.rationale or "", [])
            print(f"proposed {l.id} (pending)")
        elif args.from_feedback:
            rep = build_feedback(rt.cfg.path("runs_dir"), args.since)
            out = propose_from_feedback(rep, ls, args.min_count)
            for l in out:
                print(f"proposed {l.id} ({len(l.evidence)} runs): {l.text_ko[:110]}")
            if not out:
                print(f"no pattern repeated at least {args.min_count} times — nothing to backpropagate")
        elif args.from_run:
            st = rt.store.load_state(args.from_run)
            data = st.model_dump(mode="json")
            halt = data.get("halt") or {}
            if args.llm:
                if not halt:
                    print("run did not halt; nothing to draft from")
                    return 0
                record = rt.store.read_record(args.from_run, halt.get("record_id")) if halt.get("record_id") else {}
                materials = extract_llm_materials(record, halt)
                provider = _provider(rt, args)
                key = f"{halt.get('role')}|{halt.get('stage')}|{halt.get('reason_code') or halt.get('kind')}"
                l = propose_with_llm(provider, rt.cfg.model.default, materials, ls, halt.get("role", ""), [f"{args.from_run}:{halt.get('record_id')}"], key)
                if l is None:
                    print("model produced no usable draft (근거 부족)")
                else:
                    print(f"proposed {l.id} (pending, llm_drafted): {l.text_ko[:110]}")
                    print("보낸 입력에는 청구항 문언·원자료가 포함되지 않았다. 승인 전에는 주입되지 않는다.")
            else:
                out = propose_from_run(rt.cfg.path("runs_dir"), data, ls)
                for l in out:
                    print(f"proposed {l.id}: {l.text_ko[:100]}")
                if not out:
                    print("no open issues to propose from")
        else:
            print("--from-run, --from-feedback 또는 --text 중 하나가 필요하다")
            return 1
    return 0


def cmd_fixtures(args) -> int:
    d = Path(args.dir)
    n = 0
    for p in d.glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        data["request_preview"] = ""
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        n += 1
    print(f"sanitized {n} fixtures in {d}")
    return 0


# ----------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claim-agent", description="Claim-Agent Python + Gemini runtime")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--project-root", default=".")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    tui = sub.add_parser("tui", help="open the mouse-friendly terminal workspace")
    tui.add_argument("files", nargs="*")

    def open_tui(args):
        from .tui import ClaimAgentApp

        ClaimAgentApp(Path(args.project_root), args.files, Path(args.config) if args.config else None).run()
        return 0

    tui.set_defaults(func=open_tui)

    wb = sub.add_parser("web", help="open the local web chat (same entry as the desktop launcher)")
    wb.add_argument("--no-browser", action="store_true")

    def open_web(args):
        from .web import main as web_main

        argv = ["--project-root", str(Path(args.project_root).resolve())]
        if args.config:
            argv += ["--config", str(Path(args.config).resolve())]
        if args.no_browser:
            argv.append("--no-browser")
        return web_main(argv)

    wb.set_defaults(func=open_web)

    def common_provider(sp):
        sp.add_argument("--model")
        sp.add_argument("--no-cache", action="store_true")
        sp.add_argument("--replay", help="fixtures dir for offline replay")
        sp.add_argument("--strict-replay", action="store_true")
        sp.add_argument("--record", help="fixtures dir to record live responses")
        sp.add_argument("--variant", help="experiments/<name>.yaml")
        sp.add_argument("--max-calls", type=int, help="budget guard: halt before exceeding this many provider calls")
        sp.add_argument("--max-total-tokens", type=int, help="budget guard: halt once prompt+output+thoughts tokens exceed this")
        sp.add_argument("--max-cost-usd", type=float, help="budget guard: halt once the estimated cost exceeds this (needs telemetry.pricing)")
        sp.add_argument("--expand-multi", action="store_true", help="reconstruct multi-dependent claims once per alternative parent chain (pipeline.expand_multi_dependent)")

    r = sub.add_parser("run", help="run the authoring/finalization pipeline")
    common_provider(r)
    r.add_argument("--mode", choices=[m.value for m in RequestMode])
    r.add_argument("--request")
    r.add_argument("--request-file")
    r.add_argument("--request-yaml")
    r.add_argument("--source", action="append")
    r.add_argument("--spec")
    r.add_argument("--drawing", action="append")
    r.add_argument("--prior-art", action="append")
    r.add_argument("--user-lock")
    r.add_argument("--user-lock-file")
    r.add_argument("--candidate-id")
    r.add_argument("--dependent", action="store_true")
    r.add_argument("--dependent-target")
    r.add_argument("--dependent-set-id")
    r.add_argument("--claim-file")
    r.add_argument("--shadow", help="experiments/<name>.yaml to run as shadow")
    r.add_argument("--claims-only", action="store_true")
    r.add_argument("--run-id")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="continue a halted run with a user decision")
    common_provider(rs)
    rs.add_argument("run_id")
    rs.add_argument("--decide")
    rs.add_argument("--decision-file")
    rs.add_argument("--apply-style-fix", action="store_true")
    rs.add_argument("--apply-meaning-fix", action="store_true")
    rs.add_argument("--redesign", action="store_true")
    rs.add_argument("--add-source", action="append")
    rs.add_argument("--accept-unverified", action="store_true")
    rs.add_argument("--restart-from", choices=["ARCHITECT", "DEP_ARCHITECT"])
    rs.add_argument("--scope", choices=["INDEPENDENT", "DEPENDENT"], help="which revision a style/meaning fix targets on a finished run")
    rs.set_defaults(func=cmd_resume)

    rv = sub.add_parser("revise", help="user-initiated new revision of a finished run")
    common_provider(rv)
    rv.add_argument("run_id")
    g = rv.add_mutually_exclusive_group(required=True)
    g.add_argument("--style-only")
    g.add_argument("--meaning")
    g.add_argument("--design")
    rv.add_argument("--scope", choices=["INDEPENDENT", "DEPENDENT"])
    rv.set_defaults(func=cmd_revise)

    rw = sub.add_parser("review", help="REVIEW_ONLY: run selected reviewers on given claims")
    common_provider(rw)
    rw.add_argument("--scope", default="INDEPENDENT", choices=["INDEPENDENT", "DEPENDENT_SET"])
    rw.add_argument("--reviewers", default="syntax-scope-reviewer")
    rw.add_argument("--claim-file", required=True)
    rw.add_argument("--request")
    rw.add_argument("--source", action="append")
    rw.add_argument("--user-lock-file")
    rw.add_argument("--candidate-id")
    rw.add_argument("--run-id")
    rw.set_defaults(func=cmd_review)

    ru = sub.add_parser("runs", help="list/show/report runs")
    rus = ru.add_subparsers(dest="sub", required=True)
    rus.add_parser("list")
    s = rus.add_parser("show")
    s.add_argument("run_id")
    rp = rus.add_parser("report")
    rp.add_argument("run_id")
    rp.add_argument("--claims-only", action="store_true")
    rr = rus.add_parser("rca", help="root cause analysis of one run: trace, fault, pattern, impact")
    rr.add_argument("run_id")
    rr.add_argument("--out")
    ru.set_defaults(func=cmd_runs)

    d = sub.add_parser("doctor", help="check environment, sources, roles, schema; --live probes the API")
    d.add_argument("--live", action="store_true")
    d.add_argument("--model")
    d.add_argument("--contracts", action="store_true")
    d.set_defaults(func=cmd_doctor)

    m = sub.add_parser("models", help="list models available to the API key")
    m.set_defaults(func=cmd_models)

    so = sub.add_parser("sources", help="show source catalog and whitelist")
    so.add_argument("sub", nargs="?", default="check")
    so.set_defaults(func=cmd_sources)

    c = sub.add_parser("cache", help="list or purge context caches")
    c.add_argument("sub", choices=["list", "purge"])
    c.set_defaults(func=cmd_cache)

    f = sub.add_parser("feedback", help="aggregate telemetry into a failure-pattern and proactive-alert report")
    f.add_argument("--since")
    f.add_argument("--out")
    f.add_argument("--window", type=int, default=10, help="최근 N run을 이전 구간과 비교 (선제 경고)")
    f.add_argument("--threshold", type=float, default=0.15, help="비-PASS·복구 비율 상승폭 임계")
    f.set_defaults(func=cmd_feedback)

    e = sub.add_parser("eval", help="run eval cases (baseline vs variant, shadow, record/replay)")
    es = e.add_subparsers(dest="sub", required=True)
    er = es.add_parser("run")
    common_provider(er)
    er.add_argument("--case")
    er.add_argument("--all", action="store_true")
    er.add_argument("--baseline", action="store_true")
    er.add_argument("--shadow", action="store_true")
    er.add_argument("--replay-only", action="store_true", help="skip cases without recorded fixtures (CI mode; never calls the API)")
    er.add_argument("--out")
    es.add_parser("list")
    en = es.add_parser("new", help="scaffold a golden case directory (request.yaml, expected.yaml, sources/)")
    en.add_argument("case_id")
    el = es.add_parser("live", help="golden-set live run: estimate calls/cost first, then call the real API (use --record to keep fixtures)")
    common_provider(el)
    el.add_argument("--case")
    el.add_argument("--all", action="store_true")
    el.add_argument("--baseline", action="store_true")
    el.add_argument("--shadow", action="store_true")
    el.add_argument("--out")
    el.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    ec = es.add_parser("compare")
    ec.add_argument("a")
    ec.add_argument("b")
    e.set_defaults(func=cmd_eval)

    l = sub.add_parser("lessons", help="approved-only lesson memory")
    lss = l.add_subparsers(dest="sub", required=True)
    ll = lss.add_parser("list")
    ll.add_argument("--status")
    lsh = lss.add_parser("show")
    lsh.add_argument("id")
    la = lss.add_parser("approve")
    la.add_argument("id")
    la.add_argument("--note")
    lr = lss.add_parser("reject")
    lr.add_argument("id")
    lr.add_argument("--note")
    lp = lss.add_parser("propose")
    lp.add_argument("--from-run")
    lp.add_argument("--from-feedback", action="store_true", help="반복 패턴을 역할별 교훈 초안으로 역전파")
    lp.add_argument("--min-count", type=int, default=2)
    lp.add_argument("--since")
    lp.add_argument("--llm", action="store_true", help="--from-run과 함께: 모델이 초안을 작성 (승인 전 주입 없음)")
    lp.add_argument("--model")
    lp.add_argument("--replay")
    lp.add_argument("--strict-replay", action="store_true")
    lp.add_argument("--record")
    lp.add_argument("--no-cache", action="store_true")
    lp.add_argument("--text")
    lp.add_argument("--roles")
    lp.add_argument("--rationale")
    l.set_defaults(func=cmd_lessons)

    fx = sub.add_parser("fixtures", help="sanitize recorded fixtures (strip request previews)")
    fx.add_argument("sub", choices=["sanitize"])
    fx.add_argument("dir")
    fx.set_defaults(func=cmd_fixtures)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
