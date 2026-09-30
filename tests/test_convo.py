# -*- coding: utf-8 -*-
"""S2：会话状态唯一所有者。过期的生成结果 / 重 roll / 取消后的晚到结果，都只看「这一代 gen 还算不算数」，
不靠会话名、也不靠全局 busy 旗标。上半是 Coordinator 纯逻辑，下半是 main 的 tick 怎么用它（假界面）。"""
import queue
from types import SimpleNamespace

import pytest

import main
from core.convo import Coordinator, Request

A, B = "QQ · 小王", "QQ · 老李"
RESULT = {"candidates": ["一", "二", "三"], "glosses": ["", "", ""], "lang": "中文"}


def say(coord, title, text, who="her"):
    coord.messages(title, [(who, None, text)])
    return list(coord.chat(title).history)


def generate(coord, title, result=None):
    """起一次生成并让它回来（被接受）。"""
    req = coord.begin(title, list(coord.chat(title).history))
    accepted, _ = coord.finish(req, result or {**RESULT, "candidates": list(RESULT["candidates"])})
    assert accepted
    return req


class TestRequest:
    def test_a_request_is_an_immutable_snapshot(self):
        c = Coordinator()
        say(c, A, "在吗")
        req = c.begin(A, list(c.chat(A).history))
        say(c, A, "人呢")  # 之后会话又长了
        assert req.msgs == (("her", "在吗", None),) and req.newest == ("her", "在吗")
        with pytest.raises(AttributeError):
            req.msgs = ()
        assert isinstance(req.msgs, tuple)

    def test_the_candidate_traces_back_to_its_request(self):
        c = Coordinator()
        say(c, A, "在吗")
        req = generate(c, A)
        assert c.chat(A).result_req is req and c.chat(A).result["candidates"][0] == "一"


class TestSwitchWhileGenerating:
    def test_A_returning_after_switch_to_B_lands_in_A_only(self):
        c = Coordinator()
        say(c, A, "在吗")
        req_a = c.begin(A, list(c.chat(A).history))
        c.set_open_chat("qq", B)  # 用户切到 B
        say(c, B, "吃了吗")
        req_b = c.begin(B, list(c.chat(B).history))
        assert req_b is not None, "B 不该被 A 在跑的生成堵住"
        assert c.finish(req_a, RESULT)[0] is True
        assert c.result_of(A) is RESULT and c.result_of(B) is None
        with pytest.raises(RuntimeError):  # B 上还没有候选，填入拿不到来历
            c.fill_expect(B)

    def test_a_queued_rerun_is_per_conversation(self):
        c = Coordinator()
        say(c, A, "a1")
        say(c, B, "b1")
        ra, rb = c.begin(A, list(c.chat(A).history)), c.begin(B, list(c.chat(B).history))
        say(c, A, "a2")
        say(c, B, "b2")
        assert c.begin(A, list(c.chat(A).history)) is None and c.begin(B, list(c.chat(B).history)) is None
        nxt_a = c.finish(ra)[1]
        nxt_b = c.finish(rb)[1]
        assert nxt_a.title == A and nxt_a.newest == ("her", "a2")
        assert nxt_b.title == B and nxt_b.newest == ("her", "b2"), "A 的排队不该被 B 挤掉"


class TestNewMessageWhileGenerating:
    def test_the_old_generation_is_dropped_and_the_newest_one_runs_next(self):
        c = Coordinator()
        say(c, A, "在吗")
        n = c.begin(A, list(c.chat(A).history))
        say(c, A, "人呢")  # N+1
        assert c.begin(A, list(c.chat(A).history)) is None  # 排队
        accepted, nxt = c.finish(n, RESULT)
        assert accepted is False and c.result_of(A) is None, "N 的结果不能当 N+1 的候选"
        assert nxt is not None and nxt.newest == ("her", "人呢") and c.is_live(nxt)

    def test_asking_again_for_the_same_input_while_it_runs_does_not_run_twice(self):
        c = Coordinator()
        say(c, A, "在吗")
        n = c.begin(A, list(c.chat(A).history))
        assert c.begin(A, list(c.chat(A).history)) is None  # 「立即生成」又点了一次：排队
        accepted, nxt = c.finish(n, RESULT)
        assert accepted is True and nxt is None, "输入没变，排队的那次不用再白跑"
        assert not c.is_generating(A)

    def test_the_old_generation_is_not_live_so_the_retry_is_skipped(self):
        c = Coordinator()
        say(c, A, "在吗")
        n = c.begin(A, list(c.chat(A).history))
        assert c.is_live(n)
        say(c, A, "人呢")
        assert not c.is_live(n)

    def test_I_replied_drops_the_wait_and_the_queue(self):
        c = Coordinator()
        say(c, A, "在吗")
        n = c.begin(A, list(c.chat(A).history))
        say(c, A, "人呢")
        c.begin(A, list(c.chat(A).history))
        say(c, A, "在的", who="me")
        c.settle(A)
        accepted, nxt = c.finish(n, RESULT)
        assert (accepted, nxt) == (False, None) and not c.is_generating(A)


