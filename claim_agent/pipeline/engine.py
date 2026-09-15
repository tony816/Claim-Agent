"""PipelineEngine: deterministic orchestration of the 19-step CLAUDE.md pipeline."""
from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..claim_scope import parse_target
from ..config import AppConfig
from ..models.contracts import contract_for
from ..models.enums import ExecStatus, NextStep, RequestMode, Scope, Status, StyleChangeMode
from ..models.envelope import ENVELOPE_JSON_SCHEMA, RoleEnvelope
from ..models.ids import Identifiers, bump_dependent_design_revision, bump_dependent_revision, bump_design_revision, bump_revision, input_revision_id, record_id
from ..models.request import MaterialBundle, RunRequest
from ..models.state import (
    CandidateState,
    DependentRevisionState,
    DependentSetState,
    Halt,
    RecordRef,
    RevisionState,
    RunState,
    Stage,
    TargetRecon,
)
from ..provider.base import CallResult, CallSpec, GenParams, LLMProvider, ProviderError
from ..roles.prompt import PromptAssembler
from ..roles.registry import RoleRegistry
from ..sources.registry import SourceSet
from ..store.report import render_report
from ..store.runstore import RunStore
from ..store.telemetry import TelemetryRow, TelemetryWriter, estimate_cost, now
from . import packets
from .blind_guard import BlindPacket
from .claimtext import ClaimParseError, MultiDependentChain, contains_user_lock, exact_sha256, parent_chain, parent_chain_text, parse_claim_set, validate_parent_refs
from .corpus_tool import ToolLog, make_tools
from .locks import build_claim_lock, build_dependent_set_lock
from .transitions import LOOP_KEY, Transition, decide

ROLE_BY_STAGE = {
    Stage.ARCHITECT: "claim-architect",
    Stage.DRAFT: "claim-drafter",
    Stage.STYLE: "claim-style-adjuster",
    Stage.SUCCESS: "claim-success-reviewer",
    Stage.SYNTAX: "syntax-scope-reviewer",
    Stage.OA: "oa-strategy-reviewer",
    Stage.BLIND: "blind-claim-reconstruction-reviewer",
    Stage.PICTURE: "picture-claim-reconstruction-reviewer",
    Stage.DEP_ARCHITECT: "dependent-claim-strategy-architect",
    Stage.DEP_DRAFT: "claim-drafter",
    Stage.DEP_STYLE: "claim-style-adjuster",
    Stage.DEP_SUCCESS: "claim-success-reviewer",
    Stage.DEP_SYNTAX: "syntax-scope-reviewer",
    Stage.DEP_OA: "oa-strategy-reviewer",
    Stage.DEP_RECON: "blind-claim-reconstruction-reviewer",
}

REVIEWER_ROLES = {"claim-success-reviewer", "syntax-scope-reviewer", "oa-strategy-reviewer", "picture-claim-reconstruction-reviewer"}
NON_GATING_UNVERIFIED = {"SPEC_NOT_PROVIDED", "PRIOR_ART_NOT_PROVIDED"}


class PipelineHalt(Exception):
    def __init__(self, halt: Halt):
        super().__init__(f"{halt.kind} @ {halt.stage}: {halt.message}")
        self.halt = halt


def budget_violation(usage: dict[str, float], cfg: AppConfig) -> str | None:
    """Message when the next call would exceed pipeline.max_calls, or accumulated tokens/cost already exceed their caps.

    Checked before every provider call, so a completed call is never discarded; token and cost caps
    can therefore be exceeded by at most one call.
    """
    pc = cfg.pipeline
    calls = int(usage.get("calls", 0))
    if pc.max_calls is not None and calls >= pc.max_calls:
        return f"호출 수 한도 도달: {calls}/{pc.max_calls} (pipeline.max_calls)"
    tokens = int(usage.get("prompt_tokens", 0) + usage.get("output_tokens", 0) + usage.get("thoughts_tokens", 0))
    if pc.max_total_tokens is not None and tokens > pc.max_total_tokens:
        return f"토큰 한도 초과: {tokens:,}/{pc.max_total_tokens:,} (pipeline.max_total_tokens)"
    if pc.max_cost_usd is not None and usage.get("cost_usd", 0) > pc.max_cost_usd:
        return f"비용 한도 초과: ${usage.get('cost_usd', 0):.4f}/${pc.max_cost_usd:.4f} (pipeline.max_cost_usd)"
    return None


@dataclass
class Decision:
    text: str = ""
    action: str = "none"      # none | style_fix | meaning_fix | redesign | add_source | accept_unverified | restart
    add_sources: list[str] | None = None
    restart_from: str | None = None
    request_update: RunRequest | None = None  # conversational scope/source update; always redesign
    scope: str | None = None                  # INDEPENDENT | DEPENDENT: which revision a style/meaning fix targets on a finished run


