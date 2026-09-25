# -*- coding: utf-8 -*-
"""子进程：每秒用 UI 自动化读一遍 QQ / WhatsApp 当前打开的会话，按会话去重，新消息丢进队列。
跟 app/worker.py（微信截图 + OCR）发同样形状的消息，父进程不用区分来源：
  ("chat", 会话名) / ("lines", 会话名, [(who, name, text)], None) / ("uia_input", 会话名, hwnd, (x, y)) / ("status", 文字)
会话名带 App 前缀（「QQ · 张三」「WhatsApp · Anna」），跟微信的会话不会撞名。只读，不点任何东西。"""
import time
import traceback

from app.uia import APPS, PARSERS, Dedup, _Tree, find_windows


def run(q, enabled, interval=1.0):
    import ctypes

    ctypes.windll.user32.SetProcessDPIAware()  # 坐标跟 WGC / 填入用的物理像素对齐
    tree = _Tree()
    dedup = {}  # {会话名: Dedup}
    current = {}  # {app: 会话名}，变了才发 "chat"
    inputs = {}  # {会话名: (hwnd, point)}，变了才发
    warned = set()
    while True:
        if not enabled.is_set():
            enabled.wait()
        t0 = time.perf_counter()
        try:
            wins = find_windows()
        except Exception:
            wins = {}
        for app, hwnds in wins.items():
            got, error = None, None
            for hwnd in hwnds:  # 视频通话、图片查看之类的窗口没有输入框，跳过，用第一个聊天窗口
                try:
                    title, msgs, point = PARSERS[app](tree.dump(hwnd, web_root=(app == "whatsapp")))
                except Exception:  # 窗口正在关/刚弹出，单个窗口读失败很正常，别急着报
                    error = " ".join(traceback.format_exc().split())[-160:]
                    continue
                if point is not None:  # 没有输入框 = 不在聊天界面（会话列表、设置页……）
                    got = (hwnd, title, msgs, point)
                    break
            if got is None:
                if error and app not in warned:  # 一个聊天窗口都没读成，才告诉用户
                    q.put(("status", f"{APPS[app][0]} 读取失败：{error}"))
                    warned.add(app)
                continue
            warned.discard(app)
            hwnd, title, msgs, point = got
            name = f"{APPS[app][0]} · {title or '当前会话'}"
            if current.get(app) != name:
                current[app] = name
                q.put(("chat", name))
            if inputs.get(name) != (hwnd, point):
                inputs[name] = (hwnd, point)
                q.put(("uia_input", name, hwnd, point))
            new = dedup.setdefault(name, Dedup()).new(msgs)
            if new:
                q.put(("lines", name, new, None))
        time.sleep(max(0.2, interval - (time.perf_counter() - t0)))
