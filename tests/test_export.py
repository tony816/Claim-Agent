"""Markdown/DOCX export keeps the locked wording and records the identifiers."""
from __future__ import annotations

from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.sources.extract import extract_docx
from claim_agent.store.export import export_run, render_markdown, verify_docx

from . import scripted_roles as R


def test_export_md_and_docx_round_trip(rt, request_dep, tmp_path):
    engine = rt.engine(ScriptedProvider(R.happy_script(dependent=True)))
    state = engine.run(engine.start(request_dep, "export-01"))
    md = export_run(rt.store, state, "md", tmp_path / "claims.md", with_evidence=True)
    text = md.read_text(encoding="utf-8")
    assert R.ROOT_CLAIM in text and R.DEP_SET_TEXT in text and "명세서 뒷받침·실시가능성 미검증 잠정안" in text
    assert "독립항 exact sha256" in text and state.candidate.current.exact_sha256 in text and "PRIOR_ART_NOT_PROVIDED" in text
    assert "## 부록: 판정 기록" in text and "## 성능 요약" in text
    docx_path = export_run(rt.store, state, "docx", tmp_path / "claims.docx", with_evidence=True)
    assert docx_path.exists() and verify_docx(docx_path, state)
    extracted = extract_docx(docx_path.read_bytes()).text
    assert "【청구항 1】" in extracted and "【청구항 4】" in extracted and "run_id\texport-01" in extracted
    # default output lands in the run directory
    default = export_run(rt.store, state, "docx")
    assert default.parent == rt.store.run_dir("export-01") and default.name.endswith("-r1.docx")


def test_export_without_lock_is_labelled(rt, request_indep, tmp_path):
    script = R.happy_script(dependent=False)
    script["claim-architect"] = [R.architect(locked=False)]
    engine = rt.engine(ScriptedProvider(script))
    state = engine.run(engine.start(request_indep, "export-02"))
    assert "LOCK 없음" in render_markdown(rt.store, state) and "독립항 문언 없음" in render_markdown(rt.store, state)
