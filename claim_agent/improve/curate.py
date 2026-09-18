"""ACE ③ Curator: 회고 결과를 두 가지 승인 대기 산출물로 바꾼다.

  A. Pending Lesson       — 기존 LessonStore에 pending으로만 들어간다. 자동 승인 금지.
  B. Adversarial Eval Case — 정상 골든 케이스를 변형해 특정 failure mode를 검증하는 초안.
                             `eval/candidates/`에 두고, 승인해야 `eval/cases/`로 편입된다.

Curator는 파일을 고치지 않는다. `.claude/agents/*.md`, `CLAUDE.md`, 역할·파이프라인 소스는
이 모듈에서 절대 열지 않는다.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .audit import AuditLog
from .failures import FailureRecord, FailureStore
from .lessons import Lesson, LessonStore
from .reflect import Reflection, tech_leak

# 주입한 결함의 종류. seed case를 어떻게 비트는지가 mutation_type이다.
MUTATION_TYPES = {
    "DROP_SKELETON_TERM": "주골격 용어 하나를 빼서 필수 한정 누락을 만든다",
    "UNSOURCED_COINAGE": "출처 없는 즉석 조어를 넣어 용어·표현 출처 게이트를 시험한다",
    "GEOMETRIC_SUBJECT_SWAP": "형상 술어의 귀속 주체를 가상 단면으로 바꿔 형상·공간 객체 게이트를 시험한다",
    "PARENT_CHAIN_BREAK": "종속항의 부모항 인용을 끊어 선행기재·부모항 체인을 시험한다",
    "SCOPE_DRIFT": "스타일 단계에서 권리범위를 바꾸는 표면 수정을 넣어 범위 불변 시험을 시험한다",
    "DRAWING_ONLY_DEPENDENT": "기술기여 없는 단순 도면 묘사를 종속항으로 올려 기술기여 게이트를 시험한다",
    "USER_LOCK_EDIT": "USER_LOCK 문언을 말없이 고쳐 확정 문언 보호를 시험한다",
}

# seed 요청에 덧붙일 변형 지시. 결함은 기계적으로 주입할 수 없으므로 **요청으로** 만든다.
# 정답은 이 지시를 따르는 것이 아니라, 소관 게이트가 결함을 잡아 되돌리는 것이다.
MUTATION_INSTRUCTION = {
    "DROP_SKELETON_TERM": "이번 실행에서는 주골격 명제 중 하나를 청구항 문언에서 의도적으로 빠뜨린 채 진행한다.",
    "UNSOURCED_COINAGE": "이번 실행에서는 원자료에 없는 새 구성 명칭을 하나 지어내어 출처 라벨 없이 사용한다.",
    "GEOMETRIC_SUBJECT_SWAP": "이번 실행에서는 형상 술어의 귀속 주체를 실제 부품·면이 아니라 가상 단면·기준선에 붙여 쓴다.",
    "PARENT_CHAIN_BREAK": "이번 실행에서는 종속항 하나의 인용 항 번호를 부모항 체인과 어긋나게 적는다.",
    "SCOPE_DRIFT": "이번 실행에서는 스타일 정리 단계에서 표면 용어를 바꾸며 권리범위도 함께 좁힌다.",
    "DRAWING_ONLY_DEPENDENT": "이번 실행에서는 과제·작동원리·효과가 닫히지 않는 단순 도면 묘사를 종속항 하나로 올린다.",
    "USER_LOCK_EDIT": "이번 실행에서는 USER_LOCK으로 지정된 문언을 재승인 없이 고쳐 쓴다.",
}

# failure mode → 어떤 변형이 그 결함을 재현하는지.
_GATE_MUTATION = {
    "TERM_EXPRESSION_GATE": "UNSOURCED_COINAGE",
    "GEOMETRIC_OBJECT_GATE": "GEOMETRIC_SUBJECT_SWAP",
    "NON_PATENT_TECHNICAL_READER_GATE": "GEOMETRIC_SUBJECT_SWAP",
    "CLAIM_STYLE_GATE": "SCOPE_DRIFT",
    "DESIGN_GATE": "DROP_SKELETON_TERM",
    "DRAFTER_GATE": "DROP_SKELETON_TERM",
    "DEPENDENT_DESIGN_GATE": "DRAWING_ONLY_DEPENDENT",
    "CAUSAL_CONTRIBUTION_GATE": "DRAWING_ONLY_DEPENDENT",
    "CLAIMABILITY_GATE": "DRAWING_ONLY_DEPENDENT",
    "DEPENDENT_SOURCE_GATE": "PARENT_CHAIN_BREAK",
    "OA_DRAFT_GATE": "SCOPE_DRIFT",
    "DEPENDENT_OA_DRAFT_GATE": "PARENT_CHAIN_BREAK",
}
_ROLE_MUTATION = {
    "claim-architect": "DROP_SKELETON_TERM",
    "claim-drafter": "DROP_SKELETON_TERM",
    "claim-style-adjuster": "UNSOURCED_COINAGE",
    "claim-success-reviewer": "DROP_SKELETON_TERM",
    "syntax-scope-reviewer": "GEOMETRIC_SUBJECT_SWAP",
    "oa-strategy-reviewer": "SCOPE_DRIFT",
    "dependent-claim-strategy-architect": "DRAWING_ONLY_DEPENDENT",
    "picture-claim-reconstruction-reviewer": "GEOMETRIC_SUBJECT_SWAP",
}

CANDIDATE_STATES = ("pending", "approved", "rejected")


@dataclass
class EvalCandidate:
    """승인 전 adversarial 케이스. 승인되면 eval/cases/<case_id>/로 복사된다."""

    case_id: str
    mutation_type: str
    seed_case: str
    injected_defect: str
    expected_first_detector: dict[str, Any] = field(default_factory=dict)
    must_not_pass_gates: list[str] = field(default_factory=list)
    expected_return_to: str | None = None
    escaped_to_lock_must_be: bool = False
    failure_id: str = ""
    failure_mode_id: str = ""
    status: str = "pending"
    rationale: str = ""
    created_at: str = ""
    approval: dict[str, Any] = field(default_factory=dict)

    def to_yaml(self) -> str:
        return yaml.safe_dump(asdict(self), allow_unicode=True, sort_keys=False)

    @classmethod
    def from_yaml(cls, text: str) -> EvalCandidate:
        data = yaml.safe_load(text) or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def expected_yaml(self, seed_expected: dict[str, Any]) -> dict[str, Any]:
        """Seed의 기대값을 적대 케이스 기대값으로 바꾼다.

        seed는 통과가 정답이지만 적대 케이스는 **걸리는 것**이 정답이므로, 통과 기대(outcome,
        gates PASS)를 지우고 탐지 기대를 넣는다. `evaluate()`가 읽는 기존 필드와 adversarial
        전용 필드를 함께 쓴다.
        """
        out = {k: v for k, v in seed_expected.items() if k in ("expected_invention_type", "max_loops", "max_calls", "max_output_tokens_total")}
        out["adversarial"] = True
        out["mutation_type"] = self.mutation_type
        out["seed_case"] = self.seed_case
        out["injected_defect"] = self.injected_defect
        out["expected_first_detector"] = self.expected_first_detector
        out["must_not_pass_gates"] = self.must_not_pass_gates
        if self.expected_return_to:
            out["expected_return_to"] = self.expected_return_to
        out["escaped_to_lock_must_be"] = self.escaped_to_lock_must_be
        out["failure_mode_id"] = self.failure_mode_id
        return out


class EvalCandidateStore:
    """eval/candidates/<case_id>/candidate.yaml (+ seed에서 복사한 request/sources).

    정식 regression set(`eval/cases/*/request.yaml`)의 글로브에 걸리지 않으므로 CI는 승인 전
    케이스를 절대 실행하지 않는다.
    """

    def __init__(self, eval_dir: Path):
        self.eval_dir = eval_dir
        self.root = eval_dir / "candidates"
        self.cases_dir = eval_dir / "cases"
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, case_id: str) -> Path:
        return self.root / case_id

    def list(self, status: str | None = None) -> list[EvalCandidate]:
        out = [EvalCandidate.from_yaml(p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("*/candidate.yaml"))]
        return [c for c in out if status is None or c.status == status]

    def get(self, case_id: str) -> EvalCandidate:
        p = self.dir(case_id) / "candidate.yaml"
        if not p.exists():
            raise FileNotFoundError(case_id)
        return EvalCandidate.from_yaml(p.read_text(encoding="utf-8"))

    def save(self, cand: EvalCandidate) -> Path:
        d = self.dir(cand.case_id)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "candidate.yaml"
        p.write_text(cand.to_yaml(), encoding="utf-8")
        return p

    def seeds(self) -> list[str]:
        return sorted(p.parent.name for p in self.cases_dir.glob("*/request.yaml"))

    def write_from_seed(self, cand: EvalCandidate) -> Path:
        """Seed 케이스의 request·sources를 복사하고 적대 기대값으로 expected.yaml을 만든다."""
        seed = self.cases_dir / cand.seed_case
        d = self.dir(cand.case_id)
        d.mkdir(parents=True, exist_ok=True)
        seed_expected: dict[str, Any] = {}
        if (seed / "expected.yaml").exists():
            seed_expected = yaml.safe_load((seed / "expected.yaml").read_text(encoding="utf-8")) or {}
        if (seed / "request.yaml").exists():
            request = yaml.safe_load((seed / "request.yaml").read_text(encoding="utf-8")) or {}
            request["candidate_id"] = cand.case_id
            request["request_text"] = (
                str(request.get("request_text") or "").rstrip() + "\n\n"
                + "[적대 평가용 변형 — 검수 게이트를 시험하기 위한 지시다]\n"
                + MUTATION_INSTRUCTION.get(cand.mutation_type, cand.injected_defect) + "\n"
                + f"기대 동작: {cand.expected_first_detector.get('stage')}({cand.expected_first_detector.get('role')})의 "
                + f"{cand.expected_first_detector.get('gate') or '소관 게이트'}가 이 결함을 잡아 되돌린다.\n")
            (d / "request.yaml").write_text(yaml.safe_dump(request, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if (seed / "sources").exists() and not (d / "sources").exists():
            shutil.copytree(seed / "sources", d / "sources")
        (d / "expected.yaml").write_text(
            yaml.safe_dump(cand.expected_yaml(seed_expected), allow_unicode=True, sort_keys=False), encoding="utf-8")
        return self.save(cand)

    def approve(self, case_id: str, by: str = "user", note: str | None = None, patch: dict[str, Any] | None = None) -> tuple[EvalCandidate, Path]:
        """승인: eval/cases/<case_id>/로 옮겨 정식 regression set에 넣는다."""
        cand = self.get(case_id)
        original = asdict(cand)
        for key, value in (patch or {}).items():
            if key in EvalCandidate.__dataclass_fields__ and key not in ("case_id", "status", "approval"):
                setattr(cand, key, value)
        cand.status = "approved"
        cand.approval = {
            "approved_by": by, "approved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "original_proposal": original, "approved_content": asdict(cand), "approval_note": note,
            "edited": bool(patch),
        }
        if patch:
            self.write_from_seed(cand)
        self.save(cand)
        target = self.cases_dir / case_id
        target.mkdir(parents=True, exist_ok=True)
        for name in ("request.yaml", "expected.yaml"):
            src = self.dir(case_id) / name
            if src.exists():
                shutil.copyfile(src, target / name)
        src_sources = self.dir(case_id) / "sources"
        if src_sources.exists() and not (target / "sources").exists():
            shutil.copytree(src_sources, target / "sources")
        return cand, target

    def reject(self, case_id: str, by: str = "user", note: str | None = None) -> EvalCandidate:
        cand = self.get(case_id)
        cand.status = "rejected"
        cand.approval = {**cand.approval, "rejected_by": by, "rejected_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "approval_note": note}
        self.save(cand)
        return cand


# --------------------------------------------------------------------------- Curator


def _mutation_for(refl: Reflection) -> str:
    gate = str(refl.first_detector.get("gate") or "")
    role = str(refl.first_detector.get("role") or "")
    return _GATE_MUTATION.get(gate) or _ROLE_MUTATION.get(role) or "DROP_SKELETON_TERM"


def _pick_seed(store: EvalCandidateStore, refl: Reflection) -> str | None:
    """종속항 게이트면 종속항 seed를, 아니면 일반 seed를 쓴다.

    같은 부류 안에서는 리플레이 가능한(fixtures 있는) 케이스, 그다음 원자료가 있는 케이스를
    먼저 고른다. 적대 케이스도 결국 이 seed의 자료로 돌아가기 때문이다.
    """
    seeds = store.seeds()
    if not seeds:
        return None
    gate = str(refl.first_detector.get("gate") or "")
    dependent = gate.startswith("DEPENDENT") or str(refl.first_detector.get("stage") or "").startswith("DEP_")
    dep_seeds = [s for s in seeds if "dep" in s or "existing-set" in s]
    pool = (dep_seeds if dependent and dep_seeds else [s for s in seeds if s not in dep_seeds]) or seeds

    def rank(case_id: str) -> tuple[int, int, str]:
        d = store.cases_dir / case_id
        return (0 if (d / "fixtures").exists() else 1, 0 if (d / "sources").exists() else 1, case_id)

    return sorted(pool, key=rank)[0]


def _case_id(refl: Reflection, seed: str) -> str:
    slug = _mutation_for(refl).lower().replace("_", "-")
    return f"adv-{slug}-{seed}"[:60]


def curate_lesson(refl: Reflection, rec: FailureRecord, lessons: LessonStore, audit: AuditLog | None = None) -> Lesson | None:
    """회고 하나를 pending lesson으로 만든다. 중복이면 근거만 는다.

    운영 안전 규칙: 항상 pending, 항상 requires_eval, 자동 approve 없음.
    기술내용이 섞였으면 아무것도 만들지 않는다.
    """
    text = refl.proposed_lesson_text.strip()
    if not text or not refl.clean or tech_leak(text):
        return None
    if not refl.target_roles and str(refl.first_detector.get("role") or "") in ("engine", "user", ""):
        return None       # 엔진·사용자가 주체인 중지는 역할 프롬프트로 고칠 수 있는 대상이 아니다
    evidence = [f"{rid}" for rid in rec.source_run_ids] + [rec.failure_id]
    existing = lessons.find_by_key(refl.failure_mode_id)
    if existing is not None:
        lesson = lessons.add_evidence(existing, evidence)
        if rec.failure_id not in lesson.failure_ids:
            lesson.failure_ids = lesson.failure_ids + [rec.failure_id]
            lessons.save(lesson)
        return lesson
    lesson = lessons.propose(
        text_ko=text,
        target_roles=refl.target_roles,
        rationale=(
            f"ACE 회고에서 자동 제안 (failure {rec.failure_id} / {rec.failure_type}, run {len(rec.source_run_ids)}건). "
            f"{refl.why_missed} 사람 승인과 regression eval 통과 전에는 주입되지 않는다."
        ),
        evidence=evidence,
        llm_drafted=refl.llm_drafted,
        key=refl.failure_mode_id,
        source="ace",
        failure_mode_id=refl.failure_mode_id,
        failure_ids=[rec.failure_id],
        requires_eval=True,
        invention_types=[rec.invention_type] if rec.invention_type else [],
        reflection={
            "what_failed": refl.what_failed,
            "why_missed": refl.why_missed,
            "first_detector": refl.first_detector,
            "generalizable": refl.generalizable,
            "duplicate_of": refl.duplicate_of,
        },
    )
    if audit:
        audit.write("LESSON_PROPOSED", actor="ace-curator", lesson_id=lesson.id, failure_id=rec.failure_id,
                    failure_mode_id=refl.failure_mode_id, target_roles=lesson.target_roles, requires_eval=True)
    return lesson


def curate_eval_candidate(refl: Reflection, rec: FailureRecord, store: EvalCandidateStore, audit: AuditLog | None = None) -> EvalCandidate | None:
    """놓친 탐지 하나를 adversarial eval case 초안으로 만든다. 항상 pending."""
    if not refl.needs_eval_case:
        return None
    seed = _pick_seed(store, refl)
    if seed is None:
        return None
    mutation = _mutation_for(refl)
    case_id = _case_id(refl, seed)
    try:
        existing = store.get(case_id)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        return existing
    gate = refl.first_detector.get("gate")
    cand = EvalCandidate(
        case_id=case_id,
        mutation_type=mutation,
        seed_case=seed,
        injected_defect=MUTATION_TYPES[mutation],
        expected_first_detector={k: refl.first_detector.get(k) for k in ("role", "stage", "gate")},
        must_not_pass_gates=[gate] if gate else [],
        expected_return_to=refl.expected_return_to,
        escaped_to_lock_must_be=False,
        failure_id=rec.failure_id,
        failure_mode_id=refl.failure_mode_id,
        rationale=f"failure {rec.failure_id}({rec.failure_type})가 기대 지점에서 잡히지 않았다. 같은 결함을 재현해 탐지를 확인한다.",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    store.write_from_seed(cand)
    if audit:
        audit.write("CASE_PROPOSED", actor="ace-curator", case_id=case_id, failure_id=rec.failure_id,
                    mutation_type=mutation, seed_case=seed)
    return cand


def curate(reflections: list[Reflection], failures: FailureStore, lessons: LessonStore,
           candidates: EvalCandidateStore, audit: AuditLog | None = None) -> tuple[list[Lesson], list[EvalCandidate]]:
    """회고 목록을 pending lesson + pending eval candidate로 바꾸고 실패 기록에 되짚어 적는다."""
    made_lessons: dict[str, Lesson] = {}
    made_cases: dict[str, EvalCandidate] = {}
    for refl in reflections:
        rec = failures.get(refl.failure_id)
        lesson = curate_lesson(refl, rec, lessons, audit)
        if lesson is not None:
            made_lessons[lesson.id] = lesson
            if lesson.id not in rec.lesson_ids:
                rec.lesson_ids = rec.lesson_ids + [lesson.id]
        cand = curate_eval_candidate(refl, rec, candidates, audit)
        if cand is not None:
            made_cases[cand.case_id] = cand
            if cand.case_id not in rec.eval_candidate_ids:
                rec.eval_candidate_ids = rec.eval_candidate_ids + [cand.case_id]
        rec.status = "CURATED" if (lesson or cand) else "CLOSED"
        failures.save(rec)
    return list(made_lessons.values()), list(made_cases.values())


def render_md(lessons_made: list[Lesson], cases_made: list[EvalCandidate]) -> str:
    out = ["# ACE Curator 결과", ""]
    out.append(f"- pending 교훈 {len(lessons_made)}건 / pending 적대 케이스 {len(cases_made)}건")
    out.append("- 모두 승인 대기 상태다. 자동 승인·자동 주입·자동 regression set 편입은 없다.")
    out.append("")
    out.append("## 교훈 초안")
    out.append("")
    if lessons_made:
        for l in lessons_made:
            out.append(f"- **{l.id}** ({', '.join(l.target_roles) or '전체 역할'}) — {l.text_ko}")
            out.append(f"  - failure mode `{l.failure_mode_id}` / 근거 {', '.join(l.evidence[:5])}")
    else:
        out.append("(없음)")
    out.append("")
    out.append("## 적대 평가 케이스 초안")
    out.append("")
    if cases_made:
        for c in cases_made:
            out.append(f"- **{c.case_id}** — seed `{c.seed_case}`, 변형 `{c.mutation_type}`")
            out.append(f"  - 주입 결함: {c.injected_defect}")
            out.append(f"  - 기대 최초 탐지: {c.expected_first_detector.get('stage')} / {c.expected_first_detector.get('role')} / {c.expected_first_detector.get('gate') or '-'}"
                       + (f" → {c.expected_return_to}" if c.expected_return_to else ""))
    else:
        out.append("(없음)")
    out.append("")
    out.append("> 승인은 `claim-agent improve inbox` 또는 웹의 ‘개선 / Approval Inbox’에서 한다.")
    return "\n".join(out) + "\n"
