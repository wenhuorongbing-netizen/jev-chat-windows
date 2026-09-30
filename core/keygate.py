# -*- coding: utf-8 -*-
"""key 只发往它被绑定的接口——在网络出口这一层守住，而不是靠每个调用者记得先问一句。

网络函数（core/llm.py、core/jev_client.py）不再收一个裸字符串 key，只收 Credential：
key 和「它准备发往的接口」在同一个不可变对象里，发出前再跟这次真正要连的地址核对一次。
所以谁直接调用底层客户端都绕不开：没有 Credential 发不出去，Credential 的接口对不上目的地也发不出去。

Credential 只能由两个入口造：
- stored_credential：存下来的 key。它绑定的接口（config.json 的 key_bindings，只记接口不含 key）
  跟目的地不一致就抛 KeyRouteError；没有绑定记录（老版本存的 key）照旧放行，save() 换来源前会先绑上。
- typed_credential：用户刚在设置页里为当前所选接口敲的 key，接口就是页面上选的那个。
"""
from __future__ import annotations

from dataclasses import dataclass, field

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .providers import _origin
except ImportError:
    from providers import _origin

# 各协议不传地址时 SDK 自己会连的地方；Gemini 没有固定地址，用协议名
_DEFAULT_DESTINATION = {"openai": "https://api.openai.com", "anthropic": "https://api.anthropic.com",
                        "gemini": "provider:gemini"}


class KeyRouteError(RuntimeError):
    """key 是为别的接口填的：没发出去，消息里也不含 key 本身。"""


@dataclass(frozen=True)
class Credential:
    """一把 key + 它被允许发往的接口，一次性定好、之后不可改。"""
    key: str = field(repr=False)
    route: str

    def __post_init__(self):
        # 换行/控制字符进不了 HTTP 头；标准库抛的 ValueError 会把整条头（含 key）写进消息，所以在源头拒掉
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in self.key):
            raise KeyRouteError("密钥里有换行或控制字符，请重新粘贴")

    def __str__(self) -> str:  # 误 print / 拼进日志也不带出 key
        return f"<Credential for {self.route}>"


def destination_of(protocol: str, base_url: str | None) -> str:
    """这次请求真正会连到的接口（origin）；地址为空就是该协议 SDK 的默认地址。"""
    return _origin(base_url or "") or _DEFAULT_DESTINATION.get(protocol, f"provider:{protocol}")


def _bound_route(env: str) -> str | None:
    try:
        from app import settings  # 绑定记录在 config.json，由设置模块读；core 平时不认识 app
    except ImportError:
        return None
    return settings._bindings().get(env) or None


def stored_credential(env: str, key: str, destination: str) -> Credential:
    """存下来的 key（env 是它的槽位名）要发往 destination：绑定在别处就抛 KeyRouteError。"""
    bound = _bound_route(env)
    if bound and bound != destination:
        raise KeyRouteError(f"接口已换成 {destination}，但保存的 key 是给 {bound} 填的，没有发送。"
                            "请在设置里重新填这个接口的 key。")
    return Credential(key, destination)


def typed_credential(key: str, destination: str) -> Credential:
    """用户刚为 destination 敲进去的 key（设置页「获取模型」）。"""
    return Credential(key, destination)


def release(cred: object, destination: str) -> str:
    """真正要把 key 交给 SDK / HTTP 头之前的最后一道：必须是 Credential，且它的接口就是 destination。"""
    if not isinstance(cred, Credential):
        raise KeyRouteError("内部错误：key 没有绑定接口，没有发送。")
    if cred.route != destination:
        raise KeyRouteError(f"这把 key 是给 {cred.route} 的，不能发往 {destination}，没有发送。")
    return cred.key
