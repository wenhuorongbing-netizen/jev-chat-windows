# -*- coding: utf-8 -*-
"""回复卡 / 「对方说」卡的显示规则。纯逻辑，不 import Qt，便于单测（SPEC W2/W3）。"""

_CHINESE = {"中文", "chinese", "zh"}


def _is_chinese(lang) -> bool:
    """语言字段是不是中文；接口给过「中文 / Chinese / zh」几种写法，大小写不敏感。"""
    return (lang or "").strip().lower() in _CHINESE


def shown_gloss(text, gloss, lang) -> str:
    """候选正文下面那行中文意思：空、和正文重复（纯数字正文常被模型原样抄回来）、
    或对方本来就是中文时都不显示——返回 ""。否则原样返回 gloss。"""
    if not gloss or not gloss.strip():
        return ""
    if _is_chinese(lang):
        return ""
    if gloss.strip() == (text or "").strip():
        return ""
    return gloss


def shown_translation(translation, lang) -> str:
    """「对方说」卡里的译文行：空、或对方说中文时不显示（原文就是中文，再译一遍只会让人分不清）。"""
    if not translation or not translation.strip():
        return ""
    if _is_chinese(lang):
        return ""
    return translation
