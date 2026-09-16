"""Readable live log built from provider events (web and TUI).

Events carry sizes, file headers and the current request instead of full packets (`provider.base.request_summary`);
the complete call input and output live in the run's `calls/` and `records/` files. A structured (JSON) response is
buffered and shown as its verdict lines when the call ends, because the escaped one-line JSON is unreadable and its
`report_markdown` is already in the report fold.
"""
from __future__ import annotations

import json
from typing import Any

from .provider.base import clip, parse_json_text

RAW_FALLBACK_MAX = 6000     # an unparseable JSON response is shown raw, up to this
CLAIM_TEXT_MAX = 4000
LEGACY_TEXT_MAX = 600       # events from writers that still send a `text` field with the request
OTHER_EVENT_MAX = 1000


def _value(value: Any) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def summarize_envelope(text: str, run_id: str | None = None, record_id: str | None = None) -> str:
    data = parse_json_text(text)
    if data is None:
        return "(JSON 파싱 실패 — 원문)\n" + clip(text, RAW_FALLBACK_MAX) + "\n"
    if not any(key in data for key in ("status", "gates", "exact_claim_text", "claims")):
        return clip(json.dumps(data, ensure_ascii=False), OTHER_EVENT_MAX) + "\n"
    lines = [" · ".join(f"{key}: {_value(data[key])}" for key in ("status", "next_step", "handoff_ready") if key in data)]
    gates = data.get("gates")
    if isinstance(gates, dict) and any(v not in (None, "") for v in gates.values()):
        lines.append("gates: " + ", ".join(f"{k}={v}" for k, v in gates.items() if v not in (None, "")))
    claims = [c for c in data.get("claims") or [] if isinstance(c, dict) and c.get("claim_no") is not None]
    if claims:
        lines.append("claims: " + ", ".join(
            f"제{c['claim_no']}항" + (" (" + ", ".join(x for x in (f"부모 제{c['parent_claim_no']}항" if c.get("parent_claim_no") else "", c.get("dc_id") or "") if x) + ")"
                                    if c.get("parent_claim_no") or c.get("dc_id") else "") for c in claims))
    candidates = [c for c in data.get("candidates") or [] if isinstance(c, dict)]
    if candidates:
        lines.append("candidates: " + ", ".join(
            f"{c.get('dc_id')} {c.get('classification')}" + (f" → 제{c['planned_claim_no']}항" if c.get("planned_claim_no") else "")
            + (f"(부모 제{c['parent_claim_no']}항)" if c.get("parent_claim_no") else "") for c in candidates))
    issues = [o for o in data.get("open_issues") or [] if isinstance(o, dict)]
    for issue in issues[:3]:
        lines.append(f"- {issue.get('kind', '')} {issue.get('code', '')}: {clip(str(issue.get('text', '')), 200)}")
    if len(issues) > 3:
        lines.append(f"- …외 {len(issues) - 3}건")
    if data.get("exact_claim_text"):
        lines.append("exact_claim_text:\n" + clip(str(data["exact_claim_text"]), CLAIM_TEXT_MAX))
    if run_id and record_id:
        lines.append(f"보고서 전문: {run_id}/records/{record_id}.md")
    return "\n".join(line for line in lines if line) + "\n"


class LiveLogFormatter:
    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}
        self.last_call: str | None = None

    @staticmethod
    def label(event: dict) -> str:
        role = event.get("role", "에이전트")
        return role + (f" · 항 {event['target']}" if event.get("target") else "")

    def feed(self, event: dict) -> str:
        kind = event.get("kind")
        if kind == "request":
            return self._request(event)
        if kind == "delta":
            return self._delta(event)
        if kind == "response_end":
            return self._response_end(event)
        if kind == "error":
            self.calls.pop(event.get("call_id"), None)      # a failed attempt's partial output is not shown as a result
            return f"\n[{self.label(event)} 오류] {clip(str(event.get('text', '')), 2000)}\n"
        return f"\n[{self.label(event)} · {kind}] " + clip(json.dumps(event, ensure_ascii=False), OTHER_EVENT_MAX) + "\n"

    def flush(self) -> str:
        """Structured responses whose call never ended (the process stopped mid-stream), shown raw."""
        out = "".join(f"\n── {call['label']} · 응답 (완료되지 않음) ──\n" + clip("".join(call["buffer"]), RAW_FALLBACK_MAX) + "\n"
                      for call in self.calls.values() if call["json"] and call["buffer"])
        self.calls.clear()
        return out

    def _request(self, event: dict) -> str:
        label = self.label(event)
        title = " · ".join(x for x in (label, event.get("scope"), event.get("stage")) if x)
        if event.get("phase") not in (None, "", "main", "chat"):
            title += f" · {event['phase']}"
        if (event.get("attempt") or 1) > 1:
            title += f" · 재시도 {event['attempt']}"
        lines = [f"\n── {title} · 요청 ──"]
        if "packet_chars" in event:
            if event.get("system_chars"):
                lines.append(f"system: 역할 지침 {event['system_chars']:,}자")
            sources = event.get("sources") or []
            if sources:
                lines.append(f"sources: 캐시 재사용 (미전송, {len(sources)}개 파일)" if not event.get("sources_sent", True) else "sources: " + ", ".join(sources))
            materials = list(event.get("materials") or [])
            if event.get("images"):
                materials.append(f"이미지 {len(event['images'])}장")
            if materials:
                lines.append("materials: " + ", ".join(materials))
            lines.append(f"packet: {event['packet_chars']:,}자" + (f" — 전문: {event['call_file']}" if event.get("call_file") else ""))
            if event.get("request"):
                lines += ["── 현재 요청 ──", event["request"]]
        else:
            if event.get("text"):
                lines.append(clip(str(event["text"]), LEGACY_TEXT_MAX))
            if event.get("images"):
                lines.append("이미지: " + ", ".join(map(str, event["images"])))
        structured = bool(event.get("json"))
        self.calls[event.get("call_id")] = dict(json=structured, buffer=[], label=label, run_id=event.get("run_id"), record_id=event.get("record_id"))
        self.last_call = event.get("call_id")
        lines.append(f"── {label} · 응답 대기 (JSON — 완료 시 요약) ──" if structured else f"── {label} · 응답 ──")
        return "\n".join(lines) + "\n"

    def _delta(self, event: dict) -> str:
        call_id = event.get("call_id")
        call = self.calls.get(call_id)
        text = event.get("text", "")
        if call and call["json"]:
            call["buffer"].append(text)
            return ""
        header = ""
        if call_id != self.last_call:
            header = f"\n── {self.label(event)} · 응답 ──\n"
            self.last_call = call_id
        return header + text

    def _response_end(self, event: dict) -> str:
        call = self.calls.pop(event.get("call_id"), None)
        self.last_call = None
        out = "\n"
        if call and call["json"]:
            out = f"\n── {call['label']} · 응답 ──\n" + summarize_envelope("".join(call["buffer"]), call["run_id"], call["record_id"])
        tools = sum(1 for c in event.get("function_calls") or [] if isinstance(c, dict) and "name" in c)
        finish = event.get("finish_reason") or ""
        return out + f"[응답 완료{' · ' + finish if finish else ''}{f' · 도구 호출 {tools}회' if tools else ''}]\n"
