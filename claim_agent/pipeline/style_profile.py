"""The claim form the style stage writes to: the user's set when a run adds to one, the corpus otherwise.

The style adjuster read only the abstract 04 guide and left the drafter's form in place, so new claims ended
`…하는,⏎핵산 증폭 장치.` with the subject alone on a line (`상기 X는,⏎`), which no claim of the corpus does
(runs up to 2026-10-27: 94% and 67% of dependent claims, corpus 0% and 0%). Two features of that form are measured
here, on the reference the user chose: an edit of an existing set follows that set, new writing follows the corpus.
A feature the set does not decide (a tie, or no dependent claim to read) falls back to the corpus. Both features are
line breaks and a comma, so restyling them never moves the scope, and the engine returns a drifted revision to the
style adjuster.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .claimtext import ClaimParseError, as_claim_set, contains_user_lock, parse_claim_set

_CORPUS_HEAD = re.compile(r"^#*\s*【청구항\s*(\d+)】\s*$", re.MULTILINE)
_PREAMBLE_LINE = re.compile(r"^\s*제\s*\d+\s*항[^\n]*있어서\s*,\s*$")
_SUBJECT_LINE = re.compile(r"상기\s?[^,\n]{1,40}?[은는]\s*,")
_PREDICATE_END = re.compile(r"(?:되|하|있|없|갖|어지)는\s*,$")   # not 지는: 브릿지는, 수지는
_DEPENDENT = re.compile(r"^\s*제\s*\d+\s*항")

CORPUS = "CORPUS"
BASELINE_SET = "BASELINE_SET"


def _lines(body: str) -> list[str]:
    return [ln.rstrip() for ln in body.strip().splitlines() if ln.strip()]


def is_dependent(body: str) -> bool:
    return bool(_DEPENDENT.match(body))


def title_on_own_line(body: str) -> bool:
    """`…하는,⏎핵산 증폭 장치.` — the claim's name alone on the last line after a comma."""
    lines = _lines(body)
    if len(lines) < 2:
        return False
    last, prev = lines[-1].strip(), lines[-2]
    # A name has no predicate in it: `금속으로 형성되는 장치.` after `상기 커버는,` is the limitation, not the name.
    return (last.endswith(".") and prev.endswith(",") and not _PREAMBLE_LINE.match(prev) and "상기" not in last and len(last) <= 40
            and not re.search(r"[는은]\s", last))


def subject_on_own_line(body: str) -> bool:
    """`제1항에 있어서,⏎상기 X는,⏎…` — a dependent claim's subject alone on the line after the preamble."""
    lines = _lines(body)
    for i, ln in enumerate(lines[:-1]):
        if _PREAMBLE_LINE.match(ln):
            nxt = lines[i + 1].strip()
            # `…부착되는,⏎[명칭].` also ends in `는,`: a predicate line, not a subject waiting for its limitation.
            last_body = len(lines) - (2 if title_on_own_line(body) else 1)
            return bool(_SUBJECT_LINE.fullmatch(nxt)) and not _PREDICATE_END.search(nxt) and i + 1 < last_body
    return False


def corpus_claims(corpus_text: str) -> list[str]:
    """Claim bodies of the style corpus, cut before the next heading, rule or table."""
    heads = list(_CORPUS_HEAD.finditer(corpus_text))
    out: list[str] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(corpus_text)
        body = re.split(r"\n(?:#|---|\|)", corpus_text[m.end():end].strip())[0].strip()
        if body:
            out.append(body)
    return out


def _majority(flags: list[bool]) -> bool | None:
    if not flags:
        return None
    on = sum(flags)
    return None if on * 2 == len(flags) else on * 2 > len(flags)


@dataclass
class StyleProfile:
    source: str                    # CORPUS | BASELINE_SET
    title_on_own_line: bool
    subject_on_own_line: bool
    fallback: tuple[str, ...] = ()  # features the set did not decide, taken from the corpus

    @property
    def label(self) -> str:
        return "기존 세트" if self.source == BASELINE_SET else "코퍼스"


def target_profile(corpus_text: str, baseline_bodies: list[str] | None = None) -> StyleProfile:
    corpus = corpus_claims(corpus_text)
    c_title = bool(_majority([title_on_own_line(b) for b in corpus]))
    c_subject = bool(_majority([subject_on_own_line(b) for b in corpus if is_dependent(b)]))
    if not baseline_bodies:
        return StyleProfile(CORPUS, c_title, c_subject)
    title = _majority([title_on_own_line(b) for b in baseline_bodies])
    subject = _majority([subject_on_own_line(b) for b in baseline_bodies if is_dependent(b)])
    fallback = tuple(n for n, v in (("종결 명칭", title), ("주어 분절", subject)) if v is None)
    return StyleProfile(BASELINE_SET, c_title if title is None else title, c_subject if subject is None else subject, fallback)


