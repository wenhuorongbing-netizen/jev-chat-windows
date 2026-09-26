# -*- coding: utf-8 -*-
"""5B：按会话静音、未读计数徽标、移出列表。on_fill/回调全 mock，不碰真实生成。"""
from unittest import mock

import pytest
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentIcon as FIF

from app.overlay import Overlay
from app.qol import auto_generate_allowed

app = QApplication.instance() or QApplication([])


@pytest.fixture()
def overlay():
    with mock.patch("app.dock.Docker", mock.MagicMock()):
        ov = Overlay(on_fill=mock.MagicMock(), on_generate=mock.MagicMock(),
                     result_of=lambda chat: None)
    yield ov
    ov.win.close()
    ov.win.deleteLater()


@pytest.fixture()
def patched_settings(tmp_path, monkeypatch):
    import app.settings as settings
    monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
    return settings


# ---------------------------------------------------------------- 5B-1 静音
class TestMute:
    def test_auto_generate_allowed(self):
        assert auto_generate_allowed("甲", lambda t: {}) is True, "缺省（没存过）放行"
        assert auto_generate_allowed("甲", lambda t: {"muted": False}) is True
        assert auto_generate_allowed("甲", lambda t: {"muted": True}) is False

    def test_toggle_persists_and_repaints(self, overlay, patched_settings):
        overlay.set_chat("甲")
        with mock.patch.object(overlay.muteButton, "setIcon") as si:
            overlay._toggle_mute()
        assert patched_settings.chat_meta("甲")["muted"] is True
        assert si.call_args[0][0] is FIF.MUTE, "静音中用带斜杠图标"
        assert "已静音" in overlay.muteButton.toolTip()
        with mock.patch.object(overlay.muteButton, "setIcon") as si2:
            overlay._toggle_mute()
        assert patched_settings.chat_meta("甲")["muted"] is False
        assert si2.call_args[0][0] is FIF.RINGER

    def test_toggle_without_chat_is_noop(self, overlay, patched_settings):
        overlay._toggle_mute()  # _shown 为空：不炸、不落盘
        assert patched_settings.chat_meta("") == {}

    def test_switch_syncs_icon_state(self, overlay, patched_settings):
        overlay.set_chat("甲")
        overlay._toggle_mute()  # 甲静音
        overlay.set_chat("乙")
        assert "静音这个会话" in overlay.muteButton.toolTip(), "乙没静音，图标是未静音态"
        overlay._switch_to("甲")
        assert "已静音" in overlay.muteButton.toolTip(), "图标只表示当前正在看的会话"

    def test_status_hint_when_switching_to_muted_current_chat(self, overlay, patched_settings):
        overlay.set_chat("甲")
        overlay._toggle_mute()
        overlay.set_chat("乙")
        overlay.set_chat("甲")  # 前台切回静音会话
        assert "这个会话已静音" in overlay.status.text()


# ---------------------------------------------------------------- 5B-2 未读计数
class TestUnread:
    def test_accumulates_and_display_userdata_split(self, overlay):
        overlay.set_chat("甲")
        overlay.set_chat("乙")  # 正在看乙
        overlay.log_message("her", "1", chat="甲")
        overlay.log_message("her", "2", chat="甲")
        index = overlay.chatBox.findData("甲")
        assert overlay.chatBox.itemText(index) == "甲 · 2", "DisplayRole 带未读数"
        assert overlay.chatBox.itemData(index) == "甲", "UserRole 永远纯标题"

    def test_current_chat_not_counted(self, overlay):
        overlay.set_chat("甲")
        overlay.log_message("her", "在看呢", chat="甲")
        assert overlay.unread.get("甲", 0) == 0

    def test_switch_back_clears_and_restores_text(self, overlay):
        overlay.set_chat("甲")
        overlay.set_chat("乙")
        overlay.log_message("her", "1", chat="甲")
        overlay._switch_to("甲")
        assert overlay.unread.get("甲", 0) == 0
        index = overlay.chatBox.findData("甲")
        assert overlay.chatBox.itemText(index) == "甲", "清零后 DisplayRole 回到纯标题"

    def test_cap_99(self, overlay):
        overlay._add_chat("甲")
        for _ in range(105):
            overlay._bump_unread("甲")
        assert overlay.unread["甲"] == 99
        assert overlay.chatBox.itemText(overlay.chatBox.findData("甲")) == "甲 · 99"


