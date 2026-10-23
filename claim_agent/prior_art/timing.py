"""선행자료 공개일과 기준일 비교(cs2-spec-writer 6556039 pipeline/preexam._date_parts · timing 그대로)."""
from __future__ import annotations

import re


def date_parts(s: str) -> tuple | None:
    """'2024-06-14', '2024.6', '2024년 6월 14일', '2024' → (연, 월|None, 일|None). 못 읽으면 None."""
    m = re.search(r"((?:19|20)\d{2})\s*(?:[-./]|년)\s*(\d{1,2})(?:\s*(?:[-./]|월)\s*(\d{1,2}))?", s or "")
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
    m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", s or "")
    return (int(m.group(1)), None, None) if m else None


def timing(published: str, base: str) -> str:
    """선행자료 공개일과 기준일 비교. 문제가 없으면 빈 문자열."""
    p = date_parts(published)
    if p is None:
        return "공개일 확인 필요"
    b = date_parts(base)
    if b is None:
        return ""
    for x, y in zip(p, b, strict=True):
        if x is None or y is None:
            return "공개일·기준일이 가까움 — 정확한 날짜 확인 필요"
        if x != y:
            return "기준일 이후 공개 — 선행기술이 아닐 수 있음" if x > y else ""
    return "기준일과 같은 날 공개 — 선행기술이 아닐 수 있음"
