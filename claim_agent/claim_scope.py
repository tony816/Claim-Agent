"""Bound numeric dependent targets without treating cited claims as new tasks."""
from __future__ import annotations

import re


def parse_target(value: str | None) -> set[int] | None:
    """Parse numeric lists/ranges only; descriptive candidate strategies stay open."""
    if not value:
        return None
    text = re.sub(r"청구항|종속항|claims?|제|항", "", value, flags=re.I)
    text = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"\d+(?:[~–—-]\d+)?(?:[,·ㆍ、]\d+(?:[~–—-]\d+)?)*", text):
        if re.search(r"\d", text):
            raise ValueError("종속항 범위는 번호 또는 2~4,6 형식으로 지정해 주세요.")
        return None
    numbers = set()
    for part in re.split(r"[,·ㆍ、]", text):
        bounds = re.split(r"[~–—-]", part)
        start, end = int(bounds[0]), int(bounds[-1])
        if start < 2 or end < start or end > 1000:
            raise ValueError("유효하지 않은 종속항 번호 범위입니다.")
        numbers.update(range(start, end + 1))
    return numbers


def explicit_mentions(text: str) -> set[int] | None:
    # Only the current instruction, not a pasted claim set or previous messages.
    lead = text.split("\n\n", 1)[0]
    if re.search(r"전체|모든|전부|\ball\b", lead, re.I):
        return None
    matches = re.findall(r"(?:제)?(\d+)(?:\s*[~–—-]\s*(?:제)?(\d+))?\s*항", lead)
    matches += re.findall(r"\bclaims?\s+(\d+)(?:\s*[~–—-]\s*(\d+))?", lead, re.I)
    result = set()
    for start, end in matches:
        lo, hi = int(start), int(end or start)
        if hi >= lo and hi <= 1000:
            result.update(n for n in range(lo, hi + 1) if n > 1)
    return result or None


def constrain_target(text: str, dependent: bool, target: str | None) -> tuple[bool, str | None]:
    lead = text.split("\n\n", 1)[0]
    only = re.findall(r"(?:제)?(\d+)\s*항\s*만|\bonly\s+claim\s+(\d+)\b", lead, re.I)
    if len(only) == 1:
        number = int(only[0][0] or only[0][1])
        if number == 1:
            return False, None
        if dependent:
            return True, str(number)
    if not dependent:
        return False, None
    mentioned = explicit_mentions(text)
    if mentioned:
        selected = parse_target(target) if dependent else None
        # A semantic route may select a subset of mentioned claims, but never
        # introduce siblings from attachments or stale UI options.
        selected = selected & mentioned if selected else mentioned
        if not selected:
            raise ValueError("분류된 종속항이 현재 요청의 항 번호와 일치하지 않습니다.")
        return True, ",".join(map(str, sorted(selected)))
    return dependent, target
