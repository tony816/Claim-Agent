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

from .models.request import IMAGE_EXT, TEXT_EXT, RunRequest
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


def prepare_request(
    root: Path, run_id: str, request: str, pasted_material: str,
    attachments: list[Attachment], dependent: bool, dependent_target: str,
    mode: str = "AUTHORING_DRAFT",
) -> Path:
    if not request.strip():
        raise ValueError("요청 내용을 입력해 주세요.")
    if mode not in {"AUTHORING_DRAFT", "FINALIZATION"}:
        raise ValueError("지원하지 않는 작성 모드입니다.")
    checked = [validate_attachment(a.path, a.category) for a in attachments]
    if not pasted_material.strip() and not any(a.category in {"invention", "drawing", "spec"} for a in checked):
        raise ValueError("발명 자료를 첨부하거나 '발명 설명 붙여넣기'에 원자료를 입력해 주세요.")
    specs = [str(a.path) for a in checked if a.category == "spec"]
    if len(specs) > 1:
        raise ValueError("정식 명세서는 한 파일만 선택하세요. 나머지는 발명 자료로 추가할 수 있습니다.")
    if mode == "FINALIZATION" and not specs:
        raise ValueError("출원용 최종 검증에는 정식 명세서 지정이 필요합니다. 통합 첨부는 '잠정안 작성'으로 실행하고, 명세서를 지정한 최종 검증은 CLI의 --spec 옵션을 사용하세요.")
    if dependent and not dependent_target.strip():
        raise ValueError("종속항 범위를 입력해 주세요. 예: 2~8")
    folder = root / ".tui" / "requests" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    sources = [str(a.path) for a in checked if a.category == "invention"]
    if pasted_material.strip():
        material = folder / "발명설명.txt"
        material.write_text(pasted_material, encoding="utf-8")
        sources.append(str(material))
    req = RunRequest(
        request_mode=mode, request_text=request.strip(), invention_sources=sources,
        drawings=[str(a.path) for a in checked if a.category == "drawing"],
        prior_art=[str(a.path) for a in checked if a.category == "prior_art"],
        spec_path=specs[0] if specs else None, dependent=dependent,
        dependent_target=dependent_target.strip() if dependent else None,
    )
    path = folder / "request.json"
    # JSON is valid YAML; the existing request loader handles these absolute paths.
    path.write_text(req.model_dump_json(indent=2), encoding="utf-8")
    return path


def run_command(root: Path, request_path: Path, run_id: str, model: str) -> list[str]:
    return [sys.executable, "-u", "-m", "claim_agent.cli", "--project-root", str(root),
            "run", "--request-yaml", str(request_path), "--run-id", run_id, "--model", model]


RESUME_KINDS = {
    "none": [],                                  # re-run the halted stage with the decision text
    "style": ["--apply-style-fix"],              # STYLE_ONLY_REVISION, r+1
    "meaning": ["--apply-meaning-fix"],          # drafter PRE_STYLE revision, r+1
    "redesign": ["--redesign"],                  # design_revision d+1, architect first
    "restart": ["--restart-from", "ARCHITECT"],  # full restart (materials changed)
    "accept_unverified": ["--accept-unverified"],
}


def resume_command(root: Path, run_id: str, decision_path: Path, kind: str, model: str, add_sources: list[str] | None = None, scope: str | None = None) -> list[str]:
    if kind not in RESUME_KINDS:
        raise ValueError("지원하지 않는 재개 종류입니다.")
    command = [sys.executable, "-u", "-m", "claim_agent.cli", "--project-root", str(root), "resume", run_id, "--decision-file", str(decision_path), *RESUME_KINDS[kind], "--model", model]
    for src in add_sources or []:
        command += ["--add-source", src]
    if scope:
        command += ["--scope", scope]
    return command


def feedback_command(root: Path, folder: Path, state: dict, feedback: str, material: str,
                     attachments: list[Attachment], model: str) -> list[str]:
    """Keep the existing run and USER_LOCK; invalidate dependent gates via redesign."""
    import hashlib

    if not feedback.strip():
        raise ValueError("수정할 내용을 프롬프트에 입력해 주세요.")
    checked = [validate_attachment(a.path) for a in attachments]
    folder.mkdir(parents=True, exist_ok=False)
    decision = folder / "feedback.txt"
    decision.write_text(feedback, encoding="utf-8")
    command = resume_command(root, state["run_id"], decision, "restart", model)
    known = {str(Path(item["path"]).resolve()) for item in state.get("material_meta", [])}
    for item in checked:
        if str(item.path.resolve()) not in known:
            command += ["--add-source", str(item.path)]
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    if material.strip() and digest not in {item.get("sha256") for item in state.get("material_meta", [])}:
        source = folder / "추가설명.txt"
        source.write_text(material, encoding="utf-8")
        command += ["--add-source", str(source)]
    return command


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