class TestCancel:
    def test_a_cancelled_result_cannot_revive(self):
        c = Coordinator()
        say(c, A, "在吗")
        n = c.begin(A, list(c.chat(A).history))
        c.cancel(A)
        assert not c.is_live(n) and not c.is_generating(A)
        assert c.finish(n, RESULT) == (False, None) and c.result_of(A) is None

    def test_regenerate_right_after_cancel_is_not_blocked_and_the_late_zombie_changes_nothing(self):
        c = Coordinator()
        say(c, A, "在吗")
        old = c.begin(A, list(c.chat(A).history))
        c.cancel(A)
        new = c.begin(A, list(c.chat(A).history))
        assert new is not None and new.gen != old.gen
        assert c.finish(old, {"candidates": ["旧"]}) == (False, None)  # 旧线程迟到
        assert c.is_live(new) and c.is_generating(A), "迟到的旧结果不能把新一代的等待清掉"
        assert c.finish(new, RESULT)[0] is True and c.result_of(A) is RESULT


class TestReconnect:
    def test_capture_dead_cancels_everything_and_forgets_the_open_chats(self):
        c = Coordinator()
        say(c, A, "a")
        say(c, B, "b")
        ra, rb = c.begin(A, list(c.chat(A).history)), c.begin(B, list(c.chat(B).history))
        c.set_open_chat("qq", A)
        c.cancel_all()
        c.forget_open_chats()
        assert c.finish(ra, RESULT)[0] is False and c.finish(rb, RESULT)[0] is False
        assert c.open_chat("qq") is None  # 重连后要等 App 重新报，旧的不可信
        assert c.result_of(A) is None and c.result_of(B) is None


class TestRerollRaces:
    def setup_method(self):
        self.c = Coordinator()
        say(self.c, A, "在吗")
        self.first = generate(self.c, A)

    def test_reroll_of_the_shown_result_is_valid(self):
        t = self.c.reroll_begin(A, 1)
        assert t is not None and t.basis is self.first and self.c.reroll_valid(t)

    def test_regenerating_replaces_the_result_so_a_late_reroll_is_dropped(self):
        """↻ 不动会话版本；以前重 roll 的晚到结果能写进新结果里。"""
        t = self.c.reroll_begin(A, 1)
        again = self.c.begin(A, list(self.c.chat(A).history), force=True)
        assert not self.c.reroll_valid(t), "新一代在等，旧结果马上要被换掉"
        self.c.finish(again, {"candidates": ["新一", "新二", "新三"]})
        assert not self.c.reroll_valid(t), "新结果已经是另一份，重 roll 不能写进去"

    def test_a_new_message_drops_the_reroll(self):
        t = self.c.reroll_begin(A, 1)
        say(self.c, A, "在吗？？")
        assert not self.c.reroll_valid(t)

    def test_cancel_drops_the_reroll(self):
        t = self.c.reroll_begin(A, 1)
        self.c.cancel(A)
        assert not self.c.reroll_valid(t)

    def test_no_reroll_while_generating_or_without_a_result_or_out_of_range(self):
        assert self.c.reroll_begin(A, 3) is None and self.c.reroll_begin(A, -1) is None
        assert self.c.reroll_begin(B, 0) is None
        self.c.begin(A, list(self.c.chat(A).history), force=True)
        assert self.c.reroll_begin(A, 0) is None


class TestFillProvenance:
    def test_expect_is_the_newest_message_the_request_was_built_from(self):
        c = Coordinator()
        say(c, A, "在吗")
        generate(c, A)
        assert c.fill_expect(A) == (A, ("her", "在吗"))

    def test_refused_after_a_new_message_or_during_regeneration_or_without_provenance(self):
        c = Coordinator()
        say(c, A, "在吗")
        generate(c, A)
        c.begin(A, list(c.chat(A).history), force=True)
        with pytest.raises(RuntimeError):
            c.fill_expect(A)
        c.cancel(A)
        with pytest.raises(RuntimeError):
            c.fill_expect(A)  # 取消让会话版本前进
        say(c, B, "hi")
        generate(c, B)
        c.chat(B).result_req = None
        with pytest.raises(RuntimeError):
            c.fill_expect(B)


