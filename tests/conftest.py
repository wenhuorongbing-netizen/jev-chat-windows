# -*- coding: utf-8 -*-
"""pytest 公共准备：offscreen 平台（不弹真窗口）、仓库根上 sys.path。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
