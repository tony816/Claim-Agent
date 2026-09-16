"""Live log: request events carry headers only and structured responses are summarized (no network)."""
from __future__ import annotations

import json

from claim_agent.live_events import EventReader, EventWriter
from claim_agent.live_log import LiveLogFormatter
from claim_agent.pipeline.claimtext import claim_wording
from claim_agent.provider.cache import CacheManager
from claim_agent.provider.gemini import GeminiProvider
from claim_agent.roles.prompt import render_file_block
from claim_agent.tui_support import claim_proposal_route
from tests.test_gemini_provider import _client, _spec
from tests.test_live_chat import chunk

SOURCES = ("## 사전 로딩 소스\n\n" + render_file_block("sources/README.md", "b15e79fa" + "0" * 56, "긴 소스 문서 " * 5000)
           + render_file_block("sources/07_용어표현_출처게이트.md", "f7e76b73" + "1" * 56, "| 서로 대체 가능한 실시형태 또는 공정 경로 |\n" * 2000))
PACKET = ("### USER_LOCK 원문\n\n없음\n\n### 현재 발명 원자료 (전문)\n\n"
          "<<<MATERIAL [invention] project-instructions (sha256=1937ca27abcd)>>>\n" + "【청구항 1】 원자료 청구항 " * 3000 + "\n<<<END MATERIAL>>>\n\n"
          "<<<MATERIAL [drawing] 2.5.png (sha256=28868a2b9b04)>>> (이미지 파트로 첨부)\n\n"
          "## RUN_HEADER\nrun_id: run-x\n\n### 현재 요청 (종속항 세트)\n\n## 프로젝트 지침\n\n" + "지침 " * 100 + "\n\n## 현재 요청\n\n제9항만 정리해줘\n\n"
          "### 변경 금지 부모항 체인 전문 (BASELINE_SET)\n\n" + "부모항 체인 " * 2000 + "\n\n### 출력 계약\n\nJSON")
CLAIM_9 = "【청구항 9】\n제8항에 있어서,\n상기 캡 지그는 인입된 튜브를 걸림 해제 가능하게 수용하는 해제 홈을 포함하는 것을 특징으로 하는 시약 튜브 취급 장치."
ENVELOPE = {
    "status": "PASS", "next_step": "PROCEED", "handoff_ready": True,
    "gates": {"DRAFTER_GATE": "PASS", "PRE_STYLE_GEOMETRIC_OBJECT_CHECK": "PASS", "OA_DRAFT_GATE": None},
    "claims": [{"claim_no": 9, "parent_claim_no": 8, "dc_id": "DC-01", "text": CLAIM_9}], "exact_claim_text": CLAIM_9,
    "report_markdown": "## 보고서\n\n| 표 | \"인용\" |\n" * 200,
}


def test_request_event_and_log_show_headers_and_verdict_not_full_texts(tmp_path):
    client = _client()
    client.models.generate_content_stream = lambda **kwargs: iter([chunk(json.dumps(ENVELOPE, ensure_ascii=False)[:500]), chunk(json.dumps(ENVELOPE, ensure_ascii=False)[500:], "STOP")])
    path = tmp_path / "events.jsonl"
    provider = GeminiProvider(client, CacheManager(client, tmp_path / "reg.json", "3600s", True), events=EventWriter(path))
    spec = _spec(role="claim-drafter", scope="DEPENDENT_SET", sources_block=SOURCES, packet_text=PACKET, run_id="run-x", seq=7, stage="DEP_DRAFT", meta={"record_id": "dmd-01"})
    assert provider.generate(spec).cache_hit
    raw = path.read_text(encoding="utf-8")
    assert len(raw) < 20000 and "긴 소스 문서" not in raw and "부모항 체인 부모항" not in raw and "원자료 청구항" not in raw
    events = EventReader(path).read()
    request = next(e for e in events if e["kind"] == "request")
    assert request["sources_sent"] is False                                  # the cache served them: nothing was sent
    assert request["sources"] == ["sources/README.md (b15e79fa)", "sources/07_용어표현_출처게이트.md (f7e76b73)"]
    assert request["materials"] == ["project-instructions(1937ca27)"] and request["request"] == "제9항만 정리해줘"
    assert request["call_file"] == "run-x/calls/007-claim-drafter-DEPENDENT_SET.json" and request["stage"] == "DEP_DRAFT"

    formatter = LiveLogFormatter()
    log = "".join(formatter.feed(e) for e in events)
    assert log.lstrip().startswith("── claim-drafter · DEPENDENT_SET · DEP_DRAFT · 요청 ──")
    assert "sources: 캐시 재사용 (미전송, 2개 파일)" in log and "packet: " in log and "run-x/calls/007-claim-drafter-DEPENDENT_SET.json" in log
    assert "status: PASS · next_step: PROCEED · handoff_ready: true" in log
    assert "gates: DRAFTER_GATE=PASS, PRE_STYLE_GEOMETRIC_OBJECT_CHECK=PASS" in log and "OA_DRAFT_GATE" not in log
    assert "claims: 제9항 (부모 제8항, DC-01)" in log and CLAIM_9 in log and "보고서 전문: run-x/records/dmd-01.md" in log
    assert "report_markdown" not in log and "\\n" not in log and '\\"' not in log and "['" not in log
    assert len(log) < 3000


