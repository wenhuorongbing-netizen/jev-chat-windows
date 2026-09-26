# -*- coding: utf-8 -*-
"""动效系统（app/motion.py）：常量上限、时长/曲线/属性纪律、stagger 延迟、fill_success 还原、pulse 生命周期。"""
from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtCore import QAbstractAnimation, QEasingCurve
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel, QWidget

from app import motion

app = QApplication.instance() or QApplication([])


def test_constants_caps():
    assert motion.INSTANT == 90
    assert motion.FAST == 140
    assert motion.MED == 160
    assert motion.FAST <= 160 and motion.MED <= 160, "入场动画不得超过 160ms（硬规则）"
    assert motion.PULSE == 900
    assert motion.STAGGER == 40


def test_fade_in_params():
    w = QWidget()
    ani = motion.fade_in(w, duration=motion.FAST)
    assert ani.duration() == motion.FAST
    assert ani.easingCurve().type() == QEasingCurve.OutCubic, "入场 OutCubic"
    assert ani.startValue() == 0.0 and ani.endValue() == 1.0
    assert isinstance(w.graphicsEffect(), QGraphicsOpacityEffect)
    motion.stop_all(w)


def test_expand_in_params_and_restore():
    w = QLabel("一行\n两行")
    ani = motion.expand_in(w, duration=motion.FAST)
    assert ani.propertyName() == b"maximumHeight"
    assert ani.duration() == motion.FAST
    assert ani.easingCurve().type() == QEasingCurve.OutCubic
    assert ani.endValue() == w.sizeHint().height()
    ani.setCurrentTime(motion.FAST)  # 直接推到结束，触发 finished → 还原 maximumHeight
    app.processEvents()
    assert w.maximumHeight() == 16777215, "展开结束后必须还原 maximumHeight，否则布局被锁死"


def test_stagger_in_schedules_three_delayed_tasks():
    delays = []
    with mock.patch.object(motion.QTimer, "singleShot",
                           side_effect=lambda ms, fn: delays.append(ms)):
        motion.stagger_in([QWidget(), QWidget(), QWidget()])
    assert delays == [0, motion.STAGGER, motion.STAGGER * 2]


def test_fill_success_marks_number_immediately():
    label = QLabel("1")
    card = SimpleNamespace(num=label, accent=True)
    motion.fill_success(card)
    assert label.text() == "✓"
    assert "4F5BD5" in label.styleSheet().upper(), "✓ 用 ACCENT 色"


def test_fill_success_restores_after_interval():
    label = QLabel("2")
    card = SimpleNamespace(num=label, accent=False)
    motion.fill_success(card, restore_ms=0)  # 注入 0ms 时钟
    assert label.text() == "✓"
    app.processEvents()  # singleShot(0) 触发还原
    assert label.text() == "2"


def test_pulse_breathes_and_stops_clean():
    w = QWidget()
    ani = motion.pulse(w)
    assert ani.loopCount() == -1, "呼吸循环"
    assert ani.duration() == motion.PULSE
    assert ani.easingCurve().type() == QEasingCurve.InOutSine
    assert w.graphicsEffect() is not None
    motion.stop_pulse(w)
    assert ani.state() != QAbstractAnimation.Running
    assert w.graphicsEffect() is None, "stop 时销毁 effect"
