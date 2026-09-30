# -*- coding: utf-8 -*-
"""S0-B（Windows）：填入只进候选所属的会话；窗口已切走或拿不准就不打字。"""
import time
from types import SimpleNamespace

import pytest

import main
from core.fill_guard import check_fill_target

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


@pytest.fixture()
def wired(monkeypatch):
    """假界面 + 假 UIA 填入：记下 fill_uia 有没有被调到。"""
    typed = []
    # ov 只在 `python main.py` 的入口块里才建，import 时没有，所以 raising=False
    monkeypatch.setattr(main, "ov", SimpleNamespace(current_chat=lambda: A, at_prefix_enabled=lambda: False),
                        raising=False)
    monkeypatch.setattr(main, "fill_uia", lambda hwnd, app, text: typed.append((hwnd, app, text)))
    monkeypatch.setattr(main, "fill_errors", __import__("queue").Queue())
    main.state["uia"] = {A: (1234, (10, 10))}
    main.state["app_chat"] = {"qq": A}
    return typed


def _wait(pred, seconds=2.0):
    end = time.time() + seconds
    while time.time() < end and not pred():
        time.sleep(0.01)


class TestFillReply:
    def test_fills_when_the_window_still_shows_the_conversation(self, wired):
        main.fill_reply("Hallo")
        _wait(lambda: wired)
        assert wired == [(1234, "qq", "Hallo")]

    def test_switch_from_A_to_B_then_tap_A_types_nothing(self, wired):
        main.state["app_chat"]["qq"] = B
        with pytest.raises(RuntimeError):
            main.fill_reply("Hallo")
        assert wired == []

    def test_switch_between_click_and_write_types_nothing(self, wired, monkeypatch):
        """点击时还在 A，后台线程打字前 App 已经报了 B：线程里的再核验挡住。"""
        real_start = main.threading.Thread.start

        def switch_then_start(self_thread):
            main.state["app_chat"]["qq"] = B  # 模拟排队期间用户切走
            real_start(self_thread)

        monkeypatch.setattr(main.threading.Thread, "start", switch_then_start)
        main.fill_reply("Hallo")
        _wait(lambda: not main.fill_errors.empty())
        assert wired == []
        assert not main.fill_errors.empty()

    def test_app_never_reported_a_conversation_types_nothing(self, wired):
        main.state["app_chat"] = {}
        with pytest.raises(RuntimeError):
            main.fill_reply("Hallo")
        assert wired == []
