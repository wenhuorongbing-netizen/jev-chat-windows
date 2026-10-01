# -*- coding: utf-8 -*-
"""浅色置顶回复助手：回复建议和独立设置页。发送始终由用户确认。"""
import os
import re
import sys
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import ctypes
import ctypes.wintypes

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizeGrip, QSizePolicy,
    QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    Action, BodyLabel, CardWidget, CheckBox, ComboBox, DropDownPushButton, EditableComboBox,
    FluentIcon as FIF, HyperlinkButton, IndeterminateProgressBar, LineEdit, PasswordLineEdit,
    PlainTextEdit, PrimaryPushButton, PushButton, RoundMenu, ScrollArea, SpinBox, SwitchButton,
    Theme, TransparentToolButton, setCustomStyleSheet, setFont, setTheme, setThemeColor,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu

from app import motion, settings
from app.qol import STALE_DAYS, fit_rect, fmt_remaining, is_stale, pause_due
from app.reply_rules import shown_gloss, shown_translation
from app.theme import (ACCENT, ACCENT_HOVER, ACCENT_PRESS, ACCENT_SOFT, AUX, BODY, CANVAS,
                       CARD_GAP, CARD_PAD_X, CARD_PAD_Y, DANGER, DANGER_SOFT, FAINT, HAIRLINE,
                       HAIRLINE_STRONG, HOVER, INK, PRESS, QQ, R_CARD, R_PANEL, SKELETON, SUB,
                       SURFACE, TINY, TITLE, WARN, WECHAT, WHATSAPP)
from app.version import VERSION
from core import jev_client, keygate, llm, providers
from core.fill_guard import CopyOnly

_LOG_LINES = 300
# 全局热键 id → VK：1..3 = Alt+数字填卡（老规矩），4 = Alt+J 显隐面板，5 = Alt+G 立即生成
_HOTKEY_VK = ((1, 0x31), (2, 0x32), (3, 0x33), (4, 0x4A), (5, 0x47))
_GENERATE_TIP = "立即生成回复（不等对方新消息）"  # ↻ 闲态
_CANCEL_TIP = "取消这次生成"  # ✕ 忙态
_RELATIONSHIPS = [
    ("自动判断", "auto"), ("恋人", "romantic partners"), ("朋友", "friends"), ("同事", "colleagues"),
    ("家人", "family"), ("自定义", None),
]
# 面板上每个会话单独选的关系（没有「自定义」：要写长描述去设置页改默认）
_CHAT_RELATIONSHIPS = [
    ("自动", ""), ("恋人", "romantic partners"), ("朋友", "friends"), ("同事", "colleagues"),
    ("家人", "family"), ("群友", "group chat with acquaintances"), ("客户", "a client / customer I serve"),
]




def _mp_banner_path() -> str:
    """打包后在 _MEIPASS/docs，源码跑在仓库 docs/。"""
    root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "docs", "wechat-mp.png")


