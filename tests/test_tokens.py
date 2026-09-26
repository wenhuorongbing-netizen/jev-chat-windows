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
