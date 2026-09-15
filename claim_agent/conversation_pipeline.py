"""Intake and dispatch from a conversation into the existing gated engine."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from .claim_scope import constrain_target
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

    def add_text(text, origin, category="invention", path=None, is_request=False):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = (origin, category, digest)
        if not text.strip() or key in seen:
            return
        seen.add(key)
        bid = f"input-{len(blocks) + 1}"
        p = Path(path) if path else directory / f"{bid}-{digest[:12]}.txt"
        if not path:
            p.write_text(text, encoding="utf-8")
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
    categories = {a["path"]: a.get("category", "invention") for a in request.get("attachments", [])}
    for name in request.get("files", []):
        add_file(name, categories.get(name, "invention"))
    add_text(request["text"], "user", is_request=True)
    paths["drawings"] = list(dict.fromkeys(paths["drawings"]))
    return blocks, paths


def revision_path(route: RouteDecision, request: dict, blocks: list[dict], paths: dict, previous) -> tuple[str, str | None]:
    """Deterministic guard for follow-up edits: (kind, scope).

    kind ∈ restart | style | meaning. The router may only narrow the path: any new material (files, drawings,
    prior art, spec, pasted description) or a changed USER_LOCK forces a full restart from the architect, because
    the contract ties design_revision to the raw-material set. scope is DEPENDENT when the request names only
    dependent claims and the previous run has a live dependent set.
    """
    from .claim_scope import explicit_mentions

    if previous is None or route.revision_kind in ("NONE", "DESIGN"):
        return "restart", None
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
    if new_material or request.get("files"):
        return "restart", None
    kind = "style" if route.revision_kind == "STYLE_ONLY" else "meaning"
    scope = None
    mentioned = explicit_mentions(request["text"])
    if mentioned and previous.dependent and previous.dependent.current and not previous.dependent.stale:
        scope = "DEPENDENT"
    return kind, scope


def previous_state(cfg, request):
    run_id = request.get("run_id")
    if not run_id:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("올바르지 않은 이전 작업 식별자입니다.")
    from .models.state import RunState
    return RunState.model_validate_json((cfg.path("runs_dir") / run_id / "state.json").read_text(encoding="utf-8"))


def run_pipeline(rt, provider, request: dict, route: RouteDecision, blocks: list[dict],
                 paths: dict, folder: Path, previous=None):
    if route.mode in {"AUTHORING_DRAFT", "FINALIZATION"}:
        route.dependent, route.dependent_target = constrain_target(request["text"], route.dependent, route.dependent_target)
    source = next((b for b in blocks if b["id"] == route.claim_source_id), None)
    if source is None and previous and route.mode in {"AUTHORING_DRAFT", "FINALIZATION"}:
        source = next((b for b in blocks if b["category"] == "existing_claims"), None)
    req = RunRequest(
        request_mode=route.mode, request_text=request["text"],
        candidate_id=previous.candidate.candidate_id if previous else "cand-01",
        user_lock=previous.request.get("user_lock") if previous else None,
        dependent=route.dependent if route.mode != "REVIEW_ONLY" else False,
        dependent_target=route.dependent_target if route.mode != "REVIEW_ONLY" else None,
        dependent_set_id=previous.request.get("dependent_set_id") if previous else None,
        reviewers=list(dict.fromkeys(route.reviewers)), review_scope=route.review_scope,
        claim_file=source["path"] if source else None, **paths,
    )
    # Existing model claims can be a review/edit target, but are not evidence of
    # new technical facts. The packet builder passes claim_file separately.
    engine = rt.engine(provider)
    resume = previous and previous.request_mode.value == route.mode and route.mode in {"AUTHORING_DRAFT", "FINALIZATION"}
    applied = "new"
    if resume:
        kind, scope = revision_path(route, request, blocks, paths, previous)
        applied = kind + (f"/{scope}" if scope else "")
        if kind == "restart":
            state = engine.resume(previous.run_id, Decision(text=request["text"], restart_from="ARCHITECT", request_update=req))
        else:
            # Same materials, same USER_LOCK: only the wording changes, so the cheaper revision path applies.
            state = engine.resume(previous.run_id, Decision(text=request["text"], action="style_fix" if kind == "style" else "meaning_fix", scope=scope))
    else:
        state = engine.run(engine.start(req, folder.name))
    state.notes.append("자동 요청 분류: " + route.mode + " — " + route.reason + (f" (revision 경로: {applied})" if resume else ""))
    from .store.report import render_report
    rt.store.save_state(state)
    answer = render_report(state)
    rt.store.write_report(state.run_id, answer)
    (folder / "pipeline-request.json").write_text(req.model_dump_json(indent=2), encoding="utf-8")
    (folder / "route.json").write_text(json.dumps({
        **route.model_dump(), "run_id": state.run_id,
        "contract_sha256": hashlib.sha256((rt.cfg.project_root / "CLAUDE.md").read_bytes()).hexdigest(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(answer=answer, run_id=state.run_id, effective_mode=route.mode, revision_path=applied,
                status="review" if state.halt else "complete", outcome=state.outcome,
                route=route.model_dump(), finish_reason="PIPELINE_COMPLETED" if not state.halt else "PIPELINE_HALTED")
