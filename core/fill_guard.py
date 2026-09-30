# -*- coding: utf-8 -*-
"""填入前的会话核验。纯函数：候选属于哪个会话、那个 App 的窗口现在开着哪个会话，对不上就不填。

会话名只是采集来源报上来的标签，不是身份证明——所以这里只回答「现在开着的还是不是候选所属的那个」，
拿不准（App 还没报过当前会话）也按不行处理，由调用方退回「复制后自己粘贴」。
"""
from __future__ import annotations


def check_fill_target(chat: str, open_chat: str | None) -> str | None:
    """chat = 候选所属的会话；open_chat = 该 App 最近一次报上来的当前会话（None = 没报过）。
    可以填返回 None，不可以返回一句给人看的原因（不含聊天内容）。"""
    if not chat:
        return "候选不属于任何会话"
    if open_chat is None:
        return "还没确认聊天窗口现在开着哪个会话"
    if open_chat != chat:
        return "聊天窗口已经切到别的会话，没有填入"
    return None
