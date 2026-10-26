"""Jev 호출(jev-cli 없이 runner 바꿔 끼우기): 묶음 확률, --provider openrouter 고정, 한도 초과 미실행, 실패 비예외, status()."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claim_agent.prior_art import jev

SECRET = "sk-or-v1-test-secret-value"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAIM_AGENT_JEV_COMMAND", "CLAIM_AGENT_JEV_MAX_STATE_CHARS", "OPENROUTER_API_KEY", "JEV_PROVIDER"):
        monkeypatch.delenv(name, raising=False)


def _provider(args: list[str]) -> str | None:
    return args[args.index("--provider") + 1] if "--provider" in args else None


def test_batch_probabilities_and_fixed_provider(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER", "official")   # 읽지 않는다
    seen = {}

    def runner(args, timeout):
        seen["args"], seen["timeout"] = args, timeout
        seen["path"] = Path(args[1])
        seen["payload"] = json.loads(seen["path"].read_text(encoding="utf-8"))
        return {"answers": {"c0": {"noul": 0.82}, "c1": {"noul": "0.1"}}, "usage": {"cost": 0.0021}}

    state = {"invention": {"elements": ["냉각판"]}, "candidates": {"c0": "A", "c1": "B"}}
    out = jev.JevClient(runner).noul_batch(state, {"c0": "c0이 개시하는가", "c1": "c1이 개시하는가"})
    assert out["status"] == "RUN" and out["probabilities"] == {"c0": 0.82, "c1": 0.1} and out["error"] is None
    assert out["question_count"] == 2 and out["state_chars"] == len(json.dumps(state, ensure_ascii=False))
    assert isinstance(out["latency_ms"], int) and out["cost_usd"] == 0.0021
    assert seen["args"][0] == "run" and _provider(seen["args"]) == "openrouter" and seen["args"].count("--provider") == 1
    assert seen["payload"] == {"state": state, "questions": {"c0": {"type": "noul", "instructions": "c0이 개시하는가"},
                                                             "c1": {"type": "noul", "instructions": "c1이 개시하는가"}}}
    assert not seen["path"].exists()                 # 임시 파일은 지운다


def test_single_noul_args():
    seen = {}

    def runner(args, timeout):
        seen["args"] = args
        assert json.loads(Path(args[args.index("-s") + 1][1:]).read_text(encoding="utf-8")) == {"x": "가"}
        return {"answers": {"answer": {"noul": 0.3}}}

    out = jev.JevClient(runner).noul({"x": "가"}, "질문?")
    assert out["status"] == "RUN" and out["probability"] == 0.3 and out["question_count"] == 1 and out["cost_usd"] is None
    assert seen["args"][:3] == ["noul", "-q", "질문?"] and "--json-state" in seen["args"] and _provider(seen["args"]) == "openrouter"


def test_state_over_limit_is_not_run(monkeypatch):
    monkeypatch.setenv("CLAIM_AGENT_JEV_MAX_STATE_CHARS", "10")
    called = []
    client = jev.JevClient(lambda args, timeout: called.append(args) or {})
    big = {"text": "x" * 50}
    batch = client.noul_batch(big, {"q": "?"})
    single = client.noul(big, "?")
    assert batch["status"] == single["status"] == "NOT_RUN" and not called
    assert "자르지 않고" in batch["error"] and batch["probabilities"] == {} and single["probability"] is None
    assert jev.max_state_chars() == 10


def test_failures_return_failed_without_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)

    def boom(args, timeout):
        raise RuntimeError(f'{{"ok": false, "error": "API HTTP 401 for {SECRET}"}}')

    out = jev.JevClient(boom).noul_batch({"a": 1}, {"q": "?"})
    assert out["status"] == "FAILED" and "API HTTP 401" in out["error"] and SECRET not in out["error"]
    missing = jev.JevClient(lambda args, timeout: {"answers": {"other": {"noul": 0.5}}}).noul_batch({"a": 1}, {"q": "?"})
    assert missing["status"] == "FAILED" and "KeyError" in missing["error"]
    bad = jev.JevClient(lambda args, timeout: {"answers": {"answer": {"noul": 1.7}}}).noul({"a": 1}, "?")
    assert bad["status"] == "FAILED" and bad["probability"] is None


def test_status_needs_command_and_openrouter_key(monkeypatch):
    monkeypatch.setenv("CLAIM_AGENT_JEV_COMMAND", "definitely-missing-jev-command-xyz")
    ok, msg = jev.status()
    assert not ok and "jev-cli" in msg
    monkeypatch.setenv("CLAIM_AGENT_JEV_COMMAND", sys.executable)   # 있는 실행 파일로 명령 확인만 통과시킨다
    ok, msg = jev.status()
    assert not ok and "OPENROUTER_API_KEY" in msg
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    ok, msg = jev.status()
    assert ok and SECRET not in msg


def test_command_line(monkeypatch):
    monkeypatch.setenv("CLAIM_AGENT_JEV_COMMAND", "C:/tools/jev.exe")
    assert jev.command_line() == ["C:/tools/jev.exe"]
    monkeypatch.delenv("CLAIM_AGENT_JEV_COMMAND")
    monkeypatch.setattr(jev.importlib.util, "find_spec", lambda name: object() if name == "jev_cli" else None)
    line = jev.command_line()
    assert line[:2] == [sys.executable, "-c"] and "from jev_cli import main" in line[2] and "sys.exit(main())" in line[2]
    assert ("truststore.inject_into_ssl()" in line[2]) == (sys.platform == "win32")
