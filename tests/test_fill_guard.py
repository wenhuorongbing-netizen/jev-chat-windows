# -*- coding: utf-8 -*-
"""S0-B（Windows）：填入只进候选所属的会话；窗口已切走或拿不准就不打字。"""
import time
from types import SimpleNamespace

import pytest

import main
from core.convo import Coordinator
from core.fill_guard import CopyOnly, check_fill_target, check_fresh

A, B = "QQ · 小王", "QQ · 老李"


class TestCheckFillTarget:
    def test_same_conversation_is_allowed(self):
        assert check_fill_target(A, A) is None

    def test_switched_to_another_conversation_is_refused(self):
        assert check_fill_target(A, B)

    def test_unknown_open_conversation_is_refused(self):
        assert check_fill_target(A, None)

    def test_candidate_without_a_conversation_is_refused(self):
        assert check_fill_target("", A)


NEWEST = ("her", "Wie geht es dir?")


class TestCheckFresh:
    def ok(self, **kw):
        args = dict(chat=A, newest=NEWEST, fresh_chat=A, fresh_msgs=[("me", "hi"), NEWEST], has_input=True)
        args.update(kw)
        return check_fresh(args["chat"], args["newest"], args["fresh_chat"], args["fresh_msgs"], args["has_input"])

    def test_same_conversation_and_same_newest_message_is_allowed(self):
        assert self.ok() is None

    def test_other_conversation_is_refused(self):
        assert self.ok(fresh_chat=B)

    def test_unreadable_title_is_refused(self):
        assert self.ok(fresh_chat=None)

    def test_same_title_but_a_newer_message_is_refused(self):
        assert self.ok(fresh_msgs=[NEWEST, ("her", "在吗")])

    def test_same_title_but_scrolled_away_is_refused(self):
        assert self.ok(fresh_msgs=[("me", "hi")])
        assert self.ok(fresh_msgs=[])

    def test_no_input_box_is_refused(self):
        assert self.ok(has_input=False)

    def test_missing_generation_state_is_refused(self):
        assert self.ok(newest=None)
        assert self.ok(chat="")


class TestFillUiaFreshVerification:
    """fill_uia 打字前现读窗口：会话/最新消息对不上就一个字都不打（假 UIA，不碰真窗口）。"""

    @pytest.fixture()
    def rig(self, monkeypatch):
        import app.fill as fill

        typed, read = [], {"now": (A, [("me", "hi"), NEWEST], True)}
        monkeypatch.setattr(fill, "_fresh_read", lambda hwnd, app: read["now"])
        monkeypatch.setattr(fill, "_uia_input", lambda hwnd, app: SimpleNamespace(SetFocus=lambda: None))
        monkeypatch.setattr(fill, "_to_front", lambda hwnd: None)
        monkeypatch.setattr(fill, "_ctrl_end", lambda: None)
        monkeypatch.setattr(fill, "type_text", typed.append)
        return fill, typed, read

    def test_matching_window_types(self, rig):
        fill, typed, _ = rig
        fill.fill_uia(1, "qq", "Hallo", (A, NEWEST))
        assert typed == ["Hallo"]

    def test_switched_conversation_types_nothing(self, rig):
        fill, typed, read = rig
        read["now"] = (B, [NEWEST], True)
        with pytest.raises(RuntimeError):
            fill.fill_uia(1, "qq", "Hallo", (A, NEWEST))
        assert typed == []

    def test_switch_after_focus_before_typing_types_nothing(self, rig, monkeypatch):
        """第一次核验通过，取得焦点这段时间里窗口被切走：打字前第二次核验挡住。"""
        fill, typed, read = rig
        monkeypatch.setattr(fill, "_ctrl_end", lambda: read.update(now=(B, [NEWEST], True)))
        with pytest.raises(RuntimeError):
            fill.fill_uia(1, "qq", "Hallo", (A, NEWEST))
        assert typed == []

    def test_no_generation_state_types_nothing(self, rig):
        fill, typed, _ = rig
        with pytest.raises(RuntimeError):
            fill.fill_uia(1, "qq", "Hallo", None)
        assert typed == []


