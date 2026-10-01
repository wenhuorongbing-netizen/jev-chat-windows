# -*- coding: utf-8 -*-
"""密钥落盘只走这里：Windows DPAPI（CryptProtectData）加密成密文，只有同一个 Windows 用户能解开。

- 密文字符串 "dpapi1:<base64>" 可以放进 config.json；明文 key 永远不进任何文件、注册表或日志。
- slot（JEV_API_KEY / LLM_API_KEY）作为附加熵一起加密：把 A 槽的密文搬到 B 槽，解不开。
- 没有「DPAPI 失败就存明文」的退路：任何一步失败都抛 SecretError，调用方必须当作没保存。
"""
from __future__ import annotations

import base64
import ctypes
import sys

_PREFIX = "dpapi1:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretError(Exception):
    """加解密失败。消息里不含 key，也不含密文。"""


if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _blob_p = ctypes.POINTER(_Blob)
    # 签名只在导入时定一次：两个线程同时加解密不会互相改对方正在用的 argtypes
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptProtectData.argtypes = [_blob_p, wintypes.LPCWSTR, _blob_p, ctypes.c_void_p, ctypes.c_void_p,
                                         wintypes.DWORD, _blob_p]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [_blob_p, ctypes.c_void_p, _blob_p, ctypes.c_void_p, ctypes.c_void_p,
                                           wintypes.DWORD, _blob_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _entropy(slot: str) -> bytes:
    return f"jev-chat-windows/secret/v1/{slot}".encode("utf-8")


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))  # 调用方持有 buf，直到 API 返回
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _call(protect: bool, data: bytes, slot: str) -> bytes:
    if sys.platform != "win32":
        raise SecretError("本机不是 Windows，没有 DPAPI")
    src, keep_src = _blob(data)
    ent, keep_ent = _blob(_entropy(slot))
    out = _Blob()
    if protect:
        ok = _crypt32.CryptProtectData(ctypes.byref(src), "jev", ctypes.byref(ent), None, None,
                                       _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    else:
        ok = _crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                         _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise SecretError(f"DPAPI 调用失败（错误码 {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def protect(plain: str, slot: str) -> str:
    """明文 → "dpapi1:<base64>"。"""
    if not plain:
        raise SecretError("空的密钥不加密")
    try:
        blob = _call(True, plain.encode("utf-8"), slot)
    except SecretError:
        raise
    except Exception as e:  # ctypes 自己的错也算「没加密成」
        raise SecretError(f"DPAPI 调用出错（{type(e).__name__}）") from None
    return _PREFIX + base64.b64encode(blob).decode("ascii")


def unprotect(token: str, slot: str) -> str:
    """密文 → 明文。格式不对、换了 Windows 用户、槽位不对、数据被改都抛 SecretError。"""
    if not isinstance(token, str) or not token.startswith(_PREFIX):
        raise SecretError("不认识的密文格式")
    try:
        blob = base64.b64decode(token[len(_PREFIX):], validate=True)
    except ValueError:
        raise SecretError("密文不是合法的 base64") from None
    try:
        return _call(False, blob, slot).decode("utf-8")
    except SecretError:
        raise
    except UnicodeDecodeError:
        raise SecretError("解出来的不是文本") from None
    except Exception as e:
        raise SecretError(f"DPAPI 调用出错（{type(e).__name__}）") from None
