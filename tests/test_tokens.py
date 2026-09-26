# -*- coding: utf-8 -*-
"""token 唯一性：app/ 下除 theme.py 外不得出现十六进制颜色；FIF.SEND 在 app/ 零命中。"""
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")


def _py_files():
    return sorted(APP_DIR.glob("*.py"))


def test_no_hex_color_outside_theme():
    offenders = []
    for path in _py_files():
        if path.name == "theme.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if HEX_COLOR.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "十六进制颜色只能写在 app/theme.py：\n" + "\n".join(offenders)


def test_no_send_icon_anywhere_in_app():
    offenders = []
    for path in _py_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "FIF.SEND" in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "界面不得出现任何像「发送」的图标：\n" + "\n".join(offenders)


def test_theme_exports_color_v2_tokens():
    """色彩 v2（SPEC-round2 §1）全部 token 必须能从 theme 导入。"""
    import app.theme as theme
    for name in ("CANVAS", "SURFACE", "HAIRLINE", "HAIRLINE_STRONG", "INK", "SUB", "FAINT",
                 "ACCENT", "ACCENT_SOFT", "ACCENT_HOVER", "ACCENT_PRESS", "HOVER", "PRESS",
                 "DANGER", "DANGER_SOFT", "WARN", "SKELETON", "WECHAT", "QQ", "WHATSAPP"):
        assert hasattr(theme, name), f"app/theme.py 缺少色彩 v2 token：{name}"


def test_no_runtime_color_derivation_outside_theme():
    """交互色全部预算好：darker(/lighter( 只允许出现在 theme.py（实际上 v2 一个都不该有）。"""
    offenders = []
    for path in _py_files():
        if path.name == "theme.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "darker(" in line or "lighter(" in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "禁止运行时派生交互色：\n" + "\n".join(offenders)
