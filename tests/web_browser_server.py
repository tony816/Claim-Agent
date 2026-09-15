"""Disposable offline server for web_browser.cjs; never calls Gemini."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claim_agent.web import Server, Workspace

os.environ["GEMINI_API_KEY"] = "offline-browser-test"
os.environ.pop("GOOGLE_API_KEY", None)
original = subprocess.Popen
program = r'''
import json,os,sys,time
from pathlib import Path
p=Path(sys.argv[1]); r=json.loads(p.read_text(encoding="utf-8"))
event=Path(os.environ["CLAIM_AGENT_EVENT_LOG"])
def emit(kind,text):
    with event.open("a",encoding="utf-8") as f:f.write(json.dumps(dict(kind=kind,role="대화",text=text),ensure_ascii=False)+"\n")
emit("request",r["text"])
emit("delta","한글을 정상적으로 입력할 수 있습니다.")
time.sleep(2)
answer="한글을 정상적으로 입력할 수 있습니다.\n\n## 함께 수정하기\n- 이전 대화를 기억합니다.\n- **첨부 자료**도 함께 확인합니다."
history=r["history"]+[dict(role="user",parts=[dict(text=r["text"])]),dict(role="model",parts=[dict(text=answer)])]
(p.parent/"response.json").write_text(json.dumps(dict(answer=answer,history=history),ensure_ascii=False),encoding="utf-8")
'''


def fake(command, **kwargs):
    assert "claim_agent.chat" in command, "Browser fixture must never execute a live pipeline"
    return original([sys.executable, "-c", program, command[command.index("--request") + 1]], **kwargs)


subprocess.Popen = fake
with tempfile.TemporaryDirectory(prefix="claim-web-browser-") as temporary:
    workspace = Workspace(Path(temporary))
    server = Server(workspace)
    metadata = Path(sys.argv[1])
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps(dict(origin=server.origin, token=server.token)), encoding="utf-8")
    print("Offline browser test server ready", flush=True)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        workspace.close()
        server.server_close()
