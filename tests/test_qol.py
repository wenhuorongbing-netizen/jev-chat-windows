# -*- coding: utf-8 -*-
"""QOL 包（Q1–Q7）：托盘、Alt+J/G、Esc、暂停 30 分钟、位置记忆、feed 右键。
on_fill / 热键 / ctypes 全 mock，绝不触发真实填入或全局热键。"""
from unittest import mock

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import QApplication

from app.overlay import Overlay
from app.qol import fit_rect, fmt_remaining, pause_due

app = QApplication.instance() or QApplication([])

FOREIGN = {
    "candidates": ["Wie geht es dir?", "Alles klar, danke!", "Bis spaeter!"],
    "glosses": ["你最近怎么样", "都挺好，谢谢", "回头见"],
    "best_index": 0,
    "scores": [0.9, 0.5, 0.2],
    "translation": "今晚一起吃饭吗",
    "lang": "德语",
    "analysis": "对方在随口寒暄",
}


@pytest.fixture()
def overlay():
    with mock.patch("app.dock.Docker", mock.MagicMock()):
        ov = Overlay(on_fill=mock.MagicMock(), on_generate=mock.MagicMock(),
                     result_of=lambda chat: None)
    yield ov
    ov.win.close()
    ov.win.deleteLater()


# ---------------------------------------------------------------- Q4 暂停 30 分钟：纯逻辑
class TestPausePure:
    def test_fmt_remaining(self):
        assert fmt_remaining(1800) == "30:00"
        assert fmt_remaining(1799) == "29:59"
        assert fmt_remaining(61) == "1:01"
        assert fmt_remaining(60) == "1:00"
        assert fmt_remaining(59) == "0:59"
        assert fmt_remaining(0) == "0:00"
        assert fmt_remaining(-5) == "0:00", "到点那一帧不显示负数"

    def test_pause_due(self):
        assert pause_due(100, 100) is True
        assert pause_due(101, 100) is True
        assert pause_due(99, 100) is False


class TestPauseIntegration:
    def test_pause30_starts_countdown(self, overlay):
        overlay._clock = lambda: 1000.0  # 注入假时钟
        overlay.pause_capture_30()
        assert overlay._pauseUntil == 1000.0 + 30 * 60
        assert not overlay.captureSwitch.isChecked(), "走正常关采集路径"
        assert "30:00 后恢复" in overlay.status.text()

    def test_pause30_due_resumes(self, overlay):
        overlay._clock = lambda: 1000.0
        overlay.pause_capture_30()
        overlay._clock = lambda: 1000.0 + 30 * 60  # 到点
        overlay._pause_tick()
        assert overlay.captureSwitch.isChecked(), "到点自动恢复采集"
        assert overlay._pauseUntil is None
        assert "已恢复采集" in overlay.status.text()

    def test_pause30_manual_toggle_cancels(self, overlay):
        overlay._clock = lambda: 1000.0
        overlay.pause_capture_30()
        overlay.captureSwitch.setChecked(True)  # 用户等不及，手动再开
        assert overlay._pauseUntil is None
        assert not overlay._pauseTimer.isActive()


# ---------------------------------------------------------------- Q5 位置记忆：fit_rect 全分支
class TestFitRect:
    SCREENS = [(0, 0, 1920, 1040), (1920, 0, 1920, 1080)]

    def test_inside_unchanged(self):
        assert fit_rect((100, 100, 360, 760), self.SCREENS) == (100, 100, 360, 760)

    def test_left_overflow_clamped(self):
        assert fit_rect((-100, 100, 360, 760), self.SCREENS) == (0, 100, 360, 760)

    def test_right_overflow_clamped(self):
        assert fit_rect((1800, 100, 360, 760), [(0, 0, 1920, 1040)]) == (1920 - 360, 100, 360, 760)

    def test_partial_overlap_picks_max_screen(self):
        # 横跨两屏、落在第二屏的部分更多：钳进相交最多的那块屏
        assert fit_rect((1800, 100, 360, 760), self.SCREENS)[0] == 1920

    def test_fully_left_outside_returns_default(self):
        # 完全落在所有屏之外（哪怕只是左边 1px 都不沾）→ 主屏右缘默认位
        x, y, w, h = fit_rect((-500, 100, 360, 760), self.SCREENS)
        assert (x, y, w, h) == (0 + 1920 - 360 - 20, 0 + 24, 360, 760)

    def test_bottom_overflow_clamped(self):
        assert fit_rect((100, 900, 360, 760), self.SCREENS) == (100, 1040 - 760, 360, 760)

    def test_multi_screen_second_unchanged(self):
        assert fit_rect((2000, 50, 360, 700), self.SCREENS) == (2000, 50, 360, 700)

    def test_completely_outside_returns_default(self):
        x, y, w, h = fit_rect((5000, 5000, 360, 760), self.SCREENS)
        assert (x, y, w, h) == (0 + 1920 - 360 - 20, 0 + 24, 360, 760), "全出界回主屏右缘默认位"


