# -*- coding: utf-8 -*-
"""QOL 包的纯逻辑：暂停倒计时、窗口位置钳制。不 import Qt，便于单测（SPEC-round4 Q4/Q5）。"""


def fmt_remaining(seconds) -> str:
    """倒计时文本 mm:ss；负值归零（到点那一帧不显示负数）。"""
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def pause_due(now, until) -> bool:
    """到点判定：now 达到 / 超过 until 就该自动恢复了。"""
    return now >= until


def fit_rect(rect, screens):
    """窗口矩形 (x, y, w, h) 钳到各屏 availableGeometry（同格式元组列表）的并集内。
    与某屏有交集 → 钳回相交最多的那块屏里；完全落在所有屏之外 → 主屏右缘默认位。"""
    x, y, w, h = rect
    if not screens:
        return rect

    def _overlap(s):
        sx, sy, sw, sh = s
        ix = max(0, min(x + w, sx + sw) - max(x, sx))
        iy = max(0, min(y + h, sy + sh) - max(y, sy))
        return ix * iy

    best = max(screens, key=_overlap)
    if _overlap(best) == 0:
        sx, sy, sw, sh = screens[0]
        return (sx + sw - w - 20, sy + 24, w, h)  # 全出界：回主屏右缘默认位
    sx, sy, sw, sh = best
    nx = min(max(x, sx), max(sx, sx + sw - w))  # 窗口比屏还宽就靠左贴边
    ny = min(max(y, sy), max(sy, sy + sh - h))
    return (nx, ny, w, h)