def test_formatter_falls_back_for_broken_json_legacy_requests_and_failed_attempts():
    formatter = LiveLogFormatter()
    out = formatter.feed(dict(kind="request", call_id="a", role="oa-strategy-reviewer", json=True, packet_chars=10))
    out += formatter.feed(dict(kind="delta", call_id="a", role="oa-strategy-reviewer", text="partial"))
    out += formatter.feed(dict(kind="error", call_id="a", role="oa-strategy-reviewer", text="503 unavailable"))
    assert "partial" not in out and "503 unavailable" in out                  # a failed attempt's output is not a result
    out += formatter.feed(dict(kind="request", call_id="b", role="oa-strategy-reviewer", json=True, packet_chars=10, attempt=2))
    out += formatter.feed(dict(kind="delta", call_id="b", role="oa-strategy-reviewer", text="not json at all"))
    out += formatter.feed(dict(kind="response_end", call_id="b", role="oa-strategy-reviewer", finish_reason="STOP"))
    assert "재시도 2" in out and "(JSON 파싱 실패 — 원문)\nnot json at all" in out
    legacy = formatter.feed(dict(kind="request", role="대화", text="질문", images=["[drawing] a.png", "[drawing] b.png"]))
    assert "질문" in legacy and "이미지: [drawing] a.png, [drawing] b.png" in legacy and "['" not in legacy
    assert formatter.feed(dict(kind="delta", role="대화", text="답변")).endswith("답변")
    formatter.feed(dict(kind="request", call_id="c", role="claim-style-adjuster", json=True, packet_chars=1))
    formatter.feed(dict(kind="delta", call_id="c", role="claim-style-adjuster", text='{"status": "RE'))
    assert '{"status": "RE' in formatter.flush() and formatter.flush() == ""  # a stopped job still shows what arrived


def test_pasted_claim_wording_routes_a_style_decision_to_design():
    assert claim_wording(CLAIM_9) == "DEPENDENT"
    assert claim_wording("제8항에 있어서, 상기 캡 지그의 하단에는 인입된 튜브가 걸림 해제되는 해제 홈이 형성되는 것을 특징으로 하는 장치.") == "DEPENDENT"
    assert claim_wording("【청구항 1】\n기둥; 및 상기 기둥에 결합되는 판을 포함하는 장치.\n\n" + CLAIM_9) == "INDEPENDENT"
    assert claim_wording("제9항에 있어서 뒤 쉼표 빼줘") is None
    assert claim_wording("제9항에 있어서, 의 쉼표를 빼줘") is None
    assert claim_wording("3항의 조사만 고쳐줘") is None
    assert claim_proposal_route("style", "아래로 했는데 어떨지?\n\n" + CLAIM_9, None, True) == ("redesign", "DEPENDENT")
    assert claim_proposal_route("style", CLAIM_9, None, False) == ("redesign", None)
    assert claim_proposal_route("style", "조사만 고쳐줘", None, True) == ("style", None)
    assert claim_proposal_route("meaning", CLAIM_9, None, True) == ("meaning", None)
