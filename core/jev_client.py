# -*- coding: utf-8 -*-
"""模型调用的公共件：JevError、失败文案、key 读取。

早先这里还有 Jev 判断 API 的客户端（OpenRouter / TypeSafe 直连）；S4 起判断模式已删，回复只走
core/llm.py 的三种协议。文件名留着是因为 JevError / hint_for / credential_for 到处在引用。
key 只从环境变量读（app 从加密存储解出来放着的那把），绝不打进日志。
"""

from __future__ import annotations

import os
from typing import NoReturn

try:  # 当模块导入 / 当脚本直接跑 都能用
    from . import retry
    from .keygate import Credential, stored_credential
    from .providers import ENV_VARS, JEV_ENV
except ImportError:
    import retry
    from keygate import Credential, stored_credential
    from providers import ENV_VARS, JEV_ENV


class JevError(Exception):
    """kind 是 core/retry 的七类之一（认不出就是 None）；它决定重不重试。"""

    def __init__(self, message: str, status: int | None = None, kind: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind if kind is not None else (retry.kind_of_status(status) if status else None)


def redact_secrets(text: str) -> str:
    """Strip every live key from any string before print or disk write."""
    if not isinstance(text, str):
        text = str(text)
    for env in ENV_VARS:
        key = os.environ.get(env) or ""
        if key:
            text = text.replace(key, "[REDACTED]")
    return text


def _status_of(exc: Exception) -> int | None:
    """各家 SDK 放 HTTP 状态码的属性名不一样：openai/anthropic 是 status_code，
    google-genai 是 code（它的 status 是 'NOT_FOUND' 这种字符串）。"""
    for name in ("status_code", "code", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    return None


_STATUS_HINT = {401: "密钥被拒，请检查该接口的密钥", 403: "密钥被拒，请检查该接口的密钥", 404: "地址或模型名不对",
               400: "请求被拒绝，请检查模型名和接口地址", 422: "请求被拒绝，请检查模型名和接口地址",
               402: "账户余额或额度不足", 429: "服务繁忙，请稍后再试", 529: "服务繁忙，请稍后再试"}  # 文案跟 Android 一份（contracts/jev/v1/http_hint.json）


def hint_for(status: int | None) -> str:
    """一个 HTTP 状态码对应的、我们自己写的提示。绝不引用服务端返回的任何文字。"""
    if status in _STATUS_HINT:
        return _STATUS_HINT[status]
    if status is not None and 500 <= status <= 599:
        return "服务端出错，请稍后再试"
    return "请求没有成功"


def invalid_response(message: str) -> JevError:
    """服务商答了，但答案不能用（解析不出 / 空 / 拒绝回答）。文字是我们自己的固定句子，不带模型的任何原文；不重试。"""
    return JevError(message, kind=retry.INVALID_RESPONSE)


def _fail(exc: Exception, what: str) -> NoReturn:
    """SDK 抛的异常 → 一句人话的 JevError。只留 状态码 + 固定提示：SDK 异常的文字里带着服务端的响应体，
    第三方可能把 key、提示词或聊天片段回显在里面，所以整段不用，也就不需要靠脱敏去赌。"""
    if isinstance(exc, JevError):
        raise exc
    status = _status_of(exc)
    kind = retry.kind_of_sdk_error(exc, status)
    if status:
        raise JevError(f"{what} HTTP {status}: {hint_for(status)}", status, kind) from None
    raise JevError(f"{what}失败: {type(exc).__name__}", None, kind) from None


def _api_key(env: str = JEV_ENV) -> str:
    """两把 key 之一（JEV_API_KEY / LLM_API_KEY）。只读自己的名字：app 把加密存储里解出来的那把放在这里；
    OPENROUTER_API_KEY / DEEPSEEK_API_KEY 这些通用名是别的工具的东西，这里不读（S3.1）。"""
    key = (os.environ.get(env) or "").strip()
    if not key:
        raise JevError(
            f"{env} is not set. Export it in the environment; "
            "do not put the key in a file."
        )
    return key


def credential_for(env: str, destination: str) -> Credential:
    """存下来的那把 key（env 槽位）+ 它这次要发往的接口，合成一个不可变的 Credential。
    绑定在别的接口上就抛 KeyRouteError。key 没配抛 JevError。"""
    return stored_credential(env, _api_key(env), destination)


if __name__ == "__main__":
    # ponytail: 不联网。失败只留状态码 + 固定提示；归类跟状态码走。
    class _Boom(Exception):
        status_code = 429

    try:
        _fail(_Boom("rate limited sk-secret"), "起草")
    except JevError as e:
        assert e.status == 429 and e.kind == retry.RATE_LIMITED and "服务繁忙" in str(e) and "sk-secret" not in str(e)
    os.environ[JEV_ENV] = "ts-key"
    assert redact_secrets("key=ts-key") == "key=[REDACTED]"
    print("jev_client ok")
