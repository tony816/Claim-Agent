"""모델이 든 인용 문구가 선행 문헌 본문에 실제로 있는지 코드로 확인한다(cs2-spec-writer 6556039 pipeline/docs.find_quote 이식).

글자 일치(공백·줄바꿈 무시, 줄 끝 하이픈 연결, 따옴표·대시 통일, 영문 소문자)만 옮겼다. 형태소 근사 일치는 옮기지 않았으므로
근사로만 맞는 인용은 None(엔진에서 '확인 필요')이 된다.
"""
from __future__ import annotations

import re

PARA = re.compile(r"[\[【]\s*(\d{4})\s*[\]】]")
MARK = re.compile(r"[\[【]\s*(\d{4})\s*[\]】]|【([^】\n]{1,40})】")   # 문단 번호 [0001]·【0001】 또는 【요약】·【청구항 1】 같은 머리표
ELLIPSIS = re.compile(r"\s*(?:…|\.\.\.+|⋯)\s*")
MIN_CHARS = 4
_SAME = {"“": '"', "”": '"', "‘": "'", "’": "'", "－": "-", "–": "-", "—": "-"}


def _norm_map(text: str) -> tuple[str, list[int]]:
    """비교용 글(공백·하이픈 줄바꿈 제거, 소문자, 따옴표 통일)과 그 글자의 원문 위치."""
    text = re.sub(r"-\n(?=\w)", "\0\0", text)  # 줄 끝 하이픈 연결(inte-\ngration)은 자리만 지키고 지운다
    out, pos = [], []
    for i, ch in enumerate(text):
        if ch.isspace() or ch == "\0":
            continue
        out.append(_SAME.get(ch, ch).lower())
        pos.append(i)
    return "".join(out), pos


def normalize(s: str) -> str:
    return _norm_map(s or "")[0]


def _where(text: str, offset: int) -> str:
    """인용 시작 위치에 가장 가까운 앞쪽 문단 번호([0002]) 또는 【머리표】. 없으면 ''."""
    last = ""
    for m in MARK.finditer(text):
        if m.start() > offset:
            break
        last = f"[{m.group(1)}]" if m.group(1) else f"【{m.group(2).strip()}】"
    return last


def find_quote(text: str, quote: str) -> dict | None:
    """인용 문구가 본문에 글자 그대로(공백·줄바꿈 무시) 있는지. 줄임표(…, ...)로 나눈 조각은 순서대로 모두 있어야 한다.
    반환: {"where": 문단 번호·머리표 또는 '', "exact": True} 또는 None."""
    if not quote or not quote.strip() or not text:
        return None
    parts = [p for p in ELLIPSIS.split(quote.strip().strip("\"'“”‘’")) if normalize(p)]
    if not parts or sum(len(normalize(p)) for p in parts) < MIN_CHARS:
        return None
    hay, pos = _norm_map(text)
    start, first = 0, None
    for p in parts:
        needle = normalize(p)
        k = hay.find(needle, start)
        if k < 0:
            return None
        first = k if first is None else first
        start = k + len(needle)
    return {"where": _where(text, pos[first]), "exact": True}
