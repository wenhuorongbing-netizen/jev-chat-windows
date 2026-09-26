# -*- coding: utf-8 -*-
"""设计 token：全应用唯一允许写死颜色的地方（SPEC-ui-polish §1）。
改颜色只改这里；别处一律 from app.theme import ...。深色模式以后在这里加一套对应值。"""
from PySide6.QtGui import QColor

# ---------------------------------------------------------------- 颜色
INK = "#111827"          # 回复正文、译文
SUB = "#6B7280"          # 中文意思、分析、辅助文字
FAINT = "#9CA3AF"        # 头部小字、来源、悬停才显示的快捷键
SURFACE = "#FFFFFF"      # 面板、卡片
CANVAS = "#F5F6F8"       # 窗口底色
ACCENT = "#4F5BD5"       # 推荐标识、选中、主题色（靛蓝，和微信/QQ/WhatsApp 都拉开）
ACCENT_SOFT = "#EDEFFA"  # 推荐卡背景（ACCENT 10% 叠白的实色）
DANGER = "#DC2626"       # 出错
WARN = "#B45309"         # 警告（琥珀）
BORDER = "#E5E7EB"       # 窗口描边、胶囊描边

# 品牌色：只用在标题栏 App 小标上
WECHAT = "#07A35A"
QQ = "#1677FF"
WHATSAPP = "#25A244"

# 调试视图的画框颜色：不进主界面，只给「识别调试」那个独立窗口用
DEBUG_ME = "#18794E"     # 绿 = 我
DEBUG_HER = "#1F6FD0"    # 蓝 = 对方（也是消息区框）
DEBUG_GRAY = "#8A8A8A"   # 灰 = 过滤掉的灰字
DEBUG_NAME = "#E08B18"   # 橙 = 当成发言人名
DEBUG_IMAGE = "#D0342C"  # 红 = 当成图片丢掉
DEBUG_TINY = "#D4B106"   # 黄 = 小字丢掉
DEBUG_HEAD = "#8B5CF6"   # 紫 = 头部（会话名那条）
DEBUG_BG = "#1B1F1D"     # 画布底色

# ---------------------------------------------------------------- 字号（pt，4 级梯度，不许新增）
BODY = 14   # 回复正文、译文（译文在此基础上加粗）
AUX = 11    # 中文意思、分析、状态
TINY = 10   # 头部小字、来源
TITLE = 18  # 仅设置页大标题

# ---------------------------------------------------------------- 圆角
R_CARD = 10   # 卡片
R_PANEL = 14  # 面板（窗口圆角沿用）
R_PILL = 6    # 胶囊

# ---------------------------------------------------------------- 间距（SPEC 定的卡片内边距 12×9，其余按 4 的倍数）
CARD_PAD_X = 12
CARD_PAD_Y = 9
CARD_GAP = 8


def hover_of(base):
    """悬停底色：比卡片本色深一档，从 token 派生，不引入新 hex。"""
    return QColor(base).darker(104)


def pressed_of(base):
    """按压底色：比悬停再深一档。"""
    return QColor(base).darker(108)
