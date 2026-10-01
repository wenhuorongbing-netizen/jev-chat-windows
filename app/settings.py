# -*- coding: utf-8 -*-
"""设置持久化。key 硬约束（docs/KICKOFF.md #6）：明文 key 不进任何文件、注册表、日志。

- 两把 key（判断 JEV_API_KEY、起草 LLM_API_KEY，跟选哪家来源无关）只以 DPAPI 密文的形式存在 config.json 的
  secrets 字段里（app/secretstore.py）；密文和它绑定的接口（key_bindings）在同一次原子写里落盘，不会只剩一半。
- 老版本把 key 明文放在用户环境变量（HKCU\\Environment）：现在只当迁移来源，见 migrate_legacy_keys()。
- config.json 的每次写都是「写临时文件 → 刷盘 → 原子替换」，写不进去就抛 ConfigWriteError，绝不假装存了；
  配置读不出来（被占用）时不写，配置内容损坏时先原样留一份 .corrupt.<时间戳> 再写新的。"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys  # 只为下面这一处：打包后 __file__ 指向临时解包目录，config.json 得放在 exe 旁边才存得住
import threading
import time

from app import secretstore
from app.secretstore import SecretError  # noqa: F401 —— 保存 key 失败时抛它，界面层从 settings 上取
from core.keygate import KeyRouteError  # noqa: F401 —— 老名字：main / 测试从 settings 上取
from core.providers import (CUSTOM, DRAFT_PROVIDERS, JEV_ENV, LEGACY, LLM_ENV,
                            draft_route, jev_route)

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_DEFAULT_RELATIONSHIP = "auto"  # 自动判断：让模型按聊天内容自己看关系和语气
_DEFAULT_CONTEXT = 30  # 10 条在群聊里常常连在聊什么都看不出来
_DEFAULT_JEV = "openrouter"
_JEV_SOURCES = ("openrouter", "typesafe")
_DEFAULT_DRAFT = "deepseek"

_LOCK = threading.RLock()  # config.json 的「读 → 改 → 写」整段串行：界面线程和后台线程同时改设置不会互相覆盖
_ISSUES: dict[str, str] = {}  # 密钥存取上现在还有的、用户该知道的事（迁移没做完、密文解不开）；问题没了就摘掉


class ConfigWriteError(OSError):
    """config.json 没写成：原文件原样还在。消息里不含任何设置内容。"""


def _issue(key: str, msg: str) -> None:
    with _LOCK:
        _ISSUES[key] = msg


def _clear_issue(*keys: str) -> None:
    with _LOCK:
        for k in keys:
            _ISSUES.pop(k, None)


def secret_issues() -> list[str]:
    """给界面看的：密钥存取上现在还没处理完的事，空 = 没有。"""
    with _LOCK:
        return list(_ISSUES.values())


def _load_raw() -> tuple[dict | None, str]:
    """(配置, 状态)。状态：ok / missing（还没有这个文件）/ corrupt（内容不是 JSON 对象）/ unreadable（打不开，如被占用）。"""
    with _LOCK:  # 跟写入互斥：Windows 上有人开着文件时替换会被拒，读到替换瞬间的半状态也不行
        try:
            with open(_CONFIG, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}, "missing"
        except OSError:
            return None, "unreadable"
        except ValueError:
            return None, "corrupt"
    if not isinstance(data, dict):
        return None, "corrupt"
    return data, "ok"


def _commit(data: dict, keep_corrupt: bool = False) -> None:
    """整份配置写进临时文件、刷盘，再原子替换；任何一步失败原文件原样保留并抛 ConfigWriteError。"""
    tmp = _CONFIG + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if keep_corrupt:  # 损坏的老文件先留底，留不下就不写，免得把用户的东西抹掉
            shutil.copyfile(_CONFIG, f"{_CONFIG}.corrupt.{int(time.time())}")
        os.replace(tmp, _CONFIG)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ConfigWriteError(f"配置没有写进去（{type(e).__name__}）") from None


def _update(mutate) -> None:
    """读当前配置 → mutate(data) 原地改 → 提交。mutate 抛什么就什么都不写。"""
    with _LOCK:
        data, status = _load_raw()
        if status == "unreadable":
            raise ConfigWriteError("配置文件现在读不了，没有写入")
        data = data if data is not None else {}
        mutate(data)
        _commit(data, keep_corrupt=(status == "corrupt"))


def _try_update(mutate) -> bool:
    """不值得打断用户的小设置（窗口位置、会话元信息）：写成了 True，没写成 False，不抛。"""
    try:
        _update(mutate)
        return True
    except OSError:
        return False


def _read(name: str, default=None):
    """读 config.json 里的一个字段；每次都重新读文件，改设置不用重启进程。"""
    data, _ = _load_raw()
    value = data.get(name) if data else None
    return default if value is None else value

def relationship() -> str:
    """默认关系（没单独设过的会话用它）。"auto" = 自动判断。"""
    return str(_read("relationship") or _DEFAULT_RELATIONSHIP)

def chat_relationship(title: str) -> str:
    """某个会话单独设的关系；没设过返回 ""（= 用默认）。"""
    rels = _read("chat_rel", {})
    return str(rels.get(title) or "") if isinstance(rels, dict) else ""

def del_chat_meta(title: str) -> bool:
    """删掉某个会话的元信息条目（5B-3 移出列表）。只动这一个 key，其余设置原样。True = 已写进配置。"""
    def mutate(data):
        metas = data.get("chat_meta") if isinstance(data.get("chat_meta"), dict) else {}
        metas.pop(title, None)
        data["chat_meta"] = metas
    return _try_update(mutate)

def set_chat_relationship(title: str, value: str) -> bool:
    """给一个会话单独记关系；value 为空 = 删掉，回到默认。只改这一项，别的设置原样。True = 已写进配置。"""
    def mutate(data):
        rels = data.get("chat_rel") if isinstance(data.get("chat_rel"), dict) else {}
        if value:
            rels[title] = value
        else:
            rels.pop(title, None)
        data["chat_rel"] = rels
    return _try_update(mutate)

def del_chat_relationship(title: str) -> bool:
    """删掉某个会话单独设的关系（5B-3 移出列表时一并清）。只动这一个 key。True = 已写进配置。"""
    def mutate(data):
        rels = data.get("chat_rel") if isinstance(data.get("chat_rel"), dict) else {}
        rels.pop(title, None)
        data["chat_rel"] = rels
    return _try_update(mutate)


def relationship_for(title: str, group: bool = False) -> str:
    """喂给模型的关系描述：会话单独设的 > 默认；是「自动」就让模型自己判断，群聊给个提示。"""
    rel = chat_relationship(title) or relationship()
    if rel != "auto":
        return rel
    if group:
        return "group chat (several people); infer each person's relationship to me and a fitting tone from the conversation"
    return "not specified; infer our relationship and a fitting tone from the conversation itself"

def context() -> int:
    """参考上下文条数：起草和判断各看最近多少条消息。3~100，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read("context", _DEFAULT_CONTEXT))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT
    return max(3, min(100, n))

