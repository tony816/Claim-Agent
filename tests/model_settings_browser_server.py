"""Disposable model-settings UI fixture. No login or model API calls."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import claim_agent.model_settings as settings
from claim_agent.web import Server, Workspace

settings.connection = lambda *args: {"installed": True, "connected": False, "message": "테스트: 연결 대기"}
with tempfile.TemporaryDirectory(prefix="claim-model-ui-") as temporary:
    workspace = Workspace(Path(temporary))
    server = Server(workspace)
    metadata = Path(sys.argv[1])
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps({"origin": server.origin, "token": server.token}), encoding="utf-8")
    print("Offline model settings test server ready", flush=True)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        workspace.close()
        server.server_close()
