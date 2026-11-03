"""도구 추천 (자유 작업 모드): right after each free-mode answer, one small call reads it and recommends the work tools
(검수 역할·판례 도구) and feedback phrases (피드백 멘트) the user may want next.

The advice is the user's reference, not a gate, a source or a model input: the web keeps it on the answer's message, never
in the history the agents read, and a click on it only picks the tool in the panel (Enter still runs it). When the call
fails the code recommends from what it counts on the answer itself (`rules`), so every answer still has a recommendation
for the evaluation form beside it to measure (web.py `evaluate`, docs/도구추천_평가폼.md).

Run as `python -m claim_agent.tool_advice --request <folder>/advice-request.json`; the result goes to `<folder>/advice.json`.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from .provider.base import CallSpec, GenParams, LLMProvider, parse_json_text

ROLE = "tool-recommender"
ROLE_LABEL = "도구 추천 (자유 작업)"
PHRASE = "phrase"                      # 피드백 멘트, as one tool of the evaluation form
MAX_TOOLS, MAX_PHRASES = 3, 2
MAX_ANSWER = 24000
# id: (이름, 종류, 무엇을 보는지·언제 쓰는지). Review ids are free_work.REVIEW_ROLES, case-law ids graph.caselaw_tool.TOOLS.
TOOLS = {
    "claim-style-adjuster": ("문체 조정", "review", "범위를 바꾸지 않고 조사·띄어쓰기·용어 일관·종결 형식(`; 및`, 종결 명칭, `상기`)을 정리해 고친 문언을 낸다. "
                             "같은 구성이 다른 이름으로 불리거나 형식이 어긋날 때."),
    "syntax-scope-reviewer": ("통사·범위", "review", "절별 주어·수식 귀속, 머리명사 결속, `각각`·복수 분배, `상기` 선행기재, 부모항 체인을 본다. "
                              "긴 절·겹친 수식·종속항 사슬·복수 구성이 있을 때."),
    "oa-strategy-reviewer": ("OA", "review", "명확성·기능적 표현·뒷받침·카테고리 같은 거절이유 위험을 본다. `~하도록`·`~되도록`처럼 결과만 적은 기능, "
                             "기준 없는 상대어·수치가 있을 때."),
    "claim-success-reviewer": ("성공조건", "review", "주골격·구성 계층·협동관계·원자료 근거·구성–기능 연역을 감사한다. 독립항을 새로 썼거나 "
                               "근거가 흔들릴 때. 원자료가 있어야 근거를 대조한다."),
    "blind-claim-reconstruction-reviewer": ("역피처", "review", "발명을 모르는 기술자가 문언만 읽고 그리는 구조를 봉인한 뒤 원자료와 비교한다. "
                                            "형상·배치·방향·단면 같은 물리 구조가 있을 때. 원자료가 없으면 비교는 생략된다."),
    "enforce": ("권리행사 점검", "caselaw", "모델 없이 판례 기록으로 기능적 표현·전제부·수치 범위·여러 실시 주체 같은 권리행사 위험 문언을 찾는다. "
                "침해 입증이나 실시 주체가 걱정될 때."),
    "find": ("판례 찾기", "caselaw", "입력창에 적은 지적과 같은 종류의 결함을 법원이 판단한 판례를 찾는다. 특정 표현의 명확성·뒷받침이 걱정될 때 "
             "(찾을 지적 문장이 필요하다)."),
    "features": ("특허성 신호", "caselaw", "입력창에 적은 특징마다 신규성·진보성 판례 분포를 본다. 어떤 특징을 독립항에 둘지 고민될 때 "
                 "(특징 목록이 필요하다)."),
}
NEEDS_CLAIMS = {tool for tool, (_, kind, _) in TOOLS.items() if kind == "review"} | {"enforce"}
NEEDS_FOCUS = {"find", "features"}

SYSTEM = """너는 Claim-Agent 자유 작업 모드의 도구 추천 에이전트다. 사용자는 한국 특허 청구항을 일반 에이전트와 1차 작업하는 변리사·발명자다.
방금 나온 답(게이트를 거치지 않은 미검수 초안 또는 사용자가 고른 검수 결과)을 읽고, 사용자가 다음에 실행하면 좋을 작업 도구와 피드백 멘트를 추천한다.
너는 청구항을 쓰거나 고치지 않고, 게이트 통과·PASS·LOCK·신규성·진보성·등록 가능성을 판단하지 않는다. 네 추천은 참고이며 사용자가 고른다.

