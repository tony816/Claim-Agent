"""Double-click bootstrap; keep the local server out of the user's way."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def existing_server(metadata: Path, code_version: str | None = None) -> str | None:
    """URL of a live server started from this metadata file.

    With `code_version` given, a live server built from different code is asked to shut down (unless it is still
    answering a request) so the launcher starts a fresh one; otherwise a stale checkout would keep serving old
    screens after `git pull`.
    """
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
        origin = data["origin"]
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            return None
        headers = {"X-Claim-Token": data["token"], "X-Claim-Request": "1"}
        with urlopen(Request(origin + "/api/sessions", headers=headers), timeout=2) as response:
            if response.status != 200:
                return None
            info = json.loads(response.read().decode("utf-8"))
        if code_version and info.get("code_version") != code_version:
            if info.get("busy"):
                print("코드가 바뀌었지만 이전 서버가 아직 응답 중이라 그대로 엽니다. 응답이 끝나면 '프로그램 종료' 후 다시 실행해 주세요.", flush=True)
                return origin + "/?token=" + data["token"]
            print("코드가 바뀌어 이전 서버를 종료하고 다시 시작합니다.", flush=True)
            shutdown_server(origin, headers)
            return None
        return origin + "/?token=" + data["token"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def shutdown_server(origin: str, headers: dict) -> None:
    try:
        with urlopen(Request(origin + "/api/shutdown", data=b"{}", headers={**headers, "Content-Type": "application/json"}), timeout=3):
            pass
    except OSError:
        return
    for _ in range(40):
        try:
            with urlopen(Request(origin + "/api/sessions", headers=headers), timeout=1):
                pass
        except OSError:
            return
        time.sleep(0.25)


def current_code_version(root: Path) -> str | None:
    """Same fingerprint the server publishes; computed without importing the runtime's dependencies."""
    import hashlib

    base = root / "claim_agent"
    if not base.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".html", ".js", ".css"} and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(base)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    if sys.version_info < (3, 11):  # noqa: UP036 — user-facing guard for old interpreters
        print("Python 3.11 이상을 설치해 주세요.")
        return 1
    metadata = root / ".tui" / "web" / "server.json"
    url = existing_server(metadata, current_code_version(root))
    if url:
        webbrowser.open(url)
        return 0
    modules = ["dotenv", "pydantic", "yaml", "google.genai", "PIL", "pypdf", "docx", "olefile"] + (["truststore"] if os.name == "nt" else [])
    missing = False
    for module in modules:
        try:
            missing |= importlib.util.find_spec(module) is None
        except ModuleNotFoundError:
            missing = True
    if missing:
        print("처음 실행에 필요한 구성요소를 설치합니다.", flush=True)
        if subprocess.call([sys.executable, "-m", "pip", "install", "-e", str(root)]):
            return 1
    metadata.parent.mkdir(parents=True, exist_ok=True)
    with (metadata.parent / "server.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "claim_agent.web", "--project-root", str(root), "--no-browser"],
            cwd=root, env={**os.environ, "PYTHONUTF8": "1"}, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    for _ in range(100):
        if process.poll() is not None:
            break
        url = existing_server(metadata)
        if url:
            webbrowser.open(url)
            return 0
        time.sleep(0.15)
    print("웹 대화창을 시작하지 못했습니다. .tui/web/server.log를 확인해 주세요.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