def style() -> str:
    """用户自己描述的说话风格（可选，自由文本），只喂给起草模型。默认空 = 只照着最近的消息模仿。"""
    return str(_read("style") or "")

def jev_provider() -> str:
    """旧版判断来源（openrouter 默认 / typesafe）：S4 起没有发送路径，只用来读老配置里那把 key 的绑定记录。"""
    v = _read("jev_provider")
    return v if v in _JEV_SOURCES else _DEFAULT_JEV

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

def bilingual_lang() -> str:
    """双语模式下回复用的语言，默认德语。"""
    return str(_read("bilingual_lang") or "德语")

def read_images() -> bool:
    """对方最新发来的是图片时，把窗口里那张图截下来一起发给起草模型看。默认关，必须用户主动开；
    关了图片只算「[图片]」三个字。存的键是 read_images_optin：老版本的 read_images（当年默认 True）
    不算授权，升级后要在设置里重新打开一次。"""
    return bool(_read("read_images_optin", False))

def show_gloss() -> bool:
    """回复卡上显示中文意思（双语时的灰字对照）：默认开；关了只影响显示，不影响填入和缓存。"""
    return bool(_read("show_gloss", True))

def dock() -> bool:
    """悬浮窗贴靠当前聊天窗口：默认开。拖动标题栏会解除，标题栏图钉可以再开。"""
    return bool(_read("dock", True))

