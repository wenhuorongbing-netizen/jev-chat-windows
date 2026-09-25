# -*- coding: utf-8 -*-
"""对方最新发来的图片 → 缩好的 JPEG（base64），喂给能看图的起草模型。全程只在内存里，不落盘。

两条路：
- QQ 原图：QQ NT 把收到的图按月存成普通文件（Tencent Files\\<号>\\nt_qq\\nt_data\\Pic\\YYYY-MM\\Ori|Thumb）。
  UI 自动化不告诉我们是哪个文件，只能按时间对：消息出现前后十几秒里只新写了一个文件，才认它；
  拿不准（好几个群同时来图）就不用，走下一条。
- 窗口截图：PrintWindow 截聊天窗口（被别的窗口挡着也截得到），按 UI 自动化给的图片位置裁出来。
  微信 4.x 的图是加密的 .dat、WhatsApp 的图在 WebView2 的加密缓存里，这两家只能走这条。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as w
import glob
import io
import os
import time

_MAX_SIDE = 1024  # DeepSeek 一张图最多按 1024 token 算，再大也是白传

# 64 位下 GDI 句柄是指针宽度，不声明类型 ctypes 会按 32 位 int 截断/溢出
_u32, _g32 = ctypes.windll.user32, ctypes.windll.gdi32
_H = ctypes.c_void_p
for _fn, _res, _args in (
    (_u32.GetWindowDC, _H, [_H]), (_u32.ReleaseDC, ctypes.c_int, [_H, _H]),
    (_u32.PrintWindow, w.BOOL, [_H, _H, w.UINT]),
    (_g32.CreateCompatibleDC, _H, [_H]), (_g32.CreateCompatibleBitmap, _H, [_H, ctypes.c_int, ctypes.c_int]),
    (_g32.SelectObject, _H, [_H, _H]), (_g32.DeleteObject, w.BOOL, [_H]), (_g32.DeleteDC, w.BOOL, [_H]),
    (_g32.GetDIBits, ctypes.c_int, [_H, _H, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT]),
):
    _fn.restype, _fn.argtypes = _res, _args


def _encode(img) -> str:
    img = img.convert("RGB")
    img.thumbnail((_MAX_SIDE, _MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def qq_file_since(since: float, window: float = 20.0) -> str | None:
    """QQ 图片目录里 [since-window, now] 之间新写的文件：原图唯一就用原图，否则缩略图唯一就用缩略图。"""
    from PIL import Image

    month = time.strftime("%Y-%m")
    home = os.path.join(os.path.expanduser("~"), "Documents", "Tencent Files")
    for sub in ("Ori", "Thumb"):
        fresh = [p for p in glob.glob(os.path.join(home, "*", "nt_qq", "nt_data", "Pic", month, sub, "*"))
                 if os.path.getmtime(p) >= since - window]
        if len(fresh) == 1:
            try:
                with Image.open(fresh[0]) as img:
                    return _encode(img)
            except Exception:
                return None
        if len(fresh) > 1:
            return None  # 同一时间好几张，对不准是哪张，宁可不用
    return None


def crop_window(hwnd: int, rect: tuple) -> str | None:
    """PrintWindow 截整个窗口，再按屏幕坐标 rect=(l,t,r,b) 裁出图片那块。图片滚出可见区就返回 None。"""
    from PIL import Image

    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    wr = w.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(wr))
    W, H = wr.right - wr.left, wr.bottom - wr.top
    l, t, r, b = rect[0] - wr.left, rect[1] - wr.top, rect[2] - wr.left, rect[3] - wr.top
    l, t, r, b = max(l, 0), max(t, 0), min(r, W), min(b, H)
    if W <= 0 or H <= 0 or r - l < 16 or b - t < 16:
        return None
    hdc = u32.GetWindowDC(hwnd)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, W, H)
    g32.SelectObject(mdc, bmp)
    try:
        if not u32.PrintWindow(hwnd, mdc, 2):  # PW_RENDERFULLCONTENT：GPU 合成的内容（Chromium）也截得到
            return None

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                        ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                        ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                        ("biClrImportant", w.DWORD)]
        bi = BIH(ctypes.sizeof(BIH), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(W * H * 4)
        g32.GetDIBits(mdc, bmp, 0, H, buf, ctypes.byref(bi), 0)
        img = Image.frombuffer("RGBA", (W, H), buf, "raw", "BGRA", 0, 1).crop((l, t, r, b))
        return _encode(img)
    finally:
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(hwnd, hdc)
