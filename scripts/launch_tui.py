"""Double-click launcher: find project, install missing dependencies, open TUI."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    if sys.version_info < (3, 11):
        print("Python 3.11 이상을 설치해 주세요.")
        return 1
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    modules = ["textual", "dotenv", "pydantic", "yaml", "google.genai"]
    if os.name == "nt":
        modules.append("truststore")
    missing = []
    for module in modules:
        try:
            available = importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            available = False
        if not available:
            missing.append(module)
    if missing:
        print("처음 실행에 필요한 구성요소를 설치합니다. 잠시 기다려 주세요.", flush=True)
        code = subprocess.call([sys.executable, "-m", "pip", "install", "-e", str(root)])
        if code:
            return code
        # New site-package paths from a user install are picked up in a fresh process.
        return subprocess.call([sys.executable, "-m", "claim_agent.tui", "--project-root", str(root), *sys.argv[1:]])
    from claim_agent.tui import main as tui_main

    return tui_main(["--project-root", str(root), *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