추천 기준
- 도구는 0~3개, 먼저 실행할 것부터. 이유에는 답에 실제로 있는 구절을 짧게 인용하고(`'…'`) 그 구절에 그 도구가 왜 필요한지 한 문장으로 쓴다.
  답에서 근거를 찾을 수 없는 도구는 고르지 않는다. 문제가 보이지 않으면 도구를 억지로 고르지 않는다.
- '이미 실행한 도구'에 같은 문언으로 실행한 검수는 다시 고르지 않는다. 문언이 바뀌었거나 다른 관점이 필요할 때만 다시 고른다.
- '대상 청구항'이 없으면 검수 도구와 권리행사 점검은 고르지 않는다(실행할 문언이 없다).
- 원자료가 없으면 성공조건의 근거 대조와 역피처의 기준 비교가 제한된다. 그래도 고른다면 이유에 그렇게 적는다.
- 답이 검수 결과이면, 지적을 반영하라는 멘트나 아직 보지 않은 위험에 맞는 다른 검수를 고른다.
- focus: 검수 도구는 검수자에게 덧붙일 한 문장(관점·구절, 없으면 빈 문자열). 판례 찾기는 찾을 지적 한 문장, 특허성 신호는 특징 목록(한 줄에 하나) — 둘은 필수다.
  권리행사 점검은 빈 문자열.
- 피드백 멘트는 0~2개. '저장된 피드백 멘트'에 맞는 것이 있으면 그 id를 phrase_id에 적고 text에 그 문장을 그대로 적는다. 맞는 것이 없을 때만
  phrase_id를 비우고 일반 에이전트에게 보낼 새 멘트 한두 문장을 쓴다(청구항 문언을 쓰지 않는다).
