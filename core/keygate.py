# -*- coding: utf-8 -*-
"""key 只发往它被绑定的接口——在网络出口这一层守住，而不是靠每个调用者记得先问一句。

网络函数（core/llm.py、core/jev_client.py）不再收一个裸字符串 key，只收 Credential：
key 和「它准备发往的接口」在同一个不可变对象里，发出前再跟这次真正要连的地址核对一次。
所以谁直接调用底层客户端都绕不开：没有 Credential 发不出去，Credential 的接口对不上目的地也发不出去。

Credential 只能由两个入口造：
- stored_credential：存下来的 key。它绑定的接口（config.json 的 key_bindings，只记接口不含 key）
  按 BindingState 四种状态各有一个结局：对得上放行、对不上抛 KeyRouteError、没有绑定记录（老版本存的 key）
  放行（S3 之后存下来的 key 都是加密并同时绑定的；只有迁移没做完、或直接从进程环境来的 key 才会在这个状态）、记录读不了（配置损坏）拒发。
- typed_credential：用户刚在设置页里为当前所选接口敲的 key，接口就是页面上选的那个。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

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


class BindingState(str, Enum):
    """存下来的 key 跟目的地的绑定关系，四种、每种只有一个结局（contracts/jev/v1/credential_binding.json）。
    「没有记录」和「记录读不了」必须分开：以前两者都是 None，读不了也被当成老 key 放行。"""
    BOUND_MATCH = "BOUND_MATCH"                # 绑定 == 目的地 → 发
    BOUND_MISMATCH = "BOUND_MISMATCH"          # 绑定 != 目的地 → 拒
    LEGACY_UNBOUND = "LEGACY_UNBOUND"          # 读得了、但这把 key 没有记录（老版本存的）→ 发；迁移没做完（失败会报告、旧值不毁）或直接来自进程环境才会在这个状态
    BINDING_UNAVAILABLE = "BINDING_UNAVAILABLE"  # 记录读不了（配置损坏 / 设置模块加载不了）→ 拒


# 各状态该不该发；stored_credential 只认这张表
SENDS = {BindingState.BOUND_MATCH: True, BindingState.BOUND_MISMATCH: False,
         BindingState.LEGACY_UNBOUND: True, BindingState.BINDING_UNAVAILABLE: False}


def _read_bindings() -> dict | None:
    """全部绑定记录；None = 读不了。绑定记录在 config.json，由设置模块读；core 平时不认识 app。"""
    try:
        from app import settings
    except Exception:  # 导入失败本身就是「读不了」，不是「没绑」
        return None
    try:
        return settings.bindings_state()
    except Exception:
        return None


def binding_state(env: str, destination: str) -> tuple[BindingState, str]:
    """(状态, 已绑的接口)；没有绑定时接口是 ""。"""
    bindings = _read_bindings()
    if bindings is None:
        return BindingState.BINDING_UNAVAILABLE, ""
    bound = bindings.get(env)
    if bound is None:  # 这把 key 真的没有记录：老版本存的
        return BindingState.LEGACY_UNBOUND, ""
    if not isinstance(bound, str) or not bound:  # 有记录但格式不对（被改坏）：读不了，不是没有
        return BindingState.BINDING_UNAVAILABLE, ""
    return (BindingState.BOUND_MATCH if bound == destination else BindingState.BOUND_MISMATCH), bound


def stored_credential(env: str, key: str, destination: str) -> Credential:
    """存下来的 key（env 是它的槽位名）要发往 destination：按 binding_state 的结局放行或抛 KeyRouteError。"""
    state, bound = binding_state(env, destination)
    if not SENDS[state]:
        if state is BindingState.BOUND_MISMATCH:
            raise KeyRouteError(f"接口已换成 {destination}，但保存的 key 是给 {bound} 填的，没有发送。"
                                "请在设置里重新填这个接口的 key。")
        raise KeyRouteError("读不到这把 key 绑定的接口（配置文件损坏或无法读取），没有发送。"
                            "请在设置里重新保存这个接口的 key。")
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
