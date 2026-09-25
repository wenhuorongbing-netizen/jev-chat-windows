# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 → 人点「填入」。发送永远手动。静默期零调用。
上下文、结果、聊天记录都按会话名（子进程 OCR 头部标题得来）分开存，切会话不串味。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
两个模型（判断 Jev / 起草语言模型）的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import multiprocessing
import queue
import threading
import traceback
from collections import deque

from app import settings, uia_worker, update, worker
from app.capture import find_wechat_hwnd
from app.fill import fill, fill_uia
from app.overlay import Overlay
from app.version import VERSION
from core.engine import analyze, analyze_bilingual

# {会话名: {history, result, rev, target, senders}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
state = {"area": None, "busy": False, "rerun": None, "hwnd": None, "chat": "",
         "uia": {},  # uia: {会话名: (hwnd, 输入框屏幕坐标)}，QQ / WhatsApp 的会话填入走这里
         "app_chat": {},  # {App: 那个 App 当前开着的会话}，三个 App 各报各的
         "fg_app": None}  # 最近一次在前台的是哪个聊天 App；界面只跟它

_FG_EXES = {"weixin.exe": "wechat", "wechat.exe": "wechat", "qq.exe": "qq",
            "whatsapp.root.exe": "whatsapp", "whatsapp.exe": "whatsapp"}


def app_of(title):
    """会话名 → 来自哪个 App（UIA 来源带前缀，其余是微信）。"""
    if title.startswith("QQ · "):
        return "qq"
    if title.startswith("WhatsApp · "):
        return "whatsapp"
    return "wechat"


def app_of_hwnd(hwnd):
    """这个顶层窗口属于哪个聊天 App；不是聊天 App（包括助手自己）返回 None。"""
    import os

    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = ctypes.c_ulong()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)
    if not h:
        return None
    buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k32.CloseHandle(h)
    return _FG_EXES.get(os.path.basename(buf.value).lower()) if ok else None


def on_foreground(hwnd):
    """系统通知前台换了窗口（app/dock.py 的钩子，Qt 主线程里回调）：是聊天 App 就让界面跟到它当前的会话，
    悬浮窗贴过去；别的程序到前台什么都不做——悬浮窗是聊天窗口的从属窗口，会跟着主人一起被盖住。"""
    app = app_of_hwnd(hwnd)
    ov.enable_hotkeys(bool(app))  # Alt+1/2/3 只在聊天 App 在前台时占用
    if not app:
        return
    if app != state["fg_app"]:
        state["fg_app"] = app
        title = state["app_chat"].get(app)
        if title:
            ov.set_chat(title)
    ov.attach(hwnd)


def generate_now(title):
    """「立即生成回复」：不管最后一句是谁说的，按这个会话已有的记录生成。"""
    msgs = list(chat_of(title)["history"])
    if not msgs:
        ov.set_status("这个会话还没读到聊天记录", "warning")
        return
    if state["busy"]:
        state["rerun"] = (title, msgs)
        return
    start_analyze(title, msgs)
results = queue.Queue()
update_result = queue.Queue()
fill_errors = queue.Queue()  # 后台线程里填入失败的原因，tick 里报给界面  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起


def chat_of(title):
    return chats.setdefault(title, {"history": deque(maxlen=200), "result": None, "rev": 0,
                                    "target": None, "senders": []})


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def fill_reply(text):
    title = ov.current_chat()
    uia = state["uia"].get(title)
    if uia:  # QQ / WhatsApp：UI 自动化把焦点给输入框再打字。放后台线程：面板是聊天窗口的从属窗口，
        # 在界面线程里查它的 UI 自动化树会绕回自己的界面线程，容易卡死
        def run():
            try:
                fill_uia(uia[0], app_of(title), text)
            except Exception as e:
                fill_errors.put(f"{type(e).__name__}: {e}")
        threading.Thread(target=run, daemon=True).start()
        return
    if state["hwnd"] is None:  # 子进程重开过，hwnd 可能换了，用最新的
        raise RuntimeError("未找到聊天窗口，请确认已经打开")
    if state["area"] is None:
        raise RuntimeError("输入区域尚不可用，请确认聊天窗口可见（不要最小化）")
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(ov.current_chat())  # 填进去的是界面上正看着的那个会话的对象
        if target:
            text = f"@{target} " + text  # 纯文本，微信不认成真正的 @，只是让群里看得出在跟谁说
    fill(state["hwnd"], state["area"], text)


