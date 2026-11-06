"""작업 도구의 Skills: the user's own Claude skills (문제 정의, 형태학적 분석) run as one general-agent turn.

The skill's SKILL.md is copied into `claim_agent/skills/<name>/` from the user's Claude account (the desktop app keeps a
cache of account skills; `sync` refreshes the copies from it). A turn sends the composer text with the skill's
instructions as the system prompt, in either work mode, without the router or a claim role. The answer is a thinking aid:
it writes no claim wording (claims go through the gates or 자유 작업), it is not raw material and it creates no LOCK.
Skill turns stay in the conversation history so a skill with stages (발산 → 상자 → 평가) can continue on the next message.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from .provider.base import GenParams, LLMProvider
from .store.telemetry import UsageMeter

SKILL_DIR = Path(__file__).with_name("skills")
# name → (panel label, one-line description)
SKILLS = {
    "problem-definition": ("문제 정의", "막막한 문제를 모호한 단어 짚기→구체화→필요 요소→필요 가치→목적 지향 재진술 순서로 한 줄 문제 정의로 바꿉니다"),
    "morphological-analysis": ("형태학적 분석", "기존 해에서 차이 축·전제 축을 뽑아 발산하고(/발산), 상자·교차 일관성으로 조합 공간을 만든 뒤(/상자) 요청 시에만 평가합니다(/평가)"),
}
SKILL_ROLE = "Skills"
SKILL_SYSTEM = """너는 사용자가 작업 도구의 Skills에서 고른 '{label}' 스킬로 답하는 일반 에이전트다. 아래 스킬 지침을 그대로 따른다.

이 앱에서 지킬 것:
- 코드 실행·웹 검색·외부 도구는 쓸 수 없다. 지침이 Python 계산이나 검색을 말하는 곳은 손으로 정확히 할 수 있는 만큼만 하고, 해야 하는데 하지 못한 개수·거리·순위는 '계산 미실행'으로 밝히며 어림으로 단정하지 않는다.
- 지침이 다른 스킬로 넘기라고 하는 작업은 그 스킬 이름과 이유만 알린다.
- 기술 사실은 사용자 메시지·첨부·대화·프로젝트 자료에서만 가져오고, 거기에 없으면 '근거 미확인'으로 남긴다.
- 【청구항 N】 형식의 청구항 문언은 쓰지 않는다. 청구항이 필요하면 게이트 자동이나 자유 작업으로 요청하라고 한 줄 안내한다.

# 스킬 지침: {name}

{body}"""
LABEL = "※ Skills · {label} — 일반 에이전트가 사용자 스킬 지침으로 답했습니다. 청구항 문언·게이트 판정이 아니며 원자료나 LOCK 근거가 아닙니다."


def label(name: str) -> str:
    return SKILLS[name][0]


def instructions(name: str) -> str:
    """The skill's SKILL.md without its frontmatter."""
    if name not in SKILLS:
        raise ValueError("지원하지 않는 스킬입니다.")
    text = (SKILL_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S).strip()


def skill_turn(provider: LLMProvider, cfg, request: dict) -> dict:
    """One turn of the general agent under the chosen skill's instructions."""
    from .chat import generate_turn, user_message
    from .free_work import FREE_THINKING, _preset_parts
    from .store.report import usage_note

    name = request["skill"]
    started = time.time()
    provider = UsageMeter(provider, cfg.telemetry.pricing)
    message = user_message(request["text"], "", request.get("files", []))
    preset = request.get("project_attachments") or []
    sent = {"role": "user", "parts": [*_preset_parts(preset), *message["parts"]]} if preset else None
    system = SKILL_SYSTEM.format(label=label(name), name=name, body=instructions(name))
    gen = GenParams(temperature=0.5, thinking_level=FREE_THINKING, max_output_tokens=cfg.model.max_output_tokens)
    result = generate_turn(provider, request["model"], request.get("history", []), message, None, request.get("instructions"),
                           sent=sent, system=system, role=SKILL_ROLE, gen=gen)
    answer = LABEL.format(label=label(name)) + "\n\n" + result["answer"] + "\n\n" + usage_note(provider.usage, time.time() - started)
    return {**result, "answer": answer, "effective_mode": "SKILL", "status": "complete"}


def claude_cache() -> Path | None:
    """The newest folder of account skills the Claude desktop app keeps (Windows), if any."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Claude-Data"
    found = [p.parent for p in base.glob("*/local-agent-mode-sessions/skills-plugin/*/*/skills/problem-definition")] if base.is_dir() else []
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def sync(source: Path | None = None) -> list[str]:
    """Copy the SKILL.md of every skill in SKILLS from the user's Claude skills folder; returns the names copied."""
    source = source or claude_cache()
    if source is None:
        raise FileNotFoundError("Claude 데스크톱 앱의 스킬 폴더를 찾지 못했습니다. --source로 skills 폴더를 지정하세요.")
    copied = []
    for name in SKILLS:
        path = source / name / "SKILL.md"
        if path.exists():
            (SKILL_DIR / name).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, SKILL_DIR / name / "SKILL.md")
            copied.append(name)
    return copied


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Claude 계정 스킬(문제 정의·형태학적 분석)을 작업 도구 Skills로 다시 복사합니다.")
    parser.add_argument("--source", type=Path, help="SKILL.md 폴더들이 있는 skills 폴더 (기본: Claude 데스크톱 앱 캐시)")
    print("복사함:", ", ".join(sync(parser.parse_args().source)) or "없음")
