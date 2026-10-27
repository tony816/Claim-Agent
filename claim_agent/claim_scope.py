"""Bound numeric dependent targets without treating cited claims as new tasks."""
from __future__ import annotations

import re


def parse_target(value: str | None) -> set[int] | None:
    """Parse numeric lists/ranges only; descriptive candidate strategies stay open.

    A description may still carry a number ('세트 끝(10항~)에 이어지는 … 후보', run web-b013b034): any word left after the
    claim words are stripped makes it a description. Only bare digits and separators that form no list are refused.
    """
    if not value:
        return None
    text = re.sub(r"청구항|종속항|claims?|제|항", "", value, flags=re.I)
    text = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"\d+(?:[~–—-]\d+)?(?:[,·ㆍ、]\d+(?:[~–—-]\d+)?)*", text):
        if re.search(r"\d", text) and not re.search(r"[가-힣A-Za-z]", text):
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


def _mentioned_spans(text: str) -> list[tuple[int, int]]:
    # Only the current instruction, not a pasted claim set or previous messages.
    lead = text.split("\n\n", 1)[0]
    matches = re.findall(r"(?:제)?(\d+)(?:\s*[~–—-]\s*(?:제)?(\d+))?\s*항", lead)
    matches += re.findall(r"\bclaims?\s+(\d+)(?:\s*[~–—-]\s*(\d+))?", lead, re.I)
    spans = [(int(start), int(end or start)) for start, end in matches]
    return [(lo, hi) for lo, hi in spans if lo <= hi <= 1000]


def _says_where_new_claims_go(text: str) -> bool:
    """'13항부터', '12항 뒤에', '12항에 이어' — a position after the set, not a claim to write at that number."""
    lead = text.split("\n\n", 1)[0]
    return bool(re.search(r"\d+\s*항\s*(?:부터|이후|뒤|다음|에\s*이어)", lead))


def explicit_mentions(text: str) -> set[int] | None:
    if re.search(r"전체|모든|전부|\ball\b", text.split("\n\n", 1)[0], re.I):
        return None
    result = set()
    for lo, hi in _mentioned_spans(text):
        result.update(n for n in range(lo, hi + 1) if n > 1)
    return result or None


def constrain_target(text: str, dependent: bool, target: str | None, append: bool = False) -> tuple[bool, str | None]:
    """`append`: the router put the dependent claims after the end of the user's numbered set (EXISTING_SET_EDIT)."""
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
    if mentioned and append and parse_target(target) is None and claim_intent(text) != "EDIT" \
            and (any(lo == 1 for lo, _ in _mentioned_spans(text)) or _says_where_new_claims_go(text)):
        # The router chose no number and the words name claim 1 with the others ('1~12항에서 놓친 특허 포인트를 찾아
        # 추가 종속항을 써줘', run web-6be7454f) or say where the new claims go ('13항부터'). Claim 1 is no dependent
        # target, so the numbers point at the set being looked at; read as targets, they made the edit write new claims
        # at 2~12 and push the whole set back to 13~23. The design numbers the new claims on from the set's end.
        return dependent, target
    if mentioned:
        selected = parse_target(target) if dependent else None
        # A semantic route may select a subset of mentioned claims, but never
        # introduce siblings from attachments or stale UI options.
        selected = selected & mentioned if selected else mentioned
        if not selected:
            raise ValueError("분류된 종속항이 현재 요청의 항 번호와 일치하지 않습니다.")
        return True, ",".join(map(str, sorted(selected)))
    return dependent, target


def format_target(numbers: set[int]) -> str:
    """Canonical `2~4,6` form (consecutive numbers collapse into ranges) that parse_target accepts unchanged."""
    ordered = sorted(numbers)
    if not ordered:
        raise ValueError("종속항 번호를 하나 이상 지정해 주세요.")
    parts, start, prev = [], ordered[0], ordered[0]
    for n in ordered[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}~{prev}")
        if n is not None:
            start = prev = n
    return ",".join(parts)


TARGET_MODES = ("auto", "independent", "single", "range")


def resolve_claim_target(data: dict) -> tuple[bool, str | None, str]:
    """(dependent, target, mode) from the composer's structured 작성 대상 input.

    mode `auto` → 미지정 (the request text and materials decide the scope); `independent` → 독립항만; `single` with `target_claim` N → 1 means 독립항만, N ≥ 2 means only dependent claim N;
    `range` with `target_segments` (a list of {from, to} / numbers, or a `2~4,6` string) → the canonical set string.
    Requests without `target_mode` keep the legacy `dependent` + `target` fields; without `dependent` they are 미지정.
    """
    mode = data.get("target_mode")
    if mode is None:
        dependent = bool(data.get("dependent"))
        target = str(data.get("target", "2~8")).strip()
        return dependent, (target or None) if dependent else None, "range" if dependent else "auto"
    if mode not in TARGET_MODES:
        raise ValueError("작성 대상은 미지정·독립항만·특정 항 1개·종속항 범위 중 하나를 고르세요.")
    if mode in ("auto", "independent"):
        return False, None, mode
    if mode == "single":
        number = _claim_number(data.get("target_claim"), "항 번호")
        return (False, None, mode) if number == 1 else (True, str(number), mode)
    segments = data.get("target_segments")
    numbers: set[int] = set()
    if isinstance(segments, str):
        numbers = parse_target(segments) or set()
    elif isinstance(segments, list):
        for seg in segments:
            if isinstance(seg, dict):
                lo = _claim_number(seg.get("from"), "시작 항")
                hi = _claim_number(seg.get("to", lo), "끝 항")
            else:
                lo = hi = _claim_number(seg, "항 번호")
            if hi < lo:
                raise ValueError(f"종속항 구간 {lo}~{hi}의 끝 항이 시작 항보다 작습니다.")
            numbers.update(range(lo, hi + 1))
    else:
        raise ValueError("종속항 범위를 구간 목록 또는 2~4,6 형식으로 지정해 주세요.")
    if 1 in numbers:
        raise ValueError("종속항 범위에는 2항 이상만 넣을 수 있습니다. 독립항만 작성하려면 '독립항만'을 고르세요.")
    return True, format_target(numbers), mode


