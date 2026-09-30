# -*- coding: utf-8 -*-
"""哪张图可以跟着这次请求一起发给模型。纯函数，不碰界面、配置和网络。

口径（S0 收紧）：只有「对方的最新一条消息本身就是图，并且用户明确开了识别图片」才带图。
更早的图、夹在后面又来了文字的图、自己发的图，一律不带。
"""
from __future__ import annotations


def newest_image(pic, msgs, enabled: bool):
    """pic = (这张图是历史里的第几条（从 1 数）, base64)，没有则 None；msgs = 这次请求用的消息列表。
    图片必须属于 msgs 的最后一条，且最后一条是对方发的；enabled 是用户的明确授权（默认关）。"""
    if not enabled or not pic or not msgs:
        return None
    at, data = pic
    if at != len(msgs) or msgs[-1][0] != "her":
        return None
    return data or None
