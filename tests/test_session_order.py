# -*- coding: utf-8 -*-
"""5A：chat_meta 数据层 + 会话下拉框最近活跃在前 + 陈旧沉底置灰。
item 的文本是纯标题，标题原文存 userData（findData/itemData 单参版，5B 装饰文本不污染 key）。"""
import time
from unittest import mock

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from app.overlay import Overlay, _ChatComboMenu
from app.qol import STALE_DAYS, is_stale
from app.theme import FAINT, INK

app = QApplication.instance() or QApplication([])


@pytest.fixture()
def overlay():
    with mock.patch("app.dock.Docker", mock.MagicMock()):
        ov = Overlay(on_fill=mock.MagicMock(), on_generate=mock.MagicMock(),
                     result_of=lambda chat: None)
    yield ov
    ov.win.close()
    ov.win.deleteLater()


def _order(overlay):
    return [overlay.chatBox.itemData(i) for i in range(overlay.chatBox.count())]


# ---------------------------------------------------------------- 5A-1 chat_meta
class TestChatMeta:
    def test_roundtrip_and_merge(self, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "config.json"))
        assert settings.chat_meta("张三") == {}, "老配置无 chat_meta 键容错"
        settings.set_chat_meta("张三", ts=1000)
        assert settings.chat_meta("张三") == {"ts": 1000}
        settings.set_chat_meta("张三", muted=True)  # 合并写：ts 不丢
        assert settings.chat_meta("张三") == {"ts": 1000, "muted": True}
        settings.set_chat_meta("李四", ts=2000)  # 别的会话条目不受影响
        assert settings.chat_meta("张三") == {"ts": 1000, "muted": True}

    def test_dirty_data(self, tmp_path, monkeypatch):
        import json
        import app.settings as settings
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"chat_meta": {"张三": "不是字典", "李四": {"ts": 1}}}),
                       encoding="utf-8")
        monkeypatch.setattr(settings, "_CONFIG", str(cfg))
        assert settings.chat_meta("张三") == {}, "脏条目按空处理"
        assert settings.chat_meta("李四") == {"ts": 1}

    def test_full_save_preserves_chat_meta(self, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setattr(settings, "_notify_env", lambda: None)
        settings.set_chat_meta("张三", ts=1000)
        settings.save()  # 设置页「保存设置」的全量保存
        assert settings.chat_meta("张三") == {"ts": 1000}


# ---------------------------------------------------------------- 5A-3 纯逻辑 is_stale
class TestIsStale:
    def test_boundary_exactly_seven_days(self):
        assert is_stale(1000, 1000 + STALE_DAYS * 86400, STALE_DAYS) is False, "恰好 7 天还不算陈旧"
        assert is_stale(1000, 1000 + STALE_DAYS * 86400 + 1, STALE_DAYS) is True

    def test_no_ts_never_stale(self):
        assert is_stale(0, 10 ** 10, STALE_DAYS) is False
        assert is_stale(None, 10 ** 10, STALE_DAYS) is False


# ---------------------------------------------------------------- 5A-2 排序
class TestChatOrder:
    def test_recent_first_after_activity(self, overlay):
        overlay.set_chat("甲")
        overlay.set_chat("乙")
        overlay.set_chat("丙")
        assert _order(overlay) == ["丙", "乙", "甲"], "前台切换即活跃，最近在前"
        overlay.log_message("her", "新消息", chat="甲")
        assert _order(overlay)[0] == "甲", "来消息的会话排到最前"

    def test_reorder_keeps_selection_and_fires_no_signal(self, overlay):
        overlay.set_chat("甲")
        overlay.set_chat("乙")  # 正在看乙
        fired = []
        overlay.chatBox.currentIndexChanged.connect(lambda i: fired.append(i))
        overlay.log_message("her", "x", chat="甲")  # 甲挤到最前
        current = overlay.chatBox.itemData(overlay.chatBox.currentIndex())
        assert current == "乙", "重排后选中态不丢"
        assert fired == [], "重排全程 blockSignals，不触发用户选择回调"

    def test_startup_sorts_by_persisted_ts(self, overlay, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
        settings.set_chat_meta("旧会话", ts=1000)
        settings.set_chat_meta("新会话", ts=2000)
        overlay._add_chat("旧会话")
        overlay._add_chat("新会话")  # ts 更大 → 插到最前
        overlay._add_chat("没存过的")  # 没有 ts → 排最后（按加入序）
        assert _order(overlay) == ["新会话", "旧会话", "没存过的"]

    def test_ts_flush_debounced_to_disk(self, overlay, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
        overlay.set_chat("甲")
        assert "甲" in overlay._tsDirty, "内存即写、盘延后"
        overlay._flush_chat_ts()  # 防抖到点
        assert settings.chat_meta("甲").get("ts"), "落盘的是 chat_meta 的 ts 字段"
        assert overlay._tsDirty == set()


# ---------------------------------------------------------------- 5A-3 陈旧置灰
class TestStalePaint:
    def test_stale_faint_fresh_ink(self, overlay, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
        settings.set_chat_meta("老会话", ts=time.time() - (STALE_DAYS + 1) * 86400)
        settings.set_chat_meta("新会话", ts=time.time())
        overlay._add_chat("老会话")
        overlay._add_chat("新会话")
        i_old = overlay.chatBox.findData("老会话")
        i_new = overlay.chatBox.findData("新会话")
        assert overlay._chat_item_color(i_old) == QColor(FAINT)
        assert overlay._chat_item_color(i_new) == QColor(INK)

    def test_refresh_restores_ink(self, overlay, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
        settings.set_chat_meta("老会话", ts=time.time() - (STALE_DAYS + 1) * 86400)
        overlay._add_chat("老会话")
        assert overlay._chat_item_color(overlay.chatBox.findData("老会话")) == QColor(FAINT)
        overlay.log_message("her", "又说话了", chat="老会话")  # ts 刷新 → 恢复 INK
        assert overlay._chat_item_color(overlay.chatBox.findData("老会话")) == QColor(INK)

    def test_menu_applies_foreground(self, overlay):
        menu = _ChatComboMenu(overlay.chatBox, lambda row: QColor(FAINT) if row == 0 else None)
        from PySide6.QtGui import QAction
        menu.addAction(QAction("旧会话", menu))
        menu.addAction(QAction("新会话", menu))
        assert menu.view.item(0).foreground().color() == QColor(FAINT)
        assert menu.view.item(1).foreground().color() != QColor(FAINT), "未着色的条目用默认色"