@pytest.fixture()
def wired(monkeypatch):
    """假界面 + 假 UIA 填入：记下 fill_uia 有没有被调到。"""
    typed = []
    # ov 只在 `python main.py` 的入口块里才建，import 时没有，所以 raising=False
    monkeypatch.setattr(main, "ov", SimpleNamespace(current_chat=lambda: A),
                        raising=False)
    monkeypatch.setattr(main, "fill_uia", lambda hwnd, app, text, expect: typed.append((hwnd, app, text, expect)))
    monkeypatch.setattr(main, "fill_errors", __import__("queue").Queue())
    main.state["uia"] = {A: (1234, (10, 10))}
    monkeypatch.setattr(main, "coord", Coordinator())
    main.coord.set_open_chat("qq", A)
    main.coord.messages(A, [("her", None, NEWEST[1])])  # 候选是针对这一条生成的，之后没再动过
    req = main.coord.begin(A, list(main.coord.chat(A).history))
    main.coord.finish(req, {"candidates": ["Hallo"]})
    return typed


def _wait(pred, seconds=2.0):
    end = time.time() + seconds
    while time.time() < end and not pred():
        time.sleep(0.01)


class TestFillReply:
    def test_fills_when_the_window_still_shows_the_conversation(self, wired):
        main.fill_reply("Hallo")
        _wait(lambda: wired)
        assert wired == [(1234, "qq", "Hallo", (A, NEWEST))]

    def test_a_newer_message_since_generation_types_nothing(self, wired):
        """标题没变，但生成候选之后会话又来了新消息（rev 变了）：不填。"""
        main.coord.messages(A, [("her", None, "在吗")])
        with pytest.raises(RuntimeError):
            main.fill_reply("Hallo")
        assert wired == []

    def test_candidate_without_generation_record_types_nothing(self, wired):
        main.coord.chat(A).result_req = None
        with pytest.raises(RuntimeError):
            main.fill_reply("Hallo")
        assert wired == []

    def test_switch_from_A_to_B_then_tap_A_types_nothing(self, wired):
        main.coord.set_open_chat("qq", B)
        with pytest.raises(RuntimeError):
            main.fill_reply("Hallo")
        assert wired == []

    def test_switch_between_click_and_write_types_nothing(self, wired, monkeypatch):
        """点击时还在 A，后台线程打字前 App 已经报了 B：线程里的再核验挡住。"""
        real_start = main.threading.Thread.start

        def switch_then_start(self_thread):
            main.coord.set_open_chat("qq", B)  # 模拟排队期间用户切走
            real_start(self_thread)

        monkeypatch.setattr(main.threading.Thread, "start", switch_then_start)
        main.fill_reply("Hallo")
        _wait(lambda: not main.fill_errors.empty())
        assert wired == []
        assert not main.fill_errors.empty()

    def test_app_never_reported_a_conversation_types_nothing(self, wired):
        main.coord.forget_open_chats()
        with pytest.raises(RuntimeError):
            main.fill_reply("Hallo")
        assert wired == []


class TestWeChatIsCopyOnly:
    """微信是 OCR 读的，没有能现读现对的会话标识：点「填入」不碰任何输入框，只让界面去复制。"""

    def test_wechat_fill_raises_copy_only_and_types_nothing(self, wired, monkeypatch):
        W = "小王"  # 微信会话名没有前缀
        monkeypatch.setattr(main, "ov", SimpleNamespace(current_chat=lambda: W),
                            raising=False)
        main.state["uia"][W] = (1234, (10, 10))  # 就算有人误给了输入框位置也不能填
        with pytest.raises(CopyOnly):
            main.fill_reply("Hallo")
        assert wired == []
