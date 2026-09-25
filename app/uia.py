# -*- coding: utf-8 -*-
"""QQ（NT 版，Electron）和 WhatsApp（WebView2）走 Windows UI 自动化直接读文字，不截图不 OCR。

两个都是网页内核，DOM 的 class 会原样出现在 UIA 的 ClassName 里，所以按 class 认「谁说的」：
- QQ：消息容器 msg-content-container + container--self（我）/ container--others（对方），
  前面紧挨一个 avatar-span，Name 是发言人；会话名 chat-header__contact-name；输入框在
  工具栏（id-func-bar-expression）下面。
- WhatsApp：消息行 message-in / message-out，输入框是带「消息」「message」字样的 Edit。

一次 FindAllBuildCache 把整棵树连同要用的属性一起取回来，跨进程只走一趟，几百个元素 100ms 上下。
只读，不点任何按钮；填入由 app/fill.py 做（点输入框 + Ctrl+V），绝不发送。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import re

u32 = ctypes.windll.user32

# UIA 属性 id
_NAME, _CLASS, _RECT, _TYPE, _AID = 30005, 30012, 30001, 30003, 30011
_TEXT, _IMAGE, _EDIT, _DOCUMENT = 50020, 50006, 50004, 50030

APPS = {
    # key: (显示前缀, 进程名集合)
    "qq": ("QQ", {"qq.exe"}),
    "whatsapp": ("WhatsApp", {"whatsapp.root.exe", "whatsapp.exe"}),
}

_TIME = re.compile(r"^\d{1,2}:\d{2}(\s*(AM|PM|上午|下午))?$", re.I)


def _exe_of(pid):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k32.CloseHandle(h)
    return os.path.basename(buf.value).lower() if ok else ""


def find_windows():
    """→ {app: [hwnd, …]}：每个 App 所有可见、没最小化的顶层窗口，面积大的在前。
    不能只取最大的那个：QQ 开着「视频通话」窗口时它比主窗口还大，里面没有聊天记录。
    调用方挨个试，用第一个认得出聊天输入框的。"""
    found = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe_of(pid.value)
        for app, (_, exes) in APPS.items():
            if exe in exes:
                r = ctypes.wintypes.RECT()
                u32.GetWindowRect(hwnd, ctypes.byref(r))
                area = (r.right - r.left) * (r.bottom - r.top)
                if area > 200 * 200:
                    found.setdefault(app, []).append((area, hwnd))
        return True

    u32.EnumWindows(cb, 0)
    return {app: [h for _, h in sorted(ws, reverse=True)] for app, ws in found.items()}


class _Tree:
    """整棵 UIA 树压平成 [(type, class, name, aid, (l,t,r,b))]，文档顺序。"""

    def __init__(self):
        from uiautomation.uiautomation import _AutomationClient

        self.ia = _AutomationClient.instance().IUIAutomation
        self.cr = self.ia.CreateCacheRequest()
        for pid in (_NAME, _CLASS, _RECT, _TYPE, _AID):
            self.cr.AddProperty(pid)
        self.true = self.ia.CreateTrueCondition()

    def dump(self, hwnd, web_root=False):
        """web_root：先找到网页文档（RootWebArea）再从它往下取。WhatsApp 的 WinUI 外壳里
        WebView2 会被 UIA 重复遍历几十遍（实测 1.8 万个元素、4 秒），从文档起步只有两百来个、50ms。"""
        root = self.ia.ElementFromHandleBuildCache(hwnd, self.cr)
        if web_root:
            doc = root.FindFirstBuildCache(4, self.ia.CreatePropertyCondition(_AID, "RootWebArea"), self.cr)
            if not doc:
                return []
            root = doc
        arr = root.FindAllBuildCache(4, self.true, self.cr)  # TreeScope_Descendants
        out = []
        for i in range(arr.Length):
            try:
                e = arr.GetElement(i)
                r = e.CachedBoundingRectangle
                out.append((e.CachedControlType, e.CachedClassName or "", e.CachedName or "",
                            e.CachedAutomationId or "", (r.left, r.top, r.right, r.bottom)))
            except Exception:  # 读的过程中这个元素没了（消息滚动/重绘，UIA_E_ELEMENTNOTAVAILABLE），跳过它
                continue
        return out


def _inside(r, box):
    return box[0] <= r[0] and r[2] <= box[2] and box[1] <= r[1] and r[3] <= box[3]


def parse_qq(items):
    """→ (会话名, [(who, name, text, 图片屏幕矩形 or None)], 输入框点击点 or None)。"""
    title, msgs, point = "", [], None
    speaker = None
    i = 0
    while i < len(items):
        typ, cls, name, aid, rect = items[i]
        if "chat-header__contact-name" in cls and name and not title:
            title = name.strip()
        elif "avatar-span" in cls:
            speaker = name.strip() or None
        elif "msg-content-container" in cls:
            who = "me" if "container--self" in cls else "her"
            parts, pic = [], None
            j = i + 1
            while j < len(items) and _inside(items[j][4], rect):
                t, c, n, _, r = items[j]
                if t == _TEXT and n.strip():
                    parts.append(n.strip())
                elif t == _IMAGE and n.strip() in ("图片", "表情"):
                    parts.append(f"[{n.strip()}]")
                    if "pic-element" in c:
                        pic = r
                j += 1
            text = "".join(parts).strip()
            if text:
                msgs.append((who, speaker if who == "her" else None, text, pic))
            i = j
            continue
        elif aid == "id-func-bar-expression":  # 表情按钮那一排下面就是输入框
            point = (rect[0] + 60, rect[3] + 40)
        i += 1
    return title, msgs, point


def parse_whatsapp(items):
    """→ (会话名, [(who, name, text)], 输入框点击点 or None)。
    消息行是 message-in / message-out；正文取行里不是时间戳的文字。"""
    title, msgs, point = "", [], None
    edits = []
    i = 0
    while i < len(items):
        typ, cls, name, aid, rect = items[i]
        if typ == _EDIT and re.search(r"消息|message|nachricht", name, re.I):
            edits.append(rect)
        if re.search(r"(^|\s)message-(in|out)(\s|$)", cls):
            who = "me" if "message-out" in cls else "her"
            parts = []
            j = i + 1
            while j < len(items) and _inside(items[j][4], rect):
                t, c, n, _, _ = items[j]
                n = n.strip()
                if t == _TEXT and n and not _TIME.match(n) and (not parts or parts[-1] != n):
                    parts.append(n)
                j += 1
            text = " ".join(parts).strip()
            if text:
                msgs.append((who, None, text, None))
            i = j
            continue
        i += 1
    if edits:
        r = max(edits, key=lambda e: e[1])  # 最靠下那个是聊天输入框（上面的是搜索框）
        point = ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
    for typ, cls, name, aid, rect in items:  # 会话名：输入框出现后，消息区上方头部第一行字
        if typ == _TEXT and name.strip() and edits and rect[1] < min(e[1] for e in edits) and \
                rect[0] > min(e[0] for e in edits) - 80 and "title" in cls.lower():
            title = name.strip()
            break
    return title, msgs, point


PARSERS = {"qq": parse_qq, "whatsapp": parse_whatsapp}


def _key(m):
    """去重用的键：谁 + 文字；图片消息文字都是「[图片]」，再带上图的尺寸，不然第二张图会被当成见过的。"""
    pic = m[3] if len(m) > 3 else None
    return (m[0], m[2], (pic[2] - pic[0], pic[3] - pic[1]) if pic else None)


class Dedup:
    """一个会话一个：跟 app/ocr.Reader.new_lines 同一套规则，只是文字是精确的，不用模糊比。"""

    def __init__(self):
        self.seen = []
        self.first = True  # 第一次读这个会话：屏幕上的全是旧消息，只当上下文，不去抓图

    def new(self, msgs):
        keys = [_key(m) for m in msgs]
        known = [k for k, key in enumerate(keys) if key in self.seen]
        floor = max(known) if known else -1
        out = [m for k, m in enumerate(msgs) if k > floor and keys[k] not in self.seen]
        self.seen.extend(key for key in keys if key not in self.seen)
        del self.seen[:-800]
        return out