def _claims(text: str) -> list[tuple[int, str]]:
    try:
        return [(c.claim_no, c.body) for c in parse_claim_set(as_claim_set(text))]
    except ClaimParseError:
        return []


def drift(text: str, profile: StyleProfile, only_nos: set[int] | None = None, user_lock: str | None = None) -> list[str]:
    """Where the claims of `text` leave the target form, one line per claim and feature.

    Claims outside `only_nos` (an edit's baseline claims) and claims USER_LOCK holds as written are not the
    adjuster's to restyle, so they are skipped.
    """
    out: list[str] = []
    for no, body in _claims(text):
        if only_nos is not None and no not in only_nos:
            continue
        if user_lock and contains_user_lock(user_lock, body):
            continue
        if title_on_own_line(body) != profile.title_on_own_line:
            want = "마지막 술어 뒤에 쉼표를 두고 줄을 바꿔 명칭만 적는다(`…하는,⏎[명칭].`)" if profile.title_on_own_line \
                else "명칭을 마지막 술어와 같은 줄에 쉼표 없이 붙인다(`…하는 [명칭].`)"
            out.append(f"제{no}항 종결 형식: {profile.label} 기준은 {want}")
        if is_dependent(body) and subject_on_own_line(body) != profile.subject_on_own_line:
            want = "`제m항에 있어서,` 다음 줄에 `상기 X는,`만 두고 줄을 바꾼다" if profile.subject_on_own_line \
                else "`상기 X는` 뒤에 쉼표·줄바꿈 없이 한정을 이어 쓴다(`상기 X는 …하는 [명칭].`)"
            out.append(f"제{no}항 주어 분절: {profile.label} 기준은 {want}")
    return out


def _example(corpus_text: str) -> str:
    """One independent claim and three short dependent claims of the corpus, for rhythm only."""
    bodies = corpus_claims(corpus_text)
    ind = next((b for b in bodies if not is_dependent(b)), "")
    deps = [b for b in bodies if is_dependent(b) and len(b) <= 120][:3]
    return "\n\n".join(x for x in [ind, *deps] if x)


def render_target(profile: StyleProfile, corpus_text: str) -> str:
    """The packet section that tells the style adjuster which form to write."""
    title = "`…하는,⏎[명칭].` — 마지막 술어 뒤 쉼표, 다음 줄에 명칭만" if profile.title_on_own_line else "`…하는 [명칭].` — 명칭을 마지막 술어와 같은 줄에 쉼표 없이"
    subject = "`상기 X는,`만 한 줄에 두고 줄을 바꿈" if profile.subject_on_own_line else "`상기 X는 …`처럼 주어 뒤에 쉼표·줄바꿈 없이 이어 씀"
    lines = [
        f"- 기준: {profile.label}" + (" — 이번 항은 사용자가 준 기존 세트에 붙으므로 그 세트의 형식을 따른다." if profile.source == BASELINE_SET
                                     else " — 새로 쓰는 청구항은 sources 코퍼스의 형식을 따른다."),
        f"- 종결 형식(독립항·종속항): {title}",
        f"- 종속항 주어 분절: {subject}",
    ]
    if profile.fallback:
        lines.append(f"- 기존 세트가 정하지 않아 코퍼스를 따른 항목: {', '.join(profile.fallback)}")
    if profile.source == BASELINE_SET:
        lines.append("- 문장 길이·연결 어미·`상기` 재지칭 방식도 기존 세트의 항을 본보기로 맞춘다. 세트 원문 항은 고치지 않는다.")
    else:
        lines.append("- 코퍼스 종속항은 한 문장에 한 한정축을 짧게 쓴다(중앙값 약 80자). 범위가 그대로인 한 긴 절은 `~하고`, `~하며`로 나누고 "
                     "`해당`, `~됨으로써` 연쇄처럼 코퍼스에 없는 연결을 줄인다. 범위가 바뀌어야 줄일 수 있으면 줄이지 않는다.")
    lines.append("- 이 두 형식은 엔진이 최종 문언에서 다시 재며, 어긋나면 같은 지적으로 STYLE_ONLY_REVISION에 되돌린다. "
                 "USER_LOCK 문언과 기존 세트 원문 항은 재지 않는다.")
    if profile.source == CORPUS:
        lines.append("\n코퍼스 본보기(형식·리듬 참고 전용 — 기술내용·구성·용어를 가져오지 않는다):\n\n" + _example(corpus_text))
    return "\n".join(lines)
