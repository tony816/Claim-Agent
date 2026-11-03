"""The evaluation form under each free-mode answer (평가 폼): how the claims were, whether the tool recommendation fit, which
tools the user ran and how each helped, and free feedback. The records measure the app so it can be improved.

Each save appends one line to `evaluations.jsonl` next to the conversations, with what the answer carried at that time
(the recommendation, the tools recorded as used, the model); a later save of the same answer is its new revision and the
summary reads the newest one. Nothing here reaches a model, a gate or the history the agents read.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .tool_advice import PHRASE, TOOLS

FILE = "evaluations.jsonl"
MAX_FEEDBACK = 4000
MAX_NOTE = 300
USABLE = ("as_is", "edit", "unusable")          # 그대로 사용 / 고쳐서 사용 / 사용 어려움
FIT = ("hit", "partial", "miss")                # 추천이 맞음 / 일부 / 아님
RATINGS = {"good": 1.0, "ok": 0.5, "bad": 0.0}  # 도움 됨 / 보통 / 도움 안 됨
TAGS = ("scope_narrow", "scope_broad", "missing", "unsupported", "unclear", "functional", "format", "verbose")
FORM_TOOLS = (*TOOLS, PHRASE)


def validate(data: dict) -> dict:
    """The form's fields, checked; an empty form is refused."""
    score = data.get("score")
    if score is not None and (not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5):
        raise ValueError("청구항 평가는 1~5점입니다.")
    usable, fit = data.get("usable"), data.get("fit")
    if usable is not None and usable not in USABLE:
        raise ValueError("사용 가능성 값을 확인하세요.")
    if fit is not None and fit not in FIT:
        raise ValueError("추천 적합도 값을 확인하세요.")
    tags = data.get("tags") or []
    if not isinstance(tags, list) or any(t not in TAGS for t in tags):
        raise ValueError("문제 유형 값을 확인하세요.")
    rows = data.get("tools") or []
    if not isinstance(rows, list) or len(rows) > len(FORM_TOOLS):
        raise ValueError("사용한 도구 목록을 확인하세요.")
    tools, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("tool") not in FORM_TOOLS or row["tool"] in seen:
            raise ValueError("사용한 도구 목록을 확인하세요.")
        rating = row.get("rating")
        if rating is not None and rating not in RATINGS:
            raise ValueError("도구 평가 값을 확인하세요.")
        note = str(row.get("note") or "").strip()
        if len(note) > MAX_NOTE:
            raise ValueError(f"도구별 메모는 {MAX_NOTE}자까지입니다.")
        seen.add(row["tool"])
        tools.append(dict(tool=row["tool"], rating=rating, note=note))
    feedback = str(data.get("feedback") or "").strip()
    if len(feedback) > MAX_FEEDBACK:
        raise ValueError(f"피드백은 {MAX_FEEDBACK}자까지입니다.")
    form = dict(score=score, usable=usable, fit=fit, tags=list(dict.fromkeys(tags)), tools=tools, feedback=feedback)
    if not (score or usable or fit or tags or tools or feedback):
        raise ValueError("평가 항목을 하나 이상 채운 뒤 저장하세요.")
    return form


def append(folder: Path, record: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / FILE).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def latest(folder: Path) -> list[dict]:
    """The newest record of each evaluated answer, oldest first; unreadable lines are skipped."""
    found: dict[tuple, dict] = {}
    try:
        lines = (folder / FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
            found[(record["session"], record["message"])] = record
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(found.values(), key=lambda r: r.get("ts", 0))


def _share(counts: dict, total: int) -> dict:
    return {key: round(value / total, 3) if total else None for key, value in counts.items()}


def summary(folder: Path, advised: int = 0) -> dict:
    """What the 평가 측정 tab shows: averages, distributions, how often a recommendation was used, per-tool help."""
    records = latest(folder)
    forms = [r["evaluation"] for r in records]
    scores = [f["score"] for f in forms if f.get("score")]
    recommended = used = hits = 0
    per_tool = {tool: dict(recommended=0, used=0, rated=0, help=0.0, good=0, ok=0, bad=0) for tool in FORM_TOOLS}
    for record in records:
        advice = set((record.get("advice") or {}).get("tools") or [])
        chosen = {row["tool"] for row in record["evaluation"].get("tools") or [] if row["tool"] != PHRASE}
        recommended += len(advice)
        used += len(chosen)
        hits += len(advice & chosen)
        for tool in advice:
            per_tool[tool]["recommended"] += 1
        for row in record["evaluation"].get("tools") or []:
            stats = per_tool[row["tool"]]
            stats["used"] += 1
            if row.get("rating"):
                stats["rated"] += 1
                stats[row["rating"]] += 1
                stats["help"] += RATINGS[row["rating"]]
    for stats in per_tool.values():
        stats["help"] = round(stats["help"] / stats["rated"], 3) if stats["rated"] else None
    tags = {tag: sum(tag in f.get("tags", []) for f in forms) for tag in TAGS}
    by_model: dict[str, list[int]] = {}
    for record in records:
        if record["evaluation"].get("score"):
            by_model.setdefault(record.get("model") or "-", []).append(record["evaluation"]["score"])
    by_source: dict[str, dict] = {}
    for record in records:
        source = (record.get("advice") or {}).get("source") or "none"
        fit = record["evaluation"].get("fit")
        row = by_source.setdefault(source, dict(count=0, hit=0, partial=0, miss=0))
        row["count"] += 1
        if fit:
            row[fit] += 1
    usable = {key: sum(f.get("usable") == key for f in forms) for key in USABLE}
    fit = {key: sum(f.get("fit") == key for f in forms) for key in FIT}
    recent = [dict(ts=r.get("ts"), score=r["evaluation"].get("score"), usable=r["evaluation"].get("usable"), feedback=r["evaluation"]["feedback"],
                   tools=[row["tool"] for row in r["evaluation"].get("tools") or []], model=r.get("model"))
              for r in reversed(records) if r["evaluation"].get("feedback")][:20]
    return dict(
        count=len(records), advised=advised, coverage=round(len(records) / advised, 3) if advised else None,
        score_avg=round(sum(scores) / len(scores), 2) if scores else None, scores={n: scores.count(n) for n in range(1, 6)},
        usable=usable, usable_share=_share(usable, sum(usable.values())), fit=fit, fit_share=_share(fit, sum(fit.values())),
        # Of the tools recommended, how many the user then ran; of the tools the user ran, how many had been recommended.
        precision=round(hits / recommended, 3) if recommended else None, recall=round(hits / used, 3) if used else None,
        tools=per_tool, tags=dict(sorted(tags.items(), key=lambda kv: -kv[1])), by_source=by_source,
        by_model={model: dict(count=len(s), score_avg=round(sum(s) / len(s), 2)) for model, s in by_model.items()},
        recent=recent, generated=time.time())


def export(folder: Path) -> bytes:
    """Every saved record, revisions included, as JSON lines."""
    try:
        return (folder / FILE).read_bytes()
    except OSError:
        return b""
