"""Compare the free work agent with the bare model on one saved web request (docs/도구사용_개선로그.md #13).

The bare model gets the same text and images under a one-line system prompt at high effort, the way a plain chat
would. The free agent answers the same request through `free_turn`. Both answers are written to <out_dir> with a
claim count, so a prompt or gate change can be checked against the bare model before it ships.

usage: python scripts/compare_vanilla.py .tui/requests/<id>/chat.json <out_dir> [--with-history]

Without --with-history both answer as a fresh chat; with it the free agent also gets the conversation's history.
Each run makes two live model calls on the configured provider.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claim_agent import free_work  # noqa: E402
from claim_agent.chat import generate_turn, user_message  # noqa: E402
from claim_agent.provider.base import GenParams  # noqa: E402
from claim_agent.runtime import build_runtime, live_provider  # noqa: E402

BARE_SYSTEM = "You are Claude, a helpful AI assistant made by Anthropic."
_HEAD = re.compile(r"[【\[]\s*청구항\s*\d+\s*[】\]]")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chat_json", type=Path, help="a saved web request (.tui/requests/<id>/chat.json)")
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--with-history", action="store_true", help="give the free agent the conversation's history")
    args = ap.parse_args()
    request = json.loads(args.chat_json.read_text(encoding="utf-8"))
    if not args.with_history:
        request["history"] = []
    rt = build_runtime(ROOT)
    provider = live_provider(rt.cfg)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    files = [*(a["path"] for a in request.get("project_attachments") or []), *request.get("files", [])]
    bare = generate_turn(provider, request["model"], [], user_message(request["text"], "", files), None, system=BARE_SYSTEM,
                         role="bare-model", gen=GenParams(temperature=1.0, thinking_level="HIGH", max_output_tokens=rt.cfg.model.max_output_tokens))
    free = free_work.free_turn(provider, rt.cfg, request)
    for name, answer in (("bare", bare["answer"]), ("free", free["answer"])):
        (args.out_dir / f"{name}.md").write_text(answer, encoding="utf-8")
        print(f"{name}: {len(_HEAD.findall(answer))} claims -> {args.out_dir / (name + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
