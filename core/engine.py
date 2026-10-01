# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → 一次起草调用 → 结构化结果（候选 + 中文对照 + 分析）。

平台无关。悬浮窗只调 analyze_bilingual / reroll_candidate；早先的 Jev 判断→起草→排序三段式在 S4 退役了，
回复只走起草这一条路。每次调用都拿一份 ReplyPlan 快照（core/route）：路由和口径设置都在发起时定好，这里不读任何设置。
"""
from __future__ import annotations

try:
    from .draft import _similar, draft_bilingual, draft_candidates, her_latest, is_chinese
    from .jev_client import invalid_response
    from .route import ReplyPlan
except ImportError:
    from draft import _similar, draft_bilingual, draft_candidates, her_latest, is_chinese
    from jev_client import invalid_response
    from route import ReplyPlan


def analyze_bilingual(messages: list, plan: ReplyPlan, timeout: float = 30,
                      reply_to: str | None = None, image: str | None = None) -> dict:
    """智能回复（跟随对方语言），只要起草那把 key：
    对方说中文 → 中文起草（口吻规则最全），不翻译；
    对方说外语 → 一次调用拿到「语言 + 中文翻译 + 3 条同语言回复（带中文对照）」。
    messages: [(from, text[, name])]，from ∈ {her, me}，最新一条在最后。
    返回 {candidates, best_index, best_reply, scores, answers, usage, reply_to, analysis, lang, translation, glosses}；
    第一条就是推荐。没有任何可用候选就抛 invalid_response，不会拿分析当回复、也不凑数。"""
    her = her_latest(messages)
    # 只发了图、没说话：没有语言可认，按中文走（图里的外文模型自己看得懂）
    if is_chinese(her) or not her.replace("[图片]", "").replace("[表情]", "").strip():
        info = {}
        cands = draft_candidates(messages, plan.relationship, plan.route, timeout=timeout, keep=plan.context,
                                 reply_to=reply_to, style=plan.style, thinking=plan.thinking, image=image,
                                 info=info)
        if not cands:
            raise invalid_response("起草结果没有可用候选回复")
        return {"candidates": cands, "best_index": 0, "best_reply": cands[0], "scores": [],
                "answers": {}, "usage": {}, "reply_to": reply_to, "analysis": info.get("analysis", ""),
                "lang": "中文", "translation": "", "glosses": []}
    r = draft_bilingual(messages, plan.relationship, plan.route, timeout=timeout, keep=plan.context,
                        reply_to=reply_to, style=plan.style, thinking=plan.thinking, image=image)
    return {"candidates": r["candidates"], "best_index": 0, "best_reply": r["candidates"][0],
            "scores": [], "answers": {}, "usage": {}, "reply_to": reply_to,
            "lang": r["lang"] or "外语", "translation": r["translation"], "glosses": r["glosses"],
            "analysis": r.get("analysis", "")}


def reroll_candidate(messages, plan: ReplyPlan, lang, existing, timeout=30,
                     reply_to=None, image=None) -> tuple[str, str]:
    """重 roll 一条候选（「换一条」）：走与产生这批候选相同的起草路径
    （lang=="中文" → draft_candidates，否则 → draft_bilingual），existing 传给起草层避免重复。
    返回 (正文, 中文对照)：挑第一条与 existing 任何一条都不重复的（draft._similar，ratio ≥ 0.75）；
    外语路径 gloss 与正文同索引取走；都不新鲜就取第一条兜底（仍返回）。_sanitize 照旧生效。"""
    if lang == "中文":
        cands = draft_candidates(messages, plan.relationship, plan.route, timeout=timeout, keep=plan.context,
                                 reply_to=reply_to, style=plan.style, thinking=plan.thinking,
                                 avoid=list(existing))
        if not cands:
            raise invalid_response("重 roll 起草结果没有可用候选回复")
        for cand in cands:
            if not any(_similar(cand, old) for old in existing):
                return cand, ""
        return cands[0], ""
    r = draft_bilingual(messages, plan.relationship, plan.route, timeout=timeout, keep=plan.context,
                        reply_to=reply_to, style=plan.style, thinking=plan.thinking, image=image,
                        avoid=list(existing))
    for cand, gloss in zip(r["candidates"], r["glosses"]):
        if not any(_similar(cand, old) for old in existing):
            return cand, gloss
    return r["candidates"][0], r["glosses"][0] if r["glosses"] else ""


if __name__ == "__main__":
    # 候选被过滤光时要抛 invalid_response（JevError），不能在取第一条时 IndexError。
    from unittest.mock import patch

    try:
        from .jev_client import JevError
        from .route import ReplyRoute
    except ImportError:
        from jev_client import JevError
        from route import ReplyRoute

    plan = ReplyPlan(ReplyRoute("deepseek", "openai", "https://api.deepseek.com", "m",
                                "https://api.deepseek.com"), "friends", 10, "", False)
    with patch("__main__.draft_candidates", return_value=[]):
        try:
            analyze_bilingual([("her", "你好")], plan)
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "没有可用候选" in str(e) and e.kind == "invalid_response"
    print("engine ok")
