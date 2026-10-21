"""평가·시험용 오프라인 재생과 기록. 실제 WIPS·Jev 응답을 `<directory>/<도구>-<키>.json`으로 저장하고 그대로 다시 읽는다.

키는 요청 내용(도구 + 인자, 또는 Jev 종류 + state + 질문)의 sha256 앞 16자다. 응답에는 키·토큰·세션 id가 들어 있지 않다.
실패한 호출은 저장하지 않는다(재생 때 픽스처 없음 = 같은 실패로 처리된다).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .jev import JevClient
from .wips import WipsError


def fixture_key(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def wips_path(directory: Path, tool: str, args: dict) -> tuple[Path, str]:
    key = fixture_key({"tool": tool, "args": args})
    return Path(directory) / f"{tool}-{key}.json", key


class FixtureWips:
    """WipsClient.call과 같은 꼴. search·search_all·fetch에 그대로 넘긴다."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def call(self, tool: str, args: dict) -> dict:
        path, key = wips_path(self.directory, tool, args)
        if not path.is_file():
            raise WipsError(f"WIPS 픽스처 없음: {tool} {key}")
        return json.loads(path.read_text(encoding="utf-8"))


class RecordingWips:
    """실제 클라이언트로 부르고 성공한 응답을 FixtureWips가 읽을 자리에 저장한다."""

    def __init__(self, client, directory: Path):
        self.client, self.directory = client, Path(directory)

    def call(self, tool: str, args: dict) -> dict:
        data = self.client.call(tool, args)
        _write(wips_path(self.directory, tool, args)[0], data)
        return data


def jev_path(directory: Path, payload: dict) -> tuple[Path, str]:
    key = fixture_key(payload)
    return Path(directory) / f"{payload['kind']}-{key}.json", key


def _batch_payload(state: dict, questions: dict[str, str]) -> dict:
    return {"kind": "noul_batch", "state": state, "questions": questions}


def _single_payload(state: dict, question: str) -> dict:
    return {"kind": "noul", "state": state, "question": question}


class FixtureJev:
    """JevClient와 같은 꼴. 픽스처가 없으면 예외 대신 FAILED를 돌려 엔진이 대체 경로로 가게 한다."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _load(self, payload: dict, missing: dict) -> dict:
        path, key = jev_path(self.directory, payload)
        if not path.is_file():
            return {**missing, "status": "FAILED", "latency_ms": 0, "cost_usd": None, "error": f"Jev 픽스처 없음: {payload['kind']} {key}"}
        return json.loads(path.read_text(encoding="utf-8"))

    def noul_batch(self, state: dict, questions: dict[str, str]) -> dict:
        chars = len(json.dumps(state, ensure_ascii=False))
        return self._load(_batch_payload(state, questions), {"probabilities": {}, "question_count": len(questions), "state_chars": chars})

    def noul(self, state: dict, question: str) -> dict:
        chars = len(json.dumps(state, ensure_ascii=False))
        return self._load(_single_payload(state, question), {"probability": None, "question_count": 1, "state_chars": chars})


class RecordingJev:
    """실제 JevClient로 묻고 RUN·NOT_RUN 결과를 FixtureJev가 읽을 자리에 저장한다(FAILED는 저장하지 않는다)."""

    def __init__(self, client: JevClient, directory: Path):
        self.client, self.directory = client, Path(directory)

    def _keep(self, payload: dict, result: dict) -> dict:
        if result.get("status") != "FAILED":
            _write(jev_path(self.directory, payload)[0], result)
        return result

    def noul_batch(self, state: dict, questions: dict[str, str]) -> dict:
        return self._keep(_batch_payload(state, questions), self.client.noul_batch(state, questions))

    def noul(self, state: dict, question: str) -> dict:
        return self._keep(_single_payload(state, question), self.client.noul(state, question))