- summary: 이 답에서 사용자가 다음에 확인할 가장 큰 위험이나 할 일을 한두 문장으로. 평가·칭찬 문구를 쓰지 않는다.
- 한국어로, JSON 객체 하나만 답한다."""


class ToolPick(BaseModel):
    tool: str = Field(description="사용할 수 있는 도구의 id")
    reason: str = Field(description="답의 어느 구절 때문에 이 도구가 필요한지 한 문장")
    focus: str = Field("", description="검수자에게 덧붙일 말, 판례 찾기의 지적 문장, 특허성 신호의 특징 목록")


class PhrasePick(BaseModel):
    phrase_id: str = Field("", description="저장된 피드백 멘트의 id, 새 멘트면 빈 문자열")
    text: str = Field(description="멘트 문장")
    reason: str = Field("", description="왜 이 멘트가 필요한지 한 문장")


class Advice(BaseModel):
    summary: str
    tools: list[ToolPick] = Field(default_factory=list)
    phrases: list[PhrasePick] = Field(default_factory=list)


# ---- what the code counts on the answer: hints for the agent, and the whole recommendation when the call fails --------

_FUNCTIONAL = re.compile(r"(?:되도록|하도록|가능하도록|할 수 있|될 수 있|수 있도록)")
_SHAPE = re.compile(r"(?:단면|가상선|윤곽|오목|볼록|개방|돌출|관통|경사|상부|하부|일측|타측|내측|외측|상측|하측|홈|축 방향|둘레)")
_RELATIVE = re.compile(r"(?:대략|실질적으로|약\s*\d|근접|충분히|적절)")


def signals(claims: str, answer: str) -> dict:
    """Counts on the claim block (and `근거 미확인` on the whole answer); no model, no judgement."""
    from .pipeline.claimtext import ClaimParseError, numbered_claims, parse_claim_set

    numbered = numbered_claims(claims) if claims else {}
    try:
        parsed = parse_claim_set(claims) if claims else []
    except ClaimParseError:
        parsed = []
    dependent = sum(1 for c in parsed if not c.is_independent)

    def hits(pattern: re.Pattern) -> list[str]:
        return list(dict.fromkeys(m.group(0) for m in pattern.finditer(claims)))

    return dict(claims=len(numbered), independent=len(parsed) - dependent if parsed else (1 if numbered else 0), dependent=dependent,
                functional=hits(_FUNCTIONAL), shape=hits(_SHAPE), relative=hits(_RELATIVE), each=claims.count("각각"),
                unconfirmed=answer.count("근거 미확인"))


def rules(request: dict, sig: dict) -> dict:
    """The recommendation the code makes alone, from `signals`, when the agent cannot answer."""
    tools: list[dict] = []
    if sig["claims"]:
        if sig["functional"]:
            tools.append(dict(tool="oa-strategy-reviewer", reason=f"기능·결과 표현('{sig['functional'][0]}')이 있어 명확성·기능적 표현 위험을 봅니다.", focus=""))
        if sig["dependent"] or sig["each"]:
            what = f"종속항 {sig['dependent']}개" if sig["dependent"] else "'각각' 분배"
            tools.append(dict(tool="syntax-scope-reviewer", reason=f"{what}가 있어 '상기' 선행기재와 귀속을 봅니다.", focus=""))
        if sig["shape"] and (request.get("materials") or {}).get("files"):
            tools.append(dict(tool="blind-claim-reconstruction-reviewer", reason=f"형상·배치 표현('{sig['shape'][0]}')이 있어 문언만으로 구조가 그려지는지 봅니다.", focus=""))
        if not tools:
            tools.append(dict(tool="syntax-scope-reviewer", reason="두드러진 신호는 없어 기본 문언 점검으로 통사·범위를 권합니다.", focus=""))
    phrases = []
    saved = {p["text"]: p for p in request.get("phrases") or []}
    if sig["unconfirmed"]:
        wanted = next((t for t in saved if "근거가 없는 부분" in t), None)
        if wanted:
            phrases.append(dict(phrase_id=saved[wanted]["id"], text=wanted, reason=f"답에 '근거 미확인'이 {sig['unconfirmed']}곳 있습니다."))
    summary = ("청구항 블록이 없어 검수 도구를 고르지 않았습니다." if not sig["claims"] else
               f"청구항 {sig['claims']}개(종속항 {sig['dependent']}개)를 코드가 센 신호로 추천했습니다.")
    return dict(summary=summary, tools=tools[:MAX_TOOLS], phrases=phrases)


def clean(advice: Advice, request: dict) -> dict:
    """Keep what can run here: known tools once each, no claim tool without claims, saved phrases by their own text."""
    has_claims = bool((request.get("claims") or "").strip())
    tools, seen = [], set()
    for pick in advice.tools:
        tool = pick.tool.strip()
        focus = pick.focus.strip()[:600]
        if tool not in TOOLS or tool in seen or (tool in NEEDS_CLAIMS and not has_claims) or (tool in NEEDS_FOCUS and not focus):
            continue
        seen.add(tool)
        tools.append(dict(tool=tool, reason=pick.reason.strip()[:400], focus="" if tool == "enforce" else focus))
    saved = {p["id"]: p for p in request.get("phrases") or []}
    phrases, texts = [], set()
    for pick in advice.phrases:
        known = saved.get(pick.phrase_id.strip())
        text = (known["text"] if known else pick.text.strip())[:600]
        if not text or text in texts:
            continue
        texts.add(text)
        phrases.append(dict(phrase_id=known["id"] if known else "", text=text, reason=pick.reason.strip()[:300]))
    return dict(summary=advice.summary.strip()[:500], tools=tools[:MAX_TOOLS], phrases=phrases[:MAX_PHRASES])


def packet(request: dict, sig: dict) -> str:
    materials = request.get("materials") or {}
    files = materials.get("files", 0)
    lines = [
        "## 사용자의 이번 요청\n" + (request.get("asked") or "없음"),
        "## 답의 종류\n" + {"FREE": "일반 에이전트의 미검수 초안", "REVIEW": "사용자가 고른 검수의 결과", "REVISED": "검수 뒤 일반 에이전트가 고친 문언"}.get(request.get("kind", ""), "답"),
        "## 방금 나온 답\n" + (request.get("answer") or "")[:MAX_ANSWER],
        "## 대상 청구항 (검수 도구가 실행될 문언)\n" + (request.get("claims") or "없음"),
        "## 코드가 센 신호 (참고)\n" + "\n".join([
            f"- 청구항 {sig['claims']}개 (독립 {sig['independent']}, 종속 {sig['dependent']})",
            f"- 기능·결과 표현: {', '.join(sig['functional']) or '없음'}",
            f"- 형상·배치 표현: {', '.join(sig['shape']) or '없음'}",
            f"- 기준 없는 상대어: {', '.join(sig['relative']) or '없음'}",
            f"- '각각' {sig['each']}곳, '근거 미확인' {sig['unconfirmed']}곳"]),
        "## 이 대화에서 이미 실행한 도구\n" + ("\n".join("- " + r for r in request.get("ran") or []) or "없음"),
        "## 원자료\n" + (f"프로젝트·대화 첨부 {files}개 (도면·이미지 {materials.get('drawings', 0)}개)" if files else "없음"),
        "## 사용할 수 있는 도구\n" + "\n".join(f"- {tool}: {name} — {what}" for tool, (name, _, what) in TOOLS.items()),
        "## 저장된 피드백 멘트\n" + ("\n".join(f"- {p['id']}: [{p['group']}] {p['text']}" for p in request.get("phrases") or []) or "없음"),
    ]
    return "\n\n".join(lines)


def advise(provider: LLMProvider, cfg, request: dict) -> dict:
    """One recommendation. The agent's when its call returns a valid answer, the code's rules otherwise (with the reason)."""
    from .store.telemetry import UsageMeter

    started = time.time()
    meter = UsageMeter(provider, cfg.telemetry.pricing)
    sig = signals(request.get("claims") or "", request.get("answer") or "")
    rc = cfg.role(ROLE)
    model = cfg.model_for(ROLE)
    try:
        result = meter.generate(CallSpec(
            role=ROLE, scope="CHAT", model=model, system_instruction=SYSTEM, packet_text=packet(request, sig), sources_block="",
            json_schema=Advice.model_json_schema(), use_cache=False, phase="chat", stage="ADVICE", run_id="",
            gen=GenParams(temperature=rc.temperature, thinking_level=rc.thinking_level, max_output_tokens=rc.max_output_tokens or 4096)))
        data = result.parsed if result.parsed is not None else parse_json_text(result.text)
        picks, source, error = clean(Advice.model_validate(data), request), "agent", ""
    except Exception as exc:  # noqa: BLE001 — the form still needs a recommendation to measure
        picks, source, error = rules(request, sig), "rules", f"{type(exc).__name__}: {exc}"[:300]
    return dict(status="complete", source=source, error=error, model=model, signals=sig, usage=meter.usage,
                seconds=round(time.time() - started, 1), finished=time.time(), **picks)


def main(argv=None) -> int:
    from .chat import _close
    from .config import load_config
    from .live_events import EventWriter
    from .runtime import live_provider

    parser = argparse.ArgumentParser(description="자유 작업 도구 추천")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config, args.project_root)
    request = json.loads(args.request.read_text(encoding="utf-8"))
    provider = live_provider(cfg, EventWriter.from_env(cfg.api_key_env))
    try:
        result = advise(provider, cfg, request)
    finally:
        _close(provider)
    (args.request.parent / "advice.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