def win_pos():
    """上次未贴靠时的窗口位置 (x, y)；没存过 / 脏数据 → None（Q5）。"""
    v = _read("win_pos")
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return int(v[0]), int(v[1])
        except (TypeError, ValueError):
            return None
    return None

def win_width():
    """上次未贴靠时的窗口宽度；没存过 / 脏数据 → None（Q5）。"""
    try:
        w = int(_read("win_width"))
        return w if w > 0 else None
    except (TypeError, ValueError):
        return None

def chat_meta(title: str) -> dict:
    """某个会话的元信息（ts 最后活跃 epoch 秒、muted 静音）；没存过 / 脏数据 → {}。"""
    metas = _read("chat_meta", {})
    entry = metas.get(title) if isinstance(metas, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}

def set_chat_meta(title: str, **fields) -> bool:
    """合并写入某个会话的元信息（只动这一个 key，其余设置原样）。字段：ts、muted。True = 已写进配置。"""
    def mutate(data):
        metas = data.get("chat_meta") if isinstance(data.get("chat_meta"), dict) else {}
        entry = metas.get(title) if isinstance(metas.get(title), dict) else {}
        entry.update(fields)
        metas[title] = entry
        data["chat_meta"] = metas
    return _try_update(mutate)

def save_win_state(x: int, y: int, width: int) -> bool:
    """记住未贴靠时的窗口位置和宽度。只改这两项，别的设置原样。True = 已写进配置。"""
    def mutate(data):
        data["win_pos"] = [int(x), int(y)]
        data["win_width"] = int(width)
    return _try_update(mutate)

def debug_view() -> bool:
    """调试视图：另开一个窗口实时画识别框。默认关，开了子进程才往队列里送帧。"""
    return bool(_read("debug_view", False))

def _read_env(env_name: str) -> str:
    """进程环境里的值（用户自己 export 的，或本进程从加密存储里解出来放着的）。只读内存，不碰注册表。"""
    return os.environ.get(env_name, "").strip()

def _mirror_key(env_name: str, value: str) -> None:
    """解出来的 key 放进本进程环境，core/ 里的网络层按 os.environ 读。只在内存里，不落盘。"""
    os.environ[env_name] = value

def _registry_read(name: str) -> str:
    """老版本放明文 key 的地方：用户环境变量 HKCU\\Environment。只当迁移来源读。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0]).strip()
    except Exception:  # 非 Windows / 没这个值
        return ""

def _registry_delete(name: str) -> bool:
    """删掉用户环境变量里的一个值。True = 现在已经没有了（删掉了，或本来就没有）。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False

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

def _stored_key(env_name: str) -> str:
    """config.json 里加密存着的 key；没有 = 空。有但解不开（换了 Windows 账户、数据被改）也是空，并记一条给界面看的话。"""
    data, _ = _load_raw()
    secrets = data.get("secrets") if data else None
    token = secrets.get(env_name) if isinstance(secrets, dict) else None
    if not isinstance(token, str) or not token:
        return ""
    try:
        key = secretstore.unprotect(token, env_name).strip()
    except SecretError:
        _issue(f"decrypt:{env_name}", f"{env_name} 保存的密文解不开（换了 Windows 账户或数据损坏），请在设置里重新填这把 key。")
        return ""
    _clear_issue(f"decrypt:{env_name}")
    return key

def _get_key(env_name: str) -> str:
    """能拿去发请求的那把 key：只有加密存储里的（它跟接口绑在一起写进配置）。
    进程环境、用户环境变量里的旧明文、通用名变量都不是来源——迁移没做完时旧明文只保留、不使用（S3.1）。"""
    v = _stored_key(env_name)
    if v:
        _mirror_key(env_name, v)  # core/ 里的网络层按 os.environ 读，放进去的永远是加密存储里解出来的那把
    return v

