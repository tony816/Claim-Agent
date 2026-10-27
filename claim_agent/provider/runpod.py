"""RunPod Serverless vLLM transport (API key from .env), for a model served on the user's own endpoint.

The worker-vllm image takes an OpenAI chat-completions body wrapped as `openai_input`. Jobs go through the async
`/run` + `/status` routes rather than the sync `/openai` proxy: a cold start (worker pull + model load) takes minutes
and the sync route is cut at Cloudflare's 100 s edge timeout. The packet, permitted-tool loop and blind-call guard are
SubscriptionProvider's; only `_once` differs. The key is read from the environment on each call and never logged.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time

import httpx

from .base import CallSpec, ImagePart, ProviderError
from .openrouter import tls_verify
from .subscription import SubscriptionProvider

API = "https://api.runpod.ai/v2"
POLL_S = 2.0
DONE = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def api_key(config) -> str | None:
    return os.environ.get(config.api_key_env) or None


def connection_status(config) -> dict:
    if not config.endpoint_id:
        return {"installed": True, "connected": False, "message": "claim-agent.yaml의 provider.runpod.endpoint_id 설정 필요"}
    present = bool(api_key(config))
    return {"installed": True, "connected": present, "message": "API 키 설정됨" if present else f".env에 {config.api_key_env} 설정 필요"}


def _output(job: dict) -> dict:
    """worker-vllm returns the OpenAI response as the job output, wrapped in a list of yielded batches."""
    out = job.get("output")
    if isinstance(out, list):
        out = out[0] if out else {}
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            out = {}
    return out if isinstance(out, dict) else {}


class RunPodProvider(SubscriptionProvider):
    transport, billing = "runpod_serverless", "api"

    def __init__(self, config, events=None):
        super().__init__("runpod", config, events)

    def status(self) -> dict:
        return connection_status(self.config)

    def _request(self, method: str, path: str, key: str, body: dict | None = None) -> dict:
        try:
            response = httpx.request(method, f"{API}/{self.config.endpoint_id}/{path}", json=body, timeout=60, verify=tls_verify(),
                                     headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            raise ProviderError(f"RunPod 연결 실패: {type(exc).__name__}") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200:
            message = data.get("error") if isinstance(data, dict) else None
            raise ProviderError(f"RunPod {response.status_code}: {str(message or response.reason_phrase)[:300]}")
        return data if isinstance(data, dict) else {}

    def _once(self, spec: CallSpec, packet: str, images: list[ImagePart]) -> tuple[str, dict]:
        key = api_key(self.config)
        if not key:
            raise ProviderError(f".env에 {self.config.api_key_env}를 설정하세요.")
        content = [{"type": "text", "text": packet}]
        for img in images:
            content.extend([{"type": "text", "text": img.label}, {"type": "image_url", "image_url": {
                "url": f"data:{img.mime_type};base64,{base64.b64encode(img.data).decode('ascii')}"}}])
        body = {
            "model": spec.model,
            "messages": [{"role": "system", "content": spec.system_instruction}, {"role": "user", "content": content}],
            "temperature": spec.gen.temperature,
            "max_tokens": self.config.max_output_tokens,
            # The fine-tune answers directly; Qwen3.5's template would otherwise open a <think> block.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if spec.json_schema:
            body["response_format"] = {"type": "json_object"}
        job = self._request("POST", "run", key, {"input": {"openai_route": "/v1/chat/completions", "openai_input": body}})
        job_id, deadline = job.get("id"), time.monotonic() + self.config.timeout_s
        if not job_id:
            raise ProviderError("RunPod가 작업 ID를 돌려주지 않았습니다.")
        while job.get("status") not in DONE:
            if time.monotonic() > deadline:
                try:
                    self._request("POST", f"cancel/{job_id}", key)
                except ProviderError:
                    pass
                raise ProviderError(f"RunPod 작업이 {self.config.timeout_s}초 안에 끝나지 않았습니다(상태 {job.get('status')}).")
            time.sleep(POLL_S)
            job = self._request("GET", f"status/{job_id}", key)
        if job["status"] != "COMPLETED":
            raise ProviderError(f"RunPod 작업 {job['status']}: {str(job.get('error') or '')[:300]}")
        data = _output(job)
        if data.get("error") or not data.get("choices"):
            raise ProviderError(f"RunPod vLLM 오류: {str(data.get('error') or data or job.get('output'))[:300]}")
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ProviderError("RunPod 출력이 최대 길이에서 잘렸습니다.")
        text = THINK.sub("", (choice.get("message") or {}).get("content") or "")
        u = data.get("usage") or {}
        inp, out = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
        usage = {"prompt_tokens": inp, "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
                 "output_tokens": out, "thoughts_tokens": 0, "total_tokens": inp + out}
        return text, usage
