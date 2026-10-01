# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 → 人点「填入」。发送永远手动。静默期零调用。
上下文、结果、聊天记录都按会话名（子进程 OCR 头部标题得来）分开存，切会话不串味。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
起草语言模型的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import multiprocessing
import queue
import threading
import traceback

from app import settings, uia_worker, update, worker
from app.capture import find_wechat_hwnd
from app.fill import fill_uia
from app.overlay import Overlay
from app.qol import auto_generate_allowed
from app.version import VERSION
from core import retry
from core.convo import Coordinator
from core.engine import analyze_bilingual, reroll_candidate
from core.draft import IMAGE_NOTES, ImageEgress
from core.fill_guard import FILL_SUPPORT, CopyOnly, check_fill_target
from core.image_policy import newest_image
from core.route import ReplyPlan, snapshot_route

# 会话状态（历史、结果、版本、在等的生成、当前开着的会话）全在 core/convo.py 的 Coordinator 里，
# 只有它的方法能改；这里的 drain / tick / 界面回调只上报事件。history 里是 [(who, text, name)]，
# engine 只认 her/me，name 是群里的发言人；只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定。
coord = Coordinator()
state = {"hwnd": None,  # 采集几何：微信窗口句柄（采集子进程用）
         "uia": {}}  # {会话名: (hwnd, 输入框屏幕坐标)}，QQ / WhatsApp 的会话填入走这里

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
    title = coord.set_foreground(app)
    if title:
        ov.set_chat(title)
    ov.attach(hwnd)


def generate_now(title):
    """「立即生成回复」：不管最后一句是谁说的，按这个会话已有的记录生成。"""
    msgs = list(coord.chat(title).history)
    if not msgs:
        ov.set_status("这个会话还没读到聊天记录", "warning")
        return
    start_analyze(title, msgs, manual=True)  # 明确的用户动作：这一次允许带图


results = queue.Queue()  # (kind, 值, Request 或 RerollTicket)：回来的结果只带它的来历，谁算数由 coord 定
update_result = queue.Queue()
fill_errors = queue.Queue()  # 后台线程里填入失败的原因，tick 里报给界面


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    return coord.chat(title).target_name()


def _refuse_off_target(title):
    """候选属于 title；那个 App 的窗口现在开着的不是它就抛错（界面提示复制），一个字都不打。"""
    why = check_fill_target(title, coord.open_chat(app_of(title)))
    if why:
        raise RuntimeError(why)