def _unmirror_key(env_name: str) -> None:
    """本进程环境里这个名字的值不是从加密存储来的（启动时继承的旧明文、用户自己 set 的）：摘掉，网络层读不到它。
    只动本进程的内存，不碰用户环境变量本身。"""
    os.environ.pop(env_name, None)

def migrate_legacy_keys() -> list[str]:
    """把老版本放在用户环境变量里的明文 key 迁进加密存储：找到 → 加密并读回核对 → 跟绑定一起原子写进配置 →
    从磁盘重新读出来核对 → 才删旧明文。任何一步失败都停在那一步：旧明文原样还在、但不使用，
    原因记进 secret_issues()。旧明文「保留」不等于「使用」：没迁成功就没有绑定，Jev 不用它发任何请求，
    要用得在设置里重新填（填了才有密文和绑定）。不会为了「迁成功」退到明文存储。返回当前没处理完的事。
    JEV_API_KEY / LLM_API_KEY 是本程序自己的名字，迁完即删；OPENROUTER_API_KEY / DEEPSEEK_API_KEY 是别的工具
    也常用的通用名，导进来但不替用户删，只提示。"""
    with _LOCK:
        for env in (JEV_ENV, LLM_ENV):
            try:
                _migrate_one(env)
            except Exception as e:  # 不让迁移的问题挡住启动
                _issue(f"migrate:{env}", f"{env} 迁移没做完（{type(e).__name__}）：旧的 key 原样保留、但这次不会用它发请求，请在设置里重新填一次。")
            inherited = _read_env(env)
            if inherited and inherited != _stored_key(env):  # 不是从加密存储来的（继承来的旧明文、自己 set 的、读回对不上的）：不许留给网络层
                _unmirror_key(env)
                if f"migrate:{env}" not in _ISSUES:  # 迁移失败那条已经说了原因，别再报第二条
                    _issue(f"ignored:{env}", f"进程环境里的 {env} 没有绑定接口，没有使用；请在设置里填 key。")
        return secret_issues()

# 通用名那把 key 当年是为哪个接口填的：只能是它自己的来源，不能算成「现在选的来源」，否则别家的 key 会被绑去发给现在选的这家
_LEGACY_ROUTE = {JEV_ENV: lambda: jev_route("openrouter"), LLM_ENV: lambda: draft_route("deepseek")}


def _migrate_one(env: str) -> None:
    _clear_issue(f"migrate:{env}", f"plain:{env}", f"keep:{env}", f"shared:{env}", f"ignored:{env}")
    own, shared = _registry_read(env), _registry_read(LEGACY[env])
    stored = _stored_key(env)
    if stored:  # 已经在加密存储里（解得开）：新存的说了算，只剩清理旧明文
        _mirror_key(env, stored)  # 启动时继承来的旧环境变量不许压过它
        if own == stored:
            _drop_plaintext(env)
        elif own:  # 跟存着的不一样：不知道哪把才是用户要的，不删、也不用，只告诉他
            _issue(f"keep:{env}", f"用户环境变量 {env} 里的 key 跟已保存的不同，没有删除也没有使用；"
                                  "确认不要了就自己删掉，要用的话请在设置里重新填。")
        _note_shared(env, shared)
        return
    legacy = own or shared
    if not legacy:
        return
    _, status = _load_raw()
    if status in ("corrupt", "unreadable"):  # 不知道这把 key 原来绑的是哪个接口：配置没读明白之前不迁
        _issue(f"migrate:{env}", f"{env} 迁移暂缓：配置文件读不了或已损坏，旧的 key 原样保留、但这次不会用它发请求，请在设置里重新填一次。")
        return
    try:
        token = secretstore.protect(legacy, env)
        if secretstore.unprotect(token, env) != legacy:
            raise SecretError("read-back mismatch")
    except SecretError:
        _issue(f"migrate:{env}", f"{env} 没能加密（DPAPI 失败）：旧的 key 原样保留、但这次不会用它发请求，请在设置里重新填一次。")
        return
    route = _bindings().get(env) or (_current_route(env) if own else _LEGACY_ROUTE[env]())

    def mutate(data):
        secrets = dict(data["secrets"]) if isinstance(data.get("secrets"), dict) else {}
        bindings = dict(data["key_bindings"]) if isinstance(data.get("key_bindings"), dict) else {}
        secrets[env] = token
        bindings[env] = route  # 密文和它的接口绑定在同一次写里，不会只剩一半
        data["secrets"], data["key_bindings"] = secrets, bindings

    try:
        _update(mutate)
    except OSError:
        _issue(f"migrate:{env}", f"{env} 没能写进配置文件：旧的 key 原样保留、但这次不会用它发请求，请在设置里重新填一次。")
        return
    if _stored_key(env) != legacy:  # 从磁盘读回来核对，不是信内存里那份
        _issue(f"migrate:{env}", f"{env} 写进配置后读回核对不上：旧的 key 原样保留、但这次不会用它发请求，请在设置里重新填一次。")
        return
    _mirror_key(env, legacy)
    if own:
        _drop_plaintext(env)
    _note_shared(env, shared)

