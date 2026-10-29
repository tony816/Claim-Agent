"""Offline RunPod Serverless transport: async job shape, polling, secrets, routing and settings."""
import pytest

from claim_agent.config import AppConfig, RunPodConfig
from claim_agent.model_settings import PROVIDERS, connection, settings_snapshot
from claim_agent.provider import runpod as rp
from claim_agent.provider.base import CallSpec, ProviderError
from claim_agent.runtime import live_provider


def spec(**kwargs):
    return CallSpec(**{**dict(role="claim-drafter", scope="INDEPENDENT", model="claim-drafter",
                              system_instruction="역할 계약", packet_text="원자료 전문"), **kwargs})


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self.data, self.reason_phrase = status, data, "Bad"

    def json(self):
        return self.data


OPENAI = {"choices": [{"finish_reason": "stop", "message": {"content": '<think>\n\n</think>\n\n{"status":"REVIEW"}'}}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 4}}


@pytest.fixture
def runpod(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "rpa_SECRET")
    monkeypatch.setattr(rp, "POLL_S", 0)
    sent, statuses = [], iter([{"id": "job1", "status": "IN_QUEUE"}, {"id": "job1", "status": "COMPLETED", "output": [OPENAI]}])

    def request(method, url, json, timeout, headers, verify):
        sent.append((method, url, json, headers))
        return FakeResponse(200, {"id": "job1", "status": "IN_QUEUE"} if url.endswith("/run") else next(statuses))

    monkeypatch.setattr(rp.httpx, "request", request)
    return sent


def test_runpod_submits_an_async_openai_job_and_polls_it(runpod):
    result = rp.RunPodProvider(RunPodConfig(endpoint_id="ep123")).generate(spec(json_schema={"type": "object"}))
    method, url, body, headers = runpod[0]
    assert (method, url) == ("POST", "https://api.runpod.ai/v2/ep123/run") and headers["Authorization"] == "Bearer rpa_SECRET"
    job = body["input"]
    assert job["openai_route"] == "/v1/chat/completions" and job["openai_input"]["model"] == "claim-drafter"
    assert job["openai_input"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert [r[1] for r in runpod[1:]] == ["https://api.runpod.ai/v2/ep123/status/job1"] * 2
    assert result.parsed == {"status": "REVIEW"} and result.provider == "runpod" and result.usage["total_tokens"] == 14


def test_runpod_shrinks_drawings_until_the_run_body_fits(runpod, monkeypatch):
    import io

    from PIL import Image

    from claim_agent.provider.base import ImagePart

    buf = io.BytesIO()
    Image.effect_noise((3000, 3000), 100).convert("RGB").save(buf, "PNG")
    big = ImagePart("image/png", buf.getvalue(), "첨부 이미지", "sha")
    monkeypatch.setattr(rp, "MAX_BODY", 3 * len(big.data))
    rp.RunPodProvider(RunPodConfig(endpoint_id="ep123")).generate(spec(images=[big] * 10, json_schema={"type": "object"}))
    assert len(rp.json.dumps(runpod[0][2])) <= rp.MAX_BODY

    monkeypatch.setattr(rp, "MAX_BODY", 1000)
    with pytest.raises(ProviderError, match="10MB"):
        rp.RunPodProvider(RunPodConfig(endpoint_id="ep123")).generate(spec(images=[big] * 10))


def test_runpod_failed_job_and_missing_setup_never_carry_the_key(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "rpa_SECRET")
    monkeypatch.setattr(rp.httpx, "request", lambda *a, **k: FakeResponse(200, {"id": "j", "status": "FAILED", "error": "CUDA OOM"}))
    with pytest.raises(ProviderError) as err:
        rp.RunPodProvider(RunPodConfig(endpoint_id="ep123")).generate(spec())
    assert "CUDA OOM" in str(err.value) and "rpa_SECRET" not in str(err.value)
    assert "endpoint_id" in rp.connection_status(RunPodConfig())["message"]
    monkeypatch.delenv("RUNPOD_API_KEY")
    assert "RUNPOD_API_KEY" in rp.connection_status(RunPodConfig(endpoint_id="ep123"))["message"]


def test_runpod_routes_per_role_and_shows_in_settings():
    cfg = AppConfig.model_validate({"roles": {"claim-drafter": {"provider": "runpod"}}, "provider": {"runpod": {"endpoint_id": "ep123"}}})
    assert cfg.model_for("claim-drafter") == "claim-drafter"
    assert type(live_provider(cfg).factory("runpod")).__name__ == "RunPodProvider"
    assert "runpod" in PROVIDERS and "claim-drafter" in settings_snapshot(cfg)["providers"]["runpod"]["models"]
    assert connection(cfg, "runpod")["installed"]
