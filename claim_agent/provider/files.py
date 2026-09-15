"""Gemini Files API registry: upload each drawing once, reference it by URI afterwards.

Uploads are keyed by the normalised image sha256 and remembered in a small registry so that
follow-up processes (web chat runs each request in a subprocess) reuse the same file within
its retention window. Any failure falls back to inline bytes; the caller never loses an image.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RETENTION_S = 47 * 3600     # Files API keeps uploads for 48 h; leave a margin


@dataclass
class UploadedFile:
    sha256: str
    name: str
    uri: str
    mime_type: str
    expires_at: float


class FileStore:
    def __init__(self, client: Any, registry_path: Path, enabled: bool = True):
        self.client = client
        self.registry_path = registry_path
        self.enabled = enabled and client is not None
        self._lock = threading.Lock()
        self._entries: dict[str, UploadedFile] = {}
        self._failed: set[str] = set()
        self.uploads = 0
        self.reuses = 0
        self._load()

    def _load(self) -> None:
        if not self.registry_path.exists():
            return
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        now = time.time()
        for k, v in (data.get("files") or {}).items():
            try:
                entry = UploadedFile(**v)
            except TypeError:
                continue
            if entry.expires_at > now:
                self._entries[k] = entry

    def _save(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"files": {k: v.__dict__ for k, v in self._entries.items()}}, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.registry_path.parent, delete=False) as fh:
            fh.write(payload)
            tmp = fh.name
        try:
            os.replace(tmp, self.registry_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, sha256: str, data: bytes, mime_type: str, label: str = "") -> UploadedFile | None:
        """Return the uploaded file for this content, uploading once; None means 'send inline'."""
        if not self.enabled or not sha256 or sha256 in self._failed:
            return None
        with self._lock:
            entry = self._entries.get(sha256)
            if entry and entry.expires_at > time.time() + 60:
                self.reuses += 1
                return entry
            try:
                uploaded = self.client.files.upload(file=io.BytesIO(data), config={"mime_type": mime_type, "display_name": (label or sha256[:12])[:120]})
                name = str(getattr(uploaded, "name", "") or "")
                uri = str(getattr(uploaded, "uri", "") or "")
                if not uri:
                    raise RuntimeError("upload returned no uri")
            except Exception:  # noqa: BLE001 - fall back to inline bytes for this content
                self._failed.add(sha256)
                return None
            entry = UploadedFile(sha256, name, uri, mime_type, time.time() + RETENTION_S)
            self._entries[sha256] = entry
            self.uploads += 1
            self._save()
            return entry

    def forget(self, sha256: str) -> None:
        with self._lock:
            self._entries.pop(sha256, None)
            self._save()
