# -*- coding: utf-8 -*-
"""设置持久化。key 硬约束（docs/KICKOFF.md #6）：只进环境变量，绝不落文件；其余设置落 config.json。

key 的持久化走 Windows 用户环境变量（注册表 HKCU\\Environment，跟 setx 写的是同一个地方）。
全程只有两把：判断 JEV_API_KEY、起草 LLM_API_KEY，跟选哪家来源无关。
读的时候先看进程环境，没有就直接读注册表——IDE 启动时把环境快照拿走了，之后再 Run 继承的还是旧环境，
只靠 os.environ 会「保存了下次打开还是没有」。"""
from __future__ import annotations

import ctypes
import json
import os
import sys  # 只为下面这一处：打包后 __file__ 指向临时解包目录，config.json 得放在 exe 旁边才存得住

from core.providers import CUSTOM, DRAFT_PROVIDERS, JEV_ENV, JEV_PROVIDERS, LEGACY, LLM_ENV

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_DEFAULT_RELATIONSHIP = "auto"  # 自动判断：让模型按聊天内容自己看关系和语气
_DEFAULT_CONTEXT = 10
_DEFAULT_JEV = "openrouter"
_DEFAULT_DRAFT = "deepseek"


def _read(name: str, default=None):
    """读 config.json 里的一个字段；每次都重新读文件，改设置不用重启进程。"""
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            value = json.load(f).get(name)
    except (OSError, ValueError):
        return default
    return default if value is None else value

def relationship() -> str:
    """默认关系（没单独设过的会话用它）。"auto" = 自动判断。"""
    return str(_read("relationship") or _DEFAULT_RELATIONSHIP)

def chat_relationship(title: str) -> str:
    """某个会话单独设的关系；没设过返回 ""（= 用默认）。"""
    rels = _read("chat_rel", {})
    return str(rels.get(title) or "") if isinstance(rels, dict) else ""

def set_chat_relationship(title: str, value: str) -> None:
    """给一个会话单独记关系；value 为空 = 删掉，回到默认。只改这一项，别的设置原样。"""
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    rels = data.get("chat_rel") if isinstance(data.get("chat_rel"), dict) else {}
    if value:
        rels[title] = value
    else:
        rels.pop(title, None)
    data["chat_rel"] = rels
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def relationship_for(title: str, group: bool = False) -> str:
    """喂给模型的关系描述：会话单独设的 > 默认；是「自动」就让模型自己判断，群聊给个提示。"""
    rel = chat_relationship(title) or relationship()
    if rel != "auto":
        return rel
    if group:
        return "group chat (several people); infer each person's relationship to me and a fitting tone from the conversation"
    return "not specified; infer our relationship and a fitting tone from the conversation itself"

