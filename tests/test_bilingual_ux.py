# -*- coding: utf-8 -*-
"""R7：多语言对照体验——复制补全（剪贴板，非填入）、「对方说」原文 2 行省略↔展开、
设置项「回复卡上显示中文意思」。铁律：点卡仍填入、填入只填外语。"""
import json
from unittest import mock

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from app.overlay import Overlay

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

CHINESE = {
    "candidates": ["好的，马上来", "没问题", "收到"],
    "glosses": ["好的，马上来", "没问题", "收到"],
    "best_index": 0,
    "scores": [0.7, 0.5, 0.3],
    "translation": "今晚一起吃饭吗",
    "lang": "中文",
    "analysis": "",
}


@pytest.fixture()
def overlay():
    with mock.patch("app.dock.Docker", mock.MagicMock()):
        ov = Overlay(on_fill=mock.MagicMock(), on_generate=mock.MagicMock(),
                     on_cancel=mock.MagicMock(), on_reroll=mock.MagicMock(),
                     result_of=lambda chat: None)
    yield ov
    ov.win.close()
    ov.win.deleteLater()


def _press():
    return QMouseEvent(QEvent.MouseButtonPress, QPointF(3, 3), QPointF(100, 100),
                       Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)


def _release():
    return QMouseEvent(QEvent.MouseButtonRelease, QPointF(3, 3), QPointF(100, 100),
                       Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)


# ---------------------------------------------------------------- R7-1「对方说」卡复制菜单
class TestInsightCardMenu:
    def test_items_follow_content(self, overlay):
        overlay.show(FOREIGN)
        overlay._show_latest("Wie geht es dir heute?")
        menu = overlay.insight._build_menu()
        assert [a.text() for a in menu.actions()] == ["复制原文", "复制译文"]

    def test_no_translation_no_item(self, overlay):
        overlay.show(CHINESE)  # 中文：译文行隐藏
        overlay._show_latest("晚上吃啥")
        menu = overlay.insight._build_menu()
        assert [a.text() for a in menu.actions()] == ["复制原文"]

    def test_empty_original_no_item(self, overlay):
        overlay.show(FOREIGN)
        menu = overlay.insight._build_menu()  # 还没收到原文
        assert [a.text() for a in menu.actions()] == ["复制译文"]

    def test_copy_original_writes_full_text(self, overlay):
        long_text = "很长的原文" * 100  # 界面上 2 行省略，但复制要拿全文
        overlay.show(FOREIGN)
        overlay._show_latest(long_text)
        overlay.insight._build_menu().actions()[0].trigger()
        assert app.clipboard().text() == long_text
        assert overlay.toast.text() == "已复制"

    def test_copy_translation(self, overlay):
        overlay.show(FOREIGN)
        overlay._show_latest("x")
        overlay.insight._build_menu().actions()[1].trigger()
        assert app.clipboard().text() == FOREIGN["translation"]
        assert overlay.toast.text() == "已复制"


# ---------------------------------------------------------------- R7-2 原文 2 行省略 ↔ 展开
class TestLatestElide:
    def test_long_original_collapsed_then_expand(self, overlay):
        long_text = "一" * 300
        overlay.latest.set_full(long_text)
        overlay.insight.show()
        app.processEvents()  # 让布局给出真实宽度
        assert overlay.latest.toolTip() == long_text
        assert not overlay.latest._expanded
        rendered = overlay.latest.text()
        assert rendered.endswith("…"), "默认 2 行省略"
        assert len(rendered) < len(long_text)
        overlay.latest.mousePressEvent(_press())
        assert overlay.latest._expanded, "点击展开全文"
        assert overlay.latest.text() == long_text
        overlay.latest.mousePressEvent(_press())
        assert not overlay.latest._expanded, "再点收回 2 行省略"

    def test_short_original_no_interaction(self, overlay):
        overlay.latest.set_full("短")
        overlay.insight.show()
        app.processEvents()
        assert overlay.latest.text() == "短", "短原文无省略号"
        overlay.latest.mousePressEvent(_press())
        assert not overlay.latest._expanded, "短原文无交互"


# ---------------------------------------------------------------- R7-1 回复卡 gloss 复制 + 点卡仍填入
class TestReplyCardGloss:
    def test_gloss_item_only_when_gloss_present(self, overlay):
        overlay.show(FOREIGN)
        texts = [a.text() for a in overlay.cards[0]._build_menu().actions()]
        assert "复制中文意思" in texts
        overlay.show(CHINESE)  # 中文卡没有 gloss 行
        texts2 = [a.text() for a in overlay.cards[0]._build_menu().actions()]
        assert "复制中文意思" not in texts2

    def test_copy_gloss_writes_clipboard(self, overlay):
        overlay.show(FOREIGN)
        action = next(a for a in overlay.cards[1]._build_menu().actions()
                      if a.text() == "复制中文意思")
        action.trigger()
        assert app.clipboard().text() == FOREIGN["glosses"][1]
        assert overlay.toast.text() == "已复制中文意思"

    def test_gloss_selectable_and_click_still_fills(self, overlay):
        overlay.show(FOREIGN)
        card = overlay.cards[0]
        assert card.gloss.textInteractionFlags() & Qt.TextSelectableByMouse, "gloss 可选中手抄"
        assert not card.gloss.hasSelectedText()
        card.gloss.mouseReleaseEvent(_release())  # 没拖出选区的点击 = 点卡
        overlay.on_fill.assert_called_once_with(FOREIGN["candidates"][0]), "铁律：点卡仍触发填入"


# ---------------------------------------------------------------- R7-3 设置项「回复卡上显示中文意思」
class TestShowGlossSetting:
    def test_default_on_and_persist(self, tmp_path, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
        monkeypatch.setattr(settings, "_set_key", lambda *a: None)
        monkeypatch.setattr(settings, "_notify_env", lambda: None)
        assert settings.show_gloss() is True, "缺省开"
        settings.save(show_gloss_on=False)
        assert settings.show_gloss() is False

    def test_settings_switch_reflects_stored(self, overlay, tmp_path, monkeypatch):
        import app.settings as settings
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"show_gloss": False}), encoding="utf-8")
        monkeypatch.setattr(settings, "_CONFIG", str(cfg))
        overlay._load_settings()
        assert not overlay.glossSwitch.isChecked()

    def test_new_result_hides_gloss_when_off(self, overlay, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "show_gloss", lambda: False)
        overlay.show(FOREIGN)
        assert all(card.gloss is None for card in overlay.cards), "关掉后新结果无灰字行"
        overlay.cards[0].clicked.emit()
        overlay.on_fill.assert_called_once_with(FOREIGN["candidates"][0]), "填入不受影响"

    def test_show_cached_respects_setting(self, overlay, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "show_gloss", lambda: False)
        overlay.show_cached(FOREIGN)  # 回放路径也遵守
        assert all(card.gloss is None for card in overlay.cards)

    def test_reroll_replace_respects_setting(self, overlay, monkeypatch):
        import app.settings as settings
        monkeypatch.setattr(settings, "show_gloss", lambda: False)
        overlay.show(FOREIGN)
        overlay.replace_card(1, "Neu", "新意思")
        assert overlay.cards[1].gloss is None, "换一条同样遵守显示开关"
        assert overlay.cands[1] == "Neu", "正文/缓存数据不受影响"
