# -*- coding: utf-8 -*-
"""动效系统：安静的 juiciness。纯 PySide6，offscreen 可测。

纪律（SPEC-round2 §2）：
- 入场 ≤160ms、禁弹簧曲线；入场 OutCubic、离场 InCubic、呼吸 InOutSine。
- 动画只由「用户操作」或「新结果到达」触发；贴靠移动、后台日志刷新不动画。
- 控件禁用/隐藏/销毁时不得有运行中的动画：引用挂在 owner 上，销毁前 stop_all()。
"""
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect
from shiboken6 import isValid

from app.theme import ACCENT, FAINT

INSTANT = 90   # 按压反馈
FAST = 140     # 常规入场/展开
MED = 160      # 上限，禁超
PULSE = 900    # 仅骨架呼吸
STAGGER = 40   # 多卡入场依次延迟

_QWIDGETSIZE_MAX = 16777215


def alive(widget) -> bool:
    """C++ 对象还活着才允许碰：延迟回调（singleShot、restore 定时器）先过这关。"""
    return widget is not None and isValid(widget)


def _track(owner, ani):
    """动画引用挂到 owner：防 GC，也让 stop_all 能找到它。"""
    anims = getattr(owner, "_motion_anims", None)
    if anims is None:
        anims = owner._motion_anims = []
    anims.append(ani)
    return ani


def stop_all(widget):
    """销毁/隐藏前调用：停掉所有跟踪中的动画，pulse 的 effect 一并销毁。"""
    if not alive(widget):
        return
    for ani in getattr(widget, "_motion_anims", []):
        ani.stop()
    widget._motion_anims = []
    if getattr(widget, "_pulse", None) is not None:
        stop_pulse(widget)


def fade_in(widget, duration=FAST, on_finished=None):
    """0→1 淡入（QGraphicsOpacityEffect）。复用已有 effect，重复触发不叠层。"""
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
    ani = QPropertyAnimation(effect, b"opacity", widget)
    ani.setDuration(duration)
    ani.setStartValue(0.0)
    ani.setEndValue(1.0)
    ani.setEasingCurve(QEasingCurve.OutCubic)
    if on_finished:
        ani.finished.connect(on_finished)
    _track(widget, ani)
    ani.start()
    return ani


def fade_out(widget, duration=200, on_finished=None):
    """1→0 淡出（离场 InCubic）。成功 pill 收尾用。"""
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
    ani = QPropertyAnimation(effect, b"opacity", widget)
    ani.setDuration(duration)
    ani.setStartValue(effect.opacity())
    ani.setEndValue(0.0)
    ani.setEasingCurve(QEasingCurve.InCubic)
    if on_finished:
        ani.finished.connect(on_finished)
    _track(widget, ani)
    ani.start()
    return ani


def expand_in(widget, duration=FAST, on_finished=None):
    """maximumHeight 0→sizeHint 高度，结束后还原 QWIDGETSIZE_MAX（别把布局锁死）。
    与 fade_in 同播形成「浮出」。调用时机：首帧布局完成之后（sizeHint 才有效）。"""
    target = widget.sizeHint().height()
    if target <= 0:
        target = max(widget.height(), 1)
    ani = QPropertyAnimation(widget, b"maximumHeight", widget)
    ani.setDuration(duration)
    ani.setStartValue(0)
    ani.setEndValue(target)
    ani.setEasingCurve(QEasingCurve.OutCubic)

    def _restore():
        if alive(widget):
            widget.setMaximumHeight(_QWIDGETSIZE_MAX)
        if on_finished:
            on_finished()

    ani.finished.connect(_restore)
    widget.setMaximumHeight(0)
    _track(widget, ani)
    ani.start()
    return ani


def collapse_out(widget, duration=FAST, on_finished=None):
    """expand_in 的反向：当前高度 →0（InCubic），结束后 hide 并还原 maximumHeight。"""
    start = max(widget.height(), 1)
    widget.setMaximumHeight(start)
    ani = QPropertyAnimation(widget, b"maximumHeight", widget)
    ani.setDuration(duration)
    ani.setStartValue(start)
    ani.setEndValue(0)
    ani.setEasingCurve(QEasingCurve.InCubic)

    def _done():
        if alive(widget):
            widget.hide()
            widget.setMaximumHeight(_QWIDGETSIZE_MAX)
        if on_finished:
            on_finished()

    ani.finished.connect(_done)
    _track(widget, ani)
    ani.start()
    return ani


def _float_in(widget, duration):
    if not alive(widget) or not widget.isVisible():
        return  # 禁用/隐藏/已销毁的控件不得启动动画
    fade_in(widget, duration)
    expand_in(widget, duration)


def stagger_in(widgets, duration=FAST):
    """每张卡 fade+expand，依次延迟 STAGGER ms。
    用 QTimer.singleShot 做延迟（不用 startDelay），避免动画排队期间布局被锁在 0 高。"""
    for i, widget in enumerate(widgets):
        QTimer.singleShot(i * STAGGER, lambda widget=widget: _float_in(widget, duration))


def press_flash(card, duration=INSTANT):
    """卡片按压反馈：把 CardWidget 自带的 backgroundColor 动画（QPropertyAnimation 是
    QVariantAnimation 的子类，驱动 BackgroundColorObject 的 pyqtProperty(QColor)）调成
    90ms OutCubic。按下/松开由 BackgroundAnimationWidget 的 isPressed/isHover 状态机驱动，禁弹簧。"""
    card.backgroundColorAni.setDuration(duration)
    card.backgroundColorAni.setEasingCurve(QEasingCurve.OutCubic)
    return card.backgroundColorAni


def fill_success(card, restore_ms=1100):
    """填入成功：该卡序号变 ACCENT 色 ✓，restore_ms 后还原序号与颜色。"""
    num = card.num
    original = num.text()
    num.setText("✓")
    num.setStyleSheet(f"color: {ACCENT}; background: transparent;")

    def _restore():
        if not alive(num):
            return
        num.setText(original)
        num.setStyleSheet(f"color: {ACCENT if card.accent else FAINT}; background: transparent;")

    QTimer.singleShot(restore_ms, _restore)


def pulse(widget):
    """骨架呼吸：opacity 0.45↔0.75 循环（InOutSine）。stop_pulse 时销毁 effect。"""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    ani = QPropertyAnimation(effect, b"opacity", widget)
    ani.setDuration(PULSE)
    ani.setLoopCount(-1)
    ani.setStartValue(0.45)
    ani.setKeyValueAt(0.5, 0.75)
    ani.setEndValue(0.45)
    ani.setEasingCurve(QEasingCurve.InOutSine)
    widget._pulse = ani
    _track(widget, ani)
    ani.start()
    return ani


def stop_pulse(widget):
    """停呼吸并销毁 effect；骨架移除前必须走这里（隐藏控件不得有运行中动画）。"""
    ani = getattr(widget, "_pulse", None)
    if ani is not None:
        ani.stop()
        widget._pulse = None
    effect = widget.graphicsEffect()
    if isinstance(effect, QGraphicsOpacityEffect):
        effect.setOpacity(1.0)
        widget.setGraphicsEffect(None)
