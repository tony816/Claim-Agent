"""ACE ② Reflector: 하나의 failure record를 놓고 무엇이·어디서·왜 놓쳤는지 분석한다.

평가(무엇이 실패했나)와 통찰 추출(다음에 무엇을 바꿔야 하나)을 분리한다는 ACE의 설계를
그대로 따르되, 이 프로젝트의 제약을 하나 더 얹는다.

  **Reflector는 청구항 문언이나 발명의 기술내용을 일반화하지 않는다.**
  절차·검수·표현에 관한 관찰만 만든다. 기술 용어가 섞이면 게이트가 막는다.

기본은 규칙 기반이다. `--llm`을 주면 모델이 한 문단을 덧붙이지만, 그 입력에도
게이트 판정·실패 시험 이름·되돌림 경로만 들어간다.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .failures import GATE_OWNER, STAGE_ORDER, Detector, FailureRecord, FailureStore, normalize_summary

# 기술내용 유입 차단. 발명·부품·수치·재료를 가리키는 표현이 관찰문에 남으면 게이트가 실패한다.
_TECH_PATTERNS = (
    re.compile(r"\d+\s*(mm|cm|㎜|㎝|도|°|℃|mL|ml|μL|㎕|배|rpm|Hz|kPa|MPa|N·m)"),
    re.compile(r"【청구항"),
    re.compile(r"(상기|제\s*\d+\s*항에 있어서)"),
    re.compile(r"(오목면|볼록면|플랜지|리브|보스|슬롯|나사|튜브|카트리지|시약|기둥|홈부|돌기)"),
)

# 실패 유형별 기본 일반화 판정. 반복 횟수가 임계 이상이면 일반화 가능으로 본다.
GENERALIZABLE_TYPES = {"REPEATED_RETURN", "LATE_DETECTION", "ESCAPED_TO_LOCK", "EVAL_MISS"}
MIN_REPEAT_FOR_GENERAL = 2

_WHY_BY_TYPE = {
    "ESCAPED_TO_LOCK": "게이트가 형식적으로 PASS를 냈지만 그 항목을 실제로 확인하는 시험이 보고서에 남지 않았다. 판정 근거를 기록하도록 요구하지 않으면 같은 누락이 반복된다.",
    "LATE_DETECTION": "상류 역할이 자기 소관 게이트를 PASS로 넘겼고, 하류 역할이 뒤늦게 같은 항목을 잡았다. 소관 단계의 점검 항목이 비어 있거나 판정 기준이 모호하다.",
    "REPEATED_RETURN": "한 번의 되돌림으로 닫히지 않는 쟁점이다. 문언 수준에서 고치려 했지만 원인은 상류 계약(설계·기술기여·형상 객체)에 있다.",
    "HALT": "역할이 자기 단계에서 정상적으로 막았다. 탐지 자체는 옳고, 남는 질문은 더 이른 단계에서 막을 수 있었는가이다.",
    "EVAL_MISS": "주입한 결함을 기대 지점이 잡지 못했다. 해당 게이트의 점검 항목이 이 결함 유형을 포함하지 않는다.",
}

_RETURN_TARGET = {
    "claim-style-adjuster": "RETURN_TO_DRAFTER",
    "claim-drafter": "RETURN_TO_ARCHITECT",
    "claim-success-reviewer": "RETURN_TO_STYLE_ADJUSTER",
    "syntax-scope-reviewer": "RETURN_TO_DRAFTER",
    "oa-strategy-reviewer": "RETURN_TO_DRAFTER",
    "picture-claim-reconstruction-reviewer": "RETURN_TO_ARCHITECT",
    "dependent-claim-strategy-architect": "RETURN_TO_DEPENDENT_ARCHITECT",
}


@dataclass
class Reflection:
    failure_id: str
    what_failed: str = ""              # 실제 오류가 무엇이었는가
    first_detector: dict[str, Any] = field(default_factory=dict)   # 어느 역할/게이트가 최초로 잡았어야 하는가
    why_missed: str = ""               # 왜 기존 규칙으로 놓쳤는가
    generalizable: bool = False        # 단발성인지 일반화 가능한 failure mode인지
    failure_mode_id: str = ""          # 일반화된 failure mode의 안정적 식별자
    duplicate_of: list[str] = field(default_factory=list)          # 중복되는 기존 lesson id
    needs_eval_case: bool = False      # 새 adversarial eval case가 필요한가
    proposed_lesson_text: str = ""     # Curator에 넘길 절차·표현 주의사항 초안
    target_roles: list[str] = field(default_factory=list)
    expected_return_to: str | None = None
    gate: str | None = None
    tech_leak: list[str] = field(default_factory=list)              # 기술내용 유입 검사 결과
    llm_drafted: bool = False
    created_at: str = ""

    @property
    def clean(self) -> bool:
        """REFLECTOR_TECH_GATE: 기술내용이 섞이지 않았는가."""
        return not self.tech_leak

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def tech_leak(text: str) -> list[str]:
    """관찰문에 기술내용이 섞였는지. 걸린 조각을 돌려준다."""
    hits: list[str] = []
    for pattern in _TECH_PATTERNS:
        hits += [m.group(0) for m in pattern.finditer(text)]
    return sorted(set(hits))


def failure_mode_id(rec: FailureRecord) -> str:
    """같은 failure mode를 가리키는 안정적 id. lesson.key / eval candidate와 공유한다."""
    expected = rec.expected
    reason = rec.signature.rsplit("|", 1)[-1] if rec.signature else "-"
    return f"FM:{rec.failure_type}|{expected.role or '-'}|{expected.gate or '-'}|{reason}"


def _what_failed(rec: FailureRecord) -> str:
    exp, act = rec.expected, rec.actual
    if rec.failure_type == "ESCAPED_TO_LOCK":
        return f"LOCK까지 간 결과에서 사용자가 오류를 확인했다. {exp.label()}가 막았어야 할 항목이 어떤 단계에서도 걸리지 않았다."
    if rec.failure_type == "LATE_DETECTION":
        return f"{exp.label()}가 소관 게이트를 통과시켰고, {act.label()}에서야 같은 항목이 비-PASS로 잡혔다."
    if rec.failure_type == "REPEATED_RETURN":
        return f"{act.label()}가 같은 사유의 되돌림을 {rec.repeat_count}회 반복했다. 한 번의 지적으로 닫히지 않았다."
    if rec.failure_type == "EVAL_MISS":
        return f"adversarial 케이스가 주입한 결함을 {exp.label()}가 잡지 못하고 통과시켰다."
    return f"{act.label()}에서 실행이 중지되었다."


def _lesson_text(rec: FailureRecord, detector: Detector) -> str:
    """절차·표현 층위의 주의사항. 발명·문언을 인용하지 않는다."""
    gate = detector.gate or "소관 게이트"
    role = detector.role or "해당 역할"
    if rec.failure_type == "ESCAPED_TO_LOCK":
        return (
            f"{gate}를 PASS로 판정하기 전에, 그 판정을 뒷받침한 시험 이름과 확인 위치를 보고서에 각각 한 줄로 남긴다. "
            f"근거 줄을 쓸 수 없는 항목은 PASS가 아니라 UNVERIFIED로 적는다. "
            f"이 절차는 {role}의 판정이 뒤에서 뒤집힌 사례에서 왔다."
        )
    if rec.failure_type == "LATE_DETECTION":
        return (
            f"{role}는 {gate}를 자기 단계에서 끝까지 판정한다. 하류 역할이 다시 볼 것이라는 전제로 "
            f"판정을 미루거나 PASS로 넘기지 않는다. 확신이 서지 않으면 PASS 대신 되돌림 사유를 명시한다."
        )
    if rec.failure_type == "REPEATED_RETURN":
        return (
            f"{role}가 같은 사유로 두 번째 되돌림을 낼 때에는 문언 교정 지시를 반복하지 않고, "
            f"상류 계약(설계·기술기여·형상 공간 객체) 중 무엇이 미확정이라 같은 지적이 반복되는지 한 줄로 지목한다."
        )
    if rec.failure_type == "EVAL_MISS":
        return (
            f"{role}는 {gate} 판정에서 이 결함 유형을 별도 시험 항목으로 세우고, 해당 시험의 상태를 보고서에 명시한다."
        )
    return (
        f"{role}는 {gate} 비-PASS를 낼 때 되돌아갈 단계와 최소 수정 목표를 함께 적어, 같은 사유가 다음 실행에서 반복되지 않게 한다."
    )


def reflect(rec: FailureRecord, existing_lesson_keys: dict[str, str] | None = None) -> Reflection:
    """규칙 기반 회고. LLM 없이 저장된 기록만으로 판정한다."""
    detector = rec.expected
    if not detector.role and detector.gate in GATE_OWNER:
        role, stage = GATE_OWNER[detector.gate]
        detector = Detector(role, stage, detector.gate)
    mode_id = failure_mode_id(rec)
    text = normalize_summary(_lesson_text(rec, detector))
    generalizable = rec.failure_type in GENERALIZABLE_TYPES and (
        rec.repeat_count >= MIN_REPEAT_FOR_GENERAL or len(rec.source_run_ids) >= MIN_REPEAT_FOR_GENERAL or rec.escaped_to_lock
    )
    dupes = [lid for key, lid in (existing_lesson_keys or {}).items() if key == mode_id]
    return Reflection(
        failure_id=rec.failure_id,
        what_failed=normalize_summary(_what_failed(rec)),
        first_detector=asdict(detector),
        why_missed=_WHY_BY_TYPE.get(rec.failure_type, _WHY_BY_TYPE["HALT"]),
        generalizable=generalizable,
        failure_mode_id=mode_id,
        duplicate_of=dupes,
        # 놓친 탐지(escaped/late/eval miss)만 새 적대 케이스가 필요하다. 정상 중지는 이미 잡힌 것이다.
        needs_eval_case=rec.failure_type in ("ESCAPED_TO_LOCK", "LATE_DETECTION", "EVAL_MISS"),
        proposed_lesson_text=text,
        target_roles=[detector.role] if detector.role and detector.role not in ("engine", "user", "") else [],
        expected_return_to=_RETURN_TARGET.get(detector.role),
        gate=detector.gate,
        tech_leak=tech_leak(text + " " + rec.root_cause_summary),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def reflect_all(failures: FailureStore, existing_lesson_keys: dict[str, str] | None = None, status: str | None = "NEW") -> list[Reflection]:
    """아직 회고하지 않은 실패를 모두 분석하고 기록에 붙인다."""
    out: list[Reflection] = []
    for rec in failures.list(status):
        refl = reflect(rec, existing_lesson_keys)
        rec.reflection = refl.as_dict()
        rec.status = "REFLECTED"
        failures.save(rec)
        out.append(refl)
    return out


# --------------------------------------------------------------------------- 선택적 LLM 보강

LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "why_missed": {"type": "string", "description": "기존 규칙으로 놓친 이유. 절차·판정 기준에 관한 설명만."},
        "proposed_lesson_text": {"type": "string", "description": "역할 프롬프트에 덧붙일 절차·표현 주의사항 한 문단."},
        "generalizable": {"type": "boolean"},
    },
    "required": ["why_missed"],
}

_LLM_INSTRUCTION = """당신은 한국어 특허 청구항 파이프라인의 회고 분석가(Reflector)다.

