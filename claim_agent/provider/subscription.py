"""Official CLI transports. OAuth credentials stay in CLI-owned, isolated stores.

No private inference endpoints, token extraction, shell interpolation or API-key fallback.
Every call uses a fresh working directory; only the supplied packet enters the role.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .base import CallResult, CallSpec, ImagePart, ProviderError, parse_json_text, request_summary, tool_definitions

KINDS = ("claude_oauth", "codex_oauth")


def auth_home(kind: str) -> Path:
    if kind not in KINDS:
        raise ValueError("지원하지 않는 구독 연결입니다.")
    # Outside the project, its source bundles, and git. CLI owns credentials/refresh.
    return Path.home() / ".claim-agent" / "auth" / kind


def cli_env(kind: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_", "CODEX_"))
           and k not in {"CLAUDECODE", "CLAUDE_CONFIG_DIR"}}
    home = auth_home(kind)
    home.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR" if kind == "claude_oauth" else "CODEX_HOME"] = str(home)
    env["PYTHONUTF8"] = "1"
    return env


def executable(kind: str, configured: str | None = None) -> list[str]:
    name = "claude" if kind == "claude_oauth" else "codex"
    found = shutil.which(configured or name)
    if not found and not configured:
        candidate = Path.home() / ".local" / "bin" / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            found = str(candidate)
    if not found and not configured and kind == "claude_oauth":
        resolved = npm_entry(Path.home() / ".claim-agent" / "tools" / "node_modules" / "@anthropic-ai" / "claude-code", name)
        if resolved:
            return resolved
    if not found:
        raise ValueError(f"{name} CLI를 설치한 뒤 다시 확인하세요. 설정 파일에서 executable 경로를 지정할 수도 있습니다.")
    # npm Windows shims must not go through cmd.exe (model names/prompts are untrusted).
    if Path(found).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        package = "@anthropic-ai/claude-code" if name == "claude" else "@openai/codex"
        parent = Path(found).parent
        for root in (parent / "node_modules", parent.parent if parent.name == ".bin" else parent / "node_modules"):
            resolved = npm_entry(root / package, name)
            if resolved:
                return resolved
        raise ValueError(f"{name} 네이티브 CLI 또는 표준 npm 설치가 필요합니다.")
    return [found]


def npm_entry(package: Path, name: str) -> list[str] | None:
    native = package / "bin" / (name + (".exe" if os.name == "nt" else ""))
    if native.is_file():
        return [str(native)]
    script = package / ("cli.js" if name == "claude" else "bin/codex.js")
    node = shutil.which("node")
    if script.is_file() and node:
        return [node, str(script)]
    return None


def run_cli(command: list[str], kind: str, *, cwd: Path, stdin: str = "", timeout: int = 20):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", cwd=cwd, env=cli_env(kind), **options)
        try:
            stdout, stderr = process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            from ..processes import terminate_tree

            terminate_tree(process)
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        raise ProviderError("CLI 응답 시간이 초과되었습니다. 로그인 상태와 사용량을 확인하세요.") from None
    except OSError:
        raise ProviderError("CLI를 시작하지 못했습니다. 설치와 실행 경로를 확인하세요.") from None


def connection_status(kind: str, configured: str | None = None) -> dict:
    try:
        cmd = executable(kind, configured)
    except ValueError as exc:
        return {"installed": False, "connected": False, "message": str(exc)}
    args = ["auth", "status", "--json"] if kind == "claude_oauth" else ["login", "status"]
    try:
        result = run_cli(cmd + args, kind, cwd=auth_home(kind))
        if kind == "claude_oauth":
            data = json.loads(result.stdout or "{}")
            connected = result.returncode == 0 and data.get("loggedIn") is True and data.get("authMethod") == "claude.ai"
        else:
            connected = result.returncode == 0 and "chatgpt" in (result.stdout + result.stderr).lower()
        return {"installed": True, "connected": connected, "message": "구독 로그인 연결됨" if connected else "구독 계정 로그인이 필요합니다."}
    except (ValueError, ProviderError):
        return {"installed": True, "connected": False, "message": "연결 상태 확인 실패. CLI 업데이트 또는 재로그인이 필요합니다."}


def login(kind: str, configured: str | None = None) -> dict:
    cmd = executable(kind, configured)
    args = ["auth", "login", "--claudeai"] if kind == "claude_oauth" else ["login"]
    result = run_cli(cmd + args, kind, cwd=auth_home(kind), timeout=180)
    if result.returncode:
        raise ValueError("로그인이 완료되지 않았습니다. 브라우저 승인을 마친 뒤 다시 연결하세요.")
    return connection_status(kind, configured)


class SubscriptionProvider:
    def __init__(self, kind: str, config, events=None):
        self.name, self.config, self.events = kind, config, events

    def list_models(self):
        raise ProviderError("구독 CLI는 API 모델 목록을 제공하지 않습니다. 모델 설정에서 계정 기본 모델 또는 계정에서 사용 가능한 모델 ID를 선택하세요.")

    def _emit(self, kind, spec, call_id, **data):
        if self.events:
            self.events.emit(kind, call_id=call_id, role=spec.role, scope=spec.scope, model=spec.model,
                             provider=self.name, run_id=spec.run_id, stage=spec.stage, phase=spec.phase, **data)

    def generate(self, spec: CallSpec) -> CallResult:
        blind = spec.role == "blind-claim-reconstruction-reviewer"
        if blind and (spec.sources_block or spec.images or spec.history or spec.tools or spec.cache_packet_text):
            raise ProviderError("blind call must contain only its sealed instructions and packet")
        status = connection_status(self.name, self.config.executable)
        if not status["connected"]:
            raise ProviderError(status["message"])
        call_id, started = uuid.uuid4().hex, time.monotonic()
        self._emit("request", spec, call_id, **request_summary(spec))
        images = []
        history = []
        for turn in spec.history:
            parts = []
            for part in turn.get("parts", []):
                if "text" in part:
                    parts.append(part["text"])
                elif "inline_data" in part:
                    img = part["inline_data"]
                    images.append(ImagePart(img["mime_type"], base64.b64decode(img["data"]), f"history image {len(images)+1}"))
                    parts.append(f"[attached history image {len(images)}]")
            history.append({"role": turn["role"], "text": "\n".join(parts)})
        images.extend(spec.images)
        packet = ("HISTORY (context only):\n" + json.dumps(history, ensure_ascii=False) + "\n\n" if history else "")
        packet += spec.sources_block + "\n\n" + spec.packet_text
        if spec.json_schema:
            packet += "\n\nReturn one JSON object matching this schema, without markdown:\n" + json.dumps(spec.json_schema, ensure_ascii=False)
        functions = {fn.__name__: fn for fn in (spec.tools or [])}
        if functions:
            packet += ('\n\nTo use a permitted tool, return ONLY {"claim_agent_tool": {"name": "...", "arguments": {...}}}. '
                       'Otherwise return your final answer. No other tools are available. Definitions:\n'
                       + json.dumps(tool_definitions(spec.tools), ensure_ascii=False))
        calls, usage = [], {}
        try:
            for round_no in range(5):
                text, current_usage = self._once(spec, packet, images)
                for key, value in current_usage.items():
                    usage[key] = usage.get(key, 0) + value
                parsed = parse_json_text(text)
                request = parsed.get("claim_agent_tool") if parsed and functions else None
                if request is None:
                    break
                if not isinstance(request, dict) or request.get("name") not in functions or not isinstance(request.get("arguments"), dict):
                    raise ProviderError("허용되지 않은 도구 호출입니다.")
                if round_no == 4:
                    raise ProviderError("도구 호출 횟수를 초과했습니다.")
                name, args = request["name"], request["arguments"]
                result = functions[name](**args)
                calls.extend([{"name": name, "args": args}, {"response_of": name, "response": result}])
                packet += "\n\nTool request:\n" + text + "\nTool result:\n" + json.dumps(result, ensure_ascii=False)
            if not text.strip():
                raise ProviderError("CLI가 빈 응답을 반환했습니다.")
        except Exception as exc:
            self._emit("error", spec, call_id, text=str(exc))
            raise
        # Subscription usage is not a USD API bill, even if the same model is in pricing.
        usage["subscription_calls"] = 1
        self._emit("delta", spec, call_id, text=text)
        self._emit("response_end", spec, call_id, finish_reason="STOP", function_calls=calls)
        return CallResult(text=text, parsed=parsed if spec.json_schema else None, usage=usage, latency_ms=int((time.monotonic()-started)*1000),
                          function_calls=calls, finish_reason="STOP", model=spec.model, provider=self.name,
                          raw={"transport": "official_cli", "billing": "subscription", "fresh_context": True})

    def _once(self, spec: CallSpec, packet: str, images: list[ImagePart]) -> tuple[str, dict]:
        command = executable(self.name, self.config.executable)
        with tempfile.TemporaryDirectory(prefix="claim-agent-role-", ignore_cleanup_errors=True) as temp:
            folder = Path(temp)
            system = folder / "system.md"
            system.write_text(spec.system_instruction, encoding="utf-8")
            if self.name == "claude_oauth":
                settings = folder / "settings.json"
                settings.write_text(json.dumps({"disableAllHooks": True, "autoMemoryEnabled": False}), encoding="utf-8")
                command += ["-p", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json", "--system-prompt-file", str(system),
                            "--tools", "", "--disallowedTools", "mcp__*", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                            "--setting-sources", "", "--settings", str(settings), "--disable-slash-commands", "--no-session-persistence",
                            "--effort", {"MINIMAL": "low"}.get(spec.gen.thinking_level, spec.gen.thinking_level.lower())]
                if spec.model != "default":
                    command += ["--model", spec.model]
                content = [{"type": "text", "text": packet}]
                for img in images:
                    content.extend([{"type": "text", "text": img.label}, {"type": "image", "source": {
                        "type": "base64", "media_type": img.mime_type, "data": base64.b64encode(img.data).decode("ascii")}}])
                stdin = json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False) + "\n"
            else:
                output = folder / "answer.txt"
                command += ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                            "--json", "--color", "never", "--output-last-message", str(output)]
                # The dedicated auth store has no host plugins, MCP servers or instructions.
                for key, value in {"model_instructions_file": str(system), "project_doc_max_bytes": 0, "web_search": "disabled",
                                   "approval_policy": "never", "model_reasoning_effort": "low" if spec.gen.thinking_level == "MINIMAL" else spec.gen.thinking_level.lower()}.items():
                    command += ["-c", key + "=" + json.dumps(value)]
                for feature in ("shell_tool", "unified_exec", "apply_patch_freeform", "apps", "plugins", "multi_agent", "memories",
                                "hooks", "browser_use", "computer_use", "image_generation", "code_mode", "code_mode_host", "view_image",
                                "skill_search", "workspace_dependencies", "goals", "sleep_tool", "tool_suggest"):
                    command += ["-c", f"features.{feature}=false"]
                if spec.model != "default":
                    command += ["--model", spec.model]
                for number, img in enumerate(images):
                    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(img.mime_type)
                    if not suffix:
                        raise ProviderError("지원하지 않는 이미지 형식입니다.")
                    path = folder / f"image-{number}{suffix}"
                    path.write_bytes(img.data)
                    command += ["--image", str(path)]
                    packet += f"\nAttached image {number + 1}: {img.label}"
                command += ["-"]
                stdin = packet
            result = run_cli(command, self.name, cwd=folder, stdin=stdin, timeout=self.config.timeout_s)
            if result.returncode:
                # Raw CLI errors may contain OAuth callback URLs or credentials; never persist them.
                raise ProviderError(f"{self.name} 실행 실패 (exit {result.returncode}). 로그인·모델 접근 권한·사용량·CLI 버전을 확인하세요.")
            if self.name == "claude_oauth":
                events = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
                data = next((event for event in reversed(events) if event.get("type") == "result"), None)
                if data is None:
                    raise ProviderError("Claude CLI 최종 응답이 없습니다.")
                if data.get("is_error") or data.get("subtype", "success") != "success":
                    raise ProviderError("Claude CLI 응답이 완료되지 않았습니다. 계정 사용량과 모델을 확인하세요.")
                u = data.get("usage", {})
                cached = u.get("cache_read_input_tokens", 0)
                inp = u.get("input_tokens", 0) + cached + u.get("cache_creation_input_tokens", 0)
                text = json.dumps(data["structured_output"], ensure_ascii=False) if "structured_output" in data else data.get("result", "")
            else:
                events = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
                if any(e.get("type") in {"error", "turn.failed"} for e in events):
                    raise ProviderError("Codex CLI 응답이 완료되지 않았습니다. 계정 사용량과 모델을 확인하세요.")
                u = next((e.get("usage", {}) for e in reversed(events) if e.get("type") == "turn.completed"), {})
                inp, cached = u.get("input_tokens", 0), u.get("cached_input_tokens", 0)
                if not output.is_file():
                    raise ProviderError("Codex CLI 최종 응답 파일이 없습니다.")
                text = output.read_text(encoding="utf-8")
            out = u.get("output_tokens", 0)
            return text, {"prompt_tokens": inp, "cached_tokens": cached, "output_tokens": out, "thoughts_tokens": 0, "total_tokens": inp + out}
