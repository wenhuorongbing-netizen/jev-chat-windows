# -*- coding: utf-8 -*-
"""6A：生成取消（↻↔✕）。失败重试在 core/retry（见 test_contract_v1 / test_s4_route）。"""
from unittest import mock

import pytest
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentIcon as FIF

from app.overlay import Overlay

app = QApplication.instance() or QApplication([])


@pytest.fixture()
def overlay():
    with mock.patch("app.dock.Docker", mock.MagicMock()):
        ov = Overlay(on_fill=mock.MagicMock(), on_generate=mock.MagicMock(),
                     on_cancel=mock.MagicMock(), result_of=lambda chat: None)
    yield ov
    ov.win.close()
    ov.win.deleteLater()


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
