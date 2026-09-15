"""Append-only, local request/response events for the TUI (never thought parts)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_WRITE_LOCK = threading.Lock()


def visible_text(response: Any) -> str:
    candidates = getattr(response, "candidates", None)
    if candidates:
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None)
        if parts is not None:
            return "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
    return getattr(response, "text", None) or ""


class EventWriter:
    def __init__(self, path: Path, secrets: list[str] | None = None):
        self.path = path
        self.secrets = [s for s in (secrets or []) if s]
        path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, api_key_env: str = "GEMINI_API_KEY") -> EventWriter | None:
        path = os.environ.get("CLAIM_AGENT_EVENT_LOG") or os.environ.get("CLAIM_COPA_EVENT_LOG")
        if not path:
            return None
        return cls(Path(path), [os.environ.get(n, "") for n in {api_key_env, "GEMINI_API_KEY", "GOOGLE_API_KEY"}])

    def emit(self, kind: str, **data: Any) -> None:
        line = json.dumps({"kind": kind, "time": time.time(), **data}, ensure_ascii=False)
        for secret in self.secrets:
            line = line.replace(secret, "[API KEY]")
        with _WRITE_LOCK, self.path.open("ab") as stream:
            stream.write((line + "\n").encode("utf-8"))


class EventReader:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.pending = b""

    def read(self) -> list[dict]:
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                data = stream.read()
                self.offset = stream.tell()
        except FileNotFoundError:
            return []
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()
        return [json.loads(line.decode("utf-8")) for line in lines if line]
