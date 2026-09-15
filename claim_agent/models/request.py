"""Run request and raw-material bundle."""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..sources.extract import DOC_EXT, ExtractionError, extract_text
from ..sources.images import DEFAULT_MAX_SIDE, normalize_image
from .enums import RequestMode

IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
TEXT_EXT = {".md", ".txt", ".yaml", ".yml", ".json", ".csv"}


class RunRequest(BaseModel):
    request_mode: RequestMode = RequestMode.AUTHORING_DRAFT
    request_text: str
    candidate_id: str = "cand-01"
    user_lock: str | None = None
    invention_sources: list[str] = Field(default_factory=list)
    spec_path: str | None = None            # 정식 명세서 (FINALIZATION)
    drawings: list[str] = Field(default_factory=list)
    prior_art: list[str] = Field(default_factory=list)   # empty -> PRIOR_ART_SET: NONE
    dependent: bool = False
    dependent_target: str | None = None    # e.g. "2~10"
    dependent_set_id: str | None = None
    claim_file: str | None = None          # REVIEW_ONLY / FINALIZATION input claims
    reviewers: list[str] = Field(default_factory=list)   # REVIEW_ONLY
    review_scope: str = "INDEPENDENT"

    @classmethod
    def from_yaml(cls, path: Path) -> RunRequest:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        base = path.parent
        for key in ("invention_sources", "drawings", "prior_art"):
            data[key] = [str((base / p).resolve()) if not Path(p).is_absolute() else p for p in data.get(key, []) or []]
        for key in ("spec_path", "claim_file", "user_lock_file"):
            v = data.get(key)
            if v and not Path(v).is_absolute():
                data[key] = str((base / v).resolve())
        ulf = data.pop("user_lock_file", None)
        if ulf and not data.get("user_lock"):
            data["user_lock"] = Path(ulf).read_text(encoding="utf-8").strip()
        rf = data.pop("request_file", None)
        if rf and not data.get("request_text"):
            p = Path(rf) if Path(rf).is_absolute() else base / rf
            data["request_text"] = p.read_text(encoding="utf-8")
        return cls.model_validate(data)


@dataclass
class MaterialItem:
    path: str
    name: str
    kind: str                 # text | image
    sha256: str
    text: str | None = None
    mime_type: str | None = None
    data: bytes | None = None
    category: str = "invention"   # invention | spec | drawing | prior_art | claim_file
    normalized_sha256: str | None = None   # images: sha of the bytes actually sent (after resize)
    image_meta: dict | None = None         # images: original/normalized size, resized flag, note
    extraction: dict | None = None         # documents (pdf/docx/hwpx/hwp): format, pages, chars, note

    @property
    def content_sha256(self) -> str:
        return self.normalized_sha256 or self.sha256

    def as_meta(self) -> dict:
        meta = {"path": self.path, "name": self.name, "kind": self.kind, "sha256": self.sha256, "category": self.category}
        if self.normalized_sha256:
            meta["normalized_sha256"] = self.normalized_sha256
        if self.image_meta:
            meta["image_meta"] = self.image_meta
        if self.extraction:
            meta["extraction"] = self.extraction
        return meta


@dataclass
class MaterialBundle:
    items: list[MaterialItem] = field(default_factory=list)
    user_lock: str | None = None

    @classmethod
    def load(cls, req: RunRequest, max_image_side: int = DEFAULT_MAX_SIDE) -> MaterialBundle:
        items: list[MaterialItem] = []
        for cat, paths in (
            ("invention", req.invention_sources),
            ("drawing", req.drawings),
            ("prior_art", req.prior_art),
            ("spec", [req.spec_path] if req.spec_path else []),
            ("claim_file", [req.claim_file] if req.claim_file else []),
        ):
            for p in paths:
                items.extend(_load_path(Path(p), cat, max_image_side))
        return cls(items, req.user_lock)

    def by_category(self, *cats: str) -> list[MaterialItem]:
        return [i for i in self.items if i.category in cats]

    def digest(self) -> str:
        h = hashlib.sha256()
        for i in sorted(self.items, key=lambda x: (x.category, x.name)):
            h.update(f"{i.category}:{i.name}:{i.sha256}:{i.normalized_sha256 or ''}\n".encode())
        h.update(("USER_LOCK:" + (self.user_lock or "")).encode())
        return h.hexdigest()

    @property
    def spec_present(self) -> bool:
        return any(i.category == "spec" for i in self.items)

    @property
    def prior_art_present(self) -> bool:
        return any(i.category == "prior_art" for i in self.items)

    def claim_file_text(self) -> str | None:
        for i in self.items:
            if i.category == "claim_file" and i.text:
                return i.text
        return None


def _load_path(p: Path, category: str, max_image_side: int = DEFAULT_MAX_SIDE) -> list[MaterialItem]:
    if p.is_dir():
        out: list[MaterialItem] = []
        for child in sorted(p.iterdir()):
            if child.is_file():
                out.extend(_load_path(child, category, max_image_side))
        return out
    if not p.exists():
        raise FileNotFoundError(f"material not found: {p}")
    raw = p.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        norm = normalize_image(raw, IMAGE_EXT[ext], max_image_side)
        meta = {"original_size": norm.original_size, "size": norm.size, "resized": norm.resized, "bytes": len(norm.data), "original_bytes": len(raw), "note": norm.note}
        return [MaterialItem(str(p), p.name, "image", sha, None, norm.mime_type, norm.data, category, norm.sha256 if norm.resized else None, meta)]
    if ext in TEXT_EXT or ext == "":
        return [MaterialItem(str(p), p.name, "text", sha, raw.decode("utf-8", errors="replace"), None, None, category)]
    if ext in DOC_EXT:
        try:
            extracted = extract_text(p)
        except ExtractionError as exc:
            raise ValueError(f"{p.name}: {exc}") from exc
        return [MaterialItem(str(p), p.name, "text", sha, extracted.text, None, None, category, extraction=extracted.as_meta())]
    raise ValueError(f"unsupported material type {ext}: {p} (use md/txt/pdf/docx/hwpx/hwp or png/jpg/webp)")


def encode_image(item: MaterialItem) -> str:
    return base64.b64encode(item.data or b"").decode()
