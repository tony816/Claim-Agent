"""Engine hooks for layers 2 and 3: add the idea sheet and the precedent sheet to the architects' packets.

Both sheets are advisory. A failed extraction, a missing precedent file or an unparsable answer leaves a run note
and the packet unchanged — the graph never stops a run and never becomes a gate.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ..config import AppConfig
from ..models.request import MaterialBundle
from ..models.state import RunState
from ..provider.base import CallResult, CallSpec, GenParams, ImagePart, LLMProvider, parse_json_text
from . import invention as inv
from .precedent import PrecedentGraph, precedent_sheet
from .store import Graph

IDEA_TITLE = "발명 그래프 아이디어 시트 (INFERRED — 근거 아님, 원자료 위치만 근거)"
PRECEDENT_TITLE = "심사 이력 참고 시트 (경험 통계 — 근거·게이트·선행기술 아님)"
ENFORCE_TITLE = "권리행사 판례 점검표 (미검토 판례 — 근거·게이트 아님)"


class GraphHooks:
    def __init__(self, cfg: AppConfig, provider: LLMProvider, run_dir: Callable[[str], Path], account: Callable[[str, str, CallResult], None]):
        self.cfg = cfg
        self.provider = provider
        self.run_dir = run_dir
        self.account = account
        self._precedent: PrecedentGraph | None = None

    @property
    def enabled(self) -> bool:
        g = self.cfg.graph
        return g.invention == "on" or g.precedent == "on"

    # ---- layer 2

    def invention_graph(self, state: RunState, bundle: MaterialBundle) -> Graph | None:
        model = self.cfg.graph.invention_model or self.cfg.model_for(self.cfg.graph.invention_role)
        key = inv.cache_key(bundle, model)
        path = self.run_dir(state.run_id) / "graph" / f"invention-{key}.json"
        if path.is_file():
            return Graph.load(path)
        if not bundle.by_category(*inv.CATEGORIES):
            state.notes.append("발명 그래프: 원자료가 없어 만들지 않음")
            return None
        images = [ImagePart(it.mime_type or "image/png", it.data or b"", it.name, it.content_sha256)
                  for it in bundle.by_category(*inv.CATEGORIES) if it.kind == "image"]
        spec = CallSpec(role=self.cfg.graph.invention_role, scope="INDEPENDENT", model=model, system_instruction=inv.EXTRACTION_SYSTEM,
                        packet_text=inv.extraction_packet(bundle), images=images, json_schema=inv.EXTRACTION_SCHEMA,
                        gen=GenParams(temperature=0.0, thinking_level="LOW", max_output_tokens=16384), use_cache=False,
                        phase="graph", stage="ARCHITECT", run_id=state.run_id)
        try:
            result = self.provider.generate(spec)
        except Exception as exc:  # noqa: BLE001 - advisory step
            state.notes.append(f"발명 그래프: 추출 호출 실패 — 시트 없이 진행 ({exc})")
            return None
        self.account("ARCHITECT", model, result)
        raw = result.parsed or parse_json_text(result.text)
        if raw is None:
            state.notes.append("발명 그래프: 추출 응답을 JSON으로 읽지 못함 — 시트 없이 진행")
            return None
        built = inv.build(raw, bundle)
        built.graph.meta.update({"model": model, "cache_key": key})
        built.graph.save(path)
        path.with_name(path.stem + "-rejected.json").write_text(json.dumps(built.rejected, ensure_ascii=False, indent=1), encoding="utf-8")
        st = built.graph.stats()
        state.notes.append(f"발명 그래프 {key}: 노드 {st['nodes']}·관계 {st['edges']}, 원문 불일치로 버림 {len(built.rejected)}")
        return built.graph

    # ---- layer 3

    def precedent(self, state: RunState) -> PrecedentGraph | None:
        if self._precedent is None:
            p = Path(self.cfg.graph.precedent_path)
            p = p if p.is_absolute() else self.cfg.project_root / p
            try:
                self._precedent = PrecedentGraph.load(p)
            except (OSError, ValueError) as exc:
                state.notes.append(f"심사 이력 참고: 사례 파일을 읽지 못함 — 시트 없이 진행 ({exc})")
                return None
        return self._precedent

    # ---- packet decoration

    def architect_packet(self, state: RunState, bundle: MaterialBundle, text: str) -> str:
        """Independent architect: idea sheet without exam patterns; never the precedent sheet (설계 §13 #10)."""
        if self.cfg.graph.invention != "on":
            return text
        g = self.invention_graph(state, bundle)
        if g is None:
            return text
        return inv.insert_before_contract(text, inv.sheet_section(IDEA_TITLE, inv.idea_sheet(g)))

    def oa_packet(self, state: RunState, text: str, exact_text: str) -> str:
        """OA (independent and dependent): enforcement check of the exact claim text with matching cases. Code only, no model call."""
        if self.cfg.graph.caselaw_review != "on":
            return text
        from . import caselaw as cl

        try:
            data = cl.load_records(self.cfg.project_root / cl.ENFORCE_FILE)
        except (OSError, ValueError, cl.CaseLawError) as exc:
            state.notes.append(f"권리행사 판례 점검: 기록을 읽지 못함 — 점검표 없이 진행 ({exc})")
            return text
        body = cl.enforcement_markdown(cl.enforcement_report(data, exact_text))
        return inv.insert_before_contract(text, inv.sheet_section(ENFORCE_TITLE, body))

    def dependent_packet(self, state: RunState, bundle: MaterialBundle, text: str) -> str:
        g = self.invention_graph(state, bundle) if self.cfg.graph.invention == "on" or self.cfg.graph.precedent == "on" else None
        if g is not None and self.cfg.graph.invention == "on":
            text = inv.insert_before_contract(text, inv.sheet_section(IDEA_TITLE, inv.idea_sheet(g, include_exam_patterns=True)))
        if self.cfg.graph.precedent == "on" and g is not None:
            pg = self.precedent(state)
            if pg is not None:
                text = inv.insert_before_contract(text, inv.sheet_section(PRECEDENT_TITLE, precedent_sheet(pg, g, inv.causal_chains(g))))
        return text
