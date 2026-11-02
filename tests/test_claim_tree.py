"""청구항 트리 (CLAUDE.md 0a·제12단계): the problem finder runs once before the architect and feeds both designs, every
written dependent candidate names the problem node it refines, and the accepted tree is drawn into the report and lock."""
from __future__ import annotations

from claim_agent.models.contracts import claim_tree_gaps
from claim_agent.models.envelope import CandidateItem, RoleEnvelope
from claim_agent.provider.scripted import ScriptedProvider

from . import scripted_roles as R


def _run(rt, request, run_id, script=None):
    prov = ScriptedProvider(script or R.happy_script(dependent=request.dependent))
    engine = rt.engine(prov)
    return engine.run(engine.start(request, run_id)), prov


def test_problem_finder_runs_once_before_the_architect_and_feeds_both_designs(rt, request_dep):
    state, prov = _run(rt, request_dep, "tree-01")
    assert state.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state.halt
    roles = [c.role for c in prov.calls if c.phase == "main"]
    assert roles[:2] == ["invention-problem-finder", "claim-architect"] and roles.count("invention-problem-finder") == 1
    assert state.candidate.problem_record_id == "prb-clip-holder-d1-01"
    rec = state.records["prb-clip-holder-d1-01"]
    assert rec.kind == "problem" and rec.stage == "PROBLEM" and rec.role == "invention-problem-finder"
    # the record carries the structured problem, and both designs receive it as a non-source direction input
    stored = rt.store.read_record("tree-01", "prb-clip-holder-d1-01")
    assert stored["problem_structure"] == "ONE_THING" and [d["id"] for d in stored["downstream_problems"]] == ["D1", "D2"]
    arch = next(c for c in prov.calls if c.role == "claim-architect")
    dep = next(c for c in prov.calls if c.role == "dependent-claim-strategy-architect")
    assert "최상류 과제 기록 전문" in arch.packet_text and "원자료 아님" in arch.packet_text
    assert "최상류 과제 기록 전문" in dep.packet_text and "`parent_node`" in dep.packet_text and "05 §1.1a" in dep.packet_text
    # the tree: root problem → claim 1 → claims 2 and 3 under the root, claim 4 under claim 3 (DC-02)
    tree = state.dependent.claim_tree
    assert tree and tree.splitlines()[0].startswith("[최상류 과제] ")
    lines = tree.splitlines()
    i3 = next(i for i, ln in enumerate(lines) if "【청구항 3】 DC-02" in ln)
    i4 = next(i for i, ln in enumerate(lines) if "【청구항 4】 DC-03" in ln)
    assert i4 == i3 + 1 and lines[i4].startswith("   │  └─") or lines[i4].startswith("      └─")
    assert "과제: 조임 때 상판이 책상 상면에서 들뜬다" in lines[i4] and "효과:" in lines[i4]
    lock = rt.store.read_record("tree-01", state.dependent.draft_set_lock)
    assert lock["claim_tree"] == tree
    root_lock = rt.store.read_record("tree-01", state.candidate.draft_claim_lock)
    assert root_lock["problem_record_id"] == "prb-clip-holder-d1-01"
    report = (rt.store.run_dir("tree-01") / "report.md").read_text(encoding="utf-8")
    chat = (rt.store.run_dir("tree-01") / "report-chat.md").read_text(encoding="utf-8")
    assert "### 청구항 트리" in report and "### 청구항 트리" in chat and "[최상류 과제]" in chat


def test_unresolved_problem_review_or_a_failed_finder_never_stops_the_run(rt, request_indep):
    script = R.happy_script(dependent=False)
    script["invention-problem-finder"] = [R.problem_finder("UNRESOLVED")]
    state, _ = _run(rt, request_indep, "tree-02", script)
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.candidate.problem_record_id and any("UNRESOLVED" in n for n in state.notes)

    script = R.happy_script(dependent=False)
    script["invention-problem-finder"] = [{"garbage": True}]      # invalid envelope, and no response left for the repair call
    state, prov = _run(rt, request_indep, "tree-03", script)
    assert state.outcome == "DRAFT_CLAIM_LOCK", state.halt
    assert state.candidate.problem_record_id is None and state.candidate.problem_skipped
    assert any(n.startswith("과제 탐색: 실행 안 됨") for n in state.notes)
    arch = next(c for c in prov.calls if c.role == "claim-architect")
    assert "최상류 과제 기록\n\n없음" in arch.packet_text


def test_problem_finding_off_or_a_user_problem_skips_the_finder(rt, request_indep, request_dep):
    off = request_indep.model_copy(update={"problem_finding": False})
    state, prov = _run(rt, off, "tree-04")
    assert state.outcome == "DRAFT_CLAIM_LOCK" and not [c for c in prov.calls if c.role == "invention-problem-finder"]
    assert any("과제 탐색: 생략" in n and "problem_finding off" in n for n in state.notes)

    worded = request_indep.model_copy(update={"request_text": request_indep.request_text + "\nproblem_finding: off"})
    state, prov = _run(rt, worded, "tree-05")
    assert state.outcome == "DRAFT_CLAIM_LOCK" and not [c for c in prov.calls if c.role == "invention-problem-finder"]

    fixed = request_dep.model_copy(update={"user_problem": "클립이 홀더에서 빠지지 않게 한다"})
    state, prov = _run(rt, fixed, "tree-06")
    assert state.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state.halt
    assert not [c for c in prov.calls if c.role == "invention-problem-finder"]
    arch = next(c for c in prov.calls if c.role == "claim-architect")
    assert "USER_PROBLEM" in arch.packet_text and "클립이 홀더에서 빠지지 않게 한다" in arch.packet_text
    assert "USER_PROBLEM" in state.dependent.claim_tree and "클립이 홀더에서 빠지지 않게 한다" in state.dependent.claim_tree