def context() -> int:
    """参考上下文条数：起草和判断各看最近多少条消息。3~30，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read("context", _DEFAULT_CONTEXT))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT
    return max(3, min(30, n))

def style() -> str:
    """用户自己描述的说话风格（可选，自由文本），只喂给起草模型。默认空 = 只照着最近的消息模仿。"""
    return str(_read("style") or "")

def jev_provider() -> str:
    """判断模型走哪家：openrouter（默认）或 typesafe 直连。"""
    v = _read("jev_provider")
    return v if v in JEV_PROVIDERS else _DEFAULT_JEV

def jev_model() -> str:
    """判断模型 id；空 = 用该来源的默认模型。"""
    return str(_read("jev_model") or "") or JEV_PROVIDERS[jev_provider()].default

def draft_provider() -> str:
    """起草走哪家（见 core/providers.DRAFT_PROVIDERS）。老配置里的 openrouter/deepseek 照样认。"""
    v = _read("draft_provider")
    return v if v in DRAFT_PROVIDERS else _DEFAULT_DRAFT

def draft_provider_name() -> str:
    return DRAFT_PROVIDERS[draft_provider()].name

def draft_model() -> str:
    """起草模型 id；空 = 用该来源的默认模型（有的来源没有默认，那就得自己选）。"""
    return str(_read("draft_model") or "") or DRAFT_PROVIDERS[draft_provider()].default

def draft_base_url() -> str:
    """自定义来源的 Base URL；其余来源用表里的，这里返回空。"""
    return str(_read("draft_base_url") or "") if draft_provider() in CUSTOM else ""

def reply_target() -> bool:
    """群聊指定回复对象：开了才在界面上选回复给谁、才把对象喂给模型。默认关。"""
    return bool(_read("reply_target", False))

def thinking() -> bool:
    """起草时是否开思考模式：慢且贵，默认关。只有 DeepSeek / OpenRouter / Anthropic / Gemini 吃它。"""
    return bool(_read("thinking", False))

def check_update() -> bool:
    """启动时要不要去 GitHub 查一次最新版本号：默认开，只出这一次网，设置里能关。"""
    return bool(_read("check_update", True))

def bilingual() -> bool:
    """智能回复（跟随对方语言）：对方说中文就中文回；说外语就翻成中文给你看，3 条回复用对方的语言写
    （带中文对照），填入只填外语。开了就不调 Jev，只要起草那把 key。默认开。"""
    return bool(_read("bilingual", True))

def bilingual_lang() -> str:
    """双语模式下回复用的语言，默认德语。"""
    return str(_read("bilingual_lang") or "德语")

def read_images() -> bool:
    """对方最新发来的是图片时，截图/取原图一起发给起草模型看。默认开；关了图片只算「[图片]」三个字。"""
    return bool(_read("read_images", True))

def dock() -> bool:
    """悬浮窗贴靠当前聊天窗口：默认开。拖动标题栏会解除，标题栏图钉可以再开。"""
    return bool(_read("dock", True))

def debug_view() -> bool:
    """调试视图：另开一个窗口实时画识别框。默认关，开了子进程才往队列里送帧。"""
    return bool(_read("debug_view", False))

def _read_env(env_name: str) -> str:
    """进程环境优先；没有就读注册表并带进进程环境，之后 core/ 里按 os.environ 读就有了。"""
    v = os.environ.get(env_name, "").strip()
    if not v:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, env_name)[0]).strip()
        except Exception:  # 非 Windows / 没这个值
            v = ""
        if v:
            os.environ[env_name] = v
    return v

def _get_key(env_name: str) -> str:
    """两把 key 之一。新名字空着就退回老版本按来源存的变量（下次保存会抄进新名字）。"""
    return _read_env(env_name) or _read_env(LEGACY[env_name])

def _set_key(env_name: str, value: str) -> None:
    """只写进程环境 + HKCU\\Environment，不写任何文件。"""
    os.environ[env_name] = value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, env_name, 0, winreg.REG_SZ, value)
    except Exception:
        pass  # 非 Windows（本机 Mac 开发）走不到，忽略


def _notify_env() -> None:
    """告诉别的进程环境变量变了。不能用 SendMessageTimeout 对 HWND_BROADCAST：
    它会逐个窗口等回复，超时 5 秒还按窗口数累加，保存按钮在界面线程上就卡死。
    SendNotifyMessage 把消息交出去就返回。"""
    try:
        fn = ctypes.windll.user32.SendNotifyMessageW
        fn.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p)
        fn.restype = ctypes.c_int
        fn(0xFFFF, 0x001A, 0, "Environment")  # HWND_BROADCAST, WM_SETTINGCHANGE
    except Exception:
        pass

def jev_key() -> str:
    """判断那把 key，两家来源共用。"""
    return _get_key(JEV_ENV)

def has_jev_key() -> bool:
    return bool(jev_key())

def llm_key() -> str:
    """起草那把 key，所有语言模型来源共用。"""
    return _get_key(LLM_ENV)

def has_llm_key() -> bool:
    return bool(llm_key())

def has_key() -> bool:
    """界面上「配没配好」：双语模式只要起草那把，普通模式要判断那把。"""
    return has_llm_key() if bilingual() else has_jev_key()

def save(relationship_text: str | None = None, context_n: int | None = None, *,
         jev_provider_text: str | None = None, jev_key_text: str | None = None,
         jev_model_text: str | None = None, draft_provider_text: str | None = None,
         llm_key_text: str | None = None, draft_model_text: str | None = None,
         draft_base_url_text: str | None = None, reply_target_on: bool | None = None,
         style_text: str | None = None, thinking_on: bool | None = None,
         check_update_on: bool | None = None, debug_view_on: bool | None = None,
         bilingual_on: bool | None = None, bilingual_lang_text: str | None = None,
         dock_on: bool | None = None, read_images_on: bool | None = None) -> None:
    """每个参数为空/None = 保留当前值。两把 key 写进程环境 + HKCU\\Environment，不写任何文件。"""
    jev = jev_provider_text if jev_provider_text in JEV_PROVIDERS else jev_provider()
    draft = draft_provider_text if draft_provider_text in DRAFT_PROVIDERS else draft_provider()
    # 没重填就把老变量里的值抄进新名字，迁移一次性做完（_get_key 已经退回读过老的了）
    wrote_key = False
    for env, typed in ((JEV_ENV, jev_key_text), (LLM_ENV, llm_key_text)):
        value = typed or ("" if _read_env(env) else _get_key(env))
        if value:
            _set_key(env, value)
            wrote_key = True
    if wrote_key:
        _notify_env()
    n = context() if context_n is None else max(3, min(30, int(context_n)))
    # 空串 = 清掉，None = 原样留着（读原始字段，别读补过默认值的那个）
    keep = lambda new, name: str(_read(name) or "") if new is None else str(new).strip()
    flag = lambda new, now: now() if new is None else bool(new)
    # 整个 dict 必须在 open(..., "w") **之前**拼好：open 一上来就把文件截断，
    # 之后再 _read() 读到的是空文件，None 那几项就不是「保留」而是被清空了。
    data = {
        # 关系为空 = 只改别的开关（调试视图那种单项保存），别把它写没了
        "relationship": relationship_text or relationship(), "context": n,
        "style": keep(style_text, "style"),
        "jev_provider": jev, "jev_model": keep(jev_model_text, "jev_model"),
        "draft_provider": draft, "draft_model": keep(draft_model_text, "draft_model"),
        "draft_base_url": keep(draft_base_url_text, "draft_base_url"),
        "reply_target": flag(reply_target_on, reply_target),
        "thinking": flag(thinking_on, thinking),
        "check_update": flag(check_update_on, check_update),
        "debug_view": flag(debug_view_on, debug_view),
        "chat_rel": _read("chat_rel", {}),  # 每个会话单独设的关系，由 set_chat_relationship 管，这里原样留着
        "bilingual": flag(bilingual_on, bilingual),
        "dock": flag(dock_on, dock),
        "read_images": flag(read_images_on, read_images),
        "bilingual_lang": keep(bilingual_lang_text, "bilingual_lang"),
    }
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