# ---------------------------------------------------------------- 5B-3 移出列表
class TestRemove:
    def test_remove_clears_memory_and_disk(self, overlay, patched_settings):
        overlay.set_chat("甲")
        overlay.set_chat("乙")
        overlay.log_message("her", "hi", chat="甲")
        overlay.unread["甲"] = 3
        overlay.targets["甲"] = (["A"], "A")
        patched_settings.set_chat_meta("甲", ts=123, muted=True)
        patched_settings.set_chat_relationship("甲", "friends")
        overlay._switch_to("甲")
        overlay._remove_current_chat()
        assert overlay.chatBox.findData("甲") == -1, "item 消失"
        for store in (overlay.feeds, overlay.counts, overlay.hers, overlay.targets,
                      overlay.unread, overlay._chatTs):
            assert "甲" not in store, f"内存存档清空：{store}"
        assert patched_settings.chat_meta("甲") == {}, "chat_meta 条目删除"
        assert patched_settings.chat_relationship("甲") == "", "chat_rel 条目一并删"
        assert "已移出" in overlay.status.text()

    def test_remove_viewed_falls_back_to_first(self, overlay):
        overlay.set_chat("甲")
        overlay.set_chat("乙")
        overlay._switch_to("甲")
        overlay._remove_current_chat()
        assert overlay._shown == "乙", "被删的是正在看的会话 → 切到下拉框第一项"

    def test_remove_last_chat_returns_empty_state(self, overlay):
        overlay.set_chat("唯一")
        overlay._remove_current_chat()
        assert overlay.chatBox.count() == 0
        assert overlay._shown == ""
        assert overlay.empty.isVisible(), "删光回空态"

    def test_context_menu_offers_remove(self, overlay):
        overlay.set_chat("甲")
        menu = overlay.chatBox._build_context_menu()
        assert menu.actions()[0].text() == "把这个会话移出列表"
        menu.actions()[0].trigger()
        assert overlay.chatBox.count() == 0


# ---------------------------------------------------------------- 集成：两轮消息流（A 静音 B 未读）
def test_integration_muted_chat_vs_unread(overlay, patched_settings):
    overlay.set_chat("A")
    overlay._toggle_mute()  # A 静音
    assert patched_settings.chat_meta("A")["muted"] is True
    overlay.set_chat("B")  # 转去看 B
    # 第一轮：A（静音、没在看）来消息，B（在看）也来消息
    overlay.log_message("her", "A 的第一条", chat="A")
    overlay.log_message("her", "B 的第一条", chat="B")
    assert auto_generate_allowed("A", patched_settings.chat_meta) is False, "A 静音：跳过自动生成"
    assert auto_generate_allowed("B", patched_settings.chat_meta) is True
    assert overlay.unread.get("A") == 1, "静音只挡自动生成，未读照记"
    assert overlay.unread.get("B", 0) == 0, "正在看的不计未读"
    assert any("A 的第一条" in line for line in overlay.feeds["A"]), "history 照记"
    # 第二轮
    overlay.log_message("her", "A 的第二条", chat="A")
    assert overlay.unread["A"] == 2
    index = overlay.chatBox.findData("A")
    assert overlay.chatBox.itemText(index) == "A · 2"
    assert overlay.chatBox.itemData(index) == "A"
    # 前台切回 A：未读清零、静音图标态与状态提示跟上
    overlay.set_chat("A")
    assert overlay.unread.get("A", 0) == 0
    assert "已静音" in overlay.muteButton.toolTip()
    assert "这个会话已静音" in overlay.status.text()
