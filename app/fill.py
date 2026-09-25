# -*- coding: utf-8 -*-
"""把选中的候选填进微信输入框：写剪贴板 → 点输入框 → Ctrl+V。绝不发回车、绝不点发送。"""
import ctypes
import ctypes.wintypes as w
import time

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32

# 64 位下 ctypes.windll 默认 restype 是 32 位 c_int，而 GlobalAlloc 返回 64 位 HGLOBAL——
# 不声明类型句柄会被截断成垃圾值，GlobalLock(垃圾) 返回 NULL，memmove(NULL,…) 就是
# "access violation writing 0x0"。所有带句柄/指针的函数必须显式声明。
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
k32.GlobalFree.argtypes = [ctypes.c_void_p]
u32.SetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]


def set_clipboard(text):
    """写剪贴板。剪贴板可能被别的程序占着（剪贴板管理器、截图工具），重试几次。"""
    data = text.encode("utf-16-le") + b"\0\0"
    for attempt in range(10):
        if not u32.OpenClipboard(None):
            time.sleep(0.05)
            continue
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(0x2, len(data))  # GMEM_MOVEABLE
            if not h:
                raise RuntimeError("GlobalAlloc 失败")
            p = k32.GlobalLock(h)
            if not p:
                k32.GlobalFree(h)
                raise RuntimeError("GlobalLock 失败")
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(13, h):  # CF_UNICODETEXT；成功后句柄归系统，不能 Free
                k32.GlobalFree(h)
                raise RuntimeError(f"SetClipboardData 失败 (attempt {attempt})")
            return
        finally:
            u32.CloseClipboard()
    raise RuntimeError("OpenClipboard 连续失败，剪贴板被其他程序占用")


# ------------------------------------------------------------------ 不碰鼠标、不动剪贴板的填入
# 以前是「写剪贴板 → 把真鼠标挪过去点一下 → Ctrl+V」：鼠标会被抢走，剪贴板也被覆盖。现在：
# - QQ / WhatsApp：UI 自动化找到输入框，SetFocus 把焦点直接给它（顺带把窗口带到前台）；
# - 微信（Qt 自绘，没有 UI 自动化）：往窗口里 PostMessage 一个点击，只是窗口消息，真鼠标不动；
# 然后用 SendInput 的 KEYEVENTF_UNICODE 把文字逐字「打」进去——不经过剪贴板，也不经过输入法。
# 换行一律换成空格：回车在聊天软件里就是发送，绝不能出现。

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]  # pad：按 MOUSEINPUT 的大小撑够
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _U)]


u32.SendInput.argtypes = [w.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_KEYUP, _UNICODE = 0x0002, 0x0004


def _send(events):
    arr = (_INPUT * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        arr[i].type = 1  # INPUT_KEYBOARD
        arr[i].ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    u32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))


def type_text(text):
    """逐个 UTF-16 码元发 Unicode 键（emoji 这种代理对也照发两个码元，接收方会拼回去）。"""
    text = " ".join(text.split("\n")).replace("\r", " ")
    units = text.encode("utf-16-le")
    events = []
    for i in range(0, len(units), 2):
        code = units[i] | units[i + 1] << 8
        events += [(0, code, _UNICODE), (0, code, _UNICODE | _KEYUP)]
    for i in range(0, len(events), 200):  # 分批发，长句也不丢字
        _send(events[i:i + 200])
        time.sleep(0.01)


def _ctrl_end():
    """光标到已有草稿的最末尾，新内容总是接在后面（纯键盘）。"""
    _send([(0x11, 0, 0), (0x23, 0, 0), (0x23, 0, _KEYUP), (0x11, 0, _KEYUP)])


def _to_front(hwnd):
    """把聊天窗口放到前台。用户刚点了我们的「填入」，我们就是前台进程，有资格交出前台。"""
    from app.capture import unminimize

    unminimize(hwnd)
    if u32.GetForegroundWindow() != hwnd:
        u32.SetForegroundWindow(hwnd)
        time.sleep(0.12)


_tree = None


def _uia_input(hwnd, app):
    """QQ：ProseMirror 编辑器（class 带 ExEditor-qq-msg-editor）；WhatsApp：网页里最靠下那个可编辑框。
    只在聊天 App 自己的网页文档里找，不会找到挂在它下面的助手面板。"""
    global _tree
    from app.uia import _AID, _Tree

    if _tree is None:
        _tree = _Tree()
    t = _tree
    root = t.ia.ElementFromHandle(hwnd)
    doc = root.FindFirst(4, t.ia.CreatePropertyCondition(_AID, "RootWebArea")) if app == "whatsapp" \
        else root.FindFirst(4, t.ia.CreatePropertyCondition(30003, 50030))  # 第一个 Document = QQ 网页
    if not doc:
        return None
    arr = doc.FindAllBuildCache(4, t.ia.CreatePropertyCondition(30009, True), t.cr)  # 可获得键盘焦点的
    best = None
    for i in range(arr.Length):
        try:
            e = arr.GetElement(i)
            cls, typ, r = e.CachedClassName or "", e.CachedControlType, e.CachedBoundingRectangle
        except Exception:
            continue
        if app == "qq" and "ExEditor-qq-msg-editor" in cls:
            return e
        if app == "whatsapp" and typ == 50004 and (best is None or r.top > best[1]):
            best = (e, r.top)
    return best[0] if best else None


def fill_uia(hwnd, app, text):
    """QQ / WhatsApp：焦点直接给输入框，然后打字。找不到输入框就抛错，由界面提示用户复制。"""
    import comtypes

    try:  # 平时在后台线程里跑（见 main.fill_reply），这里给它初始化 COM；线程已经初始化过就沿用
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        mine = True
    except OSError:
        mine = False
    try:
        edit = _uia_input(hwnd, app)
        if edit is None:
            raise RuntimeError("没找到聊天输入框（窗口是不是停在会话列表？）")
        _to_front(hwnd)
        edit.SetFocus()
        time.sleep(0.05)
        _ctrl_end()
        type_text(text)
    finally:
        global _tree
        _tree = None  # COM 对象不跨线程留
        if mine:
            comtypes.CoUninitialize()


def fill(hwnd, area, text):
    """微信：area = 消息区 (x0, y0, x1, y1)，输入框在底线 y1 下面。往窗口里投递一次点击（窗口坐标），
    真鼠标不动；然后打字。"""
    r = w.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # 跟 WGC 帧对齐
        u32.GetWindowRect(hwnd, ctypes.byref(r))
    x0, _, _, y1 = area
    pt = w.POINT(r.left + x0 + 60, r.top + y1 + 40)  # 分隔线下 40px = 输入框文字区；工具栏和「发送」碰不到
    _to_front(hwnd)
    u32.ScreenToClient(hwnd, ctypes.byref(pt))
    lp = (pt.y & 0xFFFF) << 16 | (pt.x & 0xFFFF)
    u32.PostMessageW(hwnd, 0x0201, 0x0001, lp)  # WM_LBUTTONDOWN, MK_LBUTTON
    u32.PostMessageW(hwnd, 0x0202, 0, lp)  # WM_LBUTTONUP
    time.sleep(0.08)
    _ctrl_end()
    type_text(text)
    # 到此为止。发不发、改不改，人来。