class TestForeground:
    def test_the_ui_follows_only_the_foreground_app(self):
        c = Coordinator()
        c.set_open_chat("qq", A)
        assert c.follows("qq") and c.follows("whatsapp"), "还没判断过前台：都跟"
        assert c.set_foreground("qq") == A
        assert c.follows("qq") and not c.follows("whatsapp")
        assert c.set_foreground("qq") is None


# ---------------------------------------------------------------- main 里怎么用它：假界面
class FakeOv:
    def __init__(self, current):
        self.current = current
        self.calls = []

    def current_chat(self):
        return self.current

    def __getattr__(self, name):
        def record(*args, **kw):
            self.calls.append((name, args))
        return record

    def busy_calls(self):
        return [a[0] for n, a in self.calls if n == "set_busy"]

    def shown(self):
        return [a[0] for n, a in self.calls if n == "show"]

    def statuses(self):
        return [a for n, a in self.calls if n == "set_status"]


@pytest.fixture()
def rig(monkeypatch):
    coord = Coordinator()
    ov = FakeOv(A)
    monkeypatch.setattr(main, "coord", coord)
    monkeypatch.setattr(main, "ov", ov, raising=False)
    started = []
    monkeypatch.setattr(main, "analyze_bg", lambda req: started.append(req))
    monkeypatch.setattr(main.threading, "Thread", lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)))
    return coord, ov, started


class TestTickUsesTheOwner:
    def test_a_stale_result_does_not_clear_a_newer_generations_busy(self, rig):
        """以前：取消后重新生成，旧线程的迟到结果先把全局 busy 清成 False，还吞掉排队的重跑。"""
        coord, ov, _ = rig
        say(coord, A, "在吗")
        old = coord.begin(A, list(coord.chat(A).history))
        main.cancel_generate()
        new = coord.begin(A, list(coord.chat(A).history), force=True)
        ov.calls.clear()
        main._take_result("err", "分析失败: 超时", old)  # 旧线程迟到的失败
        assert ov.busy_calls() == [True], "新一代还在等，界面必须仍是忙"
        assert ov.statuses() == [], "过期的失败不提示"
        assert coord.is_live(new)

    def test_a_result_for_another_conversation_is_stored_but_not_shown(self, rig):
        coord, ov, _ = rig
        say(coord, B, "吃了吗")
        req = coord.begin(B, list(coord.chat(B).history))
        main._take_result("ok", RESULT, req)  # 界面正看着 A
        assert ov.shown() == [] and coord.result_of(B) is RESULT and coord.result_of(A) is None

    def test_a_current_conversation_result_is_shown(self, rig):
        coord, ov, _ = rig
        say(coord, A, "在吗")
        req = coord.begin(A, list(coord.chat(A).history))
        main._take_result("ok", RESULT, req)
        assert ov.shown() == [RESULT]

    def test_a_result_made_stale_by_a_new_message_starts_the_queued_rerun(self, rig):
        coord, ov, started = rig
        say(coord, A, "在吗")
        n = coord.begin(A, list(coord.chat(A).history))
        say(coord, A, "人呢")
        coord.begin(A, list(coord.chat(A).history))
        main._take_result("ok", RESULT, n)
        assert ov.shown() == [] and [r.newest for r in started] == [("her", "人呢")]

    def test_capture_pause_does_not_invalidate_an_in_flight_generation(self, rig, monkeypatch):
        """暂停只是不再读新消息，会话内容没变，已发出的生成仍对得上它的来历；继续采集后有新消息才作废。"""
        coord, ov, _ = rig
        say(coord, A, "在吗")
        req = coord.begin(A, list(coord.chat(A).history))
        inbox = queue.Queue()
        inbox.put(("paused",))
        monkeypatch.setattr(main, "q", inbox, raising=False)
        main.drain()
        assert ("set_capture", (False,)) in ov.calls
        assert coord.is_live(req)
        main._take_result("ok", RESULT, req)
        assert ov.shown() == [RESULT]

    def test_a_late_reroll_after_regenerate_is_dropped_by_tick(self, rig):
        coord, ov, _ = rig
        say(coord, A, "在吗")
        generate(coord, A)
        ticket = coord.reroll_begin(A, 1)
        again = coord.begin(A, list(coord.chat(A).history), force=True)
        coord.finish(again, {"candidates": ["新一", "新二", "新三"], "glosses": ["", "", ""]})
        ov.calls.clear()
        main._take_reroll((1, "晚到的", "", ""), ticket)
        assert coord.result_of(A)["candidates"][1] == "新二"
        assert ov.calls == []
