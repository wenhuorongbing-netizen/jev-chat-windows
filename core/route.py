# -*- coding: utf-8 -*-
"""一次生成用的路由快照：在主线程、发请求之前一次定好，之后后台线程只拿它，不再回头读任何设置。

早先 analyze_bg 每走一步都去问 settings（来源、模型、Base URL、思考、风格……），用户在生成中间改了设置，
这一次生成可能前半段用旧模型、后半段用新模型，key 甚至发往跟界面上不一样的地址。
现在：来源 + 协议 + 地址 + 模型 + key 的绑定核对，全在 snapshot_route 里做完，装进不可变的 ReplyRoute；
key 是跟「这次真正要连的接口」一起核对过的 Credential（core/keygate），对不上就没有 credential，原因写在 blocked 里，
调用时 require() 抛 KeyRouteError、一个字节都不发。后台线程拿着的是快照，设置怎么改都影响不到它。
"""
from __future__ import annotations

from dataclasses import dataclass, field

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .jev_client import JevError, credential_for
    from .keygate import Credential, KeyRouteError, destination_of
    from .providers import DRAFT_PROVIDERS, LLM_ENV
except ImportError:
    from jev_client import JevError, credential_for
    from keygate import Credential, KeyRouteError, destination_of
    from providers import DRAFT_PROVIDERS, LLM_ENV


@dataclass(frozen=True)
class ReplyRoute:
    provider: str
    protocol: str
    base_url: str  # 这次真正要连的地址（来源表里的、或自定义来源填的）；空 = 该协议 SDK 的默认地址
    model: str
    destination: str  # key 会被送往的 origin，跟 credential.route 是同一个值
    credential: Credential | None = field(default=None, repr=False)
    blocked: str = ""  # 没有 credential 的原因（固定文案，不含 key）

    @property
    def spec(self):
        return DRAFT_PROVIDERS[self.provider]

    def require(self) -> Credential:
        """发请求前：拿到这次要用的 credential；绑定对不上 / 没配 key 就抛，不发。"""
        if self.credential is None:
            raise KeyRouteError(self.blocked or "这把 key 不能用于这个接口，没有发送。")
        return self.credential


@dataclass(frozen=True)
class ReplyPlan:
    """一次生成的全部可变输入的快照：路由 + 口径设置。"""
    route: ReplyRoute
    relationship: str
    context: int
    style: str
    thinking: bool
    image_enabled: bool = True  # 用户开了「识别图片」才会把图发给回复模型（main.snapshot_plan 填）


def snapshot_route(provider: str, base_url: str | None, model: str | None) -> ReplyRoute:
    """读一次存下来的 key，跟这次要连的接口一起核对。核对不过的路由照样返回（credential 为空、blocked 写原因）。"""
    spec = DRAFT_PROVIDERS[provider]
    base = base_url or spec.base
    model = model or spec.default
    destination = destination_of(spec.protocol, base)
    try:
        cred, blocked = credential_for(LLM_ENV, destination), ""
    except (KeyRouteError, JevError) as e:
        cred, blocked = None, str(e)
    return ReplyRoute(provider, spec.protocol, base, model, destination, cred, blocked)
