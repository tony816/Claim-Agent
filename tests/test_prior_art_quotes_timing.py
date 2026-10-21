"""인용 글자 일치(공백·줄바꿈 무시)와 위치, 공개일·기준일 비교."""
from __future__ import annotations

from claim_agent.prior_art.quotes import find_quote, normalize
from claim_agent.prior_art.timing import date_parts, timing

TEXT = ("KR 10-1 A — 냉각판\n\n【요약】\n냉각 유로를 포함한다.\n\n【청구범위】\n【청구항 1】 냉각판;\n유로를 포함하는, 모듈.\n"
        "【청구항 2】 제1항에 있어서, 핀을 더 포함하는 모듈.\n\n【발명의 설명】\n[0001] 본 발명은 냉각판에 관한 것이다.\n\n"
        "[0002] 냉각판은 A&B 유로를\n가진다. 유로는 “곡선” 형태이며 inte-\ngration 된다.")


def test_find_quote_ignores_whitespace_and_reports_paragraph():
    assert find_quote(TEXT, "냉각판은A&B 유로를 가진다") == {"where": "[0002]", "exact": True}
    assert find_quote(TEXT, '"본 발명은   냉각판에\n관한 것이다"') == {"where": "[0001]", "exact": True}
    assert find_quote(TEXT, '유로는 "곡선" 형태이며 integration') == {"where": "[0002]", "exact": True}   # 따옴표 통일·하이픈 줄바꿈


def test_find_quote_section_headers_and_ellipsis():
    assert find_quote(TEXT, "핀을 더 포함하는 모듈") == {"where": "【청구항 2】", "exact": True}
    assert find_quote(TEXT, "냉각 유로를 포함한다") == {"where": "【요약】", "exact": True}
    assert find_quote(TEXT, "냉각판은 … 가진다") == {"where": "[0002]", "exact": True}
    assert find_quote(TEXT, "가진다 … 냉각판은") is None          # 조각 순서가 바뀌면 일치가 아니다
    assert find_quote("머리표 없는 본문 문장입니다", "본문 문장") == {"where": "", "exact": True}
    assert find_quote("【0012】 단락 번호 꼴", "단락 번호") == {"where": "[0012]", "exact": True}


def test_find_quote_misses():
    assert find_quote(TEXT, "") is None and find_quote(TEXT, "   ") is None
    assert find_quote(TEXT, "열교환기를 포함한다") is None
    assert find_quote(TEXT, "판") is None                         # 너무 짧은 인용은 확인하지 않는다
    assert find_quote("", "냉각판") is None
    assert normalize(" A  b\n“C” ") == 'ab"c"'


def test_timing_cases():
    assert date_parts("2024년 6월 14일") == (2024, 6, 14) and date_parts("2024.06") == (2024, 6, None) and date_parts("2019") == (2019, None, None)
    assert date_parts("미상") is None
    assert timing("2020-01-01", "2024-05-01") == ""
    assert timing("2024-06-14", "2024-05-01") == "기준일 이후 공개 — 선행기술이 아닐 수 있음"
    assert timing("2024-05-01", "2024-05-01") == "기준일과 같은 날 공개 — 선행기술이 아닐 수 있음"
    assert timing("", "2024-05-01") == "공개일 확인 필요"
    assert timing("2020-01-01", "") == ""
    assert timing("2024", "2024-05-01") == "공개일·기준일이 가까움 — 정확한 날짜 확인 필요"
    assert timing("2023", "2024-05-01") == ""


def test_find_quote_ignores_only_end_punctuation():
    # live run 2026-10-21: a clause-truncated quote ended in "." where the document continues with "," or ";"
    text = "[0012] the cable holes are formed in the fixing piece, and the slots open upward; a rib projects."
    assert find_quote(text, "the cable holes are formed in the fixing piece.") is not None
    assert find_quote(text, "the slots open upward.") is not None
    assert find_quote(text, "the cable holes are formed in the locking piece.") is None
