# -*- coding: utf-8 -*-
"""端到端冒烟：一段对话跑一遍完整链，打印分析和候选回复（要真 key，会联网）。

回复只走起草这一条路（S4 起不再有 Jev 判断 / 排序）。只要起草那把 key：

    set LLM_API_KEY=...                (Windows)
    export LLM_API_KEY=...             (mac/Linux)
    python tools/demo.py

默认起草走 DeepSeek 官网直连。换别家改下面的常量（可选的来源见 core/providers.py 的 DRAFT_PROVIDERS）。
"""
from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.engine import analyze_bilingual
from core.jev_client import JevError
from core.keygate import KeyRouteError
from core.route import ReplyPlan, snapshot_route

MESSAGES = [
    ("her", "你今天是不是又忘了我跟你说过什么？"),
    ("me", "记得，你先别提示我，让我自己说。"),
    ("her", "那你说。"),
    ("me", "等一下，我想说完整一点。"),
    ("her", "你最好是。"),
]
RELATIONSHIP = "romantic partners"
PROVIDER = "deepseek"  # 起草来源，见 core.providers.DRAFT_PROVIDERS


def main() -> int:
    print("对话:")
    for w, t in MESSAGES:
        print(f"  {w}: {t}")
    try:
        plan = ReplyPlan(snapshot_route(PROVIDER, None, None), RELATIONSHIP, 10, "", False)
        r = analyze_bilingual(MESSAGES, plan)
    except (JevError, KeyRouteError) as e:
        print(f"\n失败: {e}")
        return 1
    if r.get("analysis"):
        print("\n分析:\n  " + r["analysis"])
    print("\n候选（第一条是推荐）:")
    for i, c in enumerate(r["candidates"]):
        print(f"  {'★' if i == r['best_index'] else ' '} {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
