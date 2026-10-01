# -*- coding: utf-8 -*-
"""会话状态的唯一所有者。不 import Qt、网络、配置，主线程之外不写。

以前 main.py 里 chats / state["busy"] / state["rerun"] / chat["rev"] / result_rev / result_last
散在 drain、tick、取消、重 roll、填入五处各自改；「这份结果还算不算数」靠 `revision == chat["rev"]`
再加一个全局 busy 旗标拼出来，结果是：过期结果回来会把别的生成的 busy 清掉，↻ 重新生成不动 rev 所以
重 roll 的晚到结果能写进新结果里，排队重跑只有一个全局槽位，切到 B 会把 A 的挤掉。

现在：
- 一次生成 = 一个不可变的 Request（带递增的 gen 号）。网络线程只拿 Request，不回头读会话状态。
- 结果算不算数只看一件事：它的 gen 是不是这个会话**当前要的那一代**（chat.live）。新消息、取消、
  采集断开、重新生成都会让旧的 gen 不再是 live，晚到的结果自然落空。
- 候选回溯到生成它的 Request（chat.result_req）；填入、重 roll 都拿它核对，不再各自比版本号。
- 只有 Coordinator 的方法推进状态；采集、网络、界面只上报事件。
"""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Request:
    """一次生成的输入，创建后不可变。msgs 是 ((谁, 正文, 发言人), ...)。"""
    gen: int
    title: str
    msgs: tuple
    rev: int  # 发出时会话的版本；新消息 / 取消让版本前进
    reply_to: str | None = None
    image: str | None = field(default=None, repr=False)  # 发出时就定好带不带图（主线程里算的），线程里不再读会话
    plan: object = field(default=None, repr=False)  # core/route.ReplyPlan：发出时定下的路由和口径设置，线程里不再读设置

    @property
    def newest(self):
        """生成这份候选时最新一条 (谁, 正文)；没有消息 = None。"""
        return (self.msgs[-1][0], self.msgs[-1][1]) if self.msgs else None


@dataclass(frozen=True)
class RerollTicket:
    """单卡「换一条」：只对它发出时显示的那一份结果（basis）有效。"""
    title: str
    index: int
    rev: int
    basis: Request
    msgs: tuple
    image: str | None = field(default=None, repr=False)
    plan: object = field(default=None, repr=False)


class Chat:
    """一个会话的全部状态。只有 Coordinator 改它。"""

    def __init__(self):
        self.history = deque(maxlen=200)  # [(who, text, name)]
        self.result = None  # 上次接受的结果（dict）
        self.result_req = None  # 生成 result 的 Request；None = 没有可核对的来历
        self.rev = 0
        self.target = None  # 用户挑的回复对象；None = 跟着最近那个
        self.senders = []  # 发过言的人，最近的排最前
        self.image = None  # (第几条, base64)
        self.live = None  # 这个会话当前要的那一代 gen；None = 没有在等的生成
        self.running = set()  # 网络线程还没回来的 gen（取消后清空，只为合并排队）

    def target_name(self):
        if self.target in self.senders:
            return self.target
        return self.senders[0] if self.senders else None