def _drop_plaintext(env: str) -> None:
    if _registry_delete(env):
        _notify_env()
    else:
        _issue(f"plain:{env}", f"{env} 已迁进加密存储，但用户环境变量里的旧明文没能删掉，请手动删除环境变量 {env}。")

def _note_shared(env: str, shared: str) -> None:
    if shared:
        _issue(f"shared:{env}", f"用户环境变量 {LEGACY[env]} 里还有一份明文 key（别的工具可能也在用它，这里不替你删）；"
               "不再需要的话请自己删除。")

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

def _bindings() -> dict:
    """{环境变量名: 这把 key 是为哪个接口 origin 填的}。只记接口地址，不含 key 的任何部分。"""
    return bindings_state() or {}

def bindings_state() -> dict | None:
    """key 绑定记录，区分「没有」和「读不了」：文件不存在 / 没有 key_bindings 字段 = {}（还没绑过），
    文件在但打不开、不是合法 JSON 或 key_bindings 类型不对 = None（读不了，调用方必须拒发，不能当成没绑）。"""
    data, _ = _load_raw()
    if data is None:
        return None
    v = data.get("key_bindings")
    if v is None:
        v = {}
    if not isinstance(v, dict):
        return None
    v = dict(v)
    secrets = data.get("secrets")
    for env in (secrets if isinstance(secrets, dict) else {}):
        v.setdefault(env, "")  # 有密文却没有绑定（手工改坏 / 半截状态）：留一条空记录，keygate 当「读不了」拒发
    return v

def _current_route(env_name: str, jev_provider_now: str | None = None) -> str:
    return jev_route(jev_provider_now or jev_provider()) if env_name == JEV_ENV else draft_route(draft_provider(), draft_base_url())

def key_for_route(env_name: str, route: str) -> str:
    """存的 key，但只在它绑定的就是 [route] 时才给；绑在别的接口上（或没有绑定记录）就当没配，返回空。"""
    bindings = _bindings()
    return "" if env_name in bindings and bindings[env_name] != route else _get_key(env_name)

def require_key_route(env_name: str) -> None:
    """界面层的快速失败（不重试、给一句人话）：这把 key 绑定的接口跟现在选的接口不一致就抛 KeyRouteError。
    真正的边界在网络出口（core/keygate：llm / jev_client 只收 Credential），这里不是唯一一道。
    没有绑定记录的 key 不存在于加密存储里（迁移之前的明文不会被使用），这里只比对有记录的。"""
    bindings = _bindings()
    now = _current_route(env_name)
    if env_name in bindings and bindings[env_name] != now:
        bound = bindings[env_name] or "（记录缺失）"
        raise KeyRouteError(f"接口已换成 {now}，但保存的 key 是给 {bound} 填的，没有发送。请在设置里重新填这个接口的 key。")

