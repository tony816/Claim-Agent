"""Double-click bootstrap; keep the local server out of the user's way."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
import webbrowser


def existing_server(metadata: Path) -> str | None:
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
        origin = data["origin"]
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            return None
        request = Request(origin + "/api/sessions", headers={"X-Claim-Token": data["token"]})
        with urlopen(request, timeout=2) as response:
            if response.status == 200:
                return origin + "/?token=" + data["token"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    if sys.version_info < (3, 11):
        print("Python 3.11 이상을 설치해 주세요.")
        return 1
    metadata = root / ".tui" / "web" / "server.json"
    url = existing_server(metadata)
    if url:
        webbrowser.open(url)
        return 0
    modules = ["dotenv", "pydantic", "yaml", "google.genai"] + (["truststore"] if os.name == "nt" else [])
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
