"""Intake and dispatch from a conversation into the existing gated engine."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from .claim_scope import apply_ui_target, constrain_target, find_existing_set, named_parents, parse_target, ui_target_set
from .models.prior_art import PriorSearchOptions
from .models.request import IMAGE_EXT, RunRequest
from .pipeline.engine import Decision
from .routing import RouteDecision
from .sources.extract import DOC_EXT, read_text_any
from .tui_support import validate_attachment


def intake(request: dict, folder: Path, previous=None) -> tuple[list[dict], dict]:
    """Preserve user materials; model answers are references, never invention facts."""
    directory = folder / "intake"
    directory.mkdir(exist_ok=True)
    blocks: list[dict] = []
    paths = {"invention_sources": [], "drawings": [], "prior_art": [], "spec_path": None}
    seen = set()

    def add_text(text, origin, category="invention", path=None, is_request=False, name=None):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = (origin, category, digest)
        if not text.strip() or key in seen:
            return
        seen.add(key)
        bid = f"input-{len(blocks) + 1}"
        p = Path(path) if path else directory / f"{name or bid}-{digest[:12]}.txt"
        if not path:
            p.write_text(text, encoding="utf-8", newline="")     # no CRLF translation on Windows: the file is the exact text
        blocks.append(dict(id=bid, text=text, origin=origin, category=category, path=str(p), is_request=is_request))
        if origin == "user":
            field = {"spec": "spec_path", "prior_art": "prior_art"}.get(category, "invention_sources")
            if field == "spec_path":
                if paths[field] and paths[field] != str(p):
                    raise ValueError("정식 명세서는 한 파일로 지정해야 합니다.")
                paths[field] = str(p)
            else:
                paths[field].append(str(p))

    def add_file(name, category="invention"):
        item = validate_attachment(Path(name), category)
        if item.path.suffix.lower() in IMAGE_EXT:
            paths["drawings"].append(str(item.path))
        elif item.path.suffix.lower() in DOC_EXT:
            # Documents are extracted once here; the block keeps the original file as its path so the
            # engine re-extracts identically and the material identity stays the original sha256.
            add_text(read_text_any(item.path), "user", item.category, item.path)
        else:
            add_text(item.path.read_text(encoding="utf-8-sig"), "user", item.category, item.path)

    if previous:
        for item in previous.material_meta:
            if item["category"] != "claim_file":
                add_file(item["path"], item["category"])
        cur = previous.candidate.current
        dep = previous.dependent.current if previous.dependent else None
        text = "\n\n".join(t for t in [cur.exact_text if cur else None, dep.exact_text if dep else None] if t)
        if text:
            add_text(text, "assistant_reference", "existing_claims")

    for entry in request.get("history", []):
        origin = "user" if entry["role"] == "user" else "assistant_reference"
        for part in entry.get("parts", []):
            if "text" in part:
                add_text(part["text"], origin)
            elif origin == "user" and "inline_data" in part:
                image = part["inline_data"]
                ext = next((ext for ext, mime in IMAGE_EXT.items() if mime == image["mime_type"]), None)
                if not ext:
                    raise ValueError("지원하지 않는 이전 첨부 이미지 형식입니다.")
                raw = base64.b64decode(image["data"], validate=True)
                path = directory / (hashlib.sha256(raw).hexdigest() + ext)
                if not path.exists():
                    path.write_bytes(raw)
                paths["drawings"].append(str(path))
    add_text(request.get("material", ""), "user")
    add_text(request.get("reference", ""), "assistant_reference")
    # Project instructions are text the user wrote: technical descriptions and claims there are user material like a
    # pasted note. They also stay in 현재 요청 as standing directives; an unchanged text dedupes on follow-ups.
    add_text(str(request.get("instructions") or "").strip(), "user", name="project-instructions")
    categories = {a["path"]: a.get("category", "invention") for a in request.get("attachments", [])}
    for name in request.get("files", []):
        add_file(name, categories.get(name, "invention"))
    add_text(request["text"], "user", is_request=True)
    paths["drawings"] = list(dict.fromkeys(paths["drawings"]))
    return blocks, paths


def resolve_edit_scope(route: RouteDecision, request: dict, blocks: list[dict], folder: Path, previous=None) -> tuple[str, str] | None:
    """Set route.authoring_scope from the parsed input; returns (claim_file path, baseline digest) for EXISTING_SET_EDIT.

    An authoring request that names dependent claims (in its text or the composer pick, already narrowed onto
    route.dependent_target) is an in-place edit when a user-provided numbered set — a pasted or attached block, the
    project instructions or the project USER_LOCK — holds every named claim as a dependent claim. The work then stays
    inside that set: no new independent claim, no renumbering. A previous run's own dependent set keeps the ordinary
    follow-up path, because those claims are this harness's output, not a set the user supplied.

    Dependent claims asked for with no number ('히트리드 모듈에 대한 종속항들 작성해줘', run web-b013b034) that the router
    puts on such a set go after its end too, with edit_targets empty: the dependent design decides how many and numbers
    them on from the set's end, instead of a new dependent set renumbering over the user's claims.
    """
    from .pipeline.claimtext import ClaimParseError, baseline_digest, continue_set, exact_sha256

    hint = route.authoring_scope
    targets = parse_target(route.dependent_target) if route.dependent else None
    route.authoring_scope, route.edit_targets = ("NEW_DEPENDENT_SET" if route.dependent else "NEW_INDEPENDENT"), []
    # Parents named for the new claims ('14OR15 항의 종속항') mean the user's own set, whatever the router's hint said.
    append = route.dependent and not targets and (hint == "EXISTING_SET_EDIT" or bool(named_parents(request["text"])))
    if route.mode != "AUTHORING_DRAFT" or not (targets or append):
        return None
    own = previous.dependent.current if previous and previous.dependent and not previous.baseline_set else None
    if own and (not targets or targets <= {c["claim_no"] for c in own.claims}):
        return None
    targets = targets or set()
    # User blocks include the project instructions (intake turns them into a material block), so a set kept there is found.
    ordered = sorted(reversed(blocks), key=lambda b: b["id"] != route.claim_source_id)   # the router's pick first, then newest
    sources = [(b["id"], b["text"]) for b in ordered if b["origin"] == "user"]
    # A set kept in the project USER_LOCK is the user's too. It is no material block (USER_LOCK travels on its own), so
    # it is searched last and written out as the edit's claim file.
    lock = str(request.get("user_lock") or "").strip()
    found = find_existing_set(targets, sources + ([("user_lock", lock)] if lock else []))
    if not found:
        return None
    source_id, text = found
    route.authoring_scope, route.edit_targets = "EXISTING_SET_EDIT", sorted(targets)
    # Claims the user pasted to carry the set on ('[13항]' under a USER_LOCK of 1~12) belong to it: read-only like the rest.
    continued = continue_set(text, [t for sid, t in sources if sid != source_id])
    # Parents the user names but this conversation never saw come from a set another conversation of the project locked.
    lent = lend_parents(continued or text, named_parents(request["text"]), request.get("project_sets") or [])
    if lent:
        continued, lender, nos, missing = lent
        request.setdefault("scope_notes", []).append(
            f"지목한 부모항 {', '.join(f'제{n}항' for n in missing)}이 이 대화에 없어, 같은 프로젝트의 다른 대화가 잠근 세트({lender})의 "
            f"제{nos[0]}~{nos[-1]}항을 원문 그대로 읽기 전용 기준 세트에 붙였습니다. 다른 문언을 쓰려면 그 항을 요청에 붙여 넣어 주세요.")
    try:
        digest = baseline_digest(continued or text)
    except ClaimParseError:
        digest = exact_sha256(text)        # every number blank: the engine stops on the blanks before any call
    if continued:
        path = folder / "intake" / f"existing-set-{hashlib.sha256(continued.encode('utf-8')).hexdigest()[:12]}.txt"
        path.write_text(continued, encoding="utf-8", newline="")
        return str(path), digest
    if source_id == "user_lock":
        path = folder / "intake" / f"project-user-lock-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}.txt"
        path.write_text(text, encoding="utf-8", newline="")
        return str(path), digest
    return next(b["path"] for b in blocks if b["id"] == source_id), digest


def lend_parents(text: str, parents: set[int], lenders: list[tuple[str, str]]) -> tuple[str, str, list[int], list[int]] | None:
    """(joined set, lender, lent numbers, missing parents) when a lender set carries `text` on with the named parents it lacks.

    Only the lender's claims after the end of `text` are taken, and only when every earlier claim they cite reads the
    same in both sets: the user's own set stays the authority for the claims it already holds.
    """
    from .pipeline.claimtext import ClaimParseError, ancestor_nos, claim_set_text, continue_set, fill_blank_headers, parse_claim_set

    try:
        claims = parse_claim_set(fill_blank_headers(text))
    except ClaimParseError:
        return None
    ours = {c.claim_no: " ".join(c.body.split()) for c in claims}
    missing = parents - set(ours)
    if not missing or not claims:
        return None
    last = max(ours)
    for lender, lent in lenders:
        try:
            theirs = parse_claim_set(lent)
        except ClaimParseError:
            continue
        tail = [c for c in theirs if c.claim_no > last]
        tail_nos = {c.claim_no for c in tail}
        if not missing <= tail_nos:
            continue
        bodies = {c.claim_no: " ".join(c.body.split()) for c in theirs}
        try:
            cited = set().union(*(ancestor_nos(theirs, c.claim_no) for c in tail)) - tail_nos
        except ClaimParseError:
            continue
        if any(ours.get(n) != bodies.get(n) for n in cited):
            continue
        joined = continue_set(text, [claim_set_text(tail)])
        if joined:
            return joined, lender, sorted(tail_nos), sorted(missing)
    return None


def project_sets(cfg, run_ids: list[str] | None, limit: int = 20) -> list[tuple[str, str]]:
    """(label, full set) of the claim sets the project's other conversations locked, newest first."""
    from .models.state import RunState
    from .store.report import edited_set_text

    out = []
    for rid in (run_ids or [])[:limit]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", rid):
            continue
        try:
            state = RunState.model_validate_json((cfg.path("runs_dir") / rid / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        dep = state.dependent
        lock = dep and (dep.final_set_lock or dep.draft_set_lock)
        text = edited_set_text(state) if lock else None
        if text:
            out.append((f"run {rid} · {lock}", text))
    return out


def revision_path(route: RouteDecision, request: dict, blocks: list[dict], paths: dict, previous,
                  baseline_digest: str | None = None) -> tuple[str, str | None]:
    """Deterministic guard for follow-up edits: (kind, scope).

    kind ∈ restart | style | meaning | redesign | new. The router may only narrow the path: any new material (files,
    drawings, prior art, spec, pasted description) or a changed USER_LOCK forces a full restart from the architect,
    because the contract ties design_revision to the raw-material set. scope is DEPENDENT when the request names only
    dependent claims and the previous run has a live dependent set.

    An EXISTING_SET_EDIT never falls back to the architect: a follow-up on the same set and targets continues the
    dependent revision path (redesign = new dependent_design_revision), and anything else — another set, other
    targets, new material, or switching into or out of the edit scope — starts a new run.
    """
    from .claim_scope import explicit_mentions
    from .pipeline.claimtext import claim_wording

    if previous is not None and (route.authoring_scope == "EXISTING_SET_EDIT" or previous.baseline_set is not None):
        base = previous.baseline_set
        # A follow-up that again names no number stays on claims the previous design numbered itself.
        same_set = (route.authoring_scope == "EXISTING_SET_EDIT" and base is not None and base.sha256 == baseline_digest
                    and (base.edit_targets == route.edit_targets or (base.numbered_by_design and not route.edit_targets)))
        if not same_set or _new_material(request, blocks, paths, previous):
            return "new", None
        dep = previous.dependent
        live = bool(dep and dep.current and dep.current.exact_text and not dep.stale)
        if route.revision_kind == "STYLE_ONLY" and claim_wording(request["text"]):
            return "redesign", "DEPENDENT"          # pasted claim wording is outside the style adjuster's authority
        if route.revision_kind in ("STYLE_ONLY", "MEANING") and live:
            return ("style" if route.revision_kind == "STYLE_ONLY" else "meaning"), "DEPENDENT"
        return "redesign", "DEPENDENT"
    if previous is None or route.revision_kind in ("NONE", "DESIGN"):
        return "restart", None
    if _new_material(request, blocks, paths, previous):
        return "restart", None
    kind = "style" if route.revision_kind == "STYLE_ONLY" else "meaning"
    scope = None
    dependent_live = bool(previous.dependent and previous.dependent.current and not previous.dependent.stale)
    mentioned = explicit_mentions(request["text"]) or ui_target_set(request.get("ui_hints"))
    if mentioned and dependent_live:
        scope = "DEPENDENT"
    if kind == "style":
        wording = claim_wording(request["text"])
        if wording:
            return "redesign", ("DEPENDENT" if wording == "DEPENDENT" and dependent_live else None)
    return kind, scope


def asks_other_edit(route: RouteDecision, previous, text: str) -> bool:
    """Whether a message the router took as a halted edit's decision asks for other claims than the edit sealed there.

    The halted run's BASELINE_SET keeps the targets it was sealed with (or none, for claims the design numbers). Resumed
    with a request for other claims, it carries the old targets on: run web-6be7454f, sent again after the target fix,
    resumed with new claims still inserted at 2~12 and stopped on the same contract. Such a message starts a new run,
    as revision_path does for a follow-up. Only the message's own claim words count, so '진행해' still resumes.
    """
    from .claim_scope import mentions_any_claim
    from .routing import explicit_patent_request

    base = previous.baseline_set if previous else None
    if base is None or route.authoring_scope != "EXISTING_SET_EDIT" or not (mentions_any_claim(text) or explicit_patent_request(text)):
        return False
    targets = sorted(parse_target(route.dependent_target) or []) if route.dependent else []
    return not (targets == base.edit_targets or (base.numbered_by_design and not targets))


def rerouted_request(decision: str, earlier: list[str], run_request: dict, held) -> str | None:
    """The request a decision on a halted run goes to a new run with, or None to resume that run (logs #18~#21).

    A run whose set does not hold the parents the request names cannot reach them by resuming: one routed before log #18
    took them as targets of a new design (run web-54630bf2), and one started by a bare '재개' never read them (runs
    web-741d86b2, web-9d3c7750: baseline 1~12, the dependent architect stopped on 14·15 again). The request goes to a
    new run, where the parents are found in the conversation or lent from the project.

    A decision that names parents is that request. Otherwise the request it continues stands in for it, with the
    decision added when it says more than '재개': the one the halted run recorded (`continues_request`), else the
    latest user message (`earlier`, oldest first) naming parents with only bare decisions after it, else the halted
    run's own request.
    """
    body = _decision_body(decision)
    if named_parents(body):
        asked = body
    else:
        base = run_request.get("continues_request") or ""
        if not base and _bare(body):
            for text in reversed(earlier):
                earlier_body = _decision_body(text)
                if named_parents(earlier_body):
                    base = earlier_body
                    break
                if not _bare(earlier_body):
                    break
        if not base:
            base = (run_request.get("request_text") or "").split("## 현재 요청\n\n", 1)[-1].split("\n\n## ", 1)[0].strip()
        asked = base + ("" if _bare(body) or body in base else "\n\n" + body)
    return asked if named_parents(asked) - set(held) else None


def _decision_body(text: str) -> str:
    """A decision without the web's '[재개 · …]' label; of '하기 재개 / --- / <request>', the request below the rule."""
    text = re.sub(r"^\[재개[^\]\n]*\]\s*", "", text.strip())
    head, sep, rest = text.partition("\n---\n")
    return rest.strip() if sep and len(head) <= 20 else text


def _bare(text: str) -> bool:
    """A decision with no request in it: '재개', '진행해', '그대로 해줘'."""
    from .claim_scope import mentions_any_claim

    return len(text) <= 15 and not mentions_any_claim(text)


def _new_material(request: dict, blocks: list[dict], paths: dict, previous) -> bool:
    known = {str(Path(m["path"]).resolve()) for m in previous.material_meta}
    known_sha = {m.get("sha256") for m in previous.material_meta}
    request_paths = {str(Path(b["path"]).resolve()) for b in blocks if b.get("is_request")}
    new_material = []
    for key in ("invention_sources", "drawings", "prior_art"):
        for p in paths.get(key) or []:
            rp = str(Path(p).resolve())
            if rp in request_paths:
                continue
            if rp not in known and hashlib.sha256(Path(p).read_bytes()).hexdigest() not in known_sha:
                new_material.append(rp)
    if paths.get("spec_path") and str(Path(paths["spec_path"]).resolve()) not in known:
        new_material.append(paths["spec_path"])
    # Files attached in this turn are new material by intent even when their bytes match; a project's preset
    # files ride along with every turn and are already part of the run, so they never force a restart by themselves.
    preset = {a["path"] for a in request.get("attachments", []) if a.get("project_file_id")}
    return bool(new_material or [f for f in request.get("files", []) if f not in preset])


PREVIOUS_ANSWER_HEAD = ("## 앞선 답변 (이 대화에서 Claim-Agent가 낸 직전 답변 — 이번 요청이 가리키는 수정 방향 참고용이며 "
                        "발명 원자료·근거가 아님. 기술 사실은 원자료로 다시 확인하고, 지적을 따를지는 잠긴 계약과 게이트가 정한다)")


def previous_answer(request: dict) -> str:
    """The latest model turn of the conversation (e.g. a REVIEW_ONLY result the user now asks to apply)."""
    for entry in reversed(request.get("history", [])):
        if entry.get("role") == "model":
            return "\n".join(part["text"] for part in entry.get("parts", []) if "text" in part).strip()
    return ""


def effective_request_text(request: dict, with_previous_answer: bool = False) -> str:
    """The request the roles see: project instructions (standing user directives) precede the current turn's text.

    Instructions never enter the material bundle, so they cannot become invention evidence; they only travel in the
    `현재 요청` section that every role already treats as the user's instruction. For authoring, the conversation's
    latest answer follows under a heading that marks it as a non-source reference: "검토한 내용에 따라 수정해줘"
    otherwise reaches the architect and drafter without the review it points at (only the router saw it).
    """
    instructions = str(request.get("instructions") or "").strip()
    answer = previous_answer(request) if with_previous_answer else ""
    if not instructions and not answer:
        return request["text"]
    parts = []
    if instructions:
        parts.append("## 프로젝트 지침 (사용자가 프로젝트 폴더에 미리 설정한 지시 — 같은 전문이 `project-instructions` 원자료로도 전달됨)\n\n" + instructions)
    parts.append("## 현재 요청\n\n" + request["text"])
    if answer:
        parts.append(PREVIOUS_ANSWER_HEAD + "\n\n" + answer)
    return "\n\n".join(parts)


def previous_state(cfg, request):
    run_id = request.get("run_id")
    if not run_id:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("올바르지 않은 이전 작업 식별자입니다.")
    from .models.state import RunState
    return RunState.model_validate_json((cfg.path("runs_dir") / run_id / "state.json").read_text(encoding="utf-8"))


def run_pipeline(rt, provider, request: dict, route: RouteDecision, blocks: list[dict],
                 paths: dict, folder: Path, previous=None, turn: dict | None = None):
    """`turn` carries what the conversation spent before the engine started: `started_at` plus the routing usage."""
    authoring = route.mode in {"AUTHORING_DRAFT", "FINALIZATION"}
    baseline = None
    if authoring:
        route.dependent, route.dependent_target = constrain_target(request["text"], route.dependent, route.dependent_target,
                                                                   append=route.authoring_scope == "EXISTING_SET_EDIT",
                                                                   intent=route.edit_intent)
        route.dependent, route.dependent_target = apply_ui_target(route.mode, route.dependent, route.dependent_target, request["text"], request.get("ui_hints"))
        if request.get("project_runs") and named_parents(request["text"]):
            request = {**request, "project_sets": project_sets(rt.cfg, request["project_runs"])}
        baseline = resolve_edit_scope(route, request, blocks, folder, previous)
    source = next((b for b in blocks if b["id"] == route.claim_source_id), None)
    if source is None and previous and authoring:
        source = next((b for b in blocks if b["category"] == "existing_claims"), None)
    text = effective_request_text(request, with_previous_answer=authoring)
    current_lock = str(request.get("user_lock") or "").strip() or None
    req = RunRequest(
        request_mode=route.mode, request_text=text,
        candidate_id=previous.candidate.candidate_id if previous else "cand-01",
        user_lock=previous.request.get("user_lock") if previous else current_lock,
        dependent=route.dependent if route.mode != "REVIEW_ONLY" else False,
        dependent_target=route.dependent_target if route.mode != "REVIEW_ONLY" else None,
        dependent_set_id=previous.request.get("dependent_set_id") if previous else None,
        reviewers=list(dict.fromkeys(route.reviewers)), review_scope=route.review_scope,
        parent_claims=(request.get("review") or {}).get("parent_claims") or None,
        citation_fills={int(k): int(v) for k, v in ((request.get("review") or {}).get("citation_fills") or {}).items()},
        claim_file=baseline[0] if baseline else (source["path"] if source else None),
        authoring_scope=route.authoring_scope if authoring else None, **paths,
        named_parents=sorted(named_parents(request["text"])) if authoring and route.dependent else [],
        continues_request=request.get("continues_request") or (request["text"] if authoring and named_parents(request["text"]) else None),
        # The router's reading of this turn decides edit or new (개선로그 #24); the engine asks when it is UNCLEAR.
        edit_intent=route.edit_intent if baseline else "EDIT",
        # 선행기술 검색 체크(웹 작성 창·MCP claim_send): WIPS 검색 + Jev 선별 + 사전 심사, 나머지 값은 claim-agent.yaml prior_search
        prior_search=PriorSearchOptions(enabled=True) if (request.get("ui_hints") or {}).get("prior_search") and authoring and not previous else None,
    )
    # Existing model claims can be a review/edit target, but are not evidence of
    # new technical facts. The packet builder passes claim_file separately.
    engine = rt.engine(provider)
    resume = previous and previous.request_mode.value == route.mode and authoring
    applied = "new"
    if resume:
        kind, scope = revision_path(route, request, blocks, paths, previous, baseline[1] if baseline else None)
        applied = kind + (f"/{scope}" if scope else "")
        if kind == "new":
            # A new run takes the project USER_LOCK as it is now (the user may have just fixed the set kept there);
            # a resumed run keeps the one its records were built on.
            state = engine.run(engine.start(req.model_copy(update={"user_lock": current_lock}), folder.name))
        elif kind == "restart":
            state = engine.resume(previous.run_id, Decision(text=text, restart_from="ARCHITECT", request_update=req, feedback_text=request["text"]))
        elif kind == "redesign":
            # Same set and targets: a new dependent_design_revision of the edit, never the independent architect.
            state = engine.resume(previous.run_id, Decision(text=text, action="redesign", scope=scope, feedback_text=request["text"]))
        else:
            # Same materials, same USER_LOCK: only the wording changes, so the cheaper revision path applies.
            state = engine.resume(previous.run_id, Decision(text=text, action="style_fix" if kind == "style" else "meaning_fix", scope=scope,
                                                            feedback_text=request["text"]))
    else:
        state = engine.run(engine.start(req, folder.name))
    state.notes.extend(request.get("scope_notes") or [])
    state.notes.append("자동 요청 분류: " + route.mode + " — " + route.reason + (f" (revision 경로: {applied})" if resume else ""))
    if turn:
        from .store.telemetry import add_usage
        add_usage(state.turn, {k: v for k, v in turn.items() if k != "started_at"})
        state.turn["started_at"] = min(state.turn.get("started_at", turn["started_at"]), turn["started_at"])
    from .store.report import render_chat_report, render_report
    rt.store.save_state(state)
    rt.store.write_report(state.run_id, render_report(state))
    answer = render_chat_report(state)
    rt.store.write_chat_report(state.run_id, answer)
    (folder / "pipeline-request.json").write_text(req.model_dump_json(indent=2), encoding="utf-8")
    (folder / "route.json").write_text(json.dumps({
        **route.model_dump(), "run_id": state.run_id,
        "contract_sha256": hashlib.sha256((rt.cfg.project_root / "CLAUDE.md").read_bytes()).hexdigest(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(answer=answer, run_id=state.run_id, effective_mode=route.mode, revision_path=applied, styled_text=state.review_styled_text,
                status="review" if state.halt else "complete", outcome=state.outcome,
                route=route.model_dump(), finish_reason="PIPELINE_COMPLETED" if not state.halt else "PIPELINE_HALTED")
