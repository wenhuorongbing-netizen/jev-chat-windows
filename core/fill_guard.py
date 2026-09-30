# -*- coding: utf-8 -*-
"""填入前的会话核验。纯函数：候选属于哪个会话、那个 App 的窗口现在开着哪个会话，对不上就不填。

会话名只是采集来源报上来的标签，不是身份证明——所以这里只回答「现在开着的还是不是候选所属的那个」，
拿不准（App 还没报过当前会话）也按不行处理，由调用方退回「复制后自己粘贴」。
"""
from __future__ import annotations


class CopyOnly(RuntimeError):
    """这个 App 的候选只能复制、不能自动填（没有能现读现对的会话标识）：界面把回复放进剪贴板，由人粘贴。"""


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


def check_fresh(chat: str, newest: tuple | None, fresh_chat: str | None,
                fresh_msgs: list, has_input: bool) -> str | None:
    """打字前一刻的新鲜核验：调用方刚刚重新读了目标窗口，这里比对读到的东西。
    chat / newest = 生成候选时的会话名与最新一条 (谁, 正文)；fresh_chat = 这次读到的会话名（读不到标题 = None）；
    fresh_msgs = 这次读到的 [(谁, 正文)]。任何一项对不上或读不出来都不填（False refusal 可以，错填不行）。"""
    if not chat or not newest:
        return "候选没有记录生成时的会话状态"
    if fresh_chat is None:
        return "现在读不到聊天窗口的会话标题，没有填入"
    if fresh_chat != chat:
        return "聊天窗口已经切到别的会话，没有填入"
    if not has_input:
        return "聊天窗口现在没有输入框，没有填入"
    if not fresh_msgs or tuple(fresh_msgs[-1]) != tuple(newest):
        return "会话里有新消息或已滚动，候选可能过期，没有填入"
    return None


# 今天各 App 怎么填（契约 contracts/jev/v1/fill_support.json 的 Windows 一半；测试拿它对 app/uia.PARSERS 和契约文件）：
#   fresh-verified = 打字前重读窗口并比对通过才填；
#   copy-only = 没有能现读现对的会话标识，只复制、由人粘贴（main.fill_reply 抛 CopyOnly）。
FILL_SUPPORT = {
    "qq": "fresh-verified",
    "whatsapp": "fresh-verified",
    "wechat": "copy-only",
}
