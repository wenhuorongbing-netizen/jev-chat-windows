# -*- coding: utf-8 -*-
"""悬浮窗贴靠聊天窗口，全靠 Windows 原生机制，不轮询：

- 层级：把悬浮窗设成聊天窗口的「从属窗口」（owned window，GWLP_HWNDPARENT）。系统保证从属窗口永远
  在主人上面一层：主人到前台它跟着上来，主人被别的程序盖住它一起被盖，主人最小化它一起藏起来。
- 位置：SetWinEventHook 订阅聊天窗口所在进程的 EVENT_OBJECT_LOCATIONCHANGE，窗口每动一下系统就回调，
  当场 SetWindowPos 跟过去。只挪位置时带 SWP_NOSIZE，Qt 不用重排，拖起来是跟手的。
- 前台：全局订阅 EVENT_SYSTEM_FOREGROUND，切到哪个窗口立刻知道，由调用方决定要不要贴过去。

回调走 WINEVENT_OUTOFCONTEXT，系统把它投递到装钩子那个线程的消息队列——就是 Qt 主线程，
Qt 的事件循环会把它派发出来，所以回调里可以直接碰窗口。坐标全用物理像素（进程已 DPI aware）。
"""
import ctypes
import ctypes.wintypes as w

u32 = ctypes.windll.user32

_WINEVENTPROC = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)
u32.SetWinEventHook.restype = w.HANDLE
u32.SetWinEventHook.argtypes = [w.DWORD, w.DWORD, w.HMODULE, _WINEVENTPROC, w.DWORD, w.DWORD, w.DWORD]
u32.UnhookWinEvent.argtypes = [w.HANDLE]
u32.SetWindowLongPtrW.restype = ctypes.c_void_p
u32.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_void_p]
u32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
u32.GetAncestor.restype = w.HWND
u32.GetAncestor.argtypes = [w.HWND, w.UINT]

_EVENT_FOREGROUND = 0x0003
_EVENT_LOCATIONCHANGE = 0x800B
_OUTOFCONTEXT, _SKIPOWNPROCESS = 0x0000, 0x0002
_GWLP_HWNDPARENT = -8
_SWP_NOSIZE, _SWP_NOZORDER, _SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


def window_rect(hwnd):
    """可见边界（DWM，不含透明阴影）的物理像素矩形；窗口没了/最小化返回 None。"""
    if not hwnd or not u32.IsWindow(hwnd) or u32.IsIconic(hwnd):
        return None
    r = w.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(w.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


class Docker:
    """self_hwnd = 悬浮窗；width_px() → 当前要的物理宽度；on_foreground(hwnd) → 前台换了窗口（根窗口）。"""

    def __init__(self, self_hwnd, width_px, min_height_px, on_foreground=None):
        self.me = self_hwnd
        self.width_px = width_px
        self.min_height_px = min_height_px
        self.on_foreground = on_foreground
        self.target = None
        self.enabled = True
        self._loc_hook = None
        self._last = None  # 上次摆的 (x, y, w, h)
        # 回调对象必须一直有人引用着，被回收了系统再回调就是野指针
        self._loc_proc = _WINEVENTPROC(self._on_location)
        self._fg_proc = _WINEVENTPROC(self._on_fg)
        self._fg_hook = u32.SetWinEventHook(_EVENT_FOREGROUND, _EVENT_FOREGROUND, None, self._fg_proc,
                                            0, 0, _OUTOFCONTEXT | _SKIPOWNPROCESS)

    # ---------------------------------------------------------------- 钩子回调
    def _on_fg(self, hook, event, hwnd, id_obj, id_child, thread, ms):
        if hwnd and self.on_foreground:
            self.on_foreground(u32.GetAncestor(hwnd, 2))  # GA_ROOT

    def _on_location(self, hook, event, hwnd, id_obj, id_child, thread, ms):
        if id_obj == 0 and hwnd == self.target:  # OBJID_WINDOW：窗口本身动了，不是里面的控件/光标
            self.place()

    # ---------------------------------------------------------------- 对外
    def attach(self, hwnd):
        """贴到这个窗口上：设主人 + 订阅它进程的位置变化 + 立刻摆一次。同一个窗口重复调用不做事。"""
        if not self.enabled or not hwnd or hwnd == self.target:
            return
        self._unhook_location()
        self.target = hwnd
        u32.SetWindowLongPtrW(self.me, _GWLP_HWNDPARENT, hwnd)
        pid = w.DWORD()
        tid = u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        self._loc_hook = u32.SetWinEventHook(_EVENT_LOCATIONCHANGE, _EVENT_LOCATIONCHANGE, None,
                                             self._loc_proc, pid.value, tid, _OUTOFCONTEXT)
        self._last = None
        self.place()

    def detach(self):
        """不贴了：解除主人（变回普通独立窗口，留在原地）、停止跟随。"""
        self._unhook_location()
        self.target = None
        self._last = None
        u32.SetWindowLongPtrW(self.me, _GWLP_HWNDPARENT, None)

    def set_enabled(self, on):
        self.enabled = on
        if not on:
            self.detach()

    def place(self):
        """按主人现在的位置摆一次：贴它右边，放不下贴左边，都不行压在它右侧内沿；高度跟它一致。"""
        rect = window_rect(self.target)
        if rect is None:
            return
        l, t, r, b = rect
        mon = u32.MonitorFromRect(ctypes.byref(w.RECT(l, t, r, b)), 2)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        u32.GetMonitorInfoW(mon, ctypes.byref(mi))
        work = mi.rcWork
        width = self.width_px()
        if r + width <= work.right:
            x = r
        elif l - width >= work.left:
            x = l - width
        else:
            x = max(work.left, r - width)
        y = max(t, work.top)
        h = min(max(min(b, work.bottom) - y, self.min_height_px()), work.bottom - work.top)
        y = min(y, work.bottom - h)
        new = (x, y, width, h)
        if new == self._last:
            return
        flags = _SWP_NOZORDER | _SWP_NOACTIVATE
        if self._last and self._last[2:] == new[2:]:
            flags |= _SWP_NOSIZE  # 只是挪位置：不发 WM_SIZE，Qt 不重排，拖动跟手
        self._last = new
        u32.SetWindowPos(self.me, None, x, y, width, h, flags)

    def close(self):
        self._unhook_location()
        if self._fg_hook:
            u32.UnhookWinEvent(self._fg_hook)
            self._fg_hook = None

    def _unhook_location(self):
        if self._loc_hook:
            u32.UnhookWinEvent(self._loc_hook)
            self._loc_hook = None