def spawn_worker():
    """开一个采集子进程，它跟着 capture_on 走：置位=采集，清掉=暂停。"""
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, debug_on), daemon=True)
    p.start()
    return p


def set_debug(on):
    """调试视图开关：开 → 开窗 + 置位（子进程这才开始送帧，一帧 2~3MB）；关 → 清掉 + 收窗。"""
    global dbg
    if not on:
        debug_on.clear()
        if dbg is not None:
            dbg.hide()
        return
    if dbg is None:
        from app.debugwin import DebugWindow

        dbg = DebugWindow(on_close=on_debug_closed)
    dbg.show()
    debug_on.set()


def on_debug_closed():
    """用户直接关了调试窗 = 把开关也关了，否则设置页显示开着但没窗。"""
    debug_on.clear()
    ov.set_debug_switch(False)
    settings.save(debug_view_on=False)


def on_toggle_capture(on):
    """标题栏开关。启动时没找到微信就没有子进程，这会儿再找一次，找到了才真开得起来。"""
    global child
    if not on:
        capture_on.clear()
        uia_on.clear()
        return
    uia_on.set()
    if child is None:
        try:
            state["hwnd"] = find_wechat_hwnd()
        except RuntimeError:
            ov.set_capture(False, "未找到聊天窗口，打开后再开启采集")
            return
        child = spawn_worker()
    capture_on.set()


def latest_image(title, msgs):
    """对方最后连着说的那几条里要是有图，返回它；更早的图不带（跟当前回复多半没关系，还费钱）。"""
    pic = chat_of(title).get("image")
    if not pic or not msgs:
        return None
    start = len(msgs)
    while start > 0 and msgs[start - 1][0] == "her":
        start -= 1
    at, data = pic
    return data if start < at <= len(msgs) and len(msgs) == len(chat_of(title)["history"]) else None