def fill_reply(text):
    title = ov.current_chat()  # 点的那张卡属于这个会话；后面全按它核验，不再回头读界面
    if FILL_SUPPORT.get(app_of(title)) != "fresh-verified":  # 没有能现读现对的会话标识的 App（微信是 OCR 读的）：只复制，不往输入框里打字
        raise CopyOnly("这个会话没有可核验的标识，没有自动填入")
    _refuse_off_target(title)
    uia = state["uia"].get(title)
    if not uia:
        raise RuntimeError("没找到这个会话的输入框位置，没有填入")
    # QQ / WhatsApp：UI 自动化把焦点给输入框再打字。放后台线程：面板是聊天窗口的从属窗口，
    # 在界面线程里查它的 UI 自动化树会绕回自己的界面线程，容易卡死
    expect = coord.fill_expect(title)  # 点击这一刻的来历；fill_uia 里还会现读窗口再对一遍

    def run():
        try:
            _refuse_off_target(title)  # 线程排队/起来这段时间里用户可能又切了会话，打字前再核一次
            fill_uia(uia[0], app_of(title), text, expect)
        except Exception as e:
            fill_errors.put(f"{type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()


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
    settings.try_save(debug_view_on=False)


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


def latest_image(title, msgs, manual=False):
    """只有用户明确点了「立即生成」（manual）、「对方最新一条本身就是图」且设置里开了识别图片才带图；
    自动生成永远不带图——设置里的开关只表示「允许我手动用图片功能」，不是每次都上传的长期授权。
    更早的图不带（口径见 core/image_policy）。在主线程里、发请求前算好放进 Request，网络线程不再回头读会话。"""
    if not manual:
        return None
    chat = coord.chat(title)
    if len(msgs) != len(chat.history):  # 请求用的不是这个会话的最新状态，图对不上
        return None
    return newest_image(chat.image, msgs, settings.read_images())


def snapshot_plan(title, msgs):
    """一次生成要用的路由和口径设置，在主线程、发请求之前一次读完定下来（core/route）：
    来源 / 协议 / 地址 / 模型 / 跟接口核对过的 key，加关系、上下文条数、风格、思考开关。
    后台线程只拿这份快照；用户在生成中途改设置，改的是下一次，这一次不受影响。"""
    group = len({m[2] for m in msgs if m[0] == "her" and len(m) > 2 and m[2]}) >= 2  # 两个以上发言人 = 群聊
    route = snapshot_route(settings.draft_provider(), settings.draft_base_url() or None, settings.draft_model() or None)
    return ReplyPlan(route, settings.relationship_for(title, group), settings.context(),
                     settings.style(), settings.thinking(), image_enabled=settings.read_images())


def analyze_bg(req):
    """后台线程只跑网络调用，输入只来自不可变的 Request（含它带的路由快照）；结果丢队列，UI 和会话状态只在主线程的 tick 里动。
    失败按归类有界重试（core/retry）：只有限流 / 超时 / 连接失败会再试；重试前（睡之前、睡醒后各一次）
    这一代不再是 live（来了新消息 / 用户取消）就不试直接丢——跟 tick 的过期检查同一口径。"""
    plan = req.plan
    if plan is None:  # 不该发生：每个 Request 都是带着快照发出的
        results.put(("err", "分析失败: 内部错误：这次生成没有路由快照，没有发送。", req))
        return
    try:  # key 是为别的接口填的就一个字节都不发，也不重试（换来源/Base URL 之后必须重填）
        plan.route.require()
        egress = ImageEgress(lambda: coord.is_live(req))  # 整个生成（含重试）共用：图被明确拒绝后不再重复外发
        value = retry.run(lambda _n: analyze_bilingual(list(req.msgs), plan, reply_to=req.reply_to, image=req.image,
                                                       egress=egress),
                          lambda: coord.is_live(req))
    except Exception as e:
        results.put(("err", f"分析失败: {e}", req))
        return
    results.put(("ok", value, req))


def cancel_generate():
    """取消这次生成：界面立即脱身，结果必然作废——在跑的网络线程不杀，它回来时这一代已不是 live，被 tick 丢弃。
    取消后立刻点 ↻ 可重新生成，不用等旧线程。"""
    title = ov.current_chat()
    coord.cancel(title)
    ov.set_busy(False)
    ov.set_status("已取消，点 ↻ 重新生成", "success")


def reroll_reply(index):
    """单卡重 roll「换一条」：守卫（生成中 / 无 result / 下标越界）→ 拿一张只对当前这份结果有效的票，
    后台线程跑 engine.reroll_candidate，结果进 results 队列（kind="reroll"）。"""
    title = ov.current_chat()
    chat = coord.peek(title)
    # 换一条只沿用「这份结果确实带过图」的图会话；自动生成出的结果没带过图，换一条也不带
    manual = bool(chat and chat.result and chat.result.get("image_use") == "attached")
    ticket = coord.reroll_begin(title, index, latest_image(title, list(chat.history), manual) if chat else None,
                                snapshot_plan(title, list(chat.history)) if chat else None)
    if ticket is None:
        return
    ov.set_card_pending(index, True)
    threading.Thread(target=_reroll_bg, args=(ticket, dict(chat.result)), daemon=True).start()


def _reroll_bg(ticket, result):
    """后台线程跑网络；UI 只在 tick 里动。路由和口径设置用票里带的快照，图片也是票里带的。"""
    try:
        ticket.plan.route.require()
        text, gloss = reroll_candidate(list(ticket.msgs), ticket.plan,
                                       result.get("lang") or "中文",
                                       list(result.get("candidates") or []),
                                       reply_to=result.get("reply_to"), image=ticket.image,
                                       egress=ImageEgress(lambda: coord.reroll_valid(ticket)))
        results.put(("reroll", (ticket.index, text, gloss, ""), ticket))
    except Exception as e:
        results.put(("reroll", (ticket.index, "", "", str(e)[:120]), ticket))


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs, manual=False):
    if not settings.has_llm_key():
        ov.set_status(f"起草来源 {settings.draft_provider_name()} 没填密钥，去设置里补上", "warning")
        return
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    req = coord.begin(title, msgs, reply_to, latest_image(title, msgs, manual),  # 这个会话已有一次在跑 → 排队，回来后接着跑最新的
                      plan=snapshot_plan(title, msgs))
    ov.set_busy(coord.is_generating(ov.current_chat()))
    if req is not None:
        threading.Thread(target=analyze_bg, args=(req,), daemon=True).start()


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = coord.chat(title)
    chat.target = name
    msgs = list(chat.history)
    if any(m[0] == "her" for m in msgs):
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
        if kind == "chat":  # 某个 App 切了会话：记下来；只有它是当前前台 App（或还没判断过前台）才让界面跟过去
            app = app_of(msg[1])
            coord.set_open_chat(app, msg[1])
            if coord.follows(app):
                ov.set_chat(msg[1])
            continue
        if kind == "debug":  # 调试视图的一帧；窗口不在就直接丢掉
            if dbg is not None:
                dbg.show_packet(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析清掉
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
            coord.cancel_all()  # 在跑的分析作废，回来的结果不再往界面上贴
            coord.forget_open_chats()  # 重连后要等 App 重新报当前会话，之前那个不再可信
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                child.terminate()
                child.join()
                child = None
            continue
        _, title, new, _area = msg
        chat = coord.messages(title, new)  # 这个会话有新消息了，它在等的那一代作废
        if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
            ov.invalidate_replies()
        for item in new:
            ov.log_message(item[0], item[2], item[1], chat=title)
        ov.set_targets(title, chat.senders, target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        if new[-1][0] == "her":  # 只有对方最新说话才值得分析
            if auto_generate_allowed(title, settings.chat_meta):  # 静音会话：history/未读照记，不自动跑
                start_analyze(title, list(chat.history))
        else:
            coord.settle(title)
            ov.set_busy(coord.is_generating(ov.current_chat()))
            ov.set_status("你已回复，等待对方的新消息")


def _newest_is_picture(req):
    return bool(req.msgs) and req.msgs[-1][0] == "her" and "[图片]" in (req.msgs[-1][1] or "")


def _take_result(kind, r, req):
    """一份回来的生成结果：算不算数由 coord 按 gen 判；算数才动界面。"""
    accepted, nxt = coord.finish(req, r if kind == "ok" else None)
    if nxt is not None:  # 生成期间又来了新消息，接着跑最新的
        threading.Thread(target=analyze_bg, args=(nxt,), daemon=True).start()
    ov.set_busy(coord.is_generating(ov.current_chat()))
    if not accepted:
        return
    if kind == "ok":
        if req.title == ov.current_chat():  # 存着了；正看着这个会话才立刻贴上去
            ov.show(r)
            note = IMAGE_NOTES.get(r.get("image_use"))
            if r.get("image_use") == "none" and settings.read_images() and _newest_is_picture(req):
                note = "图片没有发出去；要让模型看图回复，点 ↻ 立即生成"
            if note:  # 图没发出去（设置关了 / 模型不看图 / 被拒）要说一声，别让人以为模型看过图
                ov.set_status(note, "warning")
    else:
        ov.set_status("生成失败，请检查网络和服务设置；新消息到来后会重试。", "error")
        ov.log(r)


def _take_reroll(r, ticket):
    index, text, gloss, err = r
    if not coord.reroll_valid(ticket):  # 期间来了新消息 / 被取消 / 结果已被新的生成换掉：丢弃；pending 由重建自然复位
        return
    ov.set_card_pending(index, False)
    if err:
        ov.toast.show_text("换一条没成功，原样保留")
        return
    result = coord.result_of(ticket.title)
    cands = (result or {}).get("candidates") or []
    if not (0 <= index < len(cands)):
        return
    cands[index] = text  # 缓存同步：切走再切回来还是换过的
    glosses = result.setdefault("glosses", [])
    while len(glosses) < len(cands):
        glosses.append("")
    glosses[index] = gloss
    if ticket.title == ov.current_chat():
        ov.replace_card(index, text, gloss)
    ov.toast.show_text("已换一条")


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
            kind, r, req = results.get()
            if kind == "reroll":  # 单卡重 roll：只在自己那份结果还在时生效，不碰生成状态
                _take_reroll(r, req)
            else:
                _take_result(kind, r, req)
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
    settings.migrate_legacy_keys()  # 老版本放在用户环境变量里的明文 key → 加密存储；失败原样保留，原因在 secret_issues()
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_generate=generate_now, on_cancel=cancel_generate, on_reroll=reroll_reply,
                 result_of=coord.result_of)
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
    if not settings.has_llm_key():
        ov.set_status("请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    if settings.secret_issues():  # 比「请先配置」更具体：比如密文解不开
        ov.set_status("；".join(settings.secret_issues()), "warning")
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if child is not None:
            child.terminate()
        uia_child.terminate()