class _MpBanner(QLabel):
    """公众号长条横幅，宽度跟着设置页走，高度按原图比例。"""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._src = QPixmap(path)
        self._shown = 0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        if self._src.isNull() or w <= 0 or self._src.width() <= 0:
            return 0
        return max(1, round(w * self._src.height() / self._src.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        if w <= 0 or w == self._shown or self._src.isNull():
            return
        h = self.heightForWidth(w)
        self._shown = w
        self.setPixmap(self._src.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        if self.height() != h:
            self.setFixedHeight(h)


class _ChatComboMenu(ComboBoxMenu):
    """会话下拉菜单：每次弹出重建（基类行为），建 action 时按行号问一次颜色（陈旧会话置灰）。
    color_of(row) -> QColor | None；None 用默认色。"""

    def __init__(self, parent, color_of):
        super().__init__(parent)
        self._color_of = color_of

    def addAction(self, action):
        super().addAction(action)
        if self._color_of is None:
            return
        color = self._color_of(self.view.count() - 1)
        if color is not None:
            self.view.item(self.view.count() - 1).setForeground(color)  # 委托走 QStyledItemDelegate，认 ForegroundRole


class _FitCombo(ComboBox):
    """长名字不撑开窄布局。按钮上按当前宽度省略；条目仍是全文，findText 靠它。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self._color_of = None  # (row) -> QColor | None，Overlay 设置；None = 不着色
        self.on_context_menu = None  # 会话框才设：右键「把这个会话移出列表」
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _createComboMenu(self):
        return _ChatComboMenu(self, self._color_of)

    def _build_context_menu(self):
        menu = RoundMenu(parent=self)
        action = Action("把这个会话移出列表", self)
        action.triggered.connect(lambda: self.on_context_menu and self.on_context_menu())
        menu.addAction(action)
        return menu

    def contextMenuEvent(self, event):
        if self.on_context_menu:
            self._build_context_menu().exec(event.globalPos())
        else:
            super().contextMenuEvent(event)

    def setText(self, text):
        self._full = text or ""
        QPushButton.setText(self, self._elide(self._full))
        if self._full and self.text() != self._full:
            self.setToolTip(self._full)

    def minimumSizeHint(self):
        hint = QPushButton.minimumSizeHint(self)
        return QSize(48, hint.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        shown = self._elide(self._full)
        if shown != self.text():
            QPushButton.setText(self, shown)
        if self._full and shown != self._full:
            self.setToolTip(self._full)

    def _elide(self, text):
        # 右侧箭头大约 28px。还没排上版时先按一个窄宽度省略，避免最小宽度被整句名字撑开。
        avail = self.width() - 36 if self.width() > 64 else 120
        text = re.sub(r"^(QQ|WhatsApp) · ", "", text)  # 来源已经画在左边的小标里，按钮上不重复
        return self.fontMetrics().elidedText(text, Qt.ElideRight, max(24, avail))


def _label(text="", size=14, color=None, bold=False, parent=None):
    label = BodyLabel(text, parent)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    setFont(label, size, QFont.DemiBold if bold else QFont.Normal)
    if color:
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(label, qss, qss)
    return label


def _tool(icon, title, callback, parent=None):
    button = TransparentToolButton(icon, parent)
    button.setFixedSize(28, 28)  # 统一 hit area，图标视觉 16px
    button.setIconSize(QSize(16, 16))
    button.setToolTip(title)
    button.setAccessibleName(title)
    button.clicked.connect(callback)
    return button


class _Surface(CardWidget):
    def __init__(self, parent=None, accent=False, tone="normal"):
        self.accent = accent
        self.tone = tone  # "normal" 白卡 / "danger" 出错卡（DANGER_SOFT 底）
        super().__init__(parent)
        self.setBorderRadius(R_CARD)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_tone(self, tone):
        """切换底色语义后立刻重算背景色（走 CardWidget 自己的动画管线，不用 QSS 硬盖）。"""
        self.tone = tone
        self._updateBackgroundColor()

    def _normalBackgroundColor(self):
        if self.tone == "danger":
            return QColor(DANGER_SOFT)
        return QColor(ACCENT_SOFT if self.accent else SURFACE)

    def _hoverBackgroundColor(self):
        return self._normalBackgroundColor()

    def _pressedBackgroundColor(self):
        return self._normalBackgroundColor()


class _GlossLabel(BodyLabel):
    """回复卡的中文意思行：可选中手抄（TextSelectableByMouse）。
    只是点一下（没拖出选区）算点卡填入——转发给卡片；拖出选区就是选文字，不填。"""

    def __init__(self, text, card):
        super().__init__(card)  # qfluentwidgets 的 overload 在子类里对 (text, parent) 分发有误，分开设
        self.setText(text)
        self._card = card
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        setFont(self, AUX, QFont.Normal)
        qss = f"BodyLabel {{ color: {SUB}; background: transparent; }}"
        setCustomStyleSheet(self, qss, qss)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self.hasSelectedText():
            self._card.clicked.emit()  # 没选字 = 点卡填入（铁律不变）
            return
        super().mouseReleaseEvent(event)


class _InsightCard(_Surface):
    """「对方说」卡：右键按各行内容给「复制原文 / 复制译文」（空白行不出现对应项）。
    复制是纯剪贴板行为，跟填入无关。"""

    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def _build_menu(self):
        owner = self._owner
        menu = RoundMenu(parent=self)
        original = owner.latest.toolTip() or owner.latest.text()  # tooltip 是全文（省略前的）
        if original:
            action = Action("复制原文", self)
            action.triggered.connect(lambda: owner._copy_text(original, "已复制"))
            menu.addAction(action)
        translation = owner.summary.text() if owner.summary.isVisible() else ""
        if translation:
            action = Action("复制译文", self)
            action.triggered.connect(lambda: owner._copy_text(translation, "已复制"))
            menu.addAction(action)
        return menu

    def contextMenuEvent(self, event):
        menu = self._build_menu()
        if menu.actions():
            menu.exec(event.globalPos())


class _Switch(SwitchButton):
    """标题栏采集开关：左键照常开/关，右键弹「暂停 30 分钟」。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_pause_30 = None

    def _build_menu(self):
        menu = RoundMenu(parent=self)
        action = Action("暂停 30 分钟", self)
        action.triggered.connect(lambda: self.on_pause_30 and self.on_pause_30())
        menu.addAction(action)
        return menu

    def contextMenuEvent(self, event):
        self._build_menu().exec(event.globalPos())


class _Feed(PlainTextEdit):
    """聊天记录框的右键菜单：复制所选 / 全部复制 / 清空显示（只清界面，不动 feeds 存档）。
    复制进剪贴板即完成，不给 toast——主动操作，不需要反馈。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_clear_display = None

    def _build_menu(self):
        menu = RoundMenu(parent=self)
        copy_sel = Action("复制所选", self)
        copy_sel.setEnabled(self.textCursor().hasSelection())
        copy_sel.triggered.connect(self.copy)
        menu.addAction(copy_sel)
        copy_all = Action("全部复制", self)
        copy_all.triggered.connect(lambda: QApplication.clipboard().setText(self.toPlainText()))
        menu.addAction(copy_all)
        clear = Action("清空显示", self)
        clear.triggered.connect(lambda: self.on_clear_display and self.on_clear_display())
        menu.addAction(clear)
        return menu

    def contextMenuEvent(self, event):
        self._build_menu().exec(event.globalPos())


class _Fetched(QObject):
    """取模型列表的后台线程 → 主线程：哪一组（SimpleNamespace）、取回来的模型 id、失败原因（成功是空串）。
    Qt 不让跨线程碰控件，信号是跨线程唯一干净的路。"""
    done = Signal(object, list, str)


class _TitleBar(QWidget):
    """只有标题栏可拖动，选择正文或按按钮不会意外移动窗口。"""
    def __init__(self, parent):
        super().__init__(parent)
        self._drag = None
        self.on_drag = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.window().pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag)
            if self.on_drag:  # 用户自己拖了 = 不想贴着聊天窗口了
                self.on_drag()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)


class _MainWindow(QWidget):
    """窗口大小变了就叫 Overlay 重新排布；断点没跨过时 _relayout 自己不做事，这里不用防抖。
    顺带接全局热键（WM_HOTKEY 发到这个窗口）、move/resize 落盘回调和 Esc 隐藏。"""
    def __init__(self, relayout):
        super().__init__()
        self._relayout = relayout
        self.on_hotkey = None
        self.on_geometry = None  # move/resize 之后调（500ms 防抖在 Overlay 那边）
        self.on_escape = None  # Esc：子控件不处理就轮到窗口，隐藏面板

    def nativeEvent(self, event_type, message):
        if event_type == b"windows_generic_MSG" and self.on_hotkey:
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312:  # WM_HOTKEY
                self.on_hotkey(int(msg.wParam))
                return True, 0
        return super().nativeEvent(event_type, message)

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.on_geometry:
            self.on_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout(event.size().width(), event.size().height())
        if self.on_geometry:
            self.on_geometry()

    def keyPressEvent(self, event):
        # 输入框（设置页 LineEdit 等）优先处理 Esc；没人要才隐藏面板
        if event.key() == Qt.Key_Escape and self.on_escape:
            self.on_escape()
        else:
            super().keyPressEvent(event)


class _ReplyCard(_Surface):
    """一条候选。整卡点击 = 填入（也可以 Alt+1/2/3，提示只在鼠标悬停时显示），右键 = 复制。
    卡内没有任何按钮：发送永远由用户自己在聊天窗口里确认，界面上不出现发送观感的图标。
    推荐不写「推荐」二字：ACCENT_SOFT 底色 + ACCENT 色序号。双语时正文下面一行灰色中文意思，
    只给自己看，填入不带它。"""

    def __init__(self, owner, index, recommended=False, number=1):
        super().__init__(accent=recommended)
        self._owner = owner
        self._index = index
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点一下填入输入框，右键复制")
        motion.press_flash(self)  # 按压/松开的颜色过渡：90ms OutCubic，禁弹簧
        box = QVBoxLayout(self)
        self.box = box
        box.setContentsMargins(CARD_PAD_X, CARD_PAD_Y, CARD_PAD_X, CARD_PAD_Y)
        box.setSpacing(4)
        top = QHBoxLayout()
        self.num = _label(str(number), TINY, ACCENT if recommended else FAINT, True)
        self.num.setAttribute(Qt.WA_TransparentForMouseEvents)
        top.addWidget(self.num)
        top.addStretch(1)
        self.altHint = _label(f"Alt+{number}", TINY, FAINT)
        self.altHint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.altHint.hide()  # 只在鼠标悬停时显示
        top.addWidget(self.altHint)
        box.addLayout(top)
        self.text = _label(owner.cands[index], BODY, INK)
        self.text.setAttribute(Qt.WA_TransparentForMouseEvents)  # 点字也算点卡片
        box.addWidget(self.text)
        self.gloss = None  # 中文意思：空、和正文重复、或对方说中文时这一行根本不存在
        gloss = owner.glosses[index] if index < len(owner.glosses) else ""
        if gloss:
            self.gloss = _GlossLabel(gloss, self)  # 可选中手抄；没拖出选区的点击仍算点卡填入
            box.addWidget(self.gloss)
        self.clicked.connect(lambda: owner._fill(index))

    def _hoverBackgroundColor(self):
        return QColor(ACCENT_HOVER if self.accent else HOVER)

    def _pressedBackgroundColor(self):
        return QColor(ACCENT_PRESS if self.accent else PRESS)

    def _disabledBackgroundColor(self):
        return QColor(CANVAS)  # 置灰：推荐底色也一起褪掉，一眼看出现在不能点

    def enterEvent(self, event):
        self.altHint.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.altHint.hide()
        super().leaveEvent(event)

    def _build_menu(self):
        """右键菜单：复制本条、换一条（重 roll；忙 / 非 current 时禁用）、复制中文意思（有 gloss 才有）。
        复制都是剪贴板行为，跟填入无关。"""
        menu = RoundMenu(parent=self)
        copy_action = Action("复制本条", self)
        copy_action.triggered.connect(lambda: self._owner._copy(self._index))
        menu.addAction(copy_action)
        reroll_action = Action("换一条", self)
        reroll_action.setEnabled(self._owner._current and not self._owner._busy)
        reroll_action.triggered.connect(lambda: self._owner._reroll(self._index))
        menu.addAction(reroll_action)
        if self.gloss is not None:
            gloss_action = Action("复制中文意思", self)
            gloss_action.triggered.connect(
                lambda: self._owner._copy_text(self.gloss.text(), "已复制中文意思"))
            menu.addAction(gloss_action)
        return menu

    def contextMenuEvent(self, event):
        self._build_menu().exec(event.globalPos())  # 右键 = 菜单（复制 / 换一条）

    def set_available(self, enabled):
        self.setEnabled(enabled)  # 禁用态 = 置灰不可点（浏览别的会话、生成中）
        self.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)

    def set_compact(self, compact):
        pass  # 只有一套紧凑样式了


class _ElideLine(BodyLabel):
    """一行/一段文字的省略-展开：默认 N 行省略，点一下在「省略 / 全文」间切换；tooltip 放全文。
    lines=1：单行右省略（分析行，总能点开）。lines=2：两行省略（「对方说」原文），
    内容不超过两行时无交互、无省略号。"""

    def __init__(self, parent=None, lines=1):
        super().__init__(parent)
        self._lines = lines
        self.setTextFormat(Qt.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setCursor(Qt.PointingHandCursor)
        setFont(self, AUX, QFont.Normal)
        qss = f"BodyLabel {{ color: {SUB}; background: transparent; }}"
        setCustomStyleSheet(self, qss, qss)
        self._full = ""
        self._expanded = False

    def set_full(self, text):
        self._full = text or ""
        self._expanded = False
        self.setToolTip(self._full)
        self._render()

    def _avail(self):
        return self.width() if self.width() > 24 else 240

    def _fits(self, text):
        """全文在 N 行内摆得下吗（摆得下就不省略、不响应点击）。"""
        if self._lines == 1:
            return self.fontMetrics().horizontalAdvance(text) <= self._avail()
        rect = self.fontMetrics().boundingRect(QRect(0, 0, self._avail(), 100000),
                                               Qt.TextWordWrap, text)
        return rect.height() <= self.fontMetrics().lineSpacing() * self._lines

    def _elide_lines(self, text):
        """按当前宽度把长文截到 N 行以内并补省略号（二分最长前缀）。"""
        fm = self.fontMetrics()
        limit = fm.lineSpacing() * self._lines
        width = self._avail()
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            rect = fm.boundingRect(QRect(0, 0, width, 100000), Qt.TextWordWrap, text[:mid] + "…")
            if rect.height() <= limit:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"

    def _render(self):
        if self._expanded or (self._lines > 1 and self._fits(self._full)):
            self.setWordWrap(True)
            self.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX
            BodyLabel.setText(self, self._full)
        elif self._lines == 1:
            self.setWordWrap(False)
            self.setMaximumHeight(16777215)
            BodyLabel.setText(self, self.fontMetrics().elidedText(self._full, Qt.ElideRight,
                                                                  self._avail()))
        else:
            self.setWordWrap(True)
            self.setMaximumHeight(self.fontMetrics().lineSpacing() * self._lines + 4)
            BodyLabel.setText(self, self._elide_lines(self._full))

    def mousePressEvent(self, event):
        if (event.button() == Qt.LeftButton and self._full
                and (self._lines == 1 or not self._fits(self._full))):
            self._expanded = not self._expanded
            self._render()
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._expanded and self._full:
            self._render()  # 宽度变了重新省略


class _Toast(QLabel):
    """窗口底部居中的轻提示浮层：120ms 淡入 → 停留 1600ms → 200ms 淡出。
    WA_TransparentForMouseEvents：不抢焦点、不吃鼠标；居中放置，不挡右下角 SizeGrip。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setTextFormat(Qt.PlainText)
        setFont(self, AUX)
        ink = QColor(INK)
        self.setStyleSheet(
            f"QLabel {{ background: rgba({ink.red()}, {ink.green()}, {ink.blue()}, 230); "
            f"color: {SURFACE}; border-radius: {R_CARD}px; padding: 8px 16px; }}")
        self.hide()
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._dismiss)

    def show_text(self, text, hold_ms=1600):
        motion.stop_all(self)  # 上一次还没淡出就被再次触发：先停干净，避免叠动画
        self._hold.stop()
        self.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 44)
        self.show()
        self.raise_()
        motion.fade_in(self, duration=120)
        self._hold.start(hold_ms)

    def _dismiss(self):
        motion.fade_out(self, duration=200, on_finished=self.hide)


class Overlay:
    def __init__(self, on_fill, on_toggle_capture=None, on_target_change=None, result_of=None,
                 on_toggle_debug=None, on_generate=None, on_cancel=None, on_reroll=None):
        """result_of(会话名) → 那个会话上次的结果或 None；切着看别的会话时用它把旧结果放回来。
        on_target_change(会话名, 人名) → 用户在群里挑了回复对象。
        on_toggle_debug(开不开) → 开关调试视图那个独立窗口。
        on_generate(会话名) → 不等对方新消息，按现有记录马上生成；on_cancel() → 取消这次生成。
        on_reroll(下标) → 单卡重 roll「换一条」。"""
        self.app = QApplication.instance() or QApplication([])
        setTheme(Theme.LIGHT)
        setThemeColor(ACCENT, save=False)
        self.on_fill = on_fill
        self.on_toggle_capture = on_toggle_capture
        self.on_target_change = on_target_change
        self.on_toggle_debug = on_toggle_debug
        self.on_generate = on_generate  # on_generate(会话名)：不等对方新消息，按现有记录马上生成
        self.on_cancel = on_cancel  # on_cancel()：取消正在跑的这次生成（忙态 ↻ 变 ✕）
        self.on_reroll = on_reroll  # on_reroll(候选下标)：单卡重 roll「换一条」
        self.result_of = result_of
        self.cands = []
        self.glosses = []  # 双语模式：每条候选的中文对照，跟 cands 同索引（已过 shown_gloss 过滤）
        self.cards = []
        self.skeletons = []  # 生成中的骨架卡：一条候选都没有时顶替空态文案
        self._busy = False
        self._current = False
        self._compact = None  # 断点模式：None 保证 _relayout 第一次调用必定生效
        self._pageLayouts = []
        self._hintLabels = []
        self.feeds = {}  # {会话名: [排好版的记录]}
        self.counts = {}  # {会话名: 消息条数}
        self.hers = {}  # {会话名: 对方最近一句}
        self.targets = {}  # {会话名: ([发言人], 当前回复对象)}
        self._chatTs = {}  # {会话名: 最后活跃 epoch 秒}：内存即写，盘延后（_tsTimer 防抖 5s）
        self._tsDirty = set()  # 待落盘的会话名
        self.unread = {}  # {会话名: 未读条数}：没正在看的会话来消息才 +1，切过去清零，封顶 99
        self._chat = ""  # 聊天 App 当前开着的会话
        self._shown = ""  # 界面上正在看的会话（浏览时和上面不一样）
        self.docked = settings.dock()  # 贴着当前聊天窗口；用户拖动标题栏就解除
        self._chat_hwnd = None  # 最近一个在前台的聊天窗口
        self._hotkeys = False  # Alt+1/2/3 现在注册着没有
        self._order = []  # 界面上从上往下每张卡对应的候选下标，热键按它找
        self.on_foreground = None  # on_foreground(根窗口)：main.py 决定这个窗口算不算聊天 App
        self.win = _MainWindow(self._relayout)
        self.win.setObjectName("assistantWindow")
        self.win.setWindowTitle("JevChat-Windows")
        # 不再永远置顶：贴在聊天窗口旁边，聊天 App 到前台时 raise_above() 把自己一起带上来
        self.win.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.win.setAttribute(Qt.WA_ShowWithoutActivating, True)  # 任何 show 都不抢焦点（托盘/Alt+J 也是）
        self._clock = time.time  # 暂停倒计时的时间源（测试注入假时钟）
        self._pauseUntil = None  # 暂停 30 分钟的恢复时刻；None = 没在暂停
        self._pauseTimer = QTimer(self.win)
        self._pauseTimer.setInterval(60_000)  # 倒计时每分钟刷一次，不秒刷
        self._pauseTimer.timeout.connect(self._pause_tick)
        self._geomTimer = QTimer(self.win)
        self._geomTimer.setSingleShot(True)
        self._geomTimer.setInterval(500)  # 位置/宽度落盘防抖
        self._geomTimer.timeout.connect(self._save_geometry)
        self._tsTimer = QTimer(self.win)
        self._tsTimer.setSingleShot(True)
        self._tsTimer.setInterval(5000)  # ts 落盘防抖 5s：内存即写、盘延后
        self._tsTimer.timeout.connect(self._flush_chat_ts)
        self.win.on_geometry = self._geometry_changed
        self.win.on_escape = self._hide_panel
        self.win.setStyleSheet(
            f"QWidget#assistantWindow {{ background: {CANVAS}; border: 1px solid {HAIRLINE_STRONG}; "
            f"border-radius: {R_PANEL}px; }}"
        )
        self.win.setMinimumWidth(320)
        self.win.setMaximumWidth(640)
        outer = QVBoxLayout(self.win)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        header = _TitleBar(self.win)
        header.on_drag = lambda: self.set_docked(False)
        title = QHBoxLayout(header)
        title.setContentsMargins(12, 8, 8, 8)
        title.setSpacing(4)
        self.appBadge = _label("", AUX, SURFACE, True)  # 「微信 / QQ / WhatsApp」小标，跟着会话变
        self.appBadge.setFixedHeight(20)  # 标题栏胶囊统一高度 20
        self.appBadge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.appBadge.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(self.appBadge)
        self.chatBox = _FitCombo()
        self.chatBox.setPlaceholderText("还没识别到会话")
        self.chatBox.setAccessibleName("当前会话")
        self.chatBox.setToolTip("聊天窗口切到哪个会话这里就跟到哪个；也可以自己选一个，只看它的记录和建议")
        self.chatBox.currentIndexChanged.connect(self._on_chat_selected)
        self.chatBox._color_of = self._chat_item_color  # 弹出菜单时按行着色：陈旧会话置灰
        self.chatBox.on_context_menu = self._remove_current_chat  # 右键：把这个会话移出列表
        title.addWidget(self.chatBox, 1)
        # 「关系」收进标题栏胶囊：平时只占一个小胶囊的位置，点开才看到 7 个选项
        self.relPill = DropDownPushButton("自动", header)
        self.relPill.setMinimumWidth(0)
        self.relPill.setMaximumWidth(64)  # 360 宽的标题栏，胶囊不能挤到别人
        self.relPill.setFixedHeight(20)  # 与 App 徽标统一高度
        setFont(self.relPill, AUX)
        self.relPill.setToolTip("这个会话按什么关系来写回复，点一下换；会一直记着")
        self.relPill.setAccessibleName("这个会话的关系")
        qss = (f"DropDownPushButton {{ background: {SURFACE}; border: 1px solid {HAIRLINE}; "
               f"border-radius: {R_CARD}px; color: {SUB}; padding: 2px 8px; }} "
               f"DropDownPushButton:hover {{ background: {HOVER}; }}")
        setCustomStyleSheet(self.relPill, qss, qss)
        self._relMenu = RoundMenu(parent=self.relPill)
        self._relActions = []
        relGroup = QActionGroup(self.relPill)
        relGroup.setExclusive(True)
        for i, (name, _value) in enumerate(_CHAT_RELATIONSHIPS):
            action = Action(name, self._relMenu)
            action.setCheckable(True)
            action.triggered.connect(lambda *_, i=i: self._on_rel_selected(i))
            relGroup.addAction(action)
            self._relMenu.addAction(action)
            self._relActions.append(action)
        self._relActions[0].setChecked(True)
        self.relPill.setMenu(self._relMenu)
        title.addWidget(self.relPill)
        # 静音按钮只表示「当前正在看的会话」：静音 = 只记录，不自动生成（手动 ↻ 不受限）
        self.muteButton = _tool(FIF.RINGER, "静音这个会话：只记录，不自动生成", self._toggle_mute, header)
        self.muteButton.setAccessibleName("静音这个会话")
        title.addWidget(self.muteButton)
        self.pinButton = _tool(FIF.PIN, "贴靠聊天窗口（拖动标题栏会解除）", self._toggle_dock, header)
        title.addWidget(self.pinButton)
        self.captureSwitch = _Switch(header)
        self.captureSwitch.setOnText("")
        self.captureSwitch.setOffText("")
        self.captureSwitch.setToolTip("开启或暂停采集（右键：暂停 30 分钟）")
        self.captureSwitch.setAccessibleName("开启或暂停采集")
        self.captureSwitch.setChecked(True)
        self.captureSwitch.checkedChanged.connect(self._capture_toggled)
        self.captureSwitch.on_pause_30 = self.pause_capture_30
        title.addWidget(self.captureSwitch)
        self.settingsButton = _tool(FIF.SETTING, "设置", self.open_settings, header)
        title.addWidget(self.settingsButton)
        title.addWidget(_tool(FIF.CLOSE, "关闭助手", self.win.close, header))
        outer.addWidget(header)
        self.subtitle = QLabel()  # 旧布局的副标题，紧凑模式开关还会碰它；不显示
        self.subtitle.hide()
        self.updateBar = QWidget(self.win)
        update_row = QHBoxLayout(self.updateBar)
        update_row.setContentsMargins(16, 4, 8, 4)
        update_row.setSpacing(8)
        self.updateLabel = _label("", AUX, ACCENT, True)
        update_row.addWidget(self.updateLabel, 1)
        self.updateLink = HyperlinkButton("", "去下载", self.updateBar)
        self.updateLink.setFixedHeight(24)
        update_row.addWidget(self.updateLink)
        closeUpdate = TransparentToolButton(FIF.CLOSE, self.updateBar)
        closeUpdate.setFixedSize(20, 20)
        closeUpdate.setToolTip("关闭更新提示")
        closeUpdate.setAccessibleName("关闭更新提示")
        closeUpdate.clicked.connect(lambda: self.updateBar.hide())
        update_row.addWidget(closeUpdate)
        self.updateBar.setFixedHeight(32)
        self.updateBar.hide()
        outer.addWidget(self.updateBar)
        self.pages = QStackedWidget(self.win)
        outer.addWidget(self.pages, 1)
        self._build_home()
        self._build_settings()
        self.toast = _Toast(self.win)  # 成功 pill：填入/复制的底部浮层轻反馈
        self._build_tray()  # 系统托盘：左键显隐，右键菜单；不可用就静默降级，全走标题栏
        footer = QHBoxLayout()
        footer.setContentsMargins(16, 4, 4, 4)
        footer.addWidget(_label("只填入输入框，发送由你确认", TINY, FAINT), 1)
        grip = QSizeGrip(self.win)
        grip.setFixedSize(16, 16)
        footer.addWidget(grip, 0, Qt.AlignBottom)
        outer.addLayout(footer)
        screen = self.app.primaryScreen().availableGeometry()
        self.win.setMinimumHeight(min(360, screen.height() - 32))
        self.win.resize(min(360, screen.width() - 32), min(760, screen.height() - 48))
        self.win.move(screen.right() - self.win.width() - 20, screen.top() + 24)
        if not self.docked:
            self._restore_geometry()  # 贴靠的话 dock.py 会摆，别抢
        self._relayout(self.win.width(), self.win.height())  # resizeEvent 补不到构造时这一次
        self.set_status("等待新消息" if settings.has_llm_key() else "需要配置模型",
                        "idle" if settings.has_llm_key() else "warning")
        self._paint_pin()
        self.win.show()
        from app.dock import Docker

        dpr = lambda: self.win.devicePixelRatioF() or 1.0  # noqa: E731
        self.win.on_hotkey = self._on_hotkey
        self.docker = Docker(self._hwnd(), self._width_px, lambda: int(480 * dpr()),
                             on_foreground=lambda h: self.on_foreground and self.on_foreground(h))

    def _scroll_page(self):
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("pageContent")
        content.setStyleSheet("QWidget#pageContent { background: transparent; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 8, 16, 12)
        layout.setSpacing(12)
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        self._pageLayouts.append(layout)
        return scroll, layout

    def _relayout(self, w, h):
        """宽度跨过断点才重新摆布局（省事）。高度不用管：feed 用 stretch 弹性填充剩余空间。"""
        compact = w < 300
        if compact != self._compact:
            self._compact = compact
            self._apply_compact(compact)

    def _apply_compact(self, compact):
        """紧凑/常规两套间距和可见性；断点没变时不会被调用。"""
        for label in self._hintLabels:
            label.setVisible(not compact)
        self._sync_model_fields()
        margins = (8, 4, 8, 8) if compact else (16, 8, 16, 12)
        for layout in self._pageLayouts:
            layout.setContentsMargins(*margins)
        for card in self.cards:
            card.set_compact(compact)

    def _build_home(self):
        self.home, body = self._scroll_page()
        body.setSpacing(8)
        # 状态行：状态文字（只在 生成中/警告/出错 时显示，成功提示 3 秒隐去）· 浏览中 · 立即生成
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.status = _label("", AUX, SUB)
        self.status.hide()
        self._statusTimer = QTimer(self.win)
        self._statusTimer.setSingleShot(True)
        self._statusTimer.timeout.connect(self.status.hide)
        status_row.addWidget(self.status, 1)
        self.chatFollow = _label("浏览中", AUX, SUB)
        self.chatFollow.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.chatFollow.hide()  # 只在「浏览中」时显示，「跟随」不显示
        status_row.addWidget(self.chatFollow)
        status_row.addStretch(1)  # 状态文字隐藏时把 ↻ 顶到最右，别悬在中间
        self.generateButton = _tool(FIF.SYNC, _GENERATE_TIP, self._generate_or_cancel)
        self.generateButton.setFixedSize(28, 28)
        status_row.addWidget(self.generateButton)
        body.addLayout(status_row)
        self.progress = IndeterminateProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.hide()
        body.addWidget(self.progress)

        self.targetRow = QWidget()  # 只有开了「群聊指定回复对象」且这个会话是群聊才露出来
        target_row = QHBoxLayout(self.targetRow)
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(8)
        target_row.addWidget(_label("回复对象", AUX, SUB))
        self.targetBox = _FitCombo()
        self.targetBox.setAccessibleName("回复对象")
        self.targetBox.setToolTip("三条候选都按这个人来写；不选就跟着最近说话的那位")
        self.targetBox.currentIndexChanged.connect(self._on_target_selected)
        target_row.addWidget(self.targetBox, 1)
        self.targetRow.hide()
        body.addWidget(self.targetRow)

        # 对方说的：原文（灰，最多 2 行）+ 译文（黑粗体，双语且外语时才有）；Jev 模式下这张卡还放判断摘要
        self.insight = _InsightCard(self)
        insight_box = QVBoxLayout(self.insight)
        insight_box.setContentsMargins(CARD_PAD_X, CARD_PAD_Y, CARD_PAD_X, CARD_PAD_Y)
        insight_box.setSpacing(4)
        row = QHBoxLayout()
        self.insightTitle = _label("对方说", TINY, FAINT, True)  # FAINT 小字，靠字重出层级
        row.addWidget(self.insightTitle, 1)
        insight_box.addLayout(row)
        self.latest = _ElideLine(lines=2)  # 对方原文：灰，默认 2 行省略，点击展开/收起
        insight_box.addWidget(self.latest)
        self.summary = _label("", BODY, INK, True)  # 译文 / 判断建议
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        insight_box.addWidget(self.summary)
        self.insight.hide()
        body.addWidget(self.insight)
        self.context = self.insight  # 旧代码里「对方最近说」那块现在并进这张卡

        self.empty = _Surface()
        empty_box = QVBoxLayout(self.empty)
        empty_box.setContentsMargins(16, 16, 16, 16)
        empty_box.setSpacing(8)
        self.emptyTitle = _label("等待对方的新消息", TINY, SUB, True)  # 居中构成：TINY 灰标题 + 一行说明 + 唯一动作
        self.emptyTitle.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyTitle)
        self.emptyHint = _label("", AUX, SUB)
        self.emptyHint.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyHint)
        # 一句人话 + 一个动作：缺 key → 去设置；其它出错 → 重试。按场景二选一
        self.setupButton = PrimaryPushButton("去设置")
        self.setupButton.clicked.connect(self.open_settings)
        self.setupButton.setVisible(not settings.has_llm_key())
        empty_box.addWidget(self.setupButton, 0, Qt.AlignHCenter)
        self.retryButton = PushButton("重试")
        self.retryButton.clicked.connect(
            lambda: self.on_generate and self._shown and self.on_generate(self._shown))
        self.retryButton.hide()
        empty_box.addWidget(self.retryButton, 0, Qt.AlignHCenter)
        body.addWidget(self.empty)
        self._empty_text()

        self.replyBox = QVBoxLayout()
        self.replyBox.setSpacing(CARD_GAP)
        body.addLayout(self.replyBox)
        self.analysis = _ElideLine()  # 一句话分析：回复卡列表下方，单行省略，点开看全文
        self.analysis.hide()
        body.addWidget(self.analysis)
        self.referenceNote = QLabel()  # 旧布局的提示条，不再显示
        self.referenceNote.hide()

        self.historyButton = PushButton(FIF.HISTORY, "聊天记录")
        self.historyButton.setFixedHeight(32)
        self.historyButton.clicked.connect(self._toggle_history)
        self.historyButton.setAccessibleName("展开或收起聊天记录")
        body.addWidget(self.historyButton)
        self.feed = _Feed()
        self.feed.on_clear_display = self.feed.clear  # 「清空显示」只清界面，feeds 存档不动
        self.feed.setReadOnly(True)
        self.feed.setPlaceholderText("识别到的聊天内容会显示在这里")
        self.feed.setMaximumBlockCount(_LOG_LINES)
        self.feed.hide()
        body.addWidget(self.feed, 1)  # 展开时弹性填充剩余空间；隐藏时不占位
        self._history_title()
        body.addStretch(1)  # feed 隐藏时靠它把内容上顶

    # ------------------------------------------------------------ 贴靠
    def _paint_pin(self):
        self.pinButton.setIcon(FIF.PIN if self.docked else FIF.UNPIN)
        self.pinButton.setToolTip("已贴靠聊天窗口（点一下解除；拖动标题栏也会解除）" if self.docked
                                  else "点一下贴靠到当前聊天窗口旁边")

    def _toggle_dock(self):
        self.set_docked(not self.docked)

    def set_docked(self, on):
        if on == self.docked:
            return
        self.docked = on
        settings.try_save(dock_on=on)
        self._paint_pin()
        self.docker.set_enabled(on)
        if on and self._chat_hwnd:
            self.docker.attach(self._chat_hwnd)
        if not on:
            self._restore_geometry()  # 解除贴靠：回到上次未贴靠的位置（拖动中会被后续拖动立刻覆盖）

    def _geometry_changed(self):
        """moveEvent/resizeEvent 都到这儿：500ms 防抖，别每拖一个像素就写一次盘。"""
        self._geomTimer.start()

    def _save_geometry(self):
        """只有未贴靠的位置和宽度才值得记（贴靠的位置由聊天窗口决定）。"""
        if self.docked:
            return
        p = self.win.pos()
        settings.save_win_state(p.x(), p.y(), self.win.width())

    def _restore_geometry(self):
        """未贴靠：恢复上次的位置和宽度，并钳到各屏 availableGeometry 并集内；没存过就不动。"""
        if self.docked:
            return
        pos, width = settings.win_pos(), settings.win_width()
        if width:
            w = min(max(width, self.win.minimumWidth()), self.win.maximumWidth())
            self.win.resize(w, self.win.height())
        if pos:
            screens = [tuple(s.availableGeometry().getRect()) for s in self.app.screens()]
            x, y, w, h = fit_rect((pos[0], pos[1], self.win.width(), self.win.height()), screens)
            self.win.setGeometry(x, y, w, h)

    def _hwnd(self):
        return int(self.win.winId())

    def _width_px(self):
        """贴靠时的物理宽度：用户用右下角拖柄改过宽度就沿用，没改过就是 360 逻辑像素。"""
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(self._hwnd(), ctypes.byref(r))
        return max(r.right - r.left, int(300 * (self.win.devicePixelRatioF() or 1.0)))

    def enable_hotkeys(self, on):
        """Alt+1/2/3 填卡、Alt+J 显隐、Alt+G 立即生成。全局热键会把这些组合键从别的程序手里拿走，
        所以只在聊天 App（或助手自己）在前台时注册，切到别的程序就还回去。"""
        if on == self._hotkeys:
            return
        self._hotkeys = on
        u32 = ctypes.windll.user32
        for hotkey_id, vk in _HOTKEY_VK:
            if on:
                u32.RegisterHotKey(self._hwnd(), hotkey_id, 0x0001 | 0x4000, vk)  # MOD_ALT | MOD_NOREPEAT
            else:
                u32.UnregisterHotKey(self._hwnd(), hotkey_id)

    def _on_hotkey(self, hotkey_id):
        """1..3 → 界面上从上往下第 N 张卡（推荐是第 1 张）；4 = Alt+J 显隐；5 = Alt+G 立即生成。"""
        if hotkey_id == 4:
            self.toggle_panel()
            return
        if hotkey_id == 5:  # 跟 ↻ 按钮是同一个回调，没有新行为分支
            if self.on_generate and self._shown:
                self.on_generate(self._shown)
            return
        position = hotkey_id - 1
        if 0 <= position < len(self._order):
            self._fill(self._order[position])

    def toggle_panel(self):
        """Alt+J / 托盘左键：显示 ↔ 隐藏。显示只 show + raise_，不 activateWindow——
        全局热键只在聊天 App（或助手自己）在前台时才注册，弹出来不该把焦点从聊天窗口抢走。"""
        if self.win.isVisible():
            self.win.hide()
        else:
            self.win.show()
            self.win.raise_()

    def _hide_panel(self):
        """Esc：只隐藏，不切换（已经藏着就什么都不做）。"""
        self.win.hide()

    def _build_tray(self):
        """系统托盘：图标用 FIF.CHAT 生成（实现最干净）。不可用（极少数环境）静默降级，功能全走标题栏。
        任何时候都不调 showMessage——禁止气泡通知。"""
        self.tray = None
        self.trayMenu = self._build_tray_menu()
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(FIF.CHAT.icon(), self.win)
        self.tray.setToolTip("JevChat")
        self.tray.setContextMenu(self.trayMenu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _build_tray_menu(self):
        """菜单总是建（托盘不可用时只是不挂上）：显示/隐藏、采集（跟标题栏开关双向同步）、暂停 30 分钟、退出。"""
        menu = QMenu(self.win)
        self.trayToggleAction = menu.addAction("隐藏面板")
        self.trayToggleAction.triggered.connect(self.toggle_panel)
        self.trayCaptureAction = menu.addAction("采集")
        self.trayCaptureAction.setCheckable(True)
        self.trayCaptureAction.setChecked(self.captureSwitch.isChecked())
        self.trayCaptureAction.toggled.connect(self.captureSwitch.setChecked)  # 托盘 → 开关
        self.captureSwitch.checkedChanged.connect(self.trayCaptureAction.setChecked)  # 开关 → 托盘
        pause = menu.addAction("暂停 30 分钟")
        pause.triggered.connect(self.pause_capture_30)
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.win.close)  # 与标题栏关闭同一清理路径
        menu.aboutToShow.connect(self._sync_tray_menu)
        return menu

    def _sync_tray_menu(self):
        """每次弹出前对齐：显隐文案跟当前状态，采集勾选跟标题栏开关。"""
        self.trayToggleAction.setText("隐藏面板" if self.win.isVisible() else "显示面板")
        self.trayCaptureAction.setChecked(self.captureSwitch.isChecked())

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # 左键单击 = 显示/隐藏面板
            self.toggle_panel()

    def attach(self, hwnd):
        """当前聊天窗口换了：记下来；开着贴靠就贴过去（层级 + 位置都跟它走，见 app/dock.py）。"""
        self._chat_hwnd = hwnd
        if self.docked:
            self.docker.attach(hwnd)

    def _paint_badge(self, title):
        """标题栏左边的小标：会话来自哪个 App。"""
        if title.startswith("QQ · "):
            text, color = "QQ", QQ
        elif title.startswith("WhatsApp · "):
            text, color = "WA", WHATSAPP
        elif title:
            text, color = "微信", WECHAT
        else:
            self.appBadge.hide()
            return
        self.appBadge.setText(text)
        qss = (f"BodyLabel {{ color: {SURFACE}; background: {color}; border-radius: {R_CARD}px; "
               f"padding: 2px 8px; }}")
        setCustomStyleSheet(self.appBadge, qss, qss)
        self.appBadge.show()

    def _build_settings(self):
        self.settingsPage, body = self._scroll_page()
        heading = QHBoxLayout()
        heading.addWidget(_tool(FIF.RETURN, "返回回复建议", self._back_home))
        heading.addWidget(_label("设置", TITLE, INK, True), 1)
        body.addLayout(heading)
        body.addWidget(_label("调整关系背景，配置判断和起草用的两个模型。", AUX, SUB))
        preference = _Surface()
        box = QVBoxLayout(preference)
        box.setContentsMargins(16, 16, 16, 16)
        box.setSpacing(12)
        box.addWidget(_label("回复偏好", BODY, INK, True))
        relation_label = _label("默认关系", AUX)
        box.addWidget(relation_label)
        self.relationshipBox = ComboBox()
        self.relationshipBox.setMinimumWidth(0)
        self.relationshipBox.addItems([name for name, value in _RELATIONSHIPS])
        self.relationshipBox.setAccessibleName("你们的关系")
        relation_label.setBuddy(self.relationshipBox)
        box.addWidget(self.relationshipBox)
        self.relEdit = LineEdit()
        self.relEdit.setPlaceholderText("例如：刚认识的朋友，正在慢慢熟悉")
        self.relEdit.setAccessibleName("自定义关系背景")
        box.addWidget(self.relEdit)
        self.relationshipBox.currentIndexChanged.connect(
            lambda index: self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        )
        box.addWidget(self._hint("没单独设过的会话都用它。建议「自动判断」；某个会话不准，就在面板上的「关系」里单独选，会一直记着。"))
        style_label = _label("说话风格（可选）", AUX)
        box.addWidget(style_label)
        self.styleEdit = LineEdit()
        self.styleEdit.setPlaceholderText("例如：话少、不用标点、偶尔用 doge、不说客套话")
        self.styleEdit.setAccessibleName("说话风格")
        style_label.setBuddy(self.styleEdit)
        box.addWidget(self.styleEdit)
        box.addWidget(self._hint("候选本来就照着你最近发的消息模仿；这里可以再补一句你自己的口吻。"))
        context_label = _label("参考上下文", AUX)
        box.addWidget(context_label)
        self.contextBox = SpinBox()
        self.contextBox.setRange(3, 100)
        self.contextBox.setAccessibleName("参考的最近消息条数")
        context_label.setBuddy(self.contextBox)
        box.addWidget(self.contextBox)
        box.addWidget(self._hint(
            "生成时看最近这么多条消息。太少看不懂在聊什么，建议 20–40；群聊可以再多些。"
        ))
        target_row = QHBoxLayout()
        target_row.addWidget(_label("群聊指定回复对象", AUX), 1)
        self.targetSwitch = SwitchButton()
        self.targetSwitch.setOnText("开")
        self.targetSwitch.setOffText("关")
        self.targetSwitch.setAccessibleName("群聊指定回复对象")
        target_row.addWidget(self.targetSwitch)
        box.addLayout(target_row)
        box.addWidget(self._hint(
            "开了以后群聊里可以选回复给谁，候选会针对 TA 写，填入时可带 @。关了就正常回复。"
        ))
        box.addWidget(self._hint(
            "回复跟随对方的语言：对方说中文就用中文回；说德语、英语等外语就翻成中文给你看，"
            "3 条回复用对方的语言写并附中文意思，填入只填外语。"
        ))
        gloss_row = QHBoxLayout()
        gloss_row.addWidget(_label("回复卡上显示中文意思", AUX), 1)
        self.glossSwitch = SwitchButton()
        self.glossSwitch.setOnText("开")
        self.glossSwitch.setOffText("关")
        self.glossSwitch.setAccessibleName("回复卡上显示中文意思")
        gloss_row.addWidget(self.glossSwitch)
        box.addLayout(gloss_row)
        box.addWidget(self._hint(
            "外语回复下面的那行中文对照。关掉只是不显示：填入照样只填外语，换一条和缓存都不受影响。"
        ))
        images_row = QHBoxLayout()
        images_row.addWidget(_label("识别对方发来的图片", AUX), 1)
        self.imagesSwitch = SwitchButton()
        self.imagesSwitch.setOnText("开")
        self.imagesSwitch.setOffText("关")
        self.imagesSwitch.setAccessibleName("识别对方发来的图片")
        images_row.addWidget(self.imagesSwitch)
        box.addLayout(images_row)
        box.addWidget(self._hint(
            "默认关。打开后，只有对方的最新一条本身是图片时，才截窗口里那块缩小后发给起草模型看；"
            "不读任何本地文件，不存盘。关着则图片只算「[图片]」。"
        ))
        update_row = QHBoxLayout()
        update_row.addWidget(_label("启动时检查更新", AUX), 1)
        self.updateSwitch = SwitchButton()
        self.updateSwitch.setOnText("开")
        self.updateSwitch.setOffText("关")
        self.updateSwitch.setAccessibleName("启动时检查更新")
        update_row.addWidget(self.updateSwitch)
        box.addLayout(update_row)
        box.addWidget(self._hint(
            "只向 GitHub 查最新版本号，不发送任何数据。国内访问 GitHub 慢的话关掉也行。"
        ))
        debug_row = QHBoxLayout()
        debug_row.addWidget(_label("调试视图", AUX), 1)
        self.debugSwitch = SwitchButton()
        self.debugSwitch.setOnText("开")
        self.debugSwitch.setOffText("关")
        self.debugSwitch.setAccessibleName("调试视图")
        self.debugSwitch.checkedChanged.connect(self._debug_toggled)  # 这个开关立刻生效，不等「保存设置」
        debug_row.addWidget(self.debugSwitch)
        box.addLayout(debug_row)
        box.addWidget(self._hint(
            "另开一个窗口实时显示截到的画面和识别框：绿 = 我、蓝 = 对方、灰 = 过滤掉的灰字、"
            "红 = 当成图片丢掉、黄 = 小字丢掉。只在内存里画，不存图。"
        ))
        body.addWidget(preference)

        models = _Surface()
        box = QVBoxLayout(models)
        box.setContentsMargins(16, 16, 16, 16)
        box.setSpacing(12)
        box.addWidget(_label("模型", BODY, INK, True))
        self._fetched = _Fetched()
        self._fetched.done.connect(self._models_fetched)
        self.draft = self._model_group(box, "起草 · 语言模型", "draft", providers.DRAFT_PROVIDERS)
        box.addWidget(self._hint(
            "写那三条候选。OpenAI / Anthropic / Gemini 三种接口都走各自官方 SDK。"
            "默认 DeepSeek 官网直连，国内最快。"
        ))
        think_row = QHBoxLayout()
        think_row.addWidget(_label("起草时开启思考模式", AUX), 1)
        self.thinkingSwitch = SwitchButton()
        self.thinkingSwitch.setOnText("开")
        self.thinkingSwitch.setOffText("关")
        self.thinkingSwitch.setAccessibleName("起草时开启思考模式")
        think_row.addWidget(self.thinkingSwitch)
        box.addLayout(think_row)
        box.addWidget(self._hint(
            "关：秒回，够用。开：模型先想再写，更斟酌但慢好几倍、贵一些。"
            "只有 " + " / ".join(providers.THINKING) + " 认这个开关。"
        ))
        body.addWidget(models)
        self.settingsFeedback = _label("", AUX, ACCENT)
        self.settingsFeedback.hide()
        body.addWidget(self.settingsFeedback)
        actions = QHBoxLayout()
        back = PushButton("返回")
        back.clicked.connect(self._back_home)
        actions.addWidget(back)
        actions.addStretch(1)
        self.saveButton = PrimaryPushButton("保存设置")
        self.saveButton.clicked.connect(self._save)
        actions.addWidget(self.saveButton)
        body.addLayout(actions)
        body.addWidget(self._hint("保存后用于下一次生成的回复。"))
        banner = _mp_banner_path()
        if os.path.exists(banner):
            body.addWidget(_MpBanner(banner))
        body.addStretch(1)
        self._load_settings()

    def _hint(self, text):
        """设置页字段下面的灰字说明：记下来，紧凑模式一起隐藏。"""
        label = _label(text, AUX, SUB)
        self._hintLabels.append(label)
        return label

    def _model_group(self, box, title, kind, table):
        """一组「来源 / 密钥 / 模型」控件（S4 起只剩起草一份）。table 是 core/providers.py 里那张表。"""
        group = SimpleNamespace(kind=kind, table=table, ids=list(table),
                                keyTitle="起草",
                                # 存的 key 只算给「界面上现在选的这个接口」用的：绑定在别的接口上就当没配，
                                # 「获取模型」也就不会把它发给刚选的新来源
                                stored_key=lambda k=kind: settings.key_for_route(providers.LLM_ENV, self._route_of(k)))
        heading = QHBoxLayout()
        heading.addWidget(_label(title, BODY, INK, True), 1)
        group.keyState = _label("", AUX, ACCENT)
        group.keyState.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading.addWidget(group.keyState)
        box.addLayout(heading)
        source_label = _label("来源", AUX)
        box.addWidget(source_label)
        group.providerBox = ComboBox()
        group.providerBox.setMinimumWidth(0)  # 选项文字长短不一，别让它撑开设置页
        group.providerBox.addItems([table[i].name for i in group.ids])
        group.providerBox.setAccessibleName(f"{title} 来源")
        source_label.setBuddy(group.providerBox)
        box.addWidget(group.providerBox)
        if kind == "draft":  # 只有两个「自定义」来源要自己填地址，别的来源这一行藏着
            self.baseLabel = _label("Base URL", AUX)
            box.addWidget(self.baseLabel)
            self.baseEdit = LineEdit()
            self.baseEdit.setPlaceholderText("https://你的服务/v1")
            self.baseEdit.setAccessibleName("自定义来源 Base URL")
            self.baseLabel.setBuddy(self.baseEdit)
            box.addWidget(self.baseEdit)
        key_label = _label("密钥", AUX)
        box.addWidget(key_label)
        group.keyEdit = PasswordLineEdit()
        group.keyEdit.setAccessibleName(f"{title} API 密钥")
        key_label.setBuddy(group.keyEdit)
        group.keyEdit.returnPressed.connect(self._save)
        box.addWidget(group.keyEdit)
        box.addWidget(self._hint("上面选哪家就填哪家的 key；换来源重填一次，只存这一把。"))
        model_label = _label("模型", AUX)
        box.addWidget(model_label)
        row = QHBoxLayout()
        row.setSpacing(8)
        group.modelBox = EditableComboBox()  # 能选也能手打，接口新出的模型不用等我改代码
        group.modelBox.setMinimumWidth(0)
        group.modelBox.setAccessibleName(f"{title} 模型")
        model_label.setBuddy(group.modelBox)
        row.addWidget(group.modelBox, 1)
        group.fetchButton = PushButton("获取模型")
        group.fetchButton.setAccessibleName(f"获取{title}的可用模型列表")
        group.fetchButton.clicked.connect(lambda: self._fetch_models(group))
        row.addWidget(group.fetchButton)
        box.addLayout(row)
        group.status = _label("", AUX, SUB)
        box.addWidget(group.status)
        group.providerBox.currentIndexChanged.connect(lambda _: self._provider_changed(group))
        return group

    @staticmethod
    def _provider_of(group):
        return group.ids[max(0, group.providerBox.currentIndex())]

    def _route_of(self, kind):
        """界面上现在选的接口会把 key 发往哪里（跟 settings 里绑定的同一种 origin 口径）。"""
        return providers.draft_route(self._provider_of(self.draft), self.baseEdit.text().strip())

    def _provider_changed(self, group):
        """换来源：模型框回到这家该有的值（存的就是这家才用存的，否则用它的默认），状态清掉。"""
        provider = self._provider_of(group)
        saved = settings.draft_provider()
        stored = settings.draft_model()
        group.modelBox.clear()
        group.modelBox.setText(stored if provider == saved else group.table[provider].default)
        group.status.setText("")
        self._sync_model_fields()

    def _sync_model_fields(self):
        """密钥已配置/未配置、占位文案、自定义 Base URL 行的显隐，
        外加紧凑模式下把来源按钮上的文字省略——ComboBox 是 QPushButton，
        minimumSizeHint 按整段文字算，不会自动换行/省略，长名字会把设置页撑宽。"""
        group = self.draft
        provider = self._provider_of(group)
        name = group.table[provider].name
        configured = bool(group.stored_key())
        group.keyState.setText("已配置" if configured else "未配置")
        group.keyEdit.setPlaceholderText(
            "已配置，留空保留" if configured else f"输入 {name} API 密钥")
        if self._compact:
            name = group.providerBox.fontMetrics().elidedText(name, Qt.ElideRight, 180)
        group.providerBox.setText(name)
        custom = self._provider_of(self.draft) in providers.CUSTOM
        self.baseLabel.setVisible(custom)
        self.baseEdit.setVisible(custom)

    def _fetch_models(self, group):
        """「获取模型」：拿填的 key（没填就拿存的）去问接口，网络调用丢后台线程。"""
        provider = self._provider_of(group)
        custom = group.kind == "draft" and provider in providers.CUSTOM
        base = self.baseEdit.text().strip() if custom else None
        typed = group.keyEdit.text().strip()
        key = typed or group.stored_key()
        if not key:
            group.status.setText("先填密钥")
            return
        if custom and not base:
            group.status.setText("先填 Base URL")
            return
        group.status.setText("获取中…")
        group.fetchButton.setEnabled(False)
        threading.Thread(target=lambda: self._list_models(group, provider, key, base, bool(typed)),
                         daemon=True).start()

    def _list_models(self, group, provider, key, base, typed=False):
        """后台线程：按协议走 llm 列模型；失败把原因一起送回主线程。
        key 连同要发往的接口一起装进 Credential：刚敲的 key 就是给页面上选的这个接口的；存的 key 要过绑定核对。"""
        try:
            spec = providers.DRAFT_PROVIDERS[provider]
            dest = keygate.destination_of(spec.protocol, base or spec.base)
            cred = (keygate.typed_credential(key, dest) if typed
                    else keygate.stored_credential(providers.LLM_ENV, key, dest))
            models = llm.list_models(spec.protocol, base or spec.base, cred, headers=spec.headers)
            if spec.keep:  # 目录里混了别的协议时，只留这条路打得通的
                models = [m for m in models if spec.keep(m)]
            reason = "" if models else "这个来源没返回任何模型"
        except Exception as exc:  # 线程里漏异常会静默吞掉，按钮就永远停在禁用态
            # 只有我们自己写的固定文案能显示；别的异常只报类型，不带任何异常原文
            models, reason = [], (str(exc)[:120] if isinstance(exc, (jev_client.JevError, keygate.KeyRouteError))
                                  else type(exc).__name__)
        self._fetched.done.emit(group, models, reason)

    def _models_fetched(self, group, models, reason):
        """回到主线程：填进下拉框，原来选中的还在列表里就留着。"""
        group.fetchButton.setEnabled(True)
        if not models:
            group.status.setText(reason or "获取失败，检查密钥、网络或 Base URL")
            return
        current = group.modelBox.text().strip()
        group.modelBox.clear()
        group.modelBox.addItems(models)
        if current in models:
            group.modelBox.setCurrentIndex(models.index(current))
        else:
            group.modelBox.setText(current)  # 手打的没在列表里也不清掉
        group.status.setText(f"共 {len(models)} 个")

    def _set_group(self, group, provider, model):
        """把存下来的来源和模型放回一组控件里；填充不算用户操作，别触发换来源的重置。"""
        group.providerBox.blockSignals(True)
        group.providerBox.setCurrentIndex(group.ids.index(provider))
        group.providerBox.blockSignals(False)
        group.keyEdit.clear()
        group.modelBox.clear()
        group.modelBox.setText(model)
        group.status.setText("")

    def _load_settings(self):
        relationship = settings.relationship()
        index = next((i for i, (_, value) in enumerate(_RELATIONSHIPS) if value == relationship),
                     len(_RELATIONSHIPS) - 1)
        self.relationshipBox.setCurrentIndex(index)
        self.relEdit.setText(relationship if _RELATIONSHIPS[index][1] is None else "")
        self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        self.styleEdit.setText(settings.style())
        self.contextBox.setValue(settings.context())
        self.targetSwitch.setChecked(settings.reply_target())
        self.glossSwitch.setChecked(settings.show_gloss())
        self.imagesSwitch.setChecked(settings.read_images())
        self._set_group(self.draft, settings.draft_provider(), settings.draft_model())
        self.baseEdit.setText(settings.draft_base_url())
        self.thinkingSwitch.setChecked(settings.thinking())
        self.updateSwitch.setChecked(settings.check_update())
        self.set_debug_switch(settings.debug_view())  # 屏蔽信号地拨，别在加载时开关一遍窗口
        self._sync_model_fields()  # 上面屏蔽了信号，这里补一次
        self.settingsFeedback.hide()

    def _save(self):
        relationship = _RELATIONSHIPS[self.relationshipBox.currentIndex()][1]
        relationship = relationship or self.relEdit.text().strip()
        draft_provider = self._provider_of(self.draft)
        base = self.baseEdit.text().strip()
        if not relationship:
            self._settings_feedback("请填写关系背景，或选择一个已有选项。", error=True)
            self.relEdit.setFocus()
            return
        if draft_provider in providers.CUSTOM and not base:
            self._settings_feedback("自定义来源要填 Base URL。", error=True)
            self.baseEdit.setFocus()
            return
        group = self.draft
        if not group.keyEdit.text().strip() and not group.stored_key():
            self._settings_feedback(f"请先填写 {group.keyTitle} 的 API 密钥。", error=True)
            group.keyEdit.setFocus()
            return
        if not group.modelBox.text().strip():
            self._settings_feedback(f"{group.table[draft_provider].name} 请先获取并选择一个模型。", error=True)
            group.modelBox.setFocus()
            return
        try:
            settings.save(relationship, self.contextBox.value(),
                          draft_provider_text=draft_provider,
                          llm_key_text=self.draft.keyEdit.text().strip() or None,
                          draft_model_text=self.draft.modelBox.text().strip(),
                          draft_base_url_text=base,
                          reply_target_on=self.targetSwitch.isChecked(),
                          style_text=self.styleEdit.text().strip(),
                          thinking_on=self.thinkingSwitch.isChecked(),
                          check_update_on=self.updateSwitch.isChecked(),
                          read_images_on=self.imagesSwitch.isChecked(),
                          show_gloss_on=self.glossSwitch.isChecked())
        except settings.SecretError:
            self._settings_feedback("密钥没能加密保存（Windows 数据保护不可用），什么都没有保存。", error=True)
            return
        except Exception:
            self._settings_feedback("保存失败，请检查配置文件是否可写后重试。", error=True)
            return
        self._load_settings()
        self._render_targets()  # 开关刚改过，回到首页时这一行该显该藏得重算一次
        self._settings_feedback("设置已保存，将用于下一次回复。")
        self.setupButton.hide()
        if not self.cands and not self._busy:
            self._empty_text()
            self.set_status("设置已就绪，等待新消息", "idle")

    def _debug_toggled(self, on):
        """调试视图独立于「保存设置」：拨一下就开窗/收窗，顺手落盘，重启还在。"""
        settings.try_save(debug_view_on=on)
        if self.on_toggle_debug:
            self.on_toggle_debug(on)

    def set_debug_switch(self, on):
        """调试窗被用户直接关掉时把开关拨回去；屏蔽信号，免得又回调一圈。"""
        self.debugSwitch.blockSignals(True)
        self.debugSwitch.setChecked(on)
        self.debugSwitch.blockSignals(False)

    def _settings_feedback(self, text, error=False):
        color = DANGER if error else ACCENT
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.settingsFeedback, qss, qss)
        self.settingsFeedback.setText(text)
        self.settingsFeedback.show()

    def open_settings(self):
        if self.pages.currentWidget() != self.settingsPage:
            self._load_settings()
        self.pages.setCurrentWidget(self.settingsPage)
        self.settingsButton.setEnabled(False)
        (self.relationshipBox if settings.has_llm_key() else self.draft.keyEdit).setFocus()

    def _back_home(self):
        self.draft.keyEdit.clear()
        self.pages.setCurrentWidget(self.home)
        self.settingsButton.setEnabled(True)

    def _fill(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        try:
            self.on_fill(self.cands[index])
        except CopyOnly:  # 这个 App 只复制不自动填：回复放进剪贴板，人自己粘贴
            self._copy(index)
            return
        except Exception as e:
            # 状态栏保持友好文案；真实原因和压缩堆栈进聊天记录，认得出是哪一步炸的
            import traceback
            self.set_status("未能填入，请确认聊天窗口可用后重试，或复制回复。", "error")
            self.log(f"[填入失败] {type(e).__name__}: {e}")
            self.log(f"[填入失败堆栈] {' '.join(traceback.format_exc().split())[:300]}")
            return
        self.set_status("已填入输入框，确认后自己按发送", "success")
        card = next((c for c in self.cards if c._index == index), None)
        if card is not None:
            motion.fill_success(card)  # 被点的卡序号变 ✓，1.1 秒后还原
        self.toast.show_text("已填入，确认后自己发送")

    def _copy(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        self.app.clipboard().setText(self.cands[index])
        self.set_status("回复已复制，可粘贴并修改。", "success")
        self.toast.show_text("已复制，可粘贴修改")

    def _reroll(self, index):
        """「换一条」入口（卡右键菜单）：守卫与 main 同口径（忙 / 非 current / 越界），然后回调 main。"""
        if self._busy or not self._current or index >= len(self.cands):
            return
        if self.on_reroll:
            self.on_reroll(index)

    def set_card_pending(self, index, pending):
        """重 roll 中的那张卡：禁用 + 呼吸；结束 stop_pulse 并按 _current 恢复可用态。不动其它卡。"""
        card = next((c for c in self.cards if c._index == index), None)
        if card is None:
            return
        if pending:
            card.set_available(False)
            motion.pulse(card)
        else:
            motion.stop_pulse(card)
            card.set_available(self._current and not self._busy)

    def replace_card(self, index, text, gloss):
        """换一条成功：原位重建该卡（cands/glosses 先更新），_order 与 Alt+N 映射不变，新卡淡入。
        gloss 空则这张卡不再有灰字行；填入行为不变——只填正文（外语），gloss 只显示。"""
        if not (0 <= index < len(self.cands)):
            return
        self.cands[index] = text
        while len(self.glosses) < len(self.cands):
            self.glosses.append("")
        self.glosses[index] = gloss if settings.show_gloss() else ""  # 换一条同样遵守显示开关
        position = next((i for i, c in enumerate(self.cards) if c._index == index), None)
        if position is None:
            return
        old = self.cards[position]
        motion.stop_all(old)  # 销毁前停掉呼吸/入场等所有动画
        card = _ReplyCard(self, index, recommended=old.accent, number=position + 1)
        self.replyBox.insertWidget(position, card)
        self.replyBox.removeWidget(old)
        old.hide()
        old.deleteLater()
        self.cards[position] = card
        card.set_available(self._current and not self._busy)
        motion.fade_in(card)

    def _capture_toggled(self, on):
        """用户自己拨的开关：界面先改，再通知父进程去开/停采集。手动操作优先：30 分钟倒计时作废。"""
        self._cancel_pause()
        self._capture_text(on)
        if self.on_toggle_capture:
            self.on_toggle_capture(on)

    def pause_capture_30(self):
        """暂停采集 30 分钟（托盘菜单 / 开关右键）。走正常关采集路径，60s 定时器刷倒计时，
        到点自动恢复。重启应用不记忆（状态全在内存里）。"""
        self.captureSwitch.setChecked(False)  # 会触发 _capture_toggled，先把旧倒计时清干净
        self._pauseUntil = self._clock() + 30 * 60
        self._pauseTimer.start()
        self._pause_tick()

    def _pause_tick(self):
        """到点自动恢复；没到点把剩余时间刷上状态行（warning 常驻，每分钟一次）。"""
        if self._pauseUntil is None:
            self._pauseTimer.stop()
            return
        now = self._clock()
        if pause_due(now, self._pauseUntil):
            self.captureSwitch.setChecked(True)  # 走正常开采集路径（_capture_toggled 会清暂停态）
            self.set_status("已恢复采集", "success")
            return
        self.set_status(f"采集已暂停 · {fmt_remaining(self._pauseUntil - now)} 后恢复", "warning")

    def _cancel_pause(self):
        """手动再开/再关、或应用退出（定时器是 win 的子对象，随之销毁）时取消倒计时。"""
        self._pauseUntil = None
        self._pauseTimer.stop()

    def set_update(self, latest, url):
        """main.py 后台线程查到比当前新的版本才会调这个。只显示版本号和 Release 链接，别的什么都没有。"""
        self.updateLabel.setText(f"有新版本 v{latest}")
        self.updateLink.setUrl(url)
        self.updateBar.show()

    def set_capture(self, on, reason=""):
        """父进程回报的状态：只改界面，不回调（不然和父进程来回打架）。reason 为空用默认说明。"""
        self.captureSwitch.blockSignals(True)
        self.captureSwitch.setChecked(on)
        self.captureSwitch.blockSignals(False)
        self._capture_text(on, reason)

    def _capture_text(self, on, reason=""):
        """开关状态对应的状态行和空态文案。已有的候选不受影响，暂停了照样能填入/复制。"""
        configured = settings.has_llm_key()
        if not on:
            self.set_status(reason or "采集已暂停，聊天内容不再读取", "warning")
        elif configured:
            self.set_status("等待新消息", "idle")
        else:
            self.set_status("请先在设置中配置模型", "warning")
        if self._busy or self.cands:  # 正在生成或已有候选时，空态卡片本来就看不见
            return
        if not on:
            self._empty_tone()
            self.emptyTitle.setText("采集已暂停")
            self.emptyHint.setText("聊天内容暂时不再读取。\n打开标题栏的开关，继续接收新消息。")
            self.setupButton.setVisible(not configured)
            self.retryButton.hide()
        else:
            self._empty_text()

    def _generate_or_cancel(self):
        """↻ 闲态 = 立即生成（现状）；忙态变 ✕ = 取消这次生成。"""
        if self._busy:
            if self.on_cancel:
                self.on_cancel()
            return
        if self.on_generate and self._shown:
            self.on_generate(self._shown)

    def set_busy(self, busy):
        self._busy = busy
        self.progress.setVisible(busy)
        if busy:
            self.generateButton.setIcon(FIF.CLOSE)
            self.generateButton.setToolTip(_CANCEL_TIP)
            self.generateButton.setAccessibleName(_CANCEL_TIP)
            self.invalidate_replies()
            self.progress.start()
            self.set_status("正在根据新消息整理回复…", "busy")
            if not self.cands:
                self.empty.hide()  # 骨架屏顶替空态文案
                self._show_skeletons()
        else:
            self.generateButton.setIcon(FIF.SYNC)
            self.generateButton.setToolTip(_GENERATE_TIP)
            self.generateButton.setAccessibleName(_GENERATE_TIP)
            self.progress.stop()
            self._remove_skeletons()
            if not self.cands:
                self._empty_text()
                self.empty.show()
        for card in self.cards:
            card.set_available(self._current and not busy)

    def _show_skeletons(self):
        """3 张骨架卡（两行/一行/两行，高度模拟真实卡），pulse 呼吸；生成中唯一的动效。"""
        self._remove_skeletons()
        for lines in (2, 1, 2):
            card = _Surface()
            card.setEnabled(False)  # 骨架不可点：禁用态不会触发悬停/按压动画
            box = QVBoxLayout(card)
            box.setContentsMargins(CARD_PAD_X, 12, CARD_PAD_X, 12)
            box.setSpacing(8)
            num = QFrame(card)
            num.setFixedSize(20, 8)
            num.setStyleSheet(f"QFrame {{ background: {SKELETON}; border-radius: 4px; }}")
            box.addWidget(num)
            for width in (272, 168)[:lines]:
                bar = QFrame(card)
                bar.setFixedSize(width, 12)
                bar.setStyleSheet(f"QFrame {{ background: {SKELETON}; border-radius: 4px; }}")
                box.addWidget(bar)
            motion.pulse(card)
            self.replyBox.addWidget(card)
            self.skeletons.append(card)

    def _remove_skeletons(self):
        """先停动画再销毁：隐藏/销毁的控件上不得有运行中的动画。"""
        for card in self.skeletons:
            motion.stop_all(card)
            self.replyBox.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.skeletons = []

    def _empty_tone(self, danger=False):
        """空态卡两种脸：默认白底灰字；出错 DANGER_SOFT 底 + DANGER 字。"""
        self.empty.set_tone("danger" if danger else "normal")
        color = DANGER if danger else SUB
        for label in (self.emptyTitle, self.emptyHint):
            qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
            setCustomStyleSheet(label, qss, qss)

    def _empty_text(self):
        """空态卡片的默认文案，配好没配好两套说法。"""
        self._empty_tone()  # 回到白底灰字（出错态是红底红字）
        configured = settings.has_llm_key()
        self.emptyTitle.setText("等待对方的新消息" if configured else "先设置，再开始")
        self.emptyHint.setText("对方发来新消息会自动生成；也可以点右上角 ⟳ 立即生成。"
                               if configured else "先在设置里填好模型密钥。")
        self.setupButton.setVisible(not configured)
        self.retryButton.hide()

    def invalidate_replies(self):
        self._current = False
        for card in self.cards:
            card.set_available(False)

    def set_status(self, text, kind="idle"):
        """状态行按需显示：生成中/警告/出错 一直显示到下一次变化；成功 3 秒隐去；idle 隐藏。"""
        colors = {"idle": SUB, "busy": ACCENT, "success": ACCENT,
                  "warning": WARN, "error": DANGER}
        markers = {"idle": "●", "busy": "●", "success": "✓", "warning": "!", "error": "!"}
        qss = f"BodyLabel {{ color: {colors.get(kind, SUB)}; background: transparent; }}"
        setCustomStyleSheet(self.status, qss, qss)
        self.status.setText(f"{markers.get(kind, '●')}  {text}")
        self._statusTimer.stop()
        if kind in ("busy", "warning", "error"):
            self.status.show()
        elif kind == "success":
            self.status.show()
            self._statusTimer.start(3000)
        else:
            self.status.hide()
        if kind == "error" and self._busy:
            self.set_busy(False)
        if kind == "error" and not self.cands:
            # 一句人话 + 一个动作：缺 key → 去设置；其它出错 → 重试。出错卡红底红字
            self._empty_tone(danger=True)
            self.emptyTitle.setText("暂时没有可用的回复")
            self.emptyHint.setText("请按上方提示处理。收到新的对方消息后会再次尝试。")
            configured = settings.has_llm_key()
            self.setupButton.setVisible(not configured)
            self.retryButton.setVisible(configured)

    def _toggle_history(self):
        """展开/收起都走 140ms 动画，不再瞬间切换；文案按最终态更新。"""
        motion.stop_all(self.feed)  # 上一次动画没播完就再点：先停干净
        if self.feed.isHidden():
            self.feed.show()
            motion.fade_in(self.feed)
            motion.expand_in(self.feed)
            self._history_title()
        else:
            motion.collapse_out(self.feed, on_finished=self._history_title)

    def _history_title(self):
        action = "展开" if self.feed.isHidden() else "收起"
        count = self.counts.get(self._shown, 0)
        self.historyButton.setText(f"{action}聊天记录" + (f" · {count}" if count else ""))

    def log(self, line):
        """采集状态行：只进正在看的那个会话，不按会话存。"""
        bar = self.feed.verticalScrollBar()
        follow = self.feed.isHidden() or bar.value() >= bar.maximum() - 4
        self.feed.appendPlainText(line)
        if follow:
            bar.setValue(bar.maximum())

    def log_message(self, who, text, name="", timestamp=None, chat=None):
        """按会话存一份；只有正在看的那个会往显示区里写。"""
        chat = chat or self._shown
        speaker = (name or "对方") if who == "her" else "我"
        timestamp = timestamp or datetime.now().strftime("%H:%M")
        self.counts[chat] = self.counts.get(chat, 0) + 1
        lines = self.feeds.setdefault(chat, [])
        lines.append(f"{timestamp}  {speaker}\n{text}\n")
        del lines[:-_LOG_LINES]
        if who == "her":
            self.hers[chat] = text
        self._add_chat(chat)
        self._touch_chat(chat)  # 每条消息都算活跃：排最前、落盘防抖、陈旧色刷新
        if chat != self._shown:
            self._bump_unread(chat)  # 没正在看的会话来消息才计未读；面板隐藏不影响（shown 没变）
            return
        self.log(lines[-1])
        if who == "her":
            self._show_latest(text)
        self._history_title()

    def _show_latest(self, text):
        self.latest.set_full(text)  # 2 行省略/点击展开都交给 _ElideLine；tooltip 始终是全文
        if text:
            self.insight.show()

    def _copy_text(self, text, toast):
        """纯剪贴板行为（与填入无关）：复制任意一行 + pill 反馈。"""
        self.app.clipboard().setText(text)
        self.toast.show_text(toast)

    def current_chat(self):
        """界面上正在看的会话（不一定是微信当前开着的那个）。"""
        return self._shown

    def set_chat(self, title):
        """微信切到了哪个会话：登记进下拉框并自动跟过去，不触发用户选择的回调。"""
        if not title:
            return
        browsing = self._shown != self._chat  # 正看着的就是它、但之前是「浏览中」：也得重画，把填入放开
        self._chat = title
        self._add_chat(title)
        self._touch_chat(title)  # 前台切到它 = 活跃：排最前、落盘防抖
        if title != self._shown or browsing:
            self.chatBox.blockSignals(True)
            self.chatBox.setCurrentIndex(self.chatBox.findData(title))
            self.chatBox.blockSignals(False)
            self._switch_to(title)
        self._follow_text()

    def _add_chat(self, title):
        """新会话进下拉框：按最后活跃倒序插到该在的位置（有 ts 的按 ts 倒序，没 ts 的排最后按加入序）。
        item 文本是纯标题，标题原文同时存 userData——查找一律走 findData/itemData，5B 装饰文本才不污染 key。
        第一条会自己选中（基类行为），屏蔽信号别当成用户挑的；插入在当前项前面时基类会自动跟着调序号。"""
        if not title or self.chatBox.findData(title) >= 0:
            return
        ts = self._ts_of(title)
        index = self.chatBox.count()
        if ts:
            for i in range(self.chatBox.count()):
                other = self._ts_of(self.chatBox.itemData(i))
                if not other or other < ts:  # 没 ts 的、或更旧的，排在它后面
                    index = i
                    break
        self.chatBox.blockSignals(True)
        self.chatBox.insertItem(index, title, userData=title)
        if self.chatBox.count() == 1:
            self.chatBox.setCurrentIndex(0)  # addItem 有这行为，insertItem 补上，口径一致
        self.chatBox.blockSignals(False)

    def move_to_top(self, title):
        """活跃会话排到最前：removeItem + insertItem(0)（userData 带上），全程 blockSignals。
        removeItem 换当前项是按序号不按身份的，先记下正在看的，插回去再恢复——选中态不丢。"""
        index = self.chatBox.findData(title)
        if index <= 0:
            return  # 不在列表里（_add_chat 已排好）或已经在最前
        self.chatBox.blockSignals(True)
        current = self.chatBox.itemData(self.chatBox.currentIndex())
        self.chatBox.removeItem(index)
        self.chatBox.insertItem(0, title, userData=title)
        if current is not None:
            self.chatBox.setCurrentIndex(self.chatBox.findData(current))
        self.chatBox.blockSignals(False)

    def _ts_of(self, title):
        """会话的最后活跃时刻：内存为准，没缓存才读一次 chat_meta（settings 每次都读盘，别反复读）。"""
        ts = self._chatTs.get(title)
        if ts is None:
            ts = settings.chat_meta(title).get("ts") or 0
            self._chatTs[title] = ts
        return ts

    def _touch_chat(self, title):
        """活跃时刻更新点（log_message / set_chat 都到这儿）：内存即写、盘延后、排最前、刷新颜色。"""
        if not title:
            return
        self._chatTs[title] = time.time()
        self._tsDirty.add(title)
        self._tsTimer.start()
        self.move_to_top(title)

    def _flush_chat_ts(self):
        """防抖到点：把脏的 ts 落盘（每个会话一条 chat_meta 记录）。"""
        dirty, self._tsDirty = self._tsDirty, set()
        for title in dirty:
            settings.set_chat_meta(title, ts=self._chatTs[title])

    def _chat_item_color(self, row):
        """弹出菜单按行着色：超过 STALE_DAYS 没动静的置灰 FAINT，否则 INK。"""
        title = self.chatBox.itemData(row)
        ts = self._ts_of(title) if title else 0
        return QColor(FAINT) if ts and is_stale(ts, time.time(), STALE_DAYS) else QColor(INK)

    def _on_chat_selected(self, index):
        """用户自己挑了一个会话：只换看的内容，微信那边不动。标题取 userData，装饰文本不参与。"""
        title = self.chatBox.itemData(index)
        if title and title != self._shown:
            self._switch_to(title)

    def _switch_to(self, title):
        """换正在看的会话：记录、对方最近说、条数、上次的建议一起换过去。"""
        self._shown = title
        self._paint_badge(title)
        self._load_rel(title)
        self._paint_mute()  # 静音按钮只表示当前正在看的会话
        self.unread.pop(title, None)  # 看到了，未读清零
        self._paint_chat_item_text(title)
        self.feed.clear()
        for line in self.feeds.get(title, []):
            self.feed.appendPlainText(line)
        her = self.hers.get(title)
        self._show_latest(her or "")
        self._history_title()
        self._follow_text()
        self._render_targets()
        self.show_cached(self.result_of(title) if self.result_of else None)
        if (settings.chat_meta(title).get("muted") and self._shown == self._chat):
            # 浏览别人的会话时，「只看不填」的提示更要紧，静音提示让位
            self.set_status("这个会话已静音，只记录不自动生成", "warning")

    def _load_rel(self, title):
        """把这个会话单独设的关系刷到标题栏胶囊上（文字 + 菜单勾选态）。"""
        value = settings.chat_relationship(title)
        index = next((i for i, (_, v) in enumerate(_CHAT_RELATIONSHIPS) if v == value), 0)
        self.relPill.setText(_CHAT_RELATIONSHIPS[index][0])
        for i, action in enumerate(self._relActions):
            action.setChecked(i == index)

    def _on_rel_selected(self, index):
        """用户给当前会话改了关系：记住，刷新胶囊，并按新关系马上重新生成。"""
        if not self._shown:
            return
        saved = settings.set_chat_relationship(self._shown, _CHAT_RELATIONSHIPS[index][1])
        self._load_rel(self._shown)
        if saved:
            self.set_status(f"已记住：这个会话按「{_CHAT_RELATIONSHIPS[index][0]}」来写", "success")
        else:
            self.set_status("这次的关系没能存进配置文件，重启后会恢复原样", "warning")
        if self.on_generate:
            self.on_generate(self._shown)

    def set_targets(self, chat, senders, current):
        """某个会话的发言人名单（最近的在前）和当前回复对象；正看着它才重画。"""
        self.targets[chat] = (list(senders), current)
        if chat == self._shown:
            self._render_targets()

    def _render_targets(self):
        """开关关着、或这个会话没有发言人（单聊），这一行就不出现。
        重填下拉框时屏蔽信号，别把自己的填充当成用户挑的。"""
        senders, current = self.targets.get(self._shown, ([], None))
        visible = bool(senders) and settings.reply_target()
        self.targetRow.setVisible(visible)
        if not visible:
            return
        self.targetBox.blockSignals(True)
        self.targetBox.clear()
        self.targetBox.addItems(senders)
        self.targetBox.setCurrentIndex(senders.index(current) if current in senders else 0)
        self.targetBox.blockSignals(False)

    def _on_target_selected(self, index):
        """用户挑了回复对象。浏览别的会话时改的就是那个会话的对象——记录、候选也都按会话走，口径一致。"""
        name = self.targetBox.itemText(index)
        if not name:
            return
        senders, _ = self.targets.get(self._shown, ([], None))
        self.targets[self._shown] = (senders, name)
        self.set_status(f"按「{name}」重新生成…", "busy")
        if self.on_target_change:
            self.on_target_change(self._shown, name)

    def _follow_text(self):
        """「浏览中」才显示这小字，跟随时这一格直接藏起来（去噪）。"""
        self.chatFollow.setVisible(bool(self._chat) and self._shown != self._chat)

    def _toggle_mute(self):
        """静音开关（标题栏铃铛）：只作用于当前正在看的会话，落盘 chat_meta 的 muted。"""
        if not self._shown:
            return
        muted = not settings.chat_meta(self._shown).get("muted")
        saved = settings.set_chat_meta(self._shown, muted=muted)
        self._paint_mute()
        if not saved:
            self.set_status("静音状态没能存进配置文件，重启后会恢复原样", "warning")
        elif muted:
            self.set_status("这个会话已静音，只记录不自动生成", "warning")
        else:
            self.set_status("已恢复：这个会话有新消息会自动生成", "success")

    def _paint_mute(self):
        """图标态跟着 _shown 的 muted 走：静音 = 带斜杠（MUTE），未静音 = 铃铛（RINGER）。"""
        muted = bool(self._shown) and settings.chat_meta(self._shown).get("muted")
        self.muteButton.setIcon(FIF.MUTE if muted else FIF.RINGER)
        self.muteButton.setToolTip("这个会话已静音：只记录，不自动生成；点一下恢复" if muted
                                   else "静音这个会话：只记录，不自动生成")
        self.muteButton.setAccessibleName("取消这个会话的静音" if muted else "静音这个会话")

    def _bump_unread(self, title):
        """没正在看的会话来消息：未读 +1（封顶 99），徽标写上 DisplayRole。"""
        self.unread[title] = min(99, self.unread.get(title, 0) + 1)
        self._paint_chat_item_text(title)

    def _paint_chat_item_text(self, title):
        """DisplayRole = 标题 或 标题 · n（n>0 时）；userData 永远纯标题，查找不受影响。"""
        index = self.chatBox.findData(title)
        if index < 0:
            return
        n = self.unread.get(title, 0)
        self.chatBox.setItemText(index, f"{title} · {n}" if n else title)
        if index == self.chatBox.currentIndex():
            self.chatBox.setText(self.chatBox.itemText(index))  # 按钮上正显示它，跟着刷新（走省略逻辑）

    def _remove_current_chat(self):
        """把正在看的会话移出列表（chatBox 右键）：清内存存档与持久化（chat_meta / chat_rel）。
        不弹确认框（误删代价低：新消息来了会重建条目，视为新会话）。main.py 的 chats 缓存不动。"""
        title = self._shown
        if not title or self.chatBox.findData(title) < 0:
            return
        self.chatBox.blockSignals(True)
        self.chatBox.removeItem(self.chatBox.findData(title))
        self.chatBox.blockSignals(False)
        for store in (self.feeds, self.counts, self.hers, self.targets, self.unread, self._chatTs):
            store.pop(title, None)
        self._tsDirty.discard(title)
        settings.del_chat_meta(title)
        settings.del_chat_relationship(title)
        self.set_status("已移出，收到新消息会重新出现", "success")
        if self.chatBox.count() > 0:
            # removeItem 换当前项按序号不按身份：直接显式切到第一项
            first = self.chatBox.itemData(0)
            self.chatBox.blockSignals(True)
            self.chatBox.setCurrentIndex(0)
            self.chatBox.blockSignals(False)
            self._switch_to(first)
            return
        if self._chat == title:
            self._chat = ""  # 列表已空，前台口径等下一次 set_chat 再建
        self._shown = ""
        self._paint_badge("")
        self.feed.clear()
        self._show_latest("")
        self.cands = []
        self.glosses = []
        self._order = []
        self._clear_cards()
        self.insight.hide()
        self.analysis.hide()
        self.empty.show()
        self._empty_text()
        self._history_title()
        self._follow_text()
        self._render_targets()

    def show_cached(self, result):
        """把某个会话上次的结果放回界面；没有就回到空态。浏览别的会话时只给看不给填——
        微信当前开着的不是它，填进去就串会话了。"""
        if result:
            self.show(result)
        else:
            self.cands = []
            self.glosses = []
            self._order = []
            self._clear_cards()
            self.insight.hide()
            self.analysis.hide()
            self.referenceNote.hide()
            self.empty.show()
            self._empty_text()
        if self._shown != self._chat:
            self.invalidate_replies()
            self.set_status(f"正在浏览「{self._shown}」，只看不填；切回这个会话才能用。", "warning")

    def show(self, result):
        """按推荐顺序展示，卡片始终绑定 candidates 的原始索引。"""
        self.cands = result["candidates"]
        lang = result.get("lang") or ""
        raw_glosses = result.get("glosses") or []
        # 中文意思过一遍显示规则：空、和正文重复、或对方说中文时滤掉，卡片就不创建灰字行；
        # 设置里关了「回复卡上显示中文意思」也在这里叠加（纯显示层：填入/换一条/缓存不受影响）
        allow = settings.show_gloss()
        self.glosses = [(shown_gloss(self.cands[i], raw_glosses[i] if i < len(raw_glosses) else "", lang)
                         if allow else "") for i in range(len(self.cands))]
        self.set_busy(False)
        self._current = bool(self.cands)
        self._clear_cards()
        best = result.get("best_index", 0)
        if best not in range(len(self.cands)):
            best = 0
        raw_scores = result.get("scores") or []
        scores = [raw_scores[i] if i < len(raw_scores) else 0 for i in range(len(self.cands))]
        # 按概率降序排，推荐位（API 给的 choice）强制第一，同分按原索引
        order = sorted(range(len(self.cands)), key=lambda i: (i != best, -(scores[i] or 0), i))
        self._order = order
        for position, index in enumerate(order):
            card = _ReplyCard(self, index, recommended=index == best, number=position + 1)
            self.replyBox.addWidget(card)
            self.cards.append(card)
        reply_to = result.get("reply_to")
        lang = lang or "外语"  # 这张卡放对方原文的中文翻译
        self.insightTitle.setText(f"对方说 · {lang}" + (f" · 回复给 {reply_to}" if reply_to else ""))
        translation = shown_translation(result.get("translation"), lang)
        self.summary.setText(translation)  # 中文对话没有译文行：原文就是中文，再译一遍只会分不清
        self.summary.setVisible(bool(translation))
        analysis = result.get("analysis") or ""
        self.analysis.set_full(analysis)  # 模型怎么理解的：理解错了回复多半也跑偏
        self.analysis.setVisible(bool(analysis))
        self._finish_show()

    def _finish_show(self):
        self.empty.setVisible(not self.cands)
        self.insight.setVisible(bool(self.cands) or bool(self.latest.text()))
        if self.cands:
            self.set_status("建议已更新，选一句适合你的回复", "success")
            # 新结果入场：等首帧布局完成（sizeHint 有效）再依次浮出；已销毁/被替换的卡自动跳过
            cards = list(self.cards)
            self.after(0, lambda: motion.stagger_in(
                [c for c in cards if motion.alive(c) and c in self.cards]))
        else:
            self.set_status("未生成可用回复，请等待下一条新消息。", "error")

    def _clear_cards(self):
        for card in self.cards:
            motion.stop_all(card)  # 销毁前停掉入场/呼吸等所有动画
            self.replyBox.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []

    def after(self, ms, fn):
        QTimer.singleShot(ms, fn)

    def run(self):
        self.app.exec()