class PipelineEngine:
    def __init__(
        self,
        cfg: AppConfig,
        provider: LLMProvider,
        roles: RoleRegistry,
        sources: SourceSet,
        store: RunStore,
        assembler: PromptAssembler,
        source_set_id: str,
        variant_id: str = "default",
        lessons_hash: str = "none",
        shadow_hook: Callable[[CallSpec, RoleEnvelope, RunState], None] | None = None,
    ):
        self.cfg = cfg
        self.provider = provider
        self.roles = roles
        self.sources = sources
        self.store = store
        self.assembler = assembler
        self.source_set_id = source_set_id
        self.variant_id = variant_id
        self.lessons_hash = lessons_hash
        self.shadow_hook = shadow_hook
        self._bundle: MaterialBundle | None = None
        self._tool_logs: dict[str, ToolLog] = {}
        self._lock = threading.Lock()
        self._pending_repairs: list[tuple[str, str, CallResult]] = []

    # ================================================================== run lifecycle
    def _load_bundle(self, request: RunRequest) -> MaterialBundle:
        return MaterialBundle.load(request, self.cfg.materials.max_image_side)

    def start(self, request: RunRequest, run_id: str | None = None) -> RunState:
        bundle = self._load_bundle(request)
        rid = run_id or self.store.new_run_id()
        state = RunState(
            run_id=rid,
            request=request.model_dump(mode="json"),
            request_mode=request.request_mode,
            source_set_id=self.source_set_id,
            variant_id=self.variant_id,
            lessons_hash=self.lessons_hash,
            input_revision=input_revision_id(bundle.digest()),
            material_meta=[i.as_meta() for i in bundle.items],
            spec_present=bundle.spec_present,
            prior_art_present=bundle.prior_art_present,
            candidate=CandidateState(candidate_id=request.candidate_id),
        )
        if request.request_mode == RequestMode.REVIEW_ONLY:
            state.stage = Stage.REVIEW_ONLY
        self.store.save_materials(rid, bundle)
        self.store.save_state(state)
        self._bundle = bundle
        return state

    def run(self, state: RunState) -> RunState:
        bundle = self._bundle or self._load_bundle(RunRequest.model_validate(state.request))
        self._bundle = bundle
        state.halt = None
        state.outcome = "RUNNING"
        try:
            if state.stage == Stage.REVIEW_ONLY:
                self._run_review_only(state, bundle)
            else:
                self._loop(state, bundle)
        except PipelineHalt as ph:
            state.halt = ph.halt
            state.stage = Stage.HALTED
            state.outcome = f"HALTED_{ph.halt.kind}"
        except ProviderError as exc:
            state.halt = Halt(stage=state.stage.value, role="provider", kind="ERROR", message=str(exc))
            state.stage = Stage.HALTED
            state.outcome = "HALTED_ERROR"
        self.store.save_state(state)
        self.store.write_report(state.run_id, render_report(state))
        return state

    def resume(self, run_id: str, decision: Decision) -> RunState:
        state = self.store.load_state(run_id)
        req = RunRequest.model_validate(state.request)
        if decision.request_update is not None:
            updated = decision.request_update
            if decision.restart_from != "ARCHITECT" or updated.request_mode != req.request_mode:
                raise ProviderError("요청/자료 변경은 같은 모드의 ARCHITECT 재설계로만 재개할 수 있습니다.")
            if updated.candidate_id != req.candidate_id or updated.user_lock != req.user_lock:
                raise ProviderError("요청 갱신으로 candidate_id 또는 USER_LOCK을 변경할 수 없습니다.")
            req = updated
            state.request = req.model_dump(mode="json")
            bundle = self._load_bundle(req)
            self.store.save_materials(run_id, bundle)
            state.input_revision = input_revision_id(bundle.digest())
            state.material_meta = [i.as_meta() for i in bundle.items]
            state.spec_present = bundle.spec_present
            state.prior_art_present = bundle.prior_art_present
        if state.source_set_id != self.source_set_id:
            state.mark_all_stale()
            if decision.restart_from != "ARCHITECT":
                self.store.save_state(state)
                raise ProviderError(
                    f"source_set_id changed ({state.source_set_id} -> {self.source_set_id}); records are STALE. Use --restart-from ARCHITECT to redo."
                )
            state.notes.append(f"역할·소스 계약 갱신: {state.source_set_id} → {self.source_set_id}; 이전 기록 STALE, 설계부터 재검증")
            state.source_set_id = self.source_set_id
            state.variant_id = self.variant_id
            state.lessons_hash = self.lessons_hash
        if decision.add_sources:
            req.invention_sources = list(req.invention_sources) + list(decision.add_sources)
            state.request = req.model_dump(mode="json")
            bundle = self._load_bundle(req)
            self.store.save_materials(run_id, bundle)
            state.input_revision = input_revision_id(bundle.digest())
            state.material_meta = [i.as_meta() for i in bundle.items]
            state.spec_present = bundle.spec_present
            state.prior_art_present = bundle.prior_art_present
            decision.action = "redesign" if decision.action in ("none", "add_source") else decision.action
        self._bundle = self._load_bundle(req)
        halted_stage = Stage(state.halt.stage) if state.halt else state.stage
        in_dependent = halted_stage.value.startswith("DEP_")
        if decision.scope:
            if decision.scope == "DEPENDENT" and not (state.dependent and state.dependent.current and not state.dependent.stale):
                raise ProviderError("종속항 세트가 없거나 무효화되어 DEPENDENT 범위의 수정을 적용할 수 없다")
            in_dependent = decision.scope == "DEPENDENT"
        if decision.text:
            state.notes.append(f"사용자 결정 ({halted_stage.value}): {decision.text}")
        if decision.restart_from:
            state.stage = Stage(decision.restart_from)
            if state.stage == Stage.ARCHITECT:
                self._bump_design(state, decision.text or "사용자 재시작", full_reset=True)
        elif decision.action == "style_fix":
            self._new_revision_dependent(state, StyleChangeMode.STYLE_ONLY_REVISION, decision.text) if in_dependent else self._new_revision(state, StyleChangeMode.STYLE_ONLY_REVISION, decision.text)
        elif decision.action == "meaning_fix":
            self._new_revision_dependent(state, StyleChangeMode.DRAFTER_REVISION, decision.text) if in_dependent else self._new_revision(state, StyleChangeMode.DRAFTER_REVISION, decision.text)
        elif decision.action == "redesign":
            if in_dependent and state.dependent and decision.add_sources is None:
                self._bump_dependent_design(state, decision.text)
            else:
                self._bump_design(state, decision.text, full_reset=True)
        elif decision.action == "accept_unverified":
            h = state.halt
            if not h or h.kind != "UNVERIFIED" or (h.reason_code not in NON_GATING_UNVERIFIED):
                raise ProviderError("--accept-unverified는 비게이팅 UNVERIFIED(SPEC_NOT_PROVIDED/PRIOR_ART_NOT_PROVIDED)에만 허용된다")
            state.stage = halted_stage
        else:
            # plain decision: re-run the halted stage with the decision text in the packet
            state.stage = halted_stage
        state.halt = None
        self.store.save_state(state)
        return self.run(state)

    # ================================================================== core call
    def _ids(self, state: RunState) -> Identifiers:
        c = state.candidate
        d = state.dependent
        return Identifiers(
            candidate_id=c.candidate_id,
            revision=c.current.revision if c.current else c.revision,
            design_revision=c.current.design_revision if c.current else c.design_revision,
            dependent_set_id=d.dependent_set_id if d else None,
            dependent_design_revision=d.current.dependent_design_revision if d and d.current else (d.dependent_design_revision if d else None),
            dependent_revision=d.current.dependent_revision if d and d.current else (d.dependent_revision if d else None),
            input_revision=state.input_revision,
        )

    def _gen(self, role: str) -> GenParams:
        rc = self.cfg.role(role)
        return GenParams(rc.temperature, rc.thinking_level, rc.max_output_tokens or self.cfg.model.max_output_tokens)

    def _decision_section(self, state: RunState) -> str:
        if not state.notes:
            return ""
        return "### 사용자 결정·메모 (최신순)\n\n" + "\n".join(f"- {n}" for n in state.notes[-5:][::-1]) + "\n\n"

    def _with_decisions(self, state: RunState, text: str) -> str:
        """Volatile decision notes go just before the output contract so the stable prefix stays cacheable."""
        section = self._decision_section(state)
        if not section:
            return text
        marker = "### 출력 계약"
        i = text.rfind(marker)
        return text[:i] + section + text[i:] if i >= 0 else text + section

    def _call(
        self,
        state: RunState,
        role: str,
        scope: Scope,
        stage: Stage,
        kind: str,
        packet: packets.Packet,
        ids: Identifiers,
        rid: str,
        expected_exact: str | None = None,
        use_tools: bool = False,
        blind: bool = False,
        loop_index: int = 0,
        expected_reuse: int = 1,
    ) -> tuple[RoleEnvelope, RecordRef]:
        spec_role = self.roles.get(role)
        prompt = self.assembler.assemble(spec_role, scope)
        rc = self.cfg.role(role)
        contract = contract_for(role, scope)
        self._guard_budget(state, stage)
        with self._lock:
            state.call_seq += 1
            seq = state.call_seq
        packet_text = packet.text if blind else self._with_decisions(state, packet.text)
        tool_log = ToolLog()
        tools = None
        if use_tools and rc.tools and rc.aux_mode == "two_phase" and not blind:
            tools = make_tools(self.sources, tool_log)
            # Light tool phase: only the 07 gate and the catalog, no drawings, low thinking, short output.
            # The JSON main call that follows carries the full context.
            phase_a = CallSpec(
                role=role, scope=scope.value, model=self.cfg.model_for(role), system_instruction=prompt.system_instruction,
                packet_text=packet_text + "\n### 도구 단계\n\n07 §3 조건이 성립하면 도구로 정확 조각을 검색하고, 검색이 끝났거나 필요 없으면 `SEARCH_DONE`만 출력한다. 이 단계에서는 JSON을 출력하지 않는다.\n",
                sources_block=self.assembler.sources_subset(spec_role, scope, ["README", "S07"]), images=[], json_schema=None, tools=tools,
                gen=GenParams(rc.temperature, rc.aux_thinking_level, rc.aux_max_output_tokens),
                use_cache=False, phase="tool_phase", stage=stage.value, run_id=state.run_id, seq=seq,
            )
            res_a = self.provider.generate(phase_a)
            self._telemetry_tool(state, phase_a, res_a, ids, rid, tool_log)
            packet_text += "\n### 보조 소스 사용 기록 (오케스트레이터 도구 로그; 이 내용을 그대로 반영)\n\n" + tool_log.render() + "\n"
            self._tool_logs[rid] = tool_log
            self._guard_budget(state, stage)
        spec = CallSpec(
            role=role, scope=scope.value, model=self.cfg.model_for(role), system_instruction=prompt.system_instruction,
            packet_text=packet_text, sources_block=prompt.sources_block, images=list(packet.images), json_schema=ENVELOPE_JSON_SCHEMA,
            tools=None, gen=self._gen(role), use_cache=(rc.cache and self.cfg.cache.enabled and not blind), phase="main",
            stage=stage.value, run_id=state.run_id, seq=seq, meta={"record_id": rid, "kind": kind, "target_claim_id": ids.target_claim_id},
            cache_packet_text=packet.cache_text if not blind else "", cache_images=packet.cache_images and not blind, expected_reuse=expected_reuse,
        )
        env, result, repair_used, problems = self._generate_validated(spec, contract, ids, rid, expected_exact, prompt.source_paths, blind)
        if tool_log.calls:
            env.aux_source_usage = "USED" if tool_log.used else "NOT_ACTIVATED"
        elif use_tools:
            env.aux_source_usage = env.aux_source_usage or "NOT_ACTIVATED"

        scope_problem = None
        requested = parse_target(state.request.get("dependent_target")) if scope == Scope.DEPENDENT_SET else None
        if requested and env.status in (Status.PASS, Status.PASS_RANGE):
            actual = None
            if stage == Stage.DEP_ARCHITECT:
                planned = [x.planned_claim_no for x in env.candidates if x.classification.value == "TECHNICAL_SOLUTION_CANDIDATE"]
                actual = set(planned)
                if len(planned) != len(actual):
                    scope_problem = "종속항 설계 번호가 중복되었습니다."
            elif stage in (Stage.DEP_DRAFT, Stage.DEP_STYLE):
                actual = {x.claim_no for x in env.claims}
                if len(actual) != len(env.claims):
                    scope_problem = "종속항 산출 번호가 중복되었습니다."
                try:
                    parsed_numbers = {x.claim_no for x in parse_claim_set(env.exact_claim_text or "")}
                except ClaimParseError:
                    parsed_numbers = set()
                if parsed_numbers != actual:
                    scope_problem = "종속항 구조화 목록과 exact 문언의 항 번호가 다릅니다."
            if actual is not None and actual != requested:
                scope_problem = f"요청한 종속항 {sorted(requested)}와 산출 항 번호 {sorted(str(x) for x in actual)}가 다릅니다. 임의 범위 확대/누락을 허용하지 않습니다."
        issued = contract.passes(env, state.request_mode) if role in REVIEWER_ROLES or role == "blind-claim-reconstruction-reviewer" else env.status in (Status.PASS, Status.PASS_RANGE)
        evidence_problem = None
        unconfirmed = env.unconfirmed_evidence()
        if unconfirmed and env.status in (Status.PASS, Status.PASS_RANGE):
            # 조건 4: 근거표 공란 0건. A PASS with UNCONFIRMED evidence rows contradicts itself → REVIEW.
            evidence_problem = f"EVIDENCE_UNCONFIRMED: 근거 미확인 한정 {len(unconfirmed)}건이 PASS 판정과 함께 보고됨: " + "; ".join(unconfirmed[:5])
            issued = False
        if scope_problem:
            issued = False
        text_for_hash = env.exact_claim_text if env.exact_claim_text else expected_exact
        ref = RecordRef(
            record_id=rid, kind=kind, role=role, scope=scope.value, stage=stage.value, status=env.status.value,
            execution_status=env.execution_status.value, gates=env.gates.present(), issued=issued,
            text_sha256=exact_sha256(text_for_hash.strip()) if text_for_hash else None, ids=ids.as_dict(), source_set_id=state.source_set_id,
            invention_primary=env.invention_type.primary.value if env.invention_type else None,
            evidence_counts={b: sum(1 for e in env.limitation_evidence if e.basis.value == b) for b in ("DIRECT", "DERIVED", "UNCONFIRMED")} if env.limitation_evidence else {},
        )
        call_payload = {
            "seq": seq, "role": role, "scope": scope.value, "stage": stage.value, "record_id": rid, "model": spec.model,
            "context_transport": {
                "shared_chars": len(spec.cache_packet_text), "packet_chars": len(spec.packet_text), "shared_images": len(spec.images) if spec.cache_images else 0,
                "images": len(spec.images), "image_transport": result.raw.get("image_transport"), "inline_image_bytes": result.raw.get("inline_image_bytes", sum(len(i.data) for i in spec.images)),
            },
            "scope_problem": scope_problem,
            "system_sha": spec.system_sha, "sources_sha": spec.sources_sha, "packet_sha": spec.packet_sha, "packet_text": packet_text,
            "preloaded_sources": prompt.source_paths, "tool_log": tool_log.calls, "response": result.as_dict(), "repair_used": repair_used,
            "cross_check_problems": problems, "envelope": env.model_dump(mode="json"),
        }
        ref.call_file = self.store.write_call(state.run_id, seq, role, scope.value, call_payload, env.report_markdown)
        record_payload = {
            "record_id": rid, "kind": kind, "role": role, "scope": scope.value, "stage": stage.value, "ids": ids.as_dict(),
            "status": env.status.value, "execution_status": env.execution_status.value, "gates": env.gates.present(),
            "gate_reasons": [g.model_dump() for g in env.gate_reasons], "checks": [c.model_dump() for c in env.checks],
            "issued": issued, "exact_claim_text": env.exact_claim_text,
            "claims": [c.model_dump() for c in env.claims], "candidates": [c.model_dump() for c in env.candidates],
            "per_claim_gates": [g.model_dump() for g in env.per_claim_gates], "open_issues": [o.model_dump() for o in env.open_issues],
            "limitation_evidence": [e.model_dump(mode="json") for e in env.limitation_evidence], "evidence_problem": evidence_problem,
            "source_set_id": state.source_set_id, "call_file": ref.call_file, "aux_source_usage": env.aux_source_usage,
            "tool_log": tool_log.render() if tool_log.calls else "조건부 보조 소스: NOT_ACTIVATED", "report_markdown": env.report_markdown,
        }
        self.store.write_record(state.run_id, rid, record_payload, env.report_markdown)
        with self._lock:
            state.records[rid] = ref
            self._telemetry(state, spec, result, env, ids, loop_index, repair_used)
            for r_stage, r_model, r_res in self._pending_repairs:
                self._account(state, r_stage, r_model, r_res.usage, r_res.latency_ms, r_res.cache_hit)
            self._pending_repairs.clear()
            if self.shadow_hook is not None and not blind:
                try:
                    self.shadow_hook(spec, env, state)
                except Exception as exc:  # noqa: BLE001 - shadow never affects the run
                    state.notes.append(f"shadow 실패 ({role}): {exc}")
            self.store.save_state(state)
        if scope_problem:
            raise PipelineHalt(Halt(stage=stage.value, role=role, kind="REVIEW", reason_code="OTHER", message="REQUEST_SCOPE_MISMATCH: " + scope_problem, record_id=rid))
        if evidence_problem:
            raise PipelineHalt(Halt(stage=stage.value, role=role, kind="REVIEW", reason_code="OTHER", message=evidence_problem, record_id=rid,
                                    open_issues=[{"kind": "EVIDENCE_UNCONFIRMED", "code": "COND4", "text": lim, "return_to": None} for lim in unconfirmed]))
        return env, ref

    def _guard_budget(self, state: RunState, stage: Stage) -> None:
        with self._lock:
            over = budget_violation(state.usage, self.cfg)
        if over:
            raise PipelineHalt(Halt(stage=stage.value, role="engine", kind="BUDGET_LIMIT", message=over + " — 한도를 올려 `claim-agent resume <run_id> --max-calls N --max-cost-usd X`로 재개"))

    def _generate_validated(self, spec: CallSpec, contract, ids: Identifiers, rid: str, expected_exact: str | None, preloaded: list[str], blind: bool):
        from .verify import cross_check

        repair_used = False
        problems: list[str] = []
        result = self.provider.generate(spec)
        env = self._parse(result)
        for attempt in range(2):
            if env is not None:
                chk = cross_check(env, contract, ids, rid, expected_exact, None if blind else preloaded, require_report=True)
                problems = chk.problems
                if chk.ok:
                    return env, result, repair_used, []
            if attempt == 1:
                break
            repair_used = True
            reason = "JSON 파싱 실패 또는 스키마 위반" if env is None else "봉투와 보고서 불일치: " + "; ".join(problems)
            trunc = "" if result.finish_reason not in ("MAX_TOKENS", "LENGTH") else " 이전 출력이 잘렸으므로 표를 축약해 길이를 줄인다."
            repair_spec = CallSpec(
                role=spec.role, scope=spec.scope, model=spec.model, system_instruction=spec.system_instruction,
                packet_text=spec.packet_text + f"\n### 복구 지시\n\n이전 출력 문제: {reason}.{trunc} 같은 판정을 유지하되 스키마를 정확히 따르고 `gates`·`status`·`exact_claim_text`를 보고서와 일치시켜 JSON 하나만 다시 출력한다.\n",
                sources_block=spec.sources_block, images=spec.images, json_schema=spec.json_schema, tools=None, gen=spec.gen,
                use_cache=spec.use_cache, phase="repair", stage=spec.stage, run_id=spec.run_id, seq=spec.seq, meta=spec.meta,
                cache_packet_text=spec.cache_packet_text, cache_images=spec.cache_images,
            )
            result = self.provider.generate(repair_spec)
            with self._lock:
                self._account_result(spec.stage, spec.model, result)
            env = self._parse(result)
        halt_kind = "ENVELOPE_INVALID" if env is None else "ENVELOPE_REPORT_MISMATCH"
        raise PipelineHalt(Halt(stage=spec.stage, role=spec.role, kind=halt_kind, message="; ".join(problems) or "envelope invalid", record_id=rid))

    def _account_result(self, stage: str, model: str, result: CallResult) -> None:
        """Account a provider call that produced no telemetry row of its own (repair attempts)."""
        self._pending_repairs.append((stage, model, result))

    @staticmethod
    def _parse(result: CallResult) -> RoleEnvelope | None:
        data = result.parsed
        if data is None:
            from ..provider.base import parse_json_text

            data = parse_json_text(result.text)
        if data is None:
            return None
        try:
            return RoleEnvelope.model_validate(data)
        except Exception:  # noqa: BLE001 - pydantic validation error
            return None

    def _telemetry(self, state: RunState, spec: CallSpec, result: CallResult, env: RoleEnvelope, ids: Identifiers, loop_index: int, repair_used: bool) -> None:
        usage = result.usage
        row = TelemetryRow(
            ts=now(), run_id=state.run_id, seq=spec.seq, stage=spec.stage, role=spec.role, scope=spec.scope, request_mode=state.request_mode.value,
            model=spec.model, provider=result.provider, variant_id=state.variant_id, source_set_id=state.source_set_id, lessons_hash=state.lessons_hash,
            candidate_id=ids.candidate_id, revision=ids.revision, design_revision=ids.design_revision, dependent_set_id=ids.dependent_set_id,
            dependent_revision=ids.dependent_revision, target_claim_id=ids.target_claim_id, loop_index=loop_index, retry_count=0,
            prompt_tokens=usage.get("prompt_tokens", 0), cached_tokens=usage.get("cached_tokens", 0), thoughts_tokens=usage.get("thoughts_tokens", 0),
            output_tokens=usage.get("output_tokens", 0), latency_ms=result.latency_ms, cache_hit=result.cache_hit, parse_ok=True, repair_used=repair_used,
            execution_status=env.execution_status.value, status=env.status.value, reason_code=env.reason_code, gates=env.gates.present(),
            non_pass_checks=env.non_pass_checks(), next_step=env.next_step.value, handoff_ready=env.handoff_ready,
            invention_primary=env.invention_type.primary.value if env.invention_type else None,
            cost_estimate=estimate_cost(spec.model, usage, self.cfg.telemetry.pricing), phase=spec.phase,
            record_id=(spec.meta or {}).get("record_id"),
        )
        TelemetryWriter(self.store.telemetry_path(state.run_id), self.cfg.telemetry.enabled).write(row)
        self._account(state, spec.stage, spec.model, usage, result.latency_ms, result.cache_hit)

    def _account(self, state: RunState, stage: str, model: str, usage: dict[str, int], latency_ms: int, cache_hit: bool) -> None:
        cost = estimate_cost(model, usage, self.cfg.telemetry.pricing)
        state.add_usage(stage, {
            "calls": 1, "prompt_tokens": usage.get("prompt_tokens", 0), "cached_tokens": usage.get("cached_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0), "thoughts_tokens": usage.get("thoughts_tokens", 0),
            "latency_ms": latency_ms, "cost_usd": cost or 0.0, "cache_hits": 1 if cache_hit else 0, "unpriced_calls": 0 if cost is not None else 1,
        })

    def _telemetry_tool(self, state: RunState, spec: CallSpec, result: CallResult, ids: Identifiers, record_id: str, tool_log: ToolLog) -> None:
        """One row for the tool-phase call itself, then one row per restricted corpus tool call.

        Query text is never written: only its length, so the feedback pipeline can see
        tool misuse (refusals, empty results) without storing invention wording.
        """
        writer = TelemetryWriter(self.store.telemetry_path(state.run_id), self.cfg.telemetry.enabled)
        usage = result.usage

        def base(phase: str, tool: dict[str, Any] | None = None, **over: Any) -> TelemetryRow:
            kw: dict[str, Any] = dict(
                ts=now(), run_id=state.run_id, seq=spec.seq, stage=spec.stage, role=spec.role, scope=spec.scope,
                request_mode=state.request_mode.value, model=spec.model, provider=result.provider, variant_id=state.variant_id,
                source_set_id=state.source_set_id, lessons_hash=state.lessons_hash, candidate_id=ids.candidate_id,
                revision=ids.revision, design_revision=ids.design_revision, dependent_set_id=ids.dependent_set_id,
                dependent_revision=ids.dependent_revision, target_claim_id=ids.target_claim_id, loop_index=0, retry_count=0,
                prompt_tokens=0, cached_tokens=0, thoughts_tokens=0, output_tokens=0, latency_ms=0, cache_hit=False,
                parse_ok=True, repair_used=False, execution_status="RUN", status="PASS", reason_code=None,
                phase=phase, record_id=record_id, tool=tool or {},
            )
            kw.update(over)
            return TelemetryRow(**kw)

        writer.write(
            base(
                "tool_phase",
                prompt_tokens=usage.get("prompt_tokens", 0), cached_tokens=usage.get("cached_tokens", 0),
                thoughts_tokens=usage.get("thoughts_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                latency_ms=result.latency_ms, cache_hit=result.cache_hit,
                cost_estimate=estimate_cost(spec.model, usage, self.cfg.telemetry.pricing),
                tool={"calls": len(tool_log.calls), "activated": tool_log.used},
            )
        )
        self._account(state, spec.stage, spec.model, usage, result.latency_ms, result.cache_hit)
        for call in tool_log.calls:
            results = call.get("results") or []
            writer.write(
                base(
                    "tool",
                    tool={
                        "name": call.get("name", ""),
                        "mode": call.get("mode"),
                        "query_len": len(call.get("query") or ""),
                        "results": len(results),
                        "refused": bool(call.get("refused")),
                        "flagged_review": any(r.get("flagged_review") for r in results),
                    },
                )
            )

    # ================================================================== transitions
    def _feedback(self, env: RoleEnvelope, rid: str) -> str:
        parts = [f"[{o.kind}] {o.code} {o.text}".strip() for o in env.open_issues]
        head = "\n".join(parts) if parts else (env.reason_code or env.status.value)
        return f"{head}\n\n### 반환 보고서 전문 ({rid})\n\n{env.report_markdown}"

    def _halt(self, state: RunState, stage: Stage, role: str, tr: Transition, env: RoleEnvelope, rid: str) -> None:
        raise PipelineHalt(
            Halt(
                stage=stage.value, role=role, kind=tr.halt_kind or "REVIEW", reason_code=env.reason_code, message=tr.reason,
                open_issues=[o.model_dump() for o in env.open_issues], return_to=tr.return_to.value if tr.return_to else None, record_id=rid,
                report_markdown=env.report_markdown,
            )
        )

    def _decide(self, state: RunState, env: RoleEnvelope, role: str, scope: Scope, stage: Stage, rid: str, loop_counts: dict[str, int]) -> Transition:
        tr = decide(env, contract_for(role, scope), state.request_mode, loop_counts, self.cfg.pipeline.max_return_loops)
        if tr.kind == "HALT":
            self._halt(state, stage, role, tr, env, rid)
        return tr

    def _new_revision(self, state: RunState, mode: StyleChangeMode, feedback: str) -> None:
        c = state.candidate
        cur = c.current
        assert cur is not None
        c.history.append(cur)
        state.mark_superseded([r for k, r in cur.records.items() if k not in ("design",)])
        nxt = RevisionState(
            revision=bump_revision(cur.revision), design_revision=cur.design_revision, style_change_mode=mode, change_goal=feedback,
            prior_revision=cur.revision, meaning_draft_text=cur.meaning_draft_text if mode == StyleChangeMode.STYLE_ONLY_REVISION else None,
        )
        c.current = nxt
        c.revision = nxt.revision
        c.draft_claim_lock = None
        c.final_claim_lock = None
        if state.dependent:
            state.dependent.stale = True
        state.total_loops += 1
        state.stage = Stage.STYLE if mode == StyleChangeMode.STYLE_ONLY_REVISION else Stage.DRAFT

    def _bump_design(self, state: RunState, goal: str, full_reset: bool) -> None:
        c = state.candidate
        if c.current:
            c.history.append(c.current)
            state.mark_superseded(list(c.current.records.values()))
        if c.design_record_id:
            state.mark_superseded([c.design_record_id])
        c.design_revision = bump_design_revision(c.design_revision)
        c.revision = bump_revision(c.current.revision if c.current else c.revision)
        c.current = None
        c.design_record_id = None
        c.draft_claim_lock = None
        c.final_claim_lock = None
        c.loop_counts.setdefault("_redesign_goal", 0)
        state.notes.append(f"설계 변경 목표 ({c.design_revision}): {goal}")
        if state.dependent:
            state.dependent.stale = True
        state.total_loops += 1
        state.stage = Stage.ARCHITECT

    def _apply_return(self, state: RunState, tr: Transition, env: RoleEnvelope, rid: str, dependent: bool) -> None:
        key = LOOP_KEY[tr.return_to]  # type: ignore[index]
        counts = state.dependent.loop_counts if dependent and state.dependent else state.candidate.loop_counts
        counts[key] = counts.get(key, 0) + 1
        fb = self._feedback(env, rid)
        if tr.return_to == NextStep.RETURN_TO_ARCHITECT:
            self._bump_design(state, fb, full_reset=True)
        elif tr.return_to == NextStep.RETURN_TO_DEPENDENT_ARCHITECT:
            self._bump_dependent_design(state, fb)
        elif dependent:
            self._new_revision_dependent(state, StyleChangeMode.STYLE_ONLY_REVISION if tr.return_to == NextStep.RETURN_TO_STYLE_ADJUSTER else StyleChangeMode.DRAFTER_REVISION, fb)
        else:
            self._new_revision(state, StyleChangeMode.STYLE_ONLY_REVISION if tr.return_to == NextStep.RETURN_TO_STYLE_ADJUSTER else StyleChangeMode.DRAFTER_REVISION, fb)
        state.notes.append(f"{tr.return_to.value} ← {rid}")  # type: ignore[union-attr]
        self.store.save_state(state)

    # ================================================================== main loop
    def _loop(self, state: RunState, bundle: MaterialBundle) -> None:
        guard = 0
        while state.stage not in (Stage.DONE, Stage.HALTED):
            guard += 1
            if guard > 200:
                raise PipelineHalt(Halt(stage=state.stage.value, role="engine", kind="LOOP_LIMIT", message="stage guard exceeded"))
            if state.total_loops > sum(self.cfg.pipeline.max_return_loops.values()) + 2:
                raise PipelineHalt(Halt(stage=state.stage.value, role="engine", kind="LOOP_LIMIT", message="total return loops exceeded"))
            st = state.stage
            if st in (Stage.ARCHITECT, Stage.DRAFT, Stage.STYLE, Stage.SUCCESS, Stage.SYNTAX, Stage.OA, Stage.BLIND, Stage.PICTURE, Stage.LOCK):
                self._independent_step(state, bundle)
            else:
                self._dependent_step(state, bundle)
            self.store.save_state(state)

    # ------------------------------------------------------------------ independent
    def _independent_step(self, state: RunState, bundle: MaterialBundle) -> None:
        c = state.candidate
        st = state.stage
        req = RunRequest.model_validate(state.request)
        if st == Stage.ARCHITECT:
            ids = Identifiers(c.candidate_id, c.revision, c.design_revision, input_revision=state.input_revision)
            rid = record_id("design", ids)
            goal = next((n.split(": ", 1)[1] for n in reversed(state.notes) if n.startswith(f"설계 변경 목표 ({c.design_revision})")), None)
            env, ref = self._call(state, "claim-architect", Scope.INDEPENDENT, st, "design", packets.architect(state, bundle, ids, rid, req.request_text, goal), ids, rid, loop_index=c.loop_counts.get("ARCHITECT", 0))
            tr = self._decide(state, env, "claim-architect", Scope.INDEPENDENT, st, rid, c.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=False)
                return
            c.design_record_id = rid
            c.current = RevisionState(revision=c.revision, design_revision=c.design_revision)
            state.stage = Stage.DRAFT
            return

        cur = c.current
        assert cur is not None, "no current revision"
        ids = self._ids(state)
        if st == Stage.DRAFT:
            if cur.style_change_mode == StyleChangeMode.STYLE_ONLY_REVISION:
                state.stage = Stage.STYLE
                return
            rid = record_id("meaning_draft", ids)
            prior = self._prior_text(state)
            pk = packets.drafter_independent(state, self.store, bundle, ids, rid, c.design_record_id or "", prior if cur.style_change_mode == StyleChangeMode.DRAFTER_REVISION else None, cur.change_goal)
            env, ref = self._call(state, "claim-drafter", Scope.INDEPENDENT, st, "meaning_draft", pk, ids, rid, loop_index=c.loop_counts.get("DRAFTER", 0))
            tr = self._decide(state, env, "claim-drafter", Scope.INDEPENDENT, st, rid, c.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=False)
                return
            cur.meaning_draft_text = env.exact_claim_text
            cur.records["meaning_draft"] = rid
            state.stage = Stage.STYLE
            return

        if st == Stage.STYLE:
            rid = record_id("style", ids)
            prior_style = self._prior_record(state, "style")
            pk = packets.style_independent(state, self.store, bundle, ids, rid, c.design_record_id or "", cur.records.get("meaning_draft"), cur.style_change_mode, self._prior_text(state), prior_style, cur.change_goal)
            env, ref = self._call(state, "claim-style-adjuster", Scope.INDEPENDENT, st, "style", pk, ids, rid, use_tools=True, loop_index=c.loop_counts.get("STYLE_ONLY", 0))
            tr = self._decide(state, env, "claim-style-adjuster", Scope.INDEPENDENT, st, rid, c.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=False)
                return
            text = (env.exact_claim_text or "").strip()
            if not contains_user_lock(text, bundle.user_lock):
                raise PipelineHalt(Halt(stage=st.value, role="engine", kind="BLOCK", reason_code="USER_LOCK_NOT_PRESERVED", message="USER_LOCK 문언이 최종 문언에 그대로 남아 있지 않다", record_id=rid))
            cur.exact_text = text
            cur.exact_sha256 = exact_sha256(text)
            cur.records["style"] = rid
            state.stage = Stage.SUCCESS
            return

        exact = cur.exact_text or ""
        if st == Stage.SUCCESS:
            rid = record_id("success", ids)
            frags = self._corpus_fragments(cur.records.get("style"))
            pk = packets.success_independent(state, self.store, bundle, ids, rid, c.design_record_id or "", cur.records.get("meaning_draft"), cur.records.get("style", ""), exact, frags)
            env, ref = self._call(state, "claim-success-reviewer", Scope.INDEPENDENT, st, "success", pk, ids, rid, expected_exact=exact)
            tr = self._decide(state, env, "claim-success-reviewer", Scope.INDEPENDENT, st, rid, c.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=False)
                return
            cur.records["success"] = rid
            state.stage = Stage.SYNTAX
            return

        if st == Stage.SYNTAX:
            rid = record_id("syntax", ids)
            baseline = self._prior_text(state) if cur.style_change_mode != StyleChangeMode.INITIAL_FROM_DRAFTER else cur.meaning_draft_text
            pk = packets.syntax_independent(state, self.store, bundle, ids, rid, c.design_record_id or "", cur.records.get("style", ""), cur.records.get("success", ""), exact, baseline, cur.style_change_mode)
            env, ref = self._call(state, "syntax-scope-reviewer", Scope.INDEPENDENT, st, "syntax", pk, ids, rid, expected_exact=exact)
            tr = self._decide(state, env, "syntax-scope-reviewer", Scope.INDEPENDENT, st, rid, c.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=False)
                return
            cur.records["syntax"] = rid
            state.stage = Stage.OA
            return

        if st == Stage.OA:
            rid = record_id("oa", ids)
            pk = packets.oa_independent(state, self.store, bundle, ids, rid, c.design_record_id or "", cur.records.get("style", ""), cur.records.get("success", ""), cur.records.get("syntax", ""), exact)
            env, ref = self._call(state, "oa-strategy-reviewer", Scope.INDEPENDENT, st, "oa", pk, ids, rid, expected_exact=exact)
            tr = self._decide(state, env, "oa-strategy-reviewer", Scope.INDEPENDENT, st, rid, c.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=False)
                return
            cur.records["oa"] = rid
            state.stage = Stage.BLIND   # hard rule: OA_FINAL UNVERIFIED never blocks reconstruction
            return

        if st == Stage.BLIND:
            rid = record_id("blind", ids)
            bp = BlindPacket(claim_scope="INDEPENDENT", candidate_id=ids.candidate_id, revision=ids.revision, design_revision=ids.design_revision, record_id=rid, claim_text=exact)
            env, ref = self._call(state, "blind-claim-reconstruction-reviewer", Scope.INDEPENDENT, st, "blind", packets.Packet(bp.render()), ids, rid, expected_exact=None, blind=True)
            if env.execution_status != ExecStatus.BLIND_COMPLETE:
                raise PipelineHalt(Halt(stage=st.value, role="blind-claim-reconstruction-reviewer", kind="UNVERIFIED", reason_code=env.reason_code, message="blind snapshot not complete", record_id=rid))
            cur.records["blind"] = rid
            state.stage = Stage.PICTURE
            return

        if st == Stage.PICTURE:
            rid = record_id("reference_compare", ids)
            pk = packets.picture_independent(state, self.store, bundle, ids, rid, c.design_record_id or "", cur.records.get("style", ""), cur.records.get("oa", ""), cur.records.get("blind", ""), exact)
            env, ref = self._call(state, "picture-claim-reconstruction-reviewer", Scope.INDEPENDENT, st, "reference_compare", pk, ids, rid, expected_exact=exact)
            tr = self._decide(state, env, "picture-claim-reconstruction-reviewer", Scope.INDEPENDENT, st, rid, c.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=False)
                return
            cur.records["reference_compare"] = rid
            state.stage = Stage.LOCK
            return

        if st == Stage.LOCK:
            oa_ref = state.record(cur.records.get("oa"))
            final = state.request_mode == RequestMode.FINALIZATION and oa_ref is not None and oa_ref.gates.get("OA_FINAL_GATE") == "PASS"
            kind = "final_claim_lock" if final else "draft_claim_lock"
            lock_id = record_id(kind, ids)
            lock, md = build_claim_lock(state, lock_id, final, exact, [m["name"] for m in state.material_meta])
            self.store.write_record(state.run_id, lock_id, {**lock, "report_markdown": md}, md)
            state.records[lock_id] = RecordRef(record_id=lock_id, kind=kind, role="engine", scope="INDEPENDENT", stage=st.value, status="LOCKED", gates={}, text_sha256=cur.exact_sha256, ids=ids.as_dict(), source_set_id=state.source_set_id)
            cur.records[kind] = lock_id
            if final:
                c.final_claim_lock = lock_id
            else:
                c.draft_claim_lock = lock_id
                if state.request_mode == RequestMode.FINALIZATION:
                    state.notes.append("FINALIZATION 요청이지만 OA_FINAL_GATE가 PASS가 아니므로 DRAFT_CLAIM_LOCK만 기록")
            if req.dependent:
                if state.request_mode == RequestMode.FINALIZATION and not final:
                    raise PipelineHalt(Halt(stage=st.value, role="engine", kind="UNVERIFIED", reason_code="SPEC_NOT_PROVIDED", message="FINALIZATION 종속항은 유효한 FINAL_CLAIM_LOCK이 필요하다", record_id=lock_id))
                state.stage = Stage.DEP_ARCHITECT
            else:
                state.stage = Stage.DONE
                state.outcome = "FINAL_CLAIM_LOCK" if final else "DRAFT_CLAIM_LOCK"
            return

    def _prior_text(self, state: RunState) -> str | None:
        for rs in reversed(state.candidate.history):
            if rs.exact_text or rs.meaning_draft_text:
                return rs.exact_text or rs.meaning_draft_text
        return None

    def _prior_record(self, state: RunState, kind: str) -> str | None:
        for rs in reversed(state.candidate.history):
            if kind in rs.records:
                return rs.records[kind]
        return None

    def _corpus_fragments(self, style_record_id: str | None) -> str | None:
        if not style_record_id:
            return None
        log = self._tool_logs.get(style_record_id)
        if log and log.used:
            return "### 라우팅 인덱스 전문\n\n" + self.sources.get("ROUTING").text + "\n\n### 사용 조각\n\n" + log.render()
        return None

    # ------------------------------------------------------------------ dependent
    def _ensure_dependent(self, state: RunState) -> DependentSetState:
        c = state.candidate
        req = RunRequest.model_validate(state.request)
        root_lock = c.final_claim_lock or c.draft_claim_lock
        if not root_lock or not c.current or not c.current.exact_text:
            raise PipelineHalt(Halt(stage=state.stage.value, role="engine", kind="BLOCK", reason_code="ROOT_LOCK_MISSING_OR_STALE", message="유효한 루트 독립항 LOCK이 없다"))
        d = state.dependent
        if d is None or d.stale or d.root_lock_id != root_lock:
            d = DependentSetState(
                dependent_set_id=req.dependent_set_id or f"{c.candidate_id}-dep", root_lock_id=root_lock, root_candidate_id=c.candidate_id,
                root_revision=c.current.revision, root_design_revision=c.current.design_revision,
            )
            state.dependent = d
        return d

    def _new_revision_dependent(self, state: RunState, mode: StyleChangeMode, feedback: str) -> None:
        d = state.dependent
        assert d is not None and d.current is not None
        cur = d.current
        d.history.append(cur)
        state.mark_superseded(list(cur.records.values()))
        nxt = DependentRevisionState(
            dependent_revision=bump_dependent_revision(cur.dependent_revision), dependent_design_revision=cur.dependent_design_revision,
            style_change_mode=mode, change_goal=feedback, meaning_draft_text=cur.meaning_draft_text if mode == StyleChangeMode.STYLE_ONLY_REVISION else None,
        )
        d.current = nxt
        d.dependent_revision = nxt.dependent_revision
        d.draft_set_lock = None
        d.final_set_lock = None
        state.total_loops += 1
        state.stage = Stage.DEP_STYLE if mode == StyleChangeMode.STYLE_ONLY_REVISION else Stage.DEP_DRAFT

    def _bump_dependent_design(self, state: RunState, goal: str) -> None:
        d = state.dependent
        assert d is not None
        if d.current:
            d.history.append(d.current)
            state.mark_superseded(list(d.current.records.values()))
        if d.design_record_id:
            state.mark_superseded([d.design_record_id])
        d.dependent_design_revision = bump_dependent_design_revision(d.dependent_design_revision)
        d.dependent_revision = bump_dependent_revision(d.current.dependent_revision if d.current else d.dependent_revision)
        d.current = None
        d.design_record_id = None
        d.draft_set_lock = None
        d.final_set_lock = None
        state.notes.append(f"종속항 설계 변경 목표 ({d.dependent_design_revision}): {goal}")
        state.total_loops += 1
        state.stage = Stage.DEP_ARCHITECT

    def _dep_prior_text(self, state: RunState) -> str | None:
        d = state.dependent
        if not d:
            return None
        for rs in reversed(d.history):
            if rs.exact_text or rs.meaning_draft_text:
                return rs.exact_text or rs.meaning_draft_text
        return None

    def _dep_prior_record(self, state: RunState, kind: str) -> str | None:
        d = state.dependent
        if not d:
            return None
        for rs in reversed(d.history):
            if kind in rs.records:
                return rs.records[kind]
        return None

    def _dependent_step(self, state: RunState, bundle: MaterialBundle) -> None:
        st = state.stage
        c = state.candidate
        req = RunRequest.model_validate(state.request)
        d = self._ensure_dependent(state)
        root_text = c.current.exact_text or ""  # type: ignore[union-attr]
        if st == Stage.DEP_ARCHITECT:
            ids = self._ids(state)
            ids.dependent_design_revision = d.dependent_design_revision
            ids.dependent_revision = d.dependent_revision
            rid = record_id("dependent_design", ids)
            goal = next((n.split(": ", 1)[1] for n in reversed(state.notes) if n.startswith(f"종속항 설계 변경 목표 ({d.dependent_design_revision})")), None)
            req_text = req.request_text + (f"\n\n종속항 목표 범위: {req.dependent_target}" if req.dependent_target else "")
            pk = packets.dep_architect(state, self.store, bundle, ids, rid, d.root_lock_id, c.design_record_id or "", root_text, req_text, goal)
            env, ref = self._call(state, "dependent-claim-strategy-architect", Scope.DEPENDENT_SET, st, "dependent_design", pk, ids, rid, loop_index=d.loop_counts.get("DEPENDENT_ARCHITECT", 0))
            tr = self._decide(state, env, "dependent-claim-strategy-architect", Scope.DEPENDENT_SET, st, rid, d.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=True)
                return
            d.design_record_id = rid
            d.current = DependentRevisionState(dependent_revision=d.dependent_revision, dependent_design_revision=d.dependent_design_revision)
            state.stage = Stage.DEP_DRAFT
            return

        cur = d.current
        assert cur is not None
        ids = self._ids(state)
        if st == Stage.DEP_DRAFT:
            if cur.style_change_mode == StyleChangeMode.STYLE_ONLY_REVISION:
                state.stage = Stage.DEP_STYLE
                return
            rid = record_id("dependent_meaning_draft", ids)
            pk = packets.drafter_dependent(state, self.store, bundle, ids, rid, d.root_lock_id, root_text, d.design_record_id or "", self._dep_prior_text(state) if cur.style_change_mode == StyleChangeMode.DRAFTER_REVISION else None, cur.change_goal)
            env, ref = self._call(state, "claim-drafter", Scope.DEPENDENT_SET, st, "dependent_meaning_draft", pk, ids, rid, loop_index=d.loop_counts.get("DRAFTER", 0))
            tr = self._decide(state, env, "claim-drafter", Scope.DEPENDENT_SET, st, rid, d.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=True)
                return
            cur.meaning_draft_text = env.exact_claim_text or "\n\n".join(cl.text for cl in env.claims)
            cur.records["dependent_meaning_draft"] = rid
            state.stage = Stage.DEP_STYLE
            return

        if st == Stage.DEP_STYLE:
            rid = record_id("dependent_style", ids)
            pk = packets.style_dependent(state, self.store, bundle, ids, rid, d.root_lock_id, root_text, d.design_record_id or "", cur.records.get("dependent_meaning_draft"), cur.style_change_mode, self._dep_prior_text(state), self._dep_prior_record(state, "dependent_style"), cur.change_goal)
            env, ref = self._call(state, "claim-style-adjuster", Scope.DEPENDENT_SET, st, "dependent_style", pk, ids, rid, use_tools=True, loop_index=d.loop_counts.get("STYLE_ONLY", 0))
            tr = self._decide(state, env, "claim-style-adjuster", Scope.DEPENDENT_SET, st, rid, d.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=True)
                return
            set_text = (env.exact_claim_text or "\n\n".join(cl.text for cl in env.claims)).strip()
            try:
                all_claims = parse_claim_set(root_text + "\n\n" + set_text)
            except ClaimParseError as exc:
                raise PipelineHalt(Halt(stage=st.value, role="engine", kind="BLOCK", reason_code="OTHER", message=f"CLAIM_PARSE_FAILED: {exc}", record_id=rid)) from exc
            problems = validate_parent_refs(all_claims)
            if problems:
                raise PipelineHalt(Halt(stage=st.value, role="engine", kind="BLOCK", reason_code="OTHER", message="인용관계 오류: " + "; ".join(problems), record_id=rid))
            if not contains_user_lock(root_text + "\n" + set_text, bundle.user_lock):
                raise PipelineHalt(Halt(stage=st.value, role="engine", kind="BLOCK", reason_code="USER_LOCK_NOT_PRESERVED", message="USER_LOCK 문언 미보존", record_id=rid))
            dc_by_no = {cl.claim_no: cl.dc_id for cl in env.claims}
            cur.exact_text = set_text
            cur.exact_sha256 = exact_sha256(set_text)
            cur.claims = [{"claim_no": cl.claim_no, "parent_claim_no": cl.parent_no, "dc_id": dc_by_no.get(cl.claim_no), "text": cl.text} for cl in all_claims if not cl.is_independent]
            cur.records["dependent_style"] = rid
            state.stage = Stage.DEP_SUCCESS
            return

        set_text = cur.exact_text or ""
        if st == Stage.DEP_SUCCESS:
            rid = record_id("dependent_success", ids)
            pk = packets.success_dependent(state, self.store, bundle, ids, rid, d.root_lock_id, root_text, d.design_record_id or "", cur.records.get("dependent_meaning_draft"), cur.records.get("dependent_style", ""), set_text, self._corpus_fragments(cur.records.get("dependent_style")))
            env, ref = self._call(state, "claim-success-reviewer", Scope.DEPENDENT_SET, st, "dependent_success", pk, ids, rid, expected_exact=set_text)
            tr = self._decide(state, env, "claim-success-reviewer", Scope.DEPENDENT_SET, st, rid, d.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=True)
                return
            cur.records["dependent_success"] = rid
            state.stage = Stage.DEP_SYNTAX
            return

        if st == Stage.DEP_SYNTAX:
            rid = record_id("dependent_syntax", ids)
            baseline = self._dep_prior_text(state) if cur.style_change_mode != StyleChangeMode.INITIAL_FROM_DRAFTER else cur.meaning_draft_text
            pk = packets.syntax_dependent(state, self.store, bundle, ids, rid, d.root_lock_id, root_text, d.design_record_id or "", cur.records.get("dependent_style", ""), cur.records.get("dependent_success", ""), set_text, baseline, cur.style_change_mode)
            env, ref = self._call(state, "syntax-scope-reviewer", Scope.DEPENDENT_SET, st, "dependent_syntax", pk, ids, rid, expected_exact=set_text)
            tr = self._decide(state, env, "syntax-scope-reviewer", Scope.DEPENDENT_SET, st, rid, d.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=True)
                return
            cur.records["dependent_syntax"] = rid
            state.stage = Stage.DEP_OA
            return

        if st == Stage.DEP_OA:
            rid = record_id("dependent_oa", ids)
            pk = packets.oa_dependent(state, self.store, bundle, ids, rid, d.root_lock_id, root_text, d.design_record_id or "", cur.records.get("dependent_style", ""), cur.records.get("dependent_success", ""), cur.records.get("dependent_syntax", ""), set_text)
            env, ref = self._call(state, "oa-strategy-reviewer", Scope.DEPENDENT_SET, st, "dependent_oa", pk, ids, rid, expected_exact=set_text)
            tr = self._decide(state, env, "oa-strategy-reviewer", Scope.DEPENDENT_SET, st, rid, d.loop_counts)
            if tr.kind == "RETURN":
                self._apply_return(state, tr, env, rid, dependent=True)
                return
            cur.records["dependent_oa"] = rid
            state.stage = Stage.DEP_RECON
            return

        if st == Stage.DEP_RECON:
            self._dependent_reconstruction(state, bundle, d, cur, root_text, set_text)
            return

        if st == Stage.DEP_LOCK:
            oa_ref = state.record(cur.records.get("dependent_oa"))
            final = state.request_mode == RequestMode.FINALIZATION and oa_ref is not None and oa_ref.gates.get("DEPENDENT_OA_FINAL_GATE") == "PASS"
            kind = "final_dependent_set_lock" if final else "draft_dependent_set_lock"
            lock_id = record_id(kind, ids)
            lock, md = build_dependent_set_lock(state, lock_id, final, root_text, set_text, [m["name"] for m in state.material_meta])
            self.store.write_record(state.run_id, lock_id, {**lock, "report_markdown": md}, md)
            state.records[lock_id] = RecordRef(record_id=lock_id, kind=kind, role="engine", scope="DEPENDENT_SET", stage=st.value, status="LOCKED", gates={}, text_sha256=cur.exact_sha256, ids=ids.as_dict(), source_set_id=state.source_set_id)
            cur.records[kind] = lock_id
            if final:
                d.final_set_lock = lock_id
            else:
                d.draft_set_lock = lock_id
            state.stage = Stage.DONE
            state.outcome = ("FINAL_CLAIM_LOCK+" if c.final_claim_lock else "DRAFT_CLAIM_LOCK+") + ("FINAL_DEPENDENT_SET_LOCK" if final else "DRAFT_DEPENDENT_SET_LOCK")
            return

    def _dependent_reconstruction(self, state: RunState, bundle: MaterialBundle, d: DependentSetState, cur: DependentRevisionState, root_text: str, set_text: str) -> None:
        st = Stage.DEP_RECON
        all_claims = parse_claim_set(root_text + "\n\n" + set_text)
        targets = [cl for cl in all_claims if not cl.is_independent]
        jobs: list[tuple[str, str, str, str | None]] = []
        for cl in targets:
            try:
                chains = parent_chain(all_claims, cl.claim_no, expand_multi=self.cfg.pipeline.expand_multi_dependent)
            except MultiDependentChain as exc:
                raise PipelineHalt(Halt(
                    stage=st.value, role="engine", kind="REVIEW", reason_code="OTHER",
                    message=f"MULTI_DEPENDENT_CHAIN: {exc} — 다중 종속항은 대안 체인별로 blind·picture를 따로 실행해야 한다. "
                            "`claim-agent resume <run_id> --expand-multi`(또는 pipeline.expand_multi_dependent: true)로 대안 체인 확장 실행을 승인하거나, 단일 인용으로 고쳐 --apply-meaning-fix 한다.",
                    open_issues=[{"kind": "REVIEW", "code": "MULTI_DEPENDENT_CHAIN", "text": str(exc), "return_to": None}],
                )) from exc
            dc = next((c["dc_id"] for c in cur.claims if c["claim_no"] == cl.claim_no), None)
            for k, chain in enumerate(chains):
                tid = str(cl.claim_no) if len(chains) == 1 else f"{cl.claim_no}-alt{k + 1}"
                jobs.append((tid, parent_chain_text(chain), cl.text, dc))

        def one(job: tuple[str, str, str, str | None]) -> tuple[str, RoleEnvelope | None, RoleEnvelope | None, str, str, Transition | None]:
            tid, chain_text, target_text, dc = job
            ids = self._ids(state)
            ids.target_claim_id = tid
            brid = record_id("dependent_blind", ids)
            bp = BlindPacket(
                claim_scope="DEPENDENT_SINGLE", candidate_id=ids.candidate_id, revision=ids.revision, design_revision=ids.design_revision, record_id=brid,
                dependent_set_id=ids.dependent_set_id, dependent_design_revision=ids.dependent_design_revision, dependent_revision=ids.dependent_revision,
                target_claim_id=tid, parent_chain_text=chain_text, target_claim_text=target_text,
            )
            benv, _ = self._call(state, "blind-claim-reconstruction-reviewer", Scope.DEPENDENT_SINGLE, st, "dependent_blind", packets.Packet(bp.render()), ids, brid, blind=True)
            if benv.execution_status != ExecStatus.BLIND_COMPLETE:
                return tid, benv, None, brid, "", None
            prid = record_id("dependent_reference_compare", ids)
            pk = packets.picture_dependent(state, self.store, bundle, ids, prid, d.root_lock_id, state.candidate.design_record_id or "", d.design_record_id or "", cur.records.get("dependent_style", ""), cur.records.get("dependent_oa", ""), brid, chain_text, target_text, dc)
            penv, _ = self._call(state, "picture-claim-reconstruction-reviewer", Scope.DEPENDENT_SINGLE, st, "dependent_reference_compare", pk, ids, prid, expected_exact=None, expected_reuse=len(jobs))
            tr = decide(penv, contract_for("picture-claim-reconstruction-reviewer", Scope.DEPENDENT_SINGLE), state.request_mode, d.loop_counts, self.cfg.pipeline.max_return_loops)
            return tid, benv, penv, brid, prid, tr

        results = []
        if self.cfg.pipeline.max_concurrency > 1 and len(jobs) > 1:
            with ThreadPoolExecutor(max_workers=self.cfg.pipeline.max_concurrency) as ex:
                results = list(ex.map(one, jobs))
        else:
            results = [one(j) for j in jobs]

        gate = "PASS"
        first_return: tuple[Transition, RoleEnvelope, str] | None = None
        halts: list[str] = []
        for tid, benv, penv, brid, prid, tr in results:
            t = TargetRecon(target_claim_id=tid, parent_chain_text=next(j[1] for j in jobs if j[0] == tid), target_claim_text=next(j[2] for j in jobs if j[0] == tid), blind_record_id=brid, picture_record_id=prid or None)
            t.blind_status = benv.execution_status.value if benv else None
            t.picture_status = penv.status.value if penv else None
            cur.targets[tid] = t
            if penv is None or tr is None:
                gate = "UNVERIFIED"
                halts.append(f"{tid}: blind {t.blind_status}")
            elif tr.kind == "RETURN":
                gate = "BLOCK"
                if first_return is None:
                    first_return = (tr, penv, prid)
            elif tr.kind == "HALT":
                gate = tr.halt_kind or "BLOCK"
                halts.append(f"{tid}: {penv.status.value} {tr.reason}")
        cur.reconstruction_gate = gate
        self.store.save_state(state)
        if gate == "PASS":
            state.stage = Stage.DEP_LOCK
            return
        if first_return is not None:
            tr, penv, prid = first_return
            self._apply_return(state, tr, penv, prid, dependent=True)
            return
        raise PipelineHalt(Halt(stage=st.value, role="picture-claim-reconstruction-reviewer", kind=gate if gate in ("REVIEW", "BLOCK", "UNVERIFIED", "LOOP_LIMIT") else "BLOCK", message="DEPENDENT_RECONSTRUCTION_GATE 실패: " + "; ".join(halts)))

    # ------------------------------------------------------------------ review only
    def _run_review_only(self, state: RunState, bundle: MaterialBundle) -> None:
        req = RunRequest.model_validate(state.request)
        claim_text = bundle.claim_file_text() or ""
        scope = Scope(req.review_scope)
        ids = Identifiers(state.candidate.candidate_id, "r1", "N/A", input_revision=state.input_revision)
        state.candidate.design_revision = "N/A"
        state.candidate.current = RevisionState(revision="r1", design_revision="N/A", exact_text=claim_text or None, exact_sha256=exact_sha256(claim_text) if claim_text else None)
        kinds = {"syntax-scope-reviewer": "syntax", "oa-strategy-reviewer": "oa", "claim-success-reviewer": "success"}
        for role in req.reviewers or ["syntax-scope-reviewer"]:
            kind = kinds.get(role)
            if kind is None:
                raise ProviderError(f"REVIEW_ONLY에서 지원하지 않는 역할: {role}")
            rid = record_id(kind, ids)
            pk = packets.review_only(state, bundle, ids, rid, role, scope, claim_text, req.request_text)
            env, ref = self._call(state, role, scope if scope != Scope.DEPENDENT_SINGLE else Scope.INDEPENDENT, Stage.REVIEW_ONLY, kind, pk, ids, rid, expected_exact=claim_text)
            state.candidate.current.records[kind] = rid
            state.review_reports[role] = env.report_markdown
        state.stage = Stage.DONE
        state.outcome = "REVIEW_ONLY_DONE"
        state.notes.append("REVIEW_ONLY 결과는 DRAFT·FINAL LOCK의 PASS 근거가 아니다")
