# -*- coding: utf-8 -*-
"""这个模型能不能直接看图？问服务商自己的模型列表，而不是猜模型名（contracts/jev/v1/capability.json）。

四种状态：supported / unsupported / unknown / disabled_by_policy，每个结论带证据。
只有服务商亲口说了（模型列表里声明了输入类型）才有 supported / unsupported；请求失败、模型不在列表里、
没有这个字段、格式认不出，都只是 unknown——「不知道」永远不等于「不支持」。
缓存只收服务商的亲口答案，按 接口地址+模型 分开，6 小时过期；unknown 不缓存，下次再问。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .providers import _origin
except ImportError:
    from providers import _origin

SUPPORTED, UNSUPPORTED, UNKNOWN, DISABLED_BY_POLICY = "supported", "unsupported", "unknown", "disabled_by_policy"
PROVIDER_DECLARED, FIELD_ABSENT, MODEL_NOT_LISTED = "provider_declared", "field_absent", "model_not_listed"
UNKNOWN_SHAPE, FETCH_FAILED, OWNER_DISABLED = "unknown_shape", "fetch_failed", "owner_disabled"

TTL_S = 6 * 60 * 60


@dataclass(frozen=True)
class Capability:
    state: str
    evidence: str


@dataclass(frozen=True)
class ImageDecision:
    effective: str
    attach: bool
    fall_back_to_text: bool
    reason: str


def _modality_paths(base: str) -> list[list[str]]:
    """各家的模型列表把输入类型放在哪：适配器只读自己认得的字段，没读的字段对它就是不存在。"""
    if _origin(base) == "https://openrouter.ai":
        return [["architecture", "input_modalities"], ["input_modalities"]]
    return [["input_modalities"]]


def parse_image(base: str, body: str | None, model: str) -> Capability:
    unknown_shape = Capability(UNKNOWN, UNKNOWN_SHAPE)
    if not body or not body.strip():
        return unknown_shape
    try:
        data = json.loads(body).get("data")
    except (ValueError, AttributeError):
        return unknown_shape
    if not isinstance(data, list):
        return unknown_shape
    wanted = model.strip()
    entry = next((e for e in data if isinstance(e, dict) and e.get("id") == wanted), None)
    if entry is None:
        return Capability(UNKNOWN, MODEL_NOT_LISTED)
    for path in _modality_paths(base):
        holder = entry
        for key in path[:-1]:
            holder = holder.get(key) if isinstance(holder, dict) else None
        value = holder.get(path[-1]) if isinstance(holder, dict) else None
        if value is None:
            continue
        if not isinstance(value, list) or not value:
            return unknown_shape
        return Capability(SUPPORTED if "image" in value else UNSUPPORTED, PROVIDER_DECLARED)
    return Capability(UNKNOWN, FIELD_ABSENT)


def cache_key(base: str, model: str) -> str | None:
    """接口地址（来源 + 路径，结尾斜杠、主机大小写、默认端口都不算）| 模型；None = 没有可当键的东西。"""
    origin = _origin(base)
    wanted = (model or "").strip()
    if not origin or not wanted:
        return None
    return origin + urlsplit(base.strip()).path.rstrip("/") + "|" + wanted


def decide_image(capability: str, owner_enabled: bool, client_can_attach: bool) -> ImageDecision:
    """能力已知后，这次请求怎么处置图：用户关了就不带；服务商说不行就不带；不知道就带着试、失败了才退回纯文字；
    客户端自己没有带图的路（协议不支持）不是对模型的判断。"""
    if not owner_enabled or capability == DISABLED_BY_POLICY:
        return ImageDecision(DISABLED_BY_POLICY, False, False, OWNER_DISABLED)
    if capability == UNSUPPORTED:
        return ImageDecision(capability, False, False, PROVIDER_DECLARED)
    if not client_can_attach:
        return ImageDecision(capability, False, False, "client_limit")
    if capability == SUPPORTED:
        return ImageDecision(capability, True, False, PROVIDER_DECLARED)
    return ImageDecision(capability, True, True, "not_known")


class ModelCapabilities:
    """fetch(route) 返回这个路由的 /models 正文；None 或抛错 = 请求没成。"""

    def __init__(self, fetch, now=time.time, ttl_s: float = TTL_S):
        self._fetch, self._now, self._ttl = fetch, now, ttl_s
        self._cache: dict[str, tuple[Capability, float]] = {}
        self._lock = threading.Lock()

    def image_input(self, route) -> Capability:
        model = (route.model or "").strip()
        if not model:
            return Capability(UNKNOWN, MODEL_NOT_LISTED)
        key = cache_key(route.base_url, model)
        if key is None or route.credential is None:
            return Capability(UNKNOWN, FETCH_FAILED)
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[1] > self._now():
                return hit[0]
            self._cache.pop(key, None)
        try:
            body = self._fetch(route)
        except Exception:
            body = None
        if body is None:
            return Capability(UNKNOWN, FETCH_FAILED)
        cap = parse_image(route.base_url, body, model)
        if cap.state in (SUPPORTED, UNSUPPORTED):
            with self._lock:
                self._cache[key] = (cap, self._now() + self._ttl)
        return cap