def analyze_bg(msgs, title, revision, reply_to=None):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。"""
    group = len({m[2] for m in msgs if m[0] == "her" and len(m) > 2 and m[2]}) >= 2  # 两个以上发言人 = 群聊
    rel = settings.relationship_for(title, group)
    try:
        if settings.bilingual():
            results.put(("ok", analyze_bilingual(msgs, rel,
                                                 context=settings.context(),
                                                 model=settings.draft_model() or None,
                                                 provider=settings.draft_provider(),
                                                 base_url=settings.draft_base_url() or None,
                                                 reply_to=reply_to, style=settings.style(),
                                                 thinking=settings.thinking(),
                                                 image=latest_image(title, msgs)),
                         title, revision))
            return
        results.put(("ok", analyze(msgs, rel, context=settings.context(),
                                   model=settings.draft_model() or None,
                                   provider=settings.draft_provider(),
                                   base_url=settings.draft_base_url() or None,
                                   reply_to=reply_to, style=settings.style(),
                                   thinking=settings.thinking(),
                                   jev_provider=settings.jev_provider(),
                                   jev_model=settings.jev_model() or None),
                     title, revision))
    except Exception as e:
        results.put(("err", f"分析失败: {e}", title, revision))


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs):
    if not settings.bilingual() and not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if not settings.has_llm_key():
        ov.set_status(f"起草来源 {settings.draft_provider_name()} 没填密钥，去设置里补上", "warning")
        return
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg, args=(msgs, title, chat_of(title)["rev"], reply_to),
                     daemon=True).start()


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = chat_of(title)
    chat["target"] = name
    msgs = list(chat["history"])
    if not any(m[0] == "her" for m in msgs):
        return
    if state["busy"]:
        state["rerun"] = (title, msgs)
        ov.set_busy(True)
    else:
        start_analyze(title, msgs)


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind == "uia_input":  # QQ / WhatsApp 某个会话的输入框位置
            state["uia"][msg[1]] = (msg[2], msg[3])
            continue
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 某个 App 切了会话：记下来；只有它是当前前台 App（或还没判断过前台）才让界面跟过去
            app = app_of(msg[1])
            state["app_chat"][app] = msg[1]
            if state["fg_app"] in (None, app):
                state["chat"] = msg[1]
                ov.set_chat(msg[1])
            continue
        if kind == "debug":  # 调试视图的一帧；窗口不在就直接丢掉
            if dbg is not None:
                dbg.show_packet(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析和已知坐标清掉
            ov.set_status(msg[1], "warning")
            ov.log(msg[1])
            continue
        if kind == "paused":  # 子进程确认已暂停
            ov.set_capture(False)
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "dead":  # 采集彻底停了（微信关了之类），这才是真的要清状态
            state["area"] = None
            for c in chats.values():  # 在跑的分析作废，回来的结果不再往界面上贴
                c["rev"] += 1
            state["rerun"] = None
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                child.terminate()
                child.join()
                child = None
            continue
        _, title, new, area = msg
        if area is not None:  # UIA 来源不带消息区，别把微信的坐标冲掉
            state["area"] = area
        chat = chat_of(title)
        chat["rev"] += 1  # 这个会话有新消息了，它在跑的分析作废
        if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
            ov.invalidate_replies()
        for item in new:
            who, name, text = item[:3]
            chat["history"].append((who, text, name))
            if who == "her" and len(item) > 3 and item[3]:  # 对方发的图（UIA 那边抓好的 base64），记下是第几条
                chat["image"] = (len(chat["history"]), item[3])
            ov.log_message(who, text, name, chat=title)
            if who == "her" and name:  # 群里发过言的人，去重后最近的排最前
                if name in chat["senders"]:
                    chat["senders"].remove(name)
                chat["senders"].insert(0, name)
        ov.set_targets(title, chat["senders"], target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        if new[-1][0] == "her":  # 只有对方最新说话才值得分析
            msgs = list(chat["history"])
            if state["busy"]:
                state["rerun"] = (title, msgs)
                ov.set_busy(True)
            else:
                start_analyze(title, msgs)
        else:
            state["rerun"] = None
            ov.set_busy(False)
            ov.set_status("你已回复，等待对方的新消息")


def tick():
    try:
        drain()
        while not fill_errors.empty():
            err = fill_errors.get()
            ov.set_status("没能填进去，可以点复制自己粘贴", "error")
            ov.log(f"[填入失败] {err}")
        while not update_result.empty():
            latest, url = update_result.get()
            ov.set_update(latest, url)
        while not results.empty():
            kind, r, title, revision = results.get()
            state["busy"] = False
            if state["rerun"]:  # 分析期间又来了新消息，接着跑最新的
                (t, msgs), state["rerun"] = state["rerun"], None
                start_analyze(t, msgs)
                continue
            if revision != chat_of(title)["rev"]:  # 这个会话后来又说话了，这份结果过期了
                ov.set_busy(False)
                continue
            if kind == "ok":
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                if title == ov.current_chat():
                    ov.show(r)
                else:
                    ov.set_busy(False)
            else:
                ov.set_busy(False)
                ov.set_status("生成失败，请检查网络和服务设置；新消息到来后会重试。", "error")
                ov.log(r)
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    q = multiprocessing.Queue()
    capture_on = multiprocessing.Event()  # 父子进程共用的开关，置位=采集
    debug_on = multiprocessing.Event()  # 同上，置位=子进程往队列里送整帧给调试窗
    uia_on = multiprocessing.Event()  # QQ / WhatsApp 的 UI 自动化采集，跟标题栏开关走
    uia_on.set()
    uia_child = multiprocessing.Process(target=uia_worker.run, args=(q, uia_on), daemon=True)
    uia_child.start()
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_generate=generate_now,
                 result_of=lambda t: chats.get(t, {}).get("result"))
    child = dbg = None
    ov.on_foreground = on_foreground
    on_foreground(ctypes.windll.user32.GetAncestor(ctypes.windll.user32.GetForegroundWindow(), 2))
    try:
        state["hwnd"] = find_wechat_hwnd()
    except RuntimeError:
            ov.set_capture(False, "未找到聊天窗口，打开后再开启采集")
    else:
        capture_on.set()
        child = spawn_worker()
    if settings.debug_view():  # 上次开着就直接开回来
        set_debug(True)
    if not settings.has_key():
        ov.set_status("请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if child is not None:
            child.terminate()
        uia_child.terminate()