class Coordinator:
    def __init__(self):
        self._chats: dict[str, Chat] = {}
        self._gens = itertools.count(1)
        self._queued: dict[str, tuple] = {}  # 会话 → (msgs, reply_to, image, plan)：生成期间又来了新消息，回来后接着跑最新的
        self._open: dict[str, str] = {}  # App → 它当前开着的会话
        self.fg_app = None  # 最近一次在前台的聊天 App；界面只跟它

    # ---- 读
    def chat(self, title) -> Chat:
        return self._chats.setdefault(title, Chat())

    def peek(self, title) -> Chat | None:
        return self._chats.get(title)

    def result_of(self, title):
        chat = self._chats.get(title)
        return chat.result if chat else None

    def is_live(self, req: Request) -> bool:
        """线程里问「我这一代还有人要吗」（只读）。"""
        chat = self._chats.get(req.title)
        return bool(chat) and chat.live == req.gen

    def is_generating(self, title) -> bool:
        chat = self._chats.get(title)
        return bool(chat) and (chat.live is not None or title in self._queued)

    # ---- 当前开着哪个会话（采集上报）
    def set_open_chat(self, app, title):
        self._open[app] = title

    def open_chat(self, app):
        return self._open.get(app)

    def forget_open_chats(self):
        self._open.clear()

    def set_foreground(self, app):
        """聊天 App 到了前台：返回界面该跟到的会话（没有 / 没变 = None）。"""
        if app == self.fg_app:
            return None
        self.fg_app = app
        return self._open.get(app)

    def follows(self, app) -> bool:
        """这个 App 上报的会话该不该让界面跟过去：它就是前台 App，或还没判断过前台。"""
        return self.fg_app in (None, app)

    # ---- 采集上报新消息
    def messages(self, title, new):
        """new = [(谁, 发言人, 正文[, 图])]。会话版本前进，它在等的那一代作废（排队的另算）。"""
        chat = self.chat(title)
        chat.rev += 1
        chat.live = None
        for item in new:
            who, name, text = item[:3]
            chat.history.append((who, text, name))
            if who == "her" and len(item) > 3 and item[3]:
                chat.image = (len(chat.history), item[3])
            if who == "her" and name:
                if name in chat.senders:
                    chat.senders.remove(name)
                chat.senders.insert(0, name)
        return chat

    # ---- 生成
    def begin(self, title, msgs, reply_to=None, image=None, force=False, plan=None):
        """要一次生成。这个会话已经有一次在跑又没被取消（且不是强制）→ 排队合并、返回 None；否则发出 Request。"""
        chat = self.chat(title)
        if chat.running and not force:
            self._queued[title] = (tuple(msgs), reply_to, image, plan)
            return None
        return self._issue(chat, title, tuple(msgs), reply_to, image, plan)

    def _issue(self, chat, title, msgs, reply_to, image, plan=None):
        self._queued.pop(title, None)
        req = Request(next(self._gens), title, msgs, chat.rev, reply_to, image, plan)
        chat.live = req.gen
        chat.running.add(req.gen)
        return req

    def finish(self, req: Request, result=None):
        """网络线程回来了（主线程里调）。→ (accepted, next_request)：
        accepted = 它还是这个会话当前要的那一代（结果 / 错误可以用）；next_request = 排队的、该接着跑的。"""
        chat = self.chat(req.title)
        chat.running.discard(req.gen)
        accepted = chat.live == req.gen
        if accepted:
            chat.live = None
            if result is not None:
                chat.result, chat.result_req = result, req
        queued = self._queued.get(req.title)
        if accepted and queued == (req.msgs, req.reply_to, req.image, req.plan):
            queued = None  # 排队的和刚出结果的是同一份输入（生成期间又点了「立即生成」）：不再白跑一次
            self._queued.pop(req.title, None)
        nxt = None
        if queued and not chat.running:
            nxt = self._issue(chat, req.title, *queued)
        return accepted, nxt

    def settle(self, title):
        """对方之后没再说话（我已回复）：等的和排队的都不要了。"""
        chat = self.chat(title)
        chat.live = None
        self._queued.pop(title, None)

    def cancel(self, title):
        """用户取消：这次生成的结果必然作废；在跑的线程不杀，回来落空。取消后马上重新生成不用等它。"""
        chat = self.chat(title)
        chat.rev += 1
        chat.live = None
        chat.running.clear()
        self._queued.pop(title, None)

    def cancel_all(self):
        """采集彻底断开（窗口关了 / 重连）：所有会话在等的都作废。"""
        for title in list(self._chats):
            self.cancel(title)
        self._queued.clear()

    # ---- 单卡重 roll
    def reroll_begin(self, title, index, image=None, plan=None):
        chat = self._chats.get(title)
        result = chat.result if chat else None
        if (not chat or chat.live is not None or title in self._queued or result is None
                or chat.result_req is None or not (0 <= index < len(result.get("candidates") or []))):
            return None
        return RerollTicket(title, index, chat.rev, chat.result_req, tuple(chat.history), image, plan)

    def reroll_valid(self, ticket: RerollTicket) -> bool:
        """晚到的重 roll 只在：会话没动过、显示的还是它发出时那份结果、没有新的生成在替换它。"""
        chat = self._chats.get(ticket.title)
        return (chat is not None and chat.rev == ticket.rev and chat.result_req is ticket.basis
                and chat.live is None)

    # ---- 填入前的来历核对
    def fill_expect(self, title):
        """候选生成时的 (会话名, 最新一条 (谁, 正文))；来历不清或之后又动过 → 抛错。"""
        chat = self._chats.get(title)
        req = chat.result_req if chat else None
        if req is None or req.newest is None or req.rev != chat.rev or chat.live is not None:
            raise RuntimeError("会话里有新消息，候选可能过期，没有填入")
        return title, req.newest
