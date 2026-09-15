"""Claim header spellings, dependent-preamble rules and the multi-dependent REVIEW path."""
from __future__ import annotations

from pathlib import Path

import pytest

from claim_agent.pipeline.claimtext import ClaimParseError, MultiDependentChain, normalize_headers, parent_chain, parse_claim_set, parse_parent_refs
from claim_agent.pipeline.engine import Decision
from claim_agent.provider.scripted import ScriptedProvider
from claim_agent.runtime import build_runtime

from . import scripted_roles as R

ROOT = Path(__file__).resolve().parents[1]


def _rt(tmp_path, **over):
    base = {"paths.runs_dir": str(tmp_path / "runs"), "paths.lessons_dir": str(tmp_path / "lessons"), "cache.enabled": False, "pipeline.max_concurrency": 1}
    return build_runtime(ROOT, None, None, {**base, **over})

BODY1 = "A부; 및 B부를 포함하는 장치."
BODY2 = "제1항에 있어서, 상기 B부는 C를 포함하는 장치."


@pytest.mark.parametrize("h1,h2,style", [
    ("【청구항 1】", "【청구항 2】", "canonical"),
    ("【 청구항 1 】", "【청구항2】", "canonical"),
    ("[청구항 1]", "[청구항 2]", "bracket"),
    ("[ 청구항 1 ]", "[청구항2]", "bracket"),
    ("청구항 1.", "청구항 2.", "plain"),
    ("청구항 1", "청구항 2", "plain"),
    ("청구항 1:", "청구항 2:", "plain"),
    ("청구항1.", "청구항2.", "plain"),
    ("제1항", "제2항", "je-hang"),
    ("제 1 항.", "제 2 항.", "je-hang"),
    ("Claim 1", "Claim 2", "english"),
    ("Claim 1.", "CLAIM 2:", "english"),
    ("  【청구항 1】", "\t[청구항 2]", "bracket"),
])
def test_header_spellings_are_normalised(h1, h2, style):
    for sep in ("\n", "\n\n", " "):
        text = f"{h1}{sep}{BODY1}\n\n{h2}{sep}{BODY2}"
        if sep == " " and style in ("plain", "je-hang", "english"):
            continue  # line-alone spellings need their own line; otherwise 제1항 in a preamble would be a header
        claims = parse_claim_set(text)
        assert [c.claim_no for c in claims] == [1, 2]
        assert claims[0].text.startswith("【청구항 1】") and claims[1].text.startswith("【청구항 2】")
        assert claims[0].body == BODY1 and claims[1].parent_nos == [1] and not claims[1].multi_dependent
        _, changed, found = normalize_headers(text)
        assert changed == (style != "canonical") and found == style


def test_a_parent_reference_inside_a_line_is_never_a_header():
    text = "【청구항 1】\nA.\n【청구항 2】\n제1항에 있어서, B.\n【청구항 3】\n제1항 또는 제2항에 있어서, C."
    claims = parse_claim_set(text)
    assert [c.claim_no for c in claims] == [1, 2, 3] and claims[2].parent_nos == [1, 2] and claims[2].multi_dependent


@pytest.mark.parametrize("preamble,parents,multi", [
    ("제1항에 있어서, X", [1], False),
    ("제 3 항에 있어서 X", [3], False),
    ("제1항 또는 제2항에 있어서, X", [1, 2], True),
    ("제1항 및 제2항에 있어서, X", [1, 2], True),
    ("제1항 내지 제3항 중 어느 한 항에 있어서, X", [1, 2, 3], True),
    ("제2항 내지 제4항 중 어느 하나의 항에 있어서, X", [2, 3, 4], True),
    ("제1항, 제3항 또는 제5항 중 어느 한 항에 있어서, X", [1, 3, 5], True),
    ("제1항에 기재된 장치를 이용한 방법으로서, X", [1], False),
    ("제1항에 따른 장치를 제조하는 방법에 있어서, X", [1], False),
    ("제1항의 장치를 포함하는 시스템.", [1], False),
    ("A부를 포함하는 장치.", [], False),
    ("판 형태로 형성되고, 제1 방향으로 연장되는 X", [], False),  # 제1 방향 is not a claim reference
])
def test_dependent_preamble_rules(preamble, parents, multi):
    assert parse_parent_refs(preamble) == (parents, multi)


def test_unrecognised_document_raises_a_helpful_error():
    with pytest.raises(ClaimParseError, match="accepted spellings"):
        parse_claim_set("그냥 문단 텍스트")


def test_multi_dependent_reconstruction_is_review_then_expands_on_approval(tmp_path, request_dep):
    root = _rt(tmp_path)
    multi_set = R.DEP_SET_TEXT.replace("【청구항 4】\n제3항에 있어서,", "【청구항 4】\n제2항 또는 제3항에 있어서,")
    script = R.happy_script(dependent=True, n_targets=4)
    script["claim-style-adjuster"] = [R.style("INDEPENDENT"), R.style("DEPENDENT_SET", text=multi_set)]
    script["picture-claim-reconstruction-reviewer"] = [R.picture()] * 5   # root + 4 alternative-chain targets
    prov = ScriptedProvider(script)
    state = root.engine(prov).run(root.engine(prov).start(request_dep, "multi-01"))
    assert state.outcome == "HALTED_REVIEW" and state.halt.stage == "DEP_RECON" and "MULTI_DEPENDENT_CHAIN" in state.halt.message
    assert state.halt.open_issues[0]["code"] == "MULTI_DEPENDENT_CHAIN"
    claims = parse_claim_set(R.ROOT_CLAIM + "\n\n" + multi_set)
    with pytest.raises(MultiDependentChain):
        parent_chain(claims, 4)
    expanded = _rt(tmp_path, **{"pipeline.expand_multi_dependent": True})
    state2 = expanded.engine(prov).resume("multi-01", Decision())
    assert state2.outcome == "DRAFT_CLAIM_LOCK+DRAFT_DEPENDENT_SET_LOCK", state2.halt
    assert set(state2.dependent.current.targets) == {"2", "3", "4-alt1", "4-alt2"}
