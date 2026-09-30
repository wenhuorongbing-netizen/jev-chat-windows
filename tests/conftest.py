# -*- coding: utf-8 -*-
"""pytest 公共准备：offscreen 平台（不弹真窗口）、仓库根上 sys.path，以及每个测试都套上的隔离。

隔离（autouse）：测试不许碰开发者本机的东西——
- config.json 指到临时目录（以前有的测试先建 Overlay、后换路径，中间读写的是真配置）；
- key 只走进程环境，不读也不写注册表 HKCU\\Environment，也不广播环境变更；起手清掉本机已有的 key 变量；
- 网络只放行回环地址：任何测试想连外网都当场失败，而不是悄悄发出去。
"""
import os
import socket
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    from app import settings
    from core import providers

    monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(settings, "_notify_env", lambda: None)
    monkeypatch.setattr(settings, "_read_env", lambda name: os.environ.get(name, "").strip())

    def _set_key(name, value):  # 只写进程环境；monkeypatch 负责测试结束时还原
        monkeypatch.setenv(name, value)

    monkeypatch.setattr(settings, "_set_key", _set_key)
    for name in providers.ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    real_connect = socket.socket.connect

    def guarded_connect(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            raise AssertionError(f"测试里不许连外网：{host!r}")
        return real_connect(self, address, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield
    _release_qt_objects()


def _release_qt_objects():
    """夹具里 win.close() + deleteLater() 之后没有事件循环去真删，旧 Overlay 一直活着，
    而每个新 Overlay 的构造成本随存活数增长（实测 25 个后每个 ~4 秒 → 整套 5 分钟）。
    每个测试结束在这里把延迟删除冲掉，构造成本就不再累积。"""
    import gc

    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is not None:
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()
