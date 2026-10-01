# -*- coding: utf-8 -*-
"""一次模型调用失败的七种归类，以及按归类决定的有界重试（contracts/jev/v1/retry_policy.json）。

归类本身决定重不重试、总共最多试几次：只有 rate_limited / timeout / transport 会再试；
auth（含 key 绑在别的接口、一个字节都没发）、unsupported、invalid_response、cancelled 一次就是定局。
每次重试前先问「这一代还有人要吗」——睡之前问一次、睡醒再问一次——过期的生成不再打扰服务商。
失败的文字只能是我们自己写的固定句子，不带服务端响应体（core/jev_client.hint_for）。
"""
from __future__ import annotations

import time

AUTH, RATE_LIMITED, TIMEOUT = "auth", "rate_limited", "timeout"
UNSUPPORTED, INVALID_RESPONSE, CANCELLED, TRANSPORT = "unsupported", "invalid_response", "cancelled", "transport"

# 每一类总共最多试几次（含第一次）；1 = 不重试
MAX_ATTEMPTS = {AUTH: 1, RATE_LIMITED: 3, TIMEOUT: 2, UNSUPPORTED: 1, INVALID_RESPONSE: 1, CANCELLED: 1, TRANSPORT: 3}


def kind_of_status(status: int) -> str:
    if status in (401, 402, 403):
        return AUTH
    if status == 408:
        return TIMEOUT
    if status in (429, 529):
        return RATE_LIMITED
    if 500 <= status <= 599:
        return TRANSPORT
    if 400 <= status <= 499:
        return UNSUPPORTED
    return INVALID_RESPONSE


def kind_of(exc: BaseException) -> str | None:
    """这次失败属于哪一类；认不出的（我们自己的 bug 之类）是 None，永远不重试。"""
    kind = getattr(exc, "kind", None)
    if kind in MAX_ATTEMPTS:
        return kind
    try:
        from .keygate import KeyRouteError
    except ImportError:
        from keygate import KeyRouteError
    if isinstance(exc, KeyRouteError):  # key 绑在别的接口：没发出去，换来源要重填，重试没有意义
        return AUTH
    if isinstance(exc, TimeoutError):
        return TIMEOUT
    if isinstance(exc, OSError):
        return TRANSPORT
    return None


def kind_of_sdk_error(exc: BaseException, status: int | None) -> str | None:
    """各家 SDK 的异常：有状态码按状态码，没有就看是超时还是连接层。只看类型名，不引用异常里的文字。"""
    if status is not None:
        return kind_of_status(status)
    name = type(exc).__name__.lower()
    if "timeout" in name or isinstance(exc, TimeoutError):
        return TIMEOUT
    if any(w in name for w in ("connect", "network", "readerror", "writeerror", "protocol")) or isinstance(exc, OSError):
        return TRANSPORT
    return None


def backoff_pause(calls: int) -> None:
    """跟 Android 同一档：第 n 次失败后等 0.5s × 2^n。后台线程里睡，不冻界面。"""
    time.sleep(0.5 * (2 ** calls))


def run(attempt, is_live, pause=backoff_pause):
    """有界重试一次模型调用。attempt(n) 拿 1 起算的第几次；成功返回它的值，失败抛最后那个异常。
    重试条件：归类认得、归类允许重试、次数没用完、这一代还被需要（睡前问一次、睡后再问一次）。"""
    calls = 0
    while True:
        calls += 1
        try:
            return attempt(calls)
        except Exception as failure:
            kind = kind_of(failure)
            if kind is None or calls >= MAX_ATTEMPTS[kind] or not is_live():
                raise
            pause(calls)
            if not is_live():
                raise
