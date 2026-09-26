# -*- coding: utf-8 -*-
"""offscreen 下构造 Overlay，喂假结果，断言回复卡结构、双语显示规则和填入/复制行为。
on_fill 一律 mock，绝不触发真实填入；Docker 也 mock，不碰 Win32 钩子。"""
from unittest import mock

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QMouseEvent
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel

from app.overlay import Overlay

app = QApplication.instance() or QApplication([])

FOREIGN = {
    "candidates": ["Wie geht es dir?", "Alles klar, danke!", "Bis spaeter!"],
    "glosses": ["你最近怎么样", "都挺好，谢谢", "回头见"],
    "best_index": 0,
    "scores": [0.9, 0.5, 0.2],
    "translation": "今晚一起吃饭吗",
    "lang": "德语",
    "analysis": "对方在随口寒暄，语气轻松，顺着接就行",
}

CHINESE = {
    "candidates": ["好的，马上来", "没问题", "收到"],
    "glosses": ["好的，马上来", "没问题", "收到"],  # 和正文一模一样，必须被过滤
    "best_index": 0,
    "scores": [0.7, 0.5, 0.3],
    "translation": "今晚一起吃饭吗",  # 中文对话不该出现译文行
    "lang": "中文",
    "analysis": "",
}


@pytest.fixture()
def overlay():
    with mock.patch("app.dock.Docker", mock.MagicMock()):
        ov = Overlay(on_fill=mock.MagicMock(), on_generate=mock.MagicMock(),
                     result_of=lambda chat: None)
    yield ov
    ov.win.close()
    ov.win.deleteLater()


def _label_texts(widget):
    return [label.text() for label in widget.findChildren(QLabel)]


def test_cards_have_no_buttons_and_no_recommend_words(overlay):
    overlay.show(FOREIGN)
    assert len(overlay.cards) == 3
    for card in overlay.cards:
        assert card.findChildren(QAbstractButton) == [], "卡内不得有按钮（发送观感的图标就是按钮带来的）"
        for text in _label_texts(card):
            assert "推荐" not in text and "备选" not in text
            assert "%" not in text, "不再显示分数百分比"


def test_cards_have_small_numbers_and_hidden_alt_hint(overlay):
    overlay.show(FOREIGN)
    for position, card in enumerate(overlay.cards, start=1):
        assert card.num.text() == str(position)
        assert not card.altHint.isVisible(), "Alt+N 默认隐藏，悬停才显示"


def test_recommended_card_uses_accent_background(overlay):
    overlay.show(FOREIGN)
    assert overlay.cards[0].accent is True
    assert overlay.cards[1].accent is False
    assert overlay.cards[2].accent is False


def test_foreign_shows_gloss_and_translation(overlay):
    overlay.show(FOREIGN)
    for card, gloss in zip(overlay.cards, FOREIGN["glosses"]):
        assert card.gloss is not None
        assert card.gloss.text() == gloss
    assert overlay.summary.isVisible()
    assert overlay.summary.text() == FOREIGN["translation"]


def test_chinese_hides_gloss_and_translation(overlay):
    overlay.show(CHINESE)
    for card in overlay.cards:
        assert card.gloss is None, "中文对话 / gloss 与正文重复时不创建灰字行"
    assert not overlay.summary.isVisible(), "中文对话不出现译文行"


def test_click_card_fills_foreign_original_only(overlay):
    overlay.show(FOREIGN)
    overlay.cards[0].clicked.emit()
    overlay.on_fill.assert_called_once_with(FOREIGN["candidates"][0])
    filled = overlay.on_fill.call_args[0][0]
    assert filled not in FOREIGN["glosses"], "填入只填外语原文，绝不填中文意思"


def test_right_click_copies_candidate_to_clipboard(overlay):
    overlay.show(FOREIGN)
    menu = overlay.cards[1]._build_menu()
    assert menu.actions()[0].text() == "复制本条"
    menu.actions()[0].trigger()
    assert app.clipboard().text() == FOREIGN["candidates"][1]


def test_analysis_line_below_cards_and_toggleable(overlay):
    overlay.show(FOREIGN)
    assert overlay.analysis.isVisible()
    assert overlay.analysis.toolTip() == FOREIGN["analysis"]
    assert not overlay.analysis.wordWrap(), "默认单行省略"
    press = QMouseEvent(QEvent.MouseButtonPress, QPointF(3, 3), QPointF(100, 100),
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    overlay.analysis.mousePressEvent(press)
    assert overlay.analysis.wordWrap(), "点一下展开全文"
    overlay.analysis.mousePressEvent(press)
    assert not overlay.analysis.wordWrap(), "再点一下收回去"


def test_empty_analysis_stays_hidden(overlay):
    overlay.show(CHINESE)
    assert not overlay.analysis.isVisible()


def test_busy_shows_three_skeleton_cards_with_pulse(overlay):
    overlay.set_busy(True)
    assert len(overlay.skeletons) == 3, "busy 且无候选时插入 3 张骨架卡"
    for skeleton in overlay.skeletons:
        assert skeleton.graphicsEffect() is not None, "骨架卡要有 pulse 呼吸效果"
    assert not overlay.empty.isVisible(), "骨架屏顶替空态文案"
    overlay.set_busy(False)
    assert overlay.skeletons == []
    assert overlay.empty.isVisible()


def test_skeletons_leave_no_live_refs(overlay):
    import weakref
    from PySide6.QtCore import QCoreApplication, QEvent
    overlay.set_busy(True)
    refs = [weakref.ref(sk) for sk in overlay.skeletons]
    overlay.set_busy(False)  # 内部停动画、清强引用、deleteLater
    # deleteLater 的销毁走 DeferredDelete 事件，processEvents 不冲刷它，要显式送一次
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert all(ref() is None for ref in refs), "骨架销毁后不得存活控件/动画引用"


def test_fill_success_marks_card_and_shows_pill(overlay):
    overlay.show(FOREIGN)
    overlay.cards[0].clicked.emit()
    assert overlay.cards[0].num.text() == "✓", "填入成功的卡序号变 ✓"
    assert overlay.toast.isVisible()
    assert overlay.toast.text() == "已填入，确认后自己发送"


def test_copy_shows_pill(overlay):
    overlay.show(FOREIGN)
    overlay._copy(1)
    assert overlay.toast.text() == "已复制，可粘贴修改"
