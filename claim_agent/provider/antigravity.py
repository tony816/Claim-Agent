"""Antigravity (`agy`) official CLI transport on the account login `agy` stores for itself.

Same packet and tool loop as SubscriptionProvider. Each call runs in plan mode in a fresh folder. Role packets carry
whole source files, far beyond the Windows command-line limit, and `agy` takes the prompt only as an argument, so the
packet and drawings are written to files that `agy` reads with view_file. That needs one allow rule in agy's own
settings.json (`read_file(<WORKSPACE>)`); without it only short packets can run, and never with drawings.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from .base import CallSpec, ImagePart, ProviderError
from .subscription import SubscriptionProvider, run_cli

WORKSPACE = Path.home() / ".claim-agent" / "agy_ws"
SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
WIN_CMDLINE_LIMIT = 30000          # CreateProcess allows 32767 characters
SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def executable(configured: str | None = None) -> str:
    found = shutil.which(configured or "agy")
    if not found and not configured:
        candidates = [Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe",
                      Path.home() / ".local" / "bin" / ("agy.exe" if os.name == "nt" else "agy")]
        found = next((str(p) for p in candidates if p.is_file()), None)
    if not found:
        raise ValueError("agy CLI를 설치한 뒤 다시 확인하세요. 설정 파일에서 executable 경로를 지정할 수도 있습니다.")
    return found


def agy_env() -> dict[str, str]:
    # Account login only: an API key in the environment must not switch agy to key billing.
    env = {k: v for k, v in os.environ.items() if k not in {"GEMINI_API_KEY", "GOOGLE_API_KEY"}}
    env["PYTHONUTF8"] = "1"
    return env


def settings() -> dict:
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ProviderError("Antigravity settings.json을 읽을 수 없습니다.") from None
    return data if isinstance(data, dict) else {}


def file_mode() -> bool:
    try:
        allow = settings().get("permissions", {}).get("allow", [])
    except ProviderError:
        return False
    return f"read_file({WORKSPACE.resolve()})" in allow


def allow_rule() -> str:
    return f"read_file({WORKSPACE.resolve()})"


def connection_status(configured: str | None = None) -> dict:
    try:
        exe = executable(configured)
    except ValueError as exc:
        return {"installed": False, "connected": False, "message": str(exc)}
    try:
        if settings().get("modelProvider"):
            return {"installed": True, "connected": False, "message": "구독 계정 인증이 필요합니다. agy settings.json의 modelProvider를 지우고 agy로 로그인하세요."}
        # `agy models` answers at once when signed out, unlike print mode, which waits for a browser login.
        result = run_cli([exe, "models"], "antigravity", cwd=Path.home(), timeout=60, env=agy_env())
    except ProviderError:
        return {"installed": True, "connected": False, "message": "연결 상태 확인 실패. agy 업데이트 또는 재로그인이 필요합니다."}
    if result.returncode or "sign in" in (result.stdout + result.stderr).lower():
        return {"installed": True, "connected": False, "message": "터미널에서 agy를 한 번 실행해 구독 계정으로 로그인하세요."}
    note = "" if file_mode() else f" · 긴 요청·도면용 읽기 허용 규칙 없음({allow_rule()})"
    return {"installed": True, "connected": True, "message": "구독 로그인 연결됨" + note}


class AntigravityProvider(SubscriptionProvider):
    def __init__(self, config, events=None):
        super().__init__("antigravity", config, events)

    def status(self) -> dict:
        return connection_status(self.config.executable)

    def _once(self, spec: CallSpec, packet: str, images: list[ImagePart]) -> tuple[str, dict]:
        exe = executable(self.config.executable)
        files_ok = file_mode()
        if images and not files_ok:
            raise ProviderError(f"Antigravity 도면 입력에는 agy settings.json의 읽기 허용 규칙이 필요합니다: {allow_rule()}")
        task = f"<지침>\n{spec.system_instruction}\n</지침>\n\n{packet}"
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        folder = WORKSPACE / uuid.uuid4().hex[:12]
        folder.mkdir()
        try:
            structured = bool(spec.json_schema) and not spec.tools
            command = [exe, "--output-format", "json" if structured else "text", "--model", spec.model, "--mode", "plan",
                       "--print-timeout", f"{self.config.timeout_s}s",
                       "--effort", {"MINIMAL": "low"}.get(spec.gen.thinking_level, spec.gen.thinking_level.lower())]
            if structured:
                schema = folder / "schema.json"
                schema.write_text(json.dumps(spec.json_schema, ensure_ascii=False), encoding="utf-8")
                command += ["--json-schema", str(schema)]
            if files_ok:
                paths = [folder / "task.md"]
                paths[0].write_text(task, encoding="utf-8")
                for number, img in enumerate(images):
                    suffix = SUFFIX.get(img.mime_type)
                    if not suffix:
                        raise ProviderError("지원하지 않는 이미지 형식입니다.")
                    path = folder / f"image-{number + 1}{suffix}"
                    path.write_bytes(img.data)
                    paths.append(path)
                prompt = ("view_file 도구로 다음 파일을 모두 읽은 뒤 task.md의 지침과 요청을 따르라: "
                          + ", ".join(str(p.resolve()) for p in paths) + ". view_file 외의 도구는 쓰지 말고 지정된 형식으로만 답한다.")
            else:
                prompt = task + "\n\n(도구를 쓰지 말고 지정된 형식으로만 답한다.)"
                if len(prompt) > WIN_CMDLINE_LIMIT:
                    raise ProviderError(f"요청 {len(prompt):,}자가 명령줄 한도 {WIN_CMDLINE_LIMIT:,}자를 넘습니다. "
                                        f"agy settings.json의 permissions.allow에 {allow_rule()}를 추가하세요.")
            # The prompt never starts with '/', so slash commands stay on (turning them off drops plan mode).
            command.append("-p=" + prompt)
            result = run_cli(command, self.name, cwd=folder, timeout=self.config.timeout_s + 30, env=agy_env())
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if result.returncode:
            # agy output may carry an OAuth sign-in URL; never persist it.
            raise ProviderError(f"antigravity 실행 실패 (exit {result.returncode}). 로그인·모델 접근 권한·사용량·CLI 버전을 확인하세요.")
        if not structured:
            return result.stdout, {"prompt_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "thoughts_tokens": 0, "total_tokens": 0}
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise ProviderError("Antigravity 출력 형식을 해석하지 못했습니다.") from None
        if data.get("status") != "SUCCESS" or data.get("structured_output") is None:
            raise ProviderError("Antigravity 응답이 완료되지 않았습니다. 계정 사용량과 모델을 확인하세요.")
        u = data.get("usage") or {}
        inp, out = u.get("input_tokens") or 0, u.get("output_tokens") or 0
        return json.dumps(data["structured_output"], ensure_ascii=False), {
            "prompt_tokens": inp, "cached_tokens": u.get("cache_read_tokens") or 0, "output_tokens": out, "thoughts_tokens": 0, "total_tokens": inp + out}