def _tree_breaking_design(parent_node: str, parent_claim_no: int):
    def design(spec):
        env = R.dep_architect()(spec)
        for c in env["candidates"]:
            if c["dc_id"] == "DC-03":
                c.update(parent_node=parent_node, parent_claim_no=parent_claim_no)
        return env
    return design


def test_a_tree_contract_miss_is_repaired_once_then_halts_before_drafting(rt, request_dep):
    # DC-03 says it refines DC-01 (claim 2) but cites claim 3: one repair call, then the run stops at the design.
    script = R.happy_script(dependent=True)
    script["dependent-claim-strategy-architect"] = [_tree_breaking_design("DC-01", 3), _tree_breaking_design("DC-01", 3)]
    state, prov = _run(rt, request_dep, "tree-07", script)
    assert state.outcome == "HALTED_REVIEW" and state.halt.stage == "DEP_ARCHITECT", state.halt
    assert "CLAIM_TREE_CONTRACT" in state.halt.message and "DC-03" in state.halt.message
    designs = [c for c in prov.calls if c.role == "dependent-claim-strategy-architect"]
    assert len(designs) == 2 and "번호 계약 위반 복구" in designs[1].packet_text and "CLAIM_TREE_CONTRACT" in designs[1].packet_text
    assert not [c for c in prov.calls if c.role == "claim-drafter" and c.scope == "DEPENDENT_SET"]

    script = R.happy_script(dependent=True)
    script["dependent-claim-strategy-architect"] = [_tree_breaking_design("DC-01", 3), R.dep_architect()]
    state, prov = _run(rt, request_dep, "tree-08", script)
    assert state.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state.halt
    assert state.records["ddg-clip-holder-dep-dd1-01"].superseded and state.dependent.design_record_id == "ddg-clip-holder-dep-dd1-02"


def _cand(**over) -> CandidateItem:
    base = dict(dc_id="DC-01", classification="TECHNICAL_SOLUTION_CANDIDATE", DEPENDENT_SOURCE_GATE="PASS", CAUSAL_CONTRIBUTION_GATE="PASS",
                CLAIMABILITY_GATE="PASS", ENFORCEMENT_VALUE_GATE="PASS", enforcement_scenario="x", planned_claim_no=2, parent_claim_no=1,
                parent_node="ROOT", problem="p", feature="f", effect="e")
    return CandidateItem(**{**base, **over})


def _env(*cands: CandidateItem) -> RoleEnvelope:
    return RoleEnvelope(status="PASS", candidates=list(cands))


def test_claim_tree_gaps_checks_nodes_parents_and_node_contents():
    ok = _env(_cand(), _cand(dc_id="DC-02", planned_claim_no=3), _cand(dc_id="DC-03", planned_claim_no=4, parent_claim_no=3, parent_node="DC-02"))
    assert claim_tree_gaps(ok) == []
    # ROOT but the parent is a claim of this set → must name that node
    assert any("parent_node가 ROOT인데" in g for g in claim_tree_gaps(_env(_cand(), _cand(dc_id="DC-02", planned_claim_no=3, parent_claim_no=2))))
    # a node whose claim is not the parent claim
    assert any("다르다" in g for g in claim_tree_gaps(_env(_cand(), _cand(dc_id="DC-02", planned_claim_no=3, parent_claim_no=1, parent_node="DC-01"))))
    # an unknown node, a missing node, a main candidate without problem/effect, any candidate without feature
    assert any("쓰이는 후보가 아니다" in g for g in claim_tree_gaps(_env(_cand(parent_node="DC-09"))))
    assert any("parent_node 없음" in g for g in claim_tree_gaps(_env(_cand(parent_node=None))))
    gaps = claim_tree_gaps(_env(_cand(problem=None, effect=None, feature="")))
    assert any("problem(과제) 없음" in g for g in gaps) and any("effect(효과) 없음" in g for g in gaps) and any("feature" in g for g in gaps)
    # the fallback axis needs a node and a feature but no problem or effect; an unnumbered candidate is not in the set
    aux = _cand(dc_id="DC-05", classification="FALLBACK_ONLY", planned_claim_no=5, problem=None, effect=None, enforcement_scenario=None, ENFORCEMENT_VALUE_GATE="BLOCK")
    assert claim_tree_gaps(_env(_cand(), aux)) == []
    assert claim_tree_gaps(_env(_cand(), _cand(dc_id="DC-07", classification="FALLBACK_ONLY", planned_claim_no=None, parent_node=None, feature=None))) == []