아래는 한 건의 실패 기록이다. 실제 오류가 무엇이었는지, 어느 역할·게이트가 최초로 잡았어야 했는지,
왜 기존 규칙으로 놓쳤는지를 판단하고, 다음 실행에 도움이 될 **절차·검수·표현상의 일반 규칙 한 가지**를 만들어라.

반드시 지킨다:
- 발명의 기술내용, 청구항 문언, 부품 이름, 수치, 재료를 인용하거나 일반화하지 않는다.
- 특정 사건 서술이 아니라 같은 역할이 다음에도 확인해야 할 점검 절차로 쓴다.
- 역할 파일과 CLAUDE.md의 규칙을 바꾸거나 완화하는 내용을 쓰지 않는다.
- 근거가 약하면 proposed_lesson_text를 비워 둔다."""


def llm_materials(rec: FailureRecord, base: Reflection) -> str:
    """LLM 입력. 게이트·시험 이름·되돌림 경로만 담고 문언·원자료는 담지 않는다."""
    lines = [
        f"실패 유형: {rec.failure_type}",
        f"발명 유형: {rec.invention_type or '미기재'}",
        f"출처: {rec.origin} / LOCK까지 통과: {'예' if rec.escaped_to_lock else '아니오'}",
        f"최초로 잡았어야 할 지점: {base.first_detector.get('stage')} / {base.first_detector.get('role')} / {base.first_detector.get('gate') or '-'}",
        f"실제로 잡힌 지점: {rec.actual.label()}",
        f"반복 횟수: {rec.repeat_count} (run {len(rec.source_run_ids)}건)",
        f"관찰: {base.what_failed}",
    ]
    for ev in rec.evidence[:8]:
        lines.append(f"근거: [{ev.get('kind')}] {ev.get('locator')}")
    lines.append("\n위 정보만으로 JSON 하나를 출력한다.\n")
    return "\n".join(lines)


def reflect_with_llm(provider: Any, model: str, rec: FailureRecord, base: Reflection, max_output_tokens: int = 2048) -> Reflection:
    """규칙 기반 회고를 모델 한 문단으로 보강한다. 기술내용이 섞이면 원래 회고를 그대로 쓴다."""
    from ..provider.base import CallSpec, GenParams, parse_json_text

    spec = CallSpec(
        role="ace-reflector", scope="META", model=model, system_instruction=_LLM_INSTRUCTION,
        packet_text=llm_materials(rec, base), sources_block="", json_schema=LLM_SCHEMA,
        gen=GenParams(0.3, "MEDIUM", max_output_tokens), use_cache=False, phase="main", stage="ACE_REFLECT",
    )
    result = provider.generate(spec)
    data = result.parsed or parse_json_text(result.text) or {}
    text = normalize_summary(str(data.get("proposed_lesson_text") or ""))
    why = normalize_summary(str(data.get("why_missed") or ""))
    leak = tech_leak(f"{text} {why}")
    if leak:
        base.tech_leak = sorted(set(base.tech_leak) | set(leak))
        return base       # 기술내용이 섞였으면 모델 문장을 버리고 규칙 기반 결과를 쓴다
    if why:
        base.why_missed = why
    if text:
        base.proposed_lesson_text = text
        base.llm_drafted = True
    if "generalizable" in data:
        base.generalizable = bool(data["generalizable"])
    return base


def render_md(reflections: list[Reflection], failures: FailureStore) -> str:
    out = ["# ACE Reflector 보고", ""]
    if not reflections:
        out.append("(회고할 새 실패 기록이 없다)")
        return "\n".join(out) + "\n"
    out.append("| failure | 유형 | 최초 탐지 지점 | 일반화 | eval case 필요 | 기술내용 유입 |")
    out.append("|---|---|---|---|---|---|")
    for r in reflections:
        rec = failures.get(r.failure_id)
        d = r.first_detector
        out.append(
            f"| `{r.failure_id}` | {rec.failure_type} | {d.get('stage') or '-'} / {d.get('role') or '-'} / {d.get('gate') or '-'} | "
            f"{'예' if r.generalizable else '아니오'} | {'예' if r.needs_eval_case else '아니오'} | {', '.join(r.tech_leak) or '없음'} |"
        )
    out.append("")
    for r in reflections:
        out.append(f"## {r.failure_id} — {r.failure_mode_id}")
        out.append("")
        out.append(f"- 무엇이 실패했나: {r.what_failed}")
        out.append(f"- 왜 놓쳤나: {r.why_missed}")
        if r.duplicate_of:
            out.append(f"- 기존 교훈과 중복: {', '.join(r.duplicate_of)}")
        if r.proposed_lesson_text:
            out.append(f"- 교훈 초안: {r.proposed_lesson_text}")
        if r.tech_leak:
            out.append(f"- **기술내용 유입으로 차단됨**: {', '.join(r.tech_leak)}")
        out.append("")
    out.append("> Reflector는 제안만 한다. 어떤 교훈도 사람 승인과 regression eval 통과 전에는 주입되지 않는다.")
    return "\n".join(out) + "\n"


__all__ = ["Reflection", "reflect", "reflect_all", "reflect_with_llm", "failure_mode_id", "tech_leak", "render_md", "STAGE_ORDER"]
