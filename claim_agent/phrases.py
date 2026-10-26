"""Feedback phrases (피드백 멘트) of the web tool panel: preset texts a click puts into the composer.

A phrase is only text for the input box. The user can add to it and sends it with Enter, and the sent message is
handled like any other message (classified first in 게이트 자동). The list is kept on this computer next to the
conversations; without a saved list the defaults apply.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

FILE = "feedback-phrases.json"
MAX_PHRASES = 100
MAX_TEXT = 2000
MAX_GROUP = 30
DEFAULT_GROUP = "기타"
DEFAULT_PHRASES = [
    ("검수 반영", "위 검수 지적을 반영해서 청구항을 고쳐 줘."),
    ("검수 반영", "검수 지적 중 원자료로 뒷받침되는 것만 반영하고, 반영하지 않은 지적은 이유를 적어 줘."),
    ("문언 다듬기", "의미와 권리범위는 그대로 두고 더 간결하게 다듬어 줘."),
    ("문언 다듬기", "띄어쓰기·조사·문장부호만 고쳐 줘."),
    ("문언 다듬기", "'상기' 선행기재와 지칭 관계가 맞는지 확인하고 어긋난 곳을 고쳐 줘."),
    ("문언 다듬기", "기능이나 결과만 적힌 표현을 그 기능을 만드는 구조와 관계로 바꿔 줘."),
    ("권리범위", "필수 구성만 남겨 독립항을 더 넓게 써 주고, 뺀 한정은 종속항 후보로 정리해 줘."),
    ("권리범위", "이 한정을 빼면 권리범위와 회피 가능성이 어떻게 달라지는지 설명해 줘."),
    ("종속항", "독립항을 기준으로 기술적 효과가 있는 종속항 후보 3~5개를 제안하고, 항마다 효과를 한 줄씩 적어 줘."),
    ("확인", "원자료에 근거가 없는 부분을 표시하고 발명자에게 물을 질문을 정리해 줘."),
]
_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")


def defaults() -> list[dict]:
    return [dict(id=f"default-{i}", group=group, text=text) for i, (group, text) in enumerate(DEFAULT_PHRASES, 1)]


def load(folder: Path) -> dict:
    """{"phrases": [...], "custom": bool}: the saved list, or the defaults when none is saved or it is unreadable."""
    try:
        items = json.loads((folder / FILE).read_text(encoding="utf-8"))["phrases"]
        return dict(phrases=validate(items), custom=True)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(phrases=defaults(), custom=False)


def validate(items) -> list[dict]:
    if not isinstance(items, list) or len(items) > MAX_PHRASES:
        raise ValueError(f"피드백 멘트는 {MAX_PHRASES}개까지 저장할 수 있습니다.")
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("피드백 멘트 형식을 확인하세요.")
        text = str(item.get("text") or "").strip()
        group = str(item.get("group") or "").strip() or DEFAULT_GROUP
        if not text:
            raise ValueError("빈 피드백 멘트는 저장할 수 없습니다.")
        if len(text) > MAX_TEXT or len(group) > MAX_GROUP:
            raise ValueError(f"피드백 멘트는 {MAX_TEXT}자, 묶음 이름은 {MAX_GROUP}자까지입니다.")
        pid = str(item.get("id") or "")
        if not _ID.fullmatch(pid) or pid in seen:
            pid = uuid.uuid4().hex[:12]
        seen.add(pid)
        out.append(dict(id=pid, group=group, text=text))
    return out


def save(folder: Path, items) -> dict:
    phrases = validate(items)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / FILE).write_text(json.dumps(dict(phrases=phrases), ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(phrases=phrases, custom=True)


def reset(folder: Path) -> dict:
    (folder / FILE).unlink(missing_ok=True)
    return load(folder)
