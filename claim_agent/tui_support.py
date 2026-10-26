"""File intake and subprocess requests for the terminal UI (no shell commands)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .models.request import IMAGE_EXT, TEXT_EXT
from .sources.extract import DOC_EXT

CATEGORIES = {"invention": "발명 자료", "drawing": "도면", "prior_art": "선행기술", "spec": "정식 명세서"}


@dataclass(frozen=True)
class Attachment:
    path: Path
    category: str


def normalize_path(value: str, root: Path) -> Path:
    value = value.strip().strip('"').strip("'")
    if value.startswith("file://"):
        uri = urlsplit(value)
        value = unquote(uri.path)
        if uri.netloc:
            value = "//" + uri.netloc + value
        elif os.name == "nt" and re.match(r"^/[A-Za-z]:", value):
            value = value[1:]
    p = Path(value).expanduser()
    return (root / p).resolve() if not p.is_absolute() else p.resolve()


def parse_paths(text: str, root: Path) -> list[Path]:
    """Accept Explorer/terminal drops, quoted multi-path strings and file URIs."""
    result: list[Path] = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        whole = normalize_path(line, root)
        if whole.is_file():
            candidates = [whole]
        else:
            tokens = re.findall(r'"([^"]+)"|\x27([^\x27]+)\x27|(\S+)', line)
            candidates = [normalize_path(next(t for t in token if t), root) for token in tokens]
        for path in candidates:
            if path not in result:
                result.append(path)
    if not result:
        raise ValueError("파일 경로를 넣어 주세요.")
    return result


def validate_attachment(path: Path, category: str = "invention") -> Attachment:
    if not path.is_file():
        raise ValueError(f"파일을 찾을 수 없습니다: {path.name}")
    if path.name.lower() == ".env" or path.name.lower().startswith(".env."):
        raise ValueError("API 키가 담긴 .env 파일은 첨부할 수 없습니다.")
    if path.suffix.lower() not in set(TEXT_EXT) | set(IMAGE_EXT) | DOC_EXT:
        raise ValueError(f"지원하지 않는 형식: {path.name} — TXT, MD, PDF, DOCX, HWPX, HWP, PNG, JPG, WEBP를 사용하세요.")
    if category not in CATEGORIES:
        raise ValueError("첨부 자료의 종류를 선택해 주세요.")
    if category == "drawing" and path.suffix.lower() not in IMAGE_EXT:
        raise ValueError("도면에는 PNG, JPG, JPEG, WEBP 이미지를 넣어 주세요.")
    if category == "invention" and path.suffix.lower() in IMAGE_EXT:
        category = "drawing"
    return Attachment(path, category)


RESUME_KINDS = {
    "none": [],                                  # re-run the halted stage with the decision text
    "style": ["--apply-style-fix"],              # STYLE_ONLY_REVISION, r+1
    "meaning": ["--apply-meaning-fix"],          # drafter PRE_STYLE revision, r+1
    "redesign": ["--redesign"],                  # design_revision d+1, architect first
    "restart": ["--restart-from", "ARCHITECT"],  # full restart (materials changed)
    "accept_unverified": ["--accept-unverified"],
    "select_prior_art": ["--select-prior-art-from-decision"],   # decision text: skeys (comma/space) and an optional YYYY-MM-DD
}


def prior_art_decision(skeys: list[str], base_date: str | None = None) -> str:
    """The decision text of a select_prior_art resume; `none` picks no searched document."""
    return (", ".join(skeys) or "none") + (f" {base_date}" if base_date else "")


def parse_prior_art_decision(text: str) -> tuple[list[str], str | None]:
    """(skeys, base_date) from a select_prior_art decision text."""
    tokens = [t for t in re.split(r"[\s,]+", text.strip()) if t]
    dates = [t for t in tokens if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t)]
    return [t for t in tokens if t not in dates and t.lower() != "none"], (dates[-1] if dates else None)


def claim_proposal_route(kind: str, text: str, scope: str | None, dependent_live: bool) -> tuple[str, str | None]:
    """A "style" decision that pastes claim wording is a claim proposal, so it goes to the design step instead.

    The style adjuster may only change surface wording of the sealed claim; a new claim text or a changed citation is
    outside its authority and always came back RETURN_TO_*_ARCHITECT, spending one full call first. Pasted dependent
    claims on a run with a live dependent set start a new dependent_design_revision; anything else redesigns normally.
    """
    from .pipeline.claimtext import claim_wording

    wording = claim_wording(text) if kind == "style" else None
    if not wording:
        return kind, scope
    if scope is None and wording == "DEPENDENT" and dependent_live:
        scope = "DEPENDENT"
    return "redesign", scope


def resume_command(root: Path, run_id: str, decision_path: Path, kind: str, model: str, add_sources: list[str] | None = None, scope: str | None = None) -> list[str]:
    if kind not in RESUME_KINDS:
        raise ValueError("지원하지 않는 재개 종류입니다.")
    command = [sys.executable, "-u", "-m", "claim_agent.cli", "--project-root", str(root), "resume", run_id, "--decision-file", str(decision_path), *RESUME_KINDS[kind], "--model", model]
    for src in add_sources or []:
        command += ["--add-source", src]
    if scope:
        command += ["--scope", scope]
    return command


ACCEPT_UNVERIFIED = re.compile(r"미검증\s*(?:으로|을|를)?\s*(?:수용|진행|받아들)")


def chat_resume_kind(text: str, halt: dict | None, new_material: bool = False) -> str | None:
    """How a chat message continues a halted run; None when the run is not waiting for a decision.

    A halted run waits for the user's decision, so the next chat message is that decision on the same run: the
    prior-art pick when the halt asks for documents, the non-gating UNVERIFIED acceptance when the message says so,
    a restart from the architect when it brings new material, and otherwise a re-run of the halted stage.
    """
    if not halt:
        return None
    if halt.get("reason_code") == "PRIOR_ART_SELECTION":
        return "select_prior_art"
    if ACCEPT_UNVERIFIED.search(text):
        return "accept_unverified"
    return "restart" if new_material else "none"


def chat_resume_command(root: Path, folder: Path, state: dict | None, text: str, material: str,
                        attachments: list[Attachment], model: str) -> list[str] | None:
    """The resume command for a chat message on a halted run (same run, same USER_LOCK); None when not halted."""
    import hashlib

    if not (state or {}).get("halt"):
        return None
    checked = [validate_attachment(a.path) for a in attachments]
    known = {str(Path(item["path"]).resolve()) for item in state.get("material_meta", [])}
    add = [str(item.path) for item in checked if str(item.path.resolve()) not in known]
    folder.mkdir(parents=True, exist_ok=False)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    if material.strip() and digest not in {item.get("sha256") for item in state.get("material_meta", [])}:
        source = folder / "추가설명.txt"
        source.write_text(material, encoding="utf-8")
        add.append(str(source))
    kind = chat_resume_kind(text, state["halt"], bool(add))
    decision = folder / "decision.txt"
    decision.write_text(text, encoding="utf-8")
    return resume_command(root, state["run_id"], decision, kind, model, add if kind == "restart" else None)


def child_options(root: Path) -> dict:
    return dict(cwd=root, env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


async def native_input(root: Path, action: str) -> dict:
    options = child_options(root)
    options.pop("encoding")
    options.pop("errors")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "claim_agent.tui_native", action, str(root),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 300 if action == "pick" else 20)
        if process.returncode:
            raise ValueError("파일 선택/클립보드를 열지 못했습니다. 경로를 직접 붙여넣어 주세요.")
        return json.loads(stdout.decode("utf-8-sig"))
    finally:
        # Closing the TUI or replacing a picker must not leave a native dialog alive.
        if process.returncode is None:
            process.kill()
            await process.wait()


def read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # The pipeline may be in the middle of writing a checkpoint.
        return None