class TestWinStateSettings:
    def test_win_state_roundtrip_and_missing_fields(self, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "config.json"))
        assert settings.win_pos() is None and settings.win_width() is None, "老配置缺字段容错"
        settings.save_win_state(100, 200, 360)
        assert settings.win_pos() == (100, 200)
        assert settings.win_width() == 360

    def test_full_save_preserves_win_state(self, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setattr(settings, "_notify_env", lambda: None)
        settings.save_win_state(10, 20, 360)
        settings.save()  # 设置页「保存设置」的全量保存
        assert settings.win_pos() == (10, 20) and settings.win_width() == 360


class TestWinStateOverlay:
    def test_geometry_saved_only_when_undocked(self, overlay, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
        overlay.win.move(120, 130)
        overlay.docked = True
        overlay._save_geometry()
        assert settings.win_pos() is None, "贴靠时位置由聊天窗口决定，不记"
        overlay.docked = False
        overlay._save_geometry()
        assert settings.win_pos() == (120, 130)
        assert settings.win_width() == overlay.win.width()

    def test_restore_geometry_clamps_to_screens(self, overlay, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
        settings.save_win_state(5000, 5000, 400)  # 全出界 + 记过宽度
        overlay.docked = False
        overlay._restore_geometry()
        sx, sy, sw, sh = app.primaryScreen().availableGeometry().getRect()
        assert overlay.win.pos().x() == sx + sw - 400 - 20
        assert overlay.win.pos().y() == sy + 24
        assert overlay.win.width() == 400


# ---------------------------------------------------------------- Q2/Q7 热键
class TestHotkeys:
    def test_register_unregister_in_pairs(self, overlay):
        with mock.patch("app.overlay.ctypes.windll.user32") as u32:
            overlay.enable_hotkeys(True)
            ids_vks = sorted((c.args[1], c.args[3]) for c in u32.RegisterHotKey.call_args_list)
            assert ids_vks == [(1, 0x31), (2, 0x32), (3, 0x33), (4, 0x4A), (5, 0x47)]
            overlay.enable_hotkeys(False)
            assert sorted(c.args[1] for c in u32.UnregisterHotKey.call_args_list) == [1, 2, 3, 4, 5]
            overlay.enable_hotkeys(True)
            overlay.enable_hotkeys(False)
            assert u32.RegisterHotKey.call_count == 10
            assert u32.UnregisterHotKey.call_count == 10, "注册/注销必须成对"

    def test_dispatch(self, overlay):
        overlay.show(FOREIGN)
        overlay._on_hotkey(1)  # Alt+1 填第 1 张卡（现状不变）
        overlay.on_fill.assert_called_once_with(FOREIGN["candidates"][0])
        overlay.win.hide()
        overlay._on_hotkey(4)  # Alt+J 显示
        assert overlay.win.isVisible()
        overlay._on_hotkey(4)  # Alt+J 再按隐藏
        assert not overlay.win.isVisible()
        overlay._shown = "某个会话"
        overlay._on_hotkey(5)  # Alt+G 立即生成 = ↻ 按钮
        overlay.on_generate.assert_called_once_with("某个会话")


# ---------------------------------------------------------------- Q3 Esc
def test_esc_hides_panel(overlay):
    overlay.win.show()
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    overlay.win.keyPressEvent(event)
    assert not overlay.win.isVisible()


# ---------------------------------------------------------------- Q1 托盘
class TestTray:
    def test_menu_actions_and_quit_path(self, overlay):
        menu = overlay.trayMenu
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        assert texts == ["隐藏面板", "采集", "暂停 30 分钟", "退出"]
        quit_action = menu.actions()[-1]
        overlay.win.show()
        quit_action.trigger()
        assert not overlay.win.isVisible(), "退出与标题栏关闭同一清理路径（win.close）"

    def test_capture_action_syncs_switch_both_ways(self, overlay):
        capture = overlay.trayMenu.actions()[1]
        assert capture.isCheckable()
        capture.setChecked(False)  # 托盘 → 开关
        assert not overlay.captureSwitch.isChecked()
        overlay.captureSwitch.setChecked(True)  # 开关 → 托盘
        assert capture.isChecked()

    def test_toggle_text_follows_visibility(self, overlay):
        toggle = overlay.trayMenu.actions()[0]
        overlay.win.show()
        overlay._sync_tray_menu()
        assert toggle.text() == "隐藏面板"
        overlay.win.hide()
        overlay._sync_tray_menu()
        assert toggle.text() == "显示面板"

    def test_pause_action_from_tray(self, overlay):
        overlay._clock = lambda: 500.0
        overlay.trayMenu.actions()[2].trigger()
        assert overlay._pauseUntil == 500.0 + 30 * 60


# ---------------------------------------------------------------- Q4 触发口二：采集开关右键
def test_capture_switch_context_menu(overlay):
    menu = overlay.captureSwitch._build_menu()
    assert menu.actions()[0].text() == "暂停 30 分钟"
    overlay._clock = lambda: 500.0
    menu.actions()[0].trigger()
    assert overlay._pauseUntil == 500.0 + 30 * 60


# ---------------------------------------------------------------- Q6 feed 右键菜单
class TestFeedMenu:
    def test_copy_selected_disabled_without_selection(self, overlay):
        overlay.feed.setPlainText("line1\nline2")
        menu = overlay.feed._build_menu()
        assert not menu.actions()[0].isEnabled(), "无选区时「复制所选」禁用"
        cursor = overlay.feed.textCursor()
        cursor.select(QTextCursor.Document)
        overlay.feed.setTextCursor(cursor)
        menu2 = overlay.feed._build_menu()
        assert menu2.actions()[0].isEnabled()

    def test_copy_all(self, overlay):
        overlay.feed.setPlainText("一\n二")
        overlay.feed._build_menu().actions()[1].trigger()
        assert app.clipboard().text() == "一\n二"

    def test_clear_display_keeps_archive(self, overlay):
        overlay.feeds["某个会话"] = ["10:00  对方\n你好\n"]
        overlay.feed.setPlainText("10:00  对方\n你好\n")
        overlay.feed._build_menu().actions()[2].trigger()
        assert overlay.feed.toPlainText() == ""
        assert overlay.feeds["某个会话"], "只清界面，不动 feeds 存档"
