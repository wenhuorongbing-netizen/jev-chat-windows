# -*- coding: utf-8 -*-
"""6A：生成取消（↻↔✕）+ 失败自动重试一次（run_with_retry 纯函数）。"""
from unittest import mock

import pytest
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentIcon as FIF

from app.overlay import Overlay
from app.qol import run_with_retry

app = QApplication.instance() or QApplication([])


@pytest.fixture()
def overlay():
    with mock.patch("app.dock.Docker", mock.MagicMock()):
        ov = Overlay(on_fill=mock.MagicMock(), on_generate=mock.MagicMock(),
                     on_cancel=mock.MagicMock(), result_of=lambda chat: None)
    yield ov
    ov.win.close()
    ov.win.deleteLater()


# ---------------------------------------------------------------- 6A-2 run_with_retry 两试制
class TestRunWithRetry:
    def test_success_first_try(self):
        ok, value, attempts = run_with_retry(lambda: 42, lambda: True, 0, sleep=lambda s: None)
        assert (ok, value, attempts) == (True, 42, 1)

    def test_success_second_try(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("boom")
            return "ok"

        ok, value, attempts = run_with_retry(fn, lambda: True, 0, sleep=lambda s: None)
        assert (ok, value, attempts) == (True, "ok", 2)

    def test_both_fail_returns_last_exception(self):
        def fn():
            raise ValueError("boom")

        ok, value, attempts = run_with_retry(fn, lambda: True, 0, sleep=lambda s: None)
        assert ok is False
        assert isinstance(value, ValueError)
        assert attempts == 2

    def test_no_retry_when_should_not_continue(self):
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("boom")

        ok, value, attempts = run_with_retry(fn, lambda: False, 0, sleep=lambda s: None)
        assert ok is False and attempts == 1
        assert len(calls) == 1, "已被取消/来了新消息：不浪费第二次"

    def test_should_continue_checked_again_after_sleep(self):
        states = iter([True, False])  # 睡前放行，睡醒已被取消
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("boom")

        ok, value, attempts = run_with_retry(fn, lambda: next(states), 0, sleep=lambda s: None)
        assert ok is False and attempts == 1
        assert len(calls) == 1

    def test_sleeps_between_attempts(self):
        slept = []

        def fn():
            raise ValueError("x")

        run_with_retry(fn, lambda: True, 1.5, sleep=slept.append)
        assert slept == [1.5], "重试前等 pause_s，sleep 可注入（后台线程用 time.sleep，测试用假函数）"


# ---------------------------------------------------------------- 6A-1 忙态 ↻↔✕
class TestCancelButton:
    def test_idle_click_generates(self, overlay):
        overlay._shown = "某会话"
        overlay.generateButton.click()
        overlay.on_generate.assert_called_once_with("某会话")
        overlay.on_cancel.assert_not_called()

    def test_busy_swaps_icon_and_tooltip(self, overlay):
        with mock.patch.object(overlay.generateButton, "setIcon") as si:
            overlay.set_busy(True)
        assert si.call_args[0][0] is FIF.CLOSE
        assert overlay.generateButton.toolTip() == "取消这次生成"
        with mock.patch.object(overlay.generateButton, "setIcon") as si2:
            overlay.set_busy(False)
        assert si2.call_args[0][0] is FIF.SYNC
        assert "立即生成回复" in overlay.generateButton.toolTip()

    def test_busy_click_cancels_not_generates(self, overlay):
        overlay.set_busy(True)
        overlay.generateButton.click()
        overlay.on_cancel.assert_called_once_with()
        overlay.on_generate.assert_not_called()

    def test_after_cancel_idle_click_generates_again(self, overlay):
        overlay.set_busy(True)
        overlay.generateButton.click()  # 取消
        overlay.set_busy(False)  # main 侧 rev+1/busy 复位后按钮回 ↻
        overlay._shown = "某会话"
        overlay.generateButton.click()
        overlay.on_generate.assert_called_once_with("某会话")