def save(relationship_text: str | None = None, context_n: int | None = None, *,
         jev_provider_text: str | None = None, jev_key_text: str | None = None,
         draft_provider_text: str | None = None,
         llm_key_text: str | None = None, draft_model_text: str | None = None,
         draft_base_url_text: str | None = None, reply_target_on: bool | None = None,
         style_text: str | None = None, thinking_on: bool | None = None,
         check_update_on: bool | None = None, debug_view_on: bool | None = None,
         bilingual_lang_text: str | None = None,
         dock_on: bool | None = None, read_images_on: bool | None = None,
         show_gloss_on: bool | None = None) -> None:
    """每个参数为空/None = 保留当前值。新敲的 key 加密后跟它的接口绑定、设置一起在一次原子写里落盘；
    加密失败（SecretError）或写不进去（ConfigWriteError）就什么都没保存、本进程也不用新 key，调用方必须当作没保存。"""
    with _LOCK:
        jev = jev_provider_text if jev_provider_text in _JEV_SOURCES else jev_provider()
        draft = draft_provider_text if draft_provider_text in DRAFT_PROVIDERS else draft_provider()
        # key 跟接口绑定：这次填的 key 绑到这次保存的接口；没重填的 key 保持原来的绑定，换了来源/Base URL 就发不出去，要重填
        new_base = str(_read("draft_base_url") or "") if draft_base_url_text is None else str(draft_base_url_text).strip()
        rows = []
        for env, typed, new_route in (
                (JEV_ENV, jev_key_text, jev_route(jev)),
                (LLM_ENV, llm_key_text, draft_route(draft, new_base))):
            typed = (typed or "").strip()
            rows.append((env, typed, secretstore.protect(typed, env) if typed else "",  # 失败就抛，什么都没动
                         new_route))
        n = context() if context_n is None else max(3, min(100, int(context_n)))
        # 空串 = 清掉，None = 原样留着（读原始字段，别读补过默认值的那个）
        keep = lambda new, name: str(_read(name) or "") if new is None else str(new).strip()
        flag = lambda new, now: now() if new is None else bool(new)
        fields = {
            # 关系为空 = 只改别的开关（调试视图那种单项保存），别把它写没了
            "relationship": relationship_text or relationship(), "context": n,
            "style": keep(style_text, "style"),
            "jev_provider": jev,
            "draft_provider": draft, "draft_model": keep(draft_model_text, "draft_model"),
            "draft_base_url": keep(draft_base_url_text, "draft_base_url"),
            "reply_target": flag(reply_target_on, reply_target),
            "thinking": flag(thinking_on, thinking),
            "check_update": flag(check_update_on, check_update),
            "debug_view": flag(debug_view_on, debug_view),
            "dock": flag(dock_on, dock),
            "read_images_optin": flag(read_images_on, read_images),
            "show_gloss": flag(show_gloss_on, show_gloss),
            "bilingual_lang": keep(bilingual_lang_text, "bilingual_lang"),
        }

        def mutate(data):
            # 在读到的整份配置上原地改：别的模块管的字段（chat_rel / chat_meta / win_*）和以后新加的字段原样留着
            secrets = dict(data["secrets"]) if isinstance(data.get("secrets"), dict) else {}
            bindings = dict(data["key_bindings"]) if isinstance(data.get("key_bindings"), dict) else {}
            for env, typed, token, new_route in rows:
                if typed:
                    secrets[env] = token
                    bindings[env] = new_route
            data.update(fields)
            data["secrets"], data["key_bindings"] = secrets, bindings

        _update(mutate)
        for env, typed, *_ in rows:  # 只有真写成了才让本进程用新 key
            if typed:
                _mirror_key(env, typed)
                _clear_issue(f"decrypt:{env}")

def try_save(**fields) -> bool:
    """单项开关的保存（贴靠、调试视图）：写成了 True，没写成 False，不抛，开关本身的效果不受影响。"""
    try:
        save(**fields)
        return True
    except (OSError, SecretError):
        return False
