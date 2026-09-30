# -*- coding: utf-8 -*-
"""把选中的候选填进 QQ / WhatsApp 的输入框：UI 自动化给焦点 → 逐字打字。绝不发回车、绝不点发送。"""
import ctypes
import ctypes.wintypes as w
import time

u32 = ctypes.windll.user32

# ------------------------------------------------------------------ 不碰鼠标、不动剪贴板的填入
# 以前是「写剪贴板 → 把真鼠标挪过去点一下 → Ctrl+V」：鼠标会被抢走，剪贴板也被覆盖。现在：
# - QQ / WhatsApp：UI 自动化找到输入框，SetFocus 把焦点直接给它（顺带把窗口带到前台）；
# - 微信（Qt 自绘，没有 UI 自动化）：没有能现读现对的会话标识，不自动填，只复制（main.fill_reply 抛 CopyOnly）；
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


def _fresh_read(hwnd, app):
    """现读一遍这个窗口：→ (会话名 or None, [(谁, 正文)], 有没有输入框)。不用采集进程那份 ~1 秒前的缓存。"""
    global _tree
    from app.uia import APPS, PARSERS, _Tree

    if _tree is None:
        _tree = _Tree()
    title, msgs, point = PARSERS[app](_tree.dump(hwnd, web_root=(app == "whatsapp")))
    name = f"{APPS[app][0]} · {title}" if title else None  # 读不到标题就是「无法确认」，不套「当前会话」占位
    return name, [(m[0], m[2]) for m in msgs], point is not None


def _verify_fresh(hwnd, app, expect):
    from core.fill_guard import check_fresh

    chat, newest = expect
    why = check_fresh(chat, newest, *_fresh_read(hwnd, app))
    if why:
        raise RuntimeError(why)


def fill_uia(hwnd, app, text, expect):
    """QQ / WhatsApp：焦点直接给输入框，然后打字。找不到输入框就抛错，由界面提示用户复制。
    expect = (候选所属会话名, 生成时最新一条 (谁, 正文))；打字前会现读窗口核对，对不上或没给就不打字。"""
    import comtypes

    if not expect:
        raise RuntimeError("没有生成时的会话状态可核对，没有填入")
    try:  # 平时在后台线程里跑（见 main.fill_reply），这里给它初始化 COM；线程已经初始化过就沿用
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        mine = True
    except OSError:
        mine = False
    try:
        _verify_fresh(hwnd, app, expect)  # 动焦点之前
        edit = _uia_input(hwnd, app)
        if edit is None:
            raise RuntimeError("没找到聊天输入框（窗口是不是停在会话列表？）")
        _to_front(hwnd)
        edit.SetFocus()
        time.sleep(0.05)
        _ctrl_end()
        _verify_fresh(hwnd, app, expect)  # 打字前一刻：带前台/聚焦的这段时间里也可能变
        type_text(text)
    finally:
        global _tree
        _tree = None  # COM 对象不跨线程留
        if mine:
            comtypes.CoUninitialize()
