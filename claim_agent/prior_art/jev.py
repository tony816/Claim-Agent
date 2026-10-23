"""TypeSafe Jev 호출(cs2-spec-writer 6556039 pipeline/jev.py 이식). jev-cli를 subprocess로 실행한다.

공급자는 OpenRouter로 고정한다(`--provider openrouter`를 항상 붙이고 JEV_PROVIDER는 읽지 않는다). 키는 jev-cli가
OPENROUTER_API_KEY에서 직접 읽으므로 이 모듈은 키를 인자·기록·오류 문구에 넣지 않는다. 실패는 예외로 올리지 않고
status FAILED로 돌려 엔진이 대체 경로를 고르게 한다. state가 한도를 넘으면 자르지 않고 NOT_RUN으로 둔다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

PROVIDER = "openrouter"
KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_COMMAND = "jev"
MAX_STATE_CHARS = 200_000   # 기본값. 로컬 안전장치일 뿐 Jev 서비스의 확인된 한도가 아니다.
BATCH_TIMEOUT, SINGLE_TIMEOUT = 300, 180

Runner = Callable[[list[str], int], dict]


def command() -> str:
    return (os.getenv("CLAIM_AGENT_JEV_COMMAND") or "").strip() or DEFAULT_COMMAND


def max_state_chars() -> int:
    try:
        return int(os.getenv("CLAIM_AGENT_JEV_MAX_STATE_CHARS") or MAX_STATE_CHARS)
    except ValueError:
        return MAX_STATE_CHARS


def _bundled() -> bool:
    return command() == DEFAULT_COMMAND and importlib.util.find_spec("jev_cli") is not None


def command_line() -> list[str]:
    """설치된 jev_cli는 이 파이썬으로 돌린다(Windows는 urllib 연결 전에 OS 인증서 저장소를 쓰게 한다).
    CLAIM_AGENT_JEV_COMMAND를 따로 주면 그 명령을 그대로 쓴다."""
    if _bundled():
        boot = "import truststore; truststore.inject_into_ssl(); " if sys.platform == "win32" else ""
        return [sys.executable, "-c", boot + "import sys; from jev_cli import main; sys.exit(main())"]
    return [command()]


def _scrub(text: str) -> str:
    key = (os.getenv(KEY_ENV) or "").strip()
    return text.replace(key, "[redacted]") if len(key) >= 4 else text


def _run(args: list[str], timeout: int) -> dict:
    proc = subprocess.run([*command_line(), *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}")
    return json.loads(proc.stdout)


def _probability(value) -> float:
    p = float(value)
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"확률 범위 밖: {p}")
    return p


def _cost(raw: dict) -> float | None:
    """응답에 비용이 있으면(OpenRouter usage.cost 등) USD로, 없으면 None."""
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    for value in (usage.get("cost"), raw.get("cost")):
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None


def _state_chars(state: dict) -> int:
    return len(json.dumps(state, ensure_ascii=False))


def _over_limit(n: int) -> str | None:
    limit = max_state_chars()
    return f"state {n}자 > 로컬 한도 {limit}자. 자르지 않고 호출하지 않음" if n > limit else None


class JevClient:
    """runner(args, timeout) → 응답 JSON. 기본은 jev-cli subprocess. 시험·픽스처는 runner를 바꿔 끼운다."""

    def __init__(self, runner: Runner | None = None):
        self.runner = runner or _run

    def _invoke(self, build: Callable[[str], list[str]], payload: dict, timeout: int) -> tuple[dict, int]:
        """payload를 임시 JSON 파일로 쓰고 build(그 경로)로 만든 인자에 공급자를 붙여 runner를 부른다."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            path = f.name
        start = time.monotonic()
        try:
            raw = self.runner([*build(path), "--provider", PROVIDER], timeout)
        finally:
            Path(path).unlink(missing_ok=True)
        return raw, int((time.monotonic() - start) * 1000)

    def noul_batch(self, state: dict, questions: dict[str, str]) -> dict:
        """같은 state에 noul 질문 여러 개를 한 요청(jev run)으로 묻는다.
        반환: {status: RUN|NOT_RUN|FAILED, probabilities(질문 키별), question_count, state_chars, latency_ms, cost_usd, error}."""
        chars = _state_chars(state)
        record = {"question_count": len(questions), "state_chars": chars, "probabilities": {}, "latency_ms": 0, "cost_usd": None}
        if over := _over_limit(chars):
            return {**record, "status": "NOT_RUN", "error": over}
        if not questions:
            return {**record, "status": "NOT_RUN", "error": "질문이 없음"}
        payload = {"state": state, "questions": {k: {"type": "noul", "instructions": q} for k, q in questions.items()}}
        start = time.monotonic()
        try:
            raw, latency = self._invoke(lambda path: ["run", path], payload, BATCH_TIMEOUT)
            probs = {k: _probability(raw["answers"][k]["noul"]) for k in questions}
        except Exception as e:  # noqa: BLE001 - 실패는 기록하고 엔진이 질문별 호출·대체 경로로 넘어간다
            return {**record, "status": "FAILED", "latency_ms": int((time.monotonic() - start) * 1000), "error": _scrub(f"{type(e).__name__}: {e}")}
        return {**record, "status": "RUN", "probabilities": probs, "latency_ms": latency, "cost_usd": _cost(raw), "error": None}

    def noul(self, state: dict, question: str) -> dict:
        """질문 하나. 반환: {status, probability, question_count(1), state_chars, latency_ms, cost_usd, error}."""
        chars = _state_chars(state)
        record = {"question_count": 1, "state_chars": chars, "probability": None, "latency_ms": 0, "cost_usd": None}
        if over := _over_limit(chars):
            return {**record, "status": "NOT_RUN", "error": over}
        start = time.monotonic()
        try:
            raw, latency = self._invoke(lambda path: ["noul", "-q", question, "-s", "@" + path, "--json-state"], state, SINGLE_TIMEOUT)
            p = _probability(raw["answers"]["answer"]["noul"])
        except Exception as e:  # noqa: BLE001 - 실패는 기록하고 엔진이 대체 경로로 넘어간다
            return {**record, "status": "FAILED", "latency_ms": int((time.monotonic() - start) * 1000), "error": _scrub(f"{type(e).__name__}: {e}")}
        return {**record, "status": "RUN", "probability": p, "latency_ms": latency, "cost_usd": _cost(raw), "error": None}


def status() -> tuple[bool, str]:
    """jev-cli(명령 또는 jev_cli 모듈)와 OPENROUTER_API_KEY가 모두 있을 때만 사용 가능. 키 값은 보여 주지 않는다."""
    cmd = command()
    found = _bundled() or shutil.which(cmd) is not None or Path(cmd).is_file()
    if not found:
        return False, f"jev-cli를 찾지 못했습니다('{cmd}' 명령 또는 jev_cli 모듈 없음 — pip install jev-cli==0.6.3, Python 3.13 이상)."
    if not (os.getenv(KEY_ENV) or "").strip():
        return False, f".env에 {KEY_ENV}가 없습니다(Jev는 OpenRouter로만 부릅니다)."
    return True, f"Jev 사용 가능(jev-cli · {PROVIDER} · {KEY_ENV} 설정됨)"