def _claim_number(value, label: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}은(는) 숫자로 입력해 주세요.") from None
    if not 1 <= number <= 1000:
        raise ValueError(f"{label}은(는) 1~1000 사이여야 합니다.")
    return number


def find_existing_set(targets: set[int], sources: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(source_id, text) of the first source whose numbered claim set holds every target as a dependent claim, or
    takes it as a new claim right after its end (제3항 added to a set of 1~2).

    The set must also contain an independent claim: a lone fragment ('제8항에 있어서, …') is not a set to edit in place.
    Blank headers (【청구항 】) are numbered by place, as the user counts them. A set still left with blank numbers
    (제항 citations) is matched by place too; the engine then stops on the blanks before any call instead of the request
    falling through to a new independent claim.

    No targets means new claims after the end of the set whose numbers the user has not named yet; the set must then
    already hold a dependent claim, since claims added to a lone claim 1 are an ordinary new dependent set.
    """
    from .pipeline.claimtext import ClaimParseError, appended_nos, claim_slots, fill_blank_headers, numbering_blanks, parse_claim_set

    for source_id, original in sources:
        text = fill_blank_headers(original)
        if numbering_blanks(text):
            slots = claim_slots(text)
            beyond = {n for n in targets if n > len(slots)}
            if len(slots) > 1 and all(n > 1 for n in targets) and beyond == set(range(len(slots) + 1, len(slots) + 1 + len(beyond))):
                return source_id, original
            continue
        try:
            claims = parse_claim_set(text)
        except ClaimParseError:
            continue
        by_no = {c.claim_no: c for c in claims}
        new = appended_nos(claims, targets)
        if not targets and not any(not c.is_independent for c in claims):
            continue
        if any(c.is_independent for c in claims) and all(n in new or (n in by_no and not by_no[n].is_independent) for n in targets):
            return source_id, original
    return None


_NEW_WORDS = re.compile(r"새로|새\s*항|신규|추가|끼워\s*넣|삽입|생성")     # not 끼워 alone: 끼워맞춤 is a fit
_EDIT_WORDS = re.compile(r"수정|고쳐|고치|고친|정리|다듬|완성|바꿔|바꾸|변경|보완|손봐|손보|다시|재작성|보정|교정")


def claim_intent(text: str) -> str | None:
    """Whether the user's words ask to rework the named claims (EDIT) or to write new claims at those numbers (NEW).

    None when they say neither or both: '4·5·6항이 첨부 이미지를 설명하도록' can mean either, and the edit then asks
    before any call (사용자 결정 2026-10-26) instead of guessing.
    """
    new, edit = bool(_NEW_WORDS.search(text)), bool(_EDIT_WORDS.search(text))
    return "NEW" if new and not edit else "EDIT" if edit and not new else None


def ui_target_set(ui_hints: dict | None) -> set[int] | None:
    """The claim numbers the user explicitly picked in the composer (single/range), or None for defaults and 독립항만."""
    hints = ui_hints or {}
    if hints.get("target_mode") not in ("single", "range") or not hints.get("dependent"):
        return None
    return parse_target(str(hints.get("target") or "")) or None


def mentions_any_claim(text: str) -> bool:
    lead = text.split("\n\n", 1)[0]
    return bool(re.search(r"\d+\s*항|\bclaims?\s+\d+", lead, re.I))


def apply_ui_target(mode: str, dependent: bool, target: str | None, text: str, ui_hints: dict | None) -> tuple[bool, str | None]:
    """Deterministically narrow an authoring route to the claims picked in the composer.

    The current request text keeps precedence: explicit claim numbers in the text win, and a text that names only
    claim 1 never gains dependent work from the picker. A picked set that shares no claim with the text's numbers is
    a conflict and stops the request instead of silently choosing either side.
    """
    picked = ui_target_set(ui_hints)
    if mode in {"AUTHORING_DRAFT", "FINALIZATION"} and (ui_hints or {}).get("target_mode") == "independent" and not explicit_mentions(text):
        return False, None              # 독립항만 picked and the text names no claim: no dependent work is added
    if mode not in {"AUTHORING_DRAFT", "FINALIZATION"} or not picked:
        return dependent, target
    mentioned = explicit_mentions(text)
    if mentioned:
        selected = mentioned & picked
        if not selected:
            raise ValueError(f"작성 옵션에서 고른 항({format_target(picked)})이 요청 문장의 항 번호({format_target(mentioned)})와 다릅니다. 옵션이나 요청 문장을 맞춰 주세요.")
        current = parse_target(target) if dependent else None
        if current:
            selected &= current
            if not selected:
                raise ValueError("분류된 종속항이 작성 옵션에서 고른 항과 일치하지 않습니다.")
        return True, format_target(selected)
    if not dependent and mentions_any_claim(text):
        return dependent, target          # e.g. "1항만 수정해줘": the text limits the work to the independent claim.
    current = parse_target(target) if dependent else None
    selected = (current & picked) if current else picked
    return True, format_target(selected or picked)
