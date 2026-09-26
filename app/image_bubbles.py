# -*- coding: utf-8 -*-
"""消息区帧（numpy RGB）里找「对方发来的图片泡泡」。纯 numpy，无 OCR 依赖，可单测。

思路：照片/表情包的颜色跟面板底色、白气泡、微信绿泡都不一样——
掩码 = 与 pane_bg 距离 > 24 且与白色距离 > 24 且不是绿泡（who_said 同款判定）的像素；
y 投影聚成带（允许小缺口，照片里的白天空会把带劈开），带内取 x 范围得候选区域；
过滤后取最靠下的那个（最新消息）。"""
import numpy as np

_MIN_SIDE = 48       # 宽/高至少这么多 px（头像 40、文字条都到不了）
_MIN_AREA = 3600
_MIN_FILL = 0.5      # 区域内掩码填充率（链接卡片、引用块到不了）
_AVATAR_RIGHT = 0.14  # 区域右缘 < 0.14*W 且宽 < 0.12*W → 头像条
_AVATAR_W = 0.12
_MY_CENTER = 0.75    # 区域中心 x > 0.75*W → 我自己发的
_GAP = 8             # 聚带时允许的小缺口行数


def find_her_image(chat_rgb, pane_bg):
    """→ ((x0, y0, x1, y1), ahash) | None。坐标相对消息区裁剪帧。"""
    h, w = chat_rgb.shape[:2]
    if h < _MIN_SIDE or w < _MIN_SIDE:
        return None
    px = chat_rgb.astype(np.int16)
    bg = np.asarray(pane_bg, dtype=np.int16).reshape(1, 1, 3)
    dist_bg = np.abs(px - bg).sum(axis=2)
    dist_white = np.abs(px - 255).sum(axis=2)
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    green = (g > r + 40) & (g > b + 40)  # 微信绿泡，跟 who_said 一个判定
    mask = (dist_bg > 24) & (dist_white > 24) & ~green

    # y 投影聚带：有掩码像素的行段，允许 ≤_GAP 行的小缺口
    has = mask.any(axis=1)
    bridged = np.convolve(has.astype(np.int32), np.ones(2 * _GAP + 1, dtype=np.int32),
                          mode="same") > 0
    best = None
    y = 0
    while y < h:
        if not bridged[y]:
            y += 1
            continue
        y0 = y
        while y < h and bridged[y]:
            y += 1
        ys = np.flatnonzero(has[y0:y])
        band = (y0 + ys[0], y0 + ys[-1] + 1)  # 带的实际起止（缺口不收进边界）
        sub = mask[band[0]:band[1]]
        cols = np.flatnonzero(sub.any(axis=0))
        x0, x1 = cols[0], cols[-1] + 1
        height, width = band[1] - band[0], x1 - x0
        fill = float(sub[:, x0:x1].mean())
        if (width >= _MIN_SIDE and height >= _MIN_SIDE and width * height >= _MIN_AREA
                and fill >= _MIN_FILL
                and not (x1 < _AVATAR_RIGHT * w and width < _AVATAR_W * w)  # 头像条
                and (x0 + x1) / 2 <= _MY_CENTER * w):  # 我发的在右边
            if best is None or band[1] > best[0][3]:
                best = ((int(x0), int(band[0]), int(x1), int(band[1])), None)
    if best is None:
        return None
    rect = best[0]
    crop = chat_rgb[rect[1]:rect[3], rect[0]:rect[2]]
    return rect, ahash(crop)


def ahash(crop):
    """16×16 灰度均值哈希（32 bytes）：纯 numpy 最近邻抽样，同一帧两次调用结果一致。"""
    h, w = crop.shape[:2]
    if h == 0 or w == 0:
        return b""
    gray = crop.astype(np.float32) @ [0.299, 0.587, 0.114]
    ys = np.linspace(0, h - 1, 16).astype(int)
    xs = np.linspace(0, w - 1, 16).astype(int)
    small = gray[np.ix_(ys, xs)]
    return np.packbits(small > small.mean()).tobytes()
