# -*- coding: utf-8 -*-
"""S0.1：key 只发往它被绑定的接口，而且是在网络出口这一层守住——
不管谁直接调用底层客户端（draft / llm / jev_client），都不需要「记得先调 require_key_route」。
另外：供应商的报错正文不进任何提示文案。传输层全部换成假的，不联网、不碰真实 config.json / 注册表。"""
import io
import json
import types
import urllib.error
import urllib.request

import pytest

from app import settings
from core import draft, jev_client, keygate, llm
from core.keygate import Credential, KeyRouteError, destination_of, release
from core.providers import JEV_ENV, LLM_ENV, draft_route, jev_route

FAKE_KEY = "FAKEKEY-not-a-real-credential-123456"
SHORT_KEY = "k123"  # 比任何「至少 N 位才打码」的门槛都短
CHAT_MARKER = "CHATMARKER-老王说明晚八点在老地方见"
MSGS = [("her", "在吗", None)]


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """假 config.json（只放绑定）+ 假环境变量里的 key。返回 bind(env, route) 用来写绑定。"""
    path = tmp_path / "c.json"
    monkeypatch.setattr(settings, "_CONFIG", str(path))
    for name in ("JEV_API_KEY", "LLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("JEV_API_KEY", FAKE_KEY)
    data = {}

    def bind(env, route):
        data.setdefault("key_bindings", {})[env] = route
        path.write_text(json.dumps(data), encoding="utf-8")

    return bind


class Transport:
    """记下有没有人向 SDK 构造过客户端、带的是什么 key、连的是什么地址。"""

    def __init__(self):
        self.built = []
        self.calls = 0

    def client_factory(self, **kw):
        self.built.append(kw)

        def create(**_):
            self.calls += 1
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content='{"analysis":"x","replies":["甲","乙","丙"]}'))])
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
            models=types.SimpleNamespace(list=lambda **_: [types.SimpleNamespace(id="m")]))


@pytest.fixture()
def transport(monkeypatch):
    import openai
    t = Transport()
    monkeypatch.setattr(openai, "OpenAI", t.client_factory)
    return t


class TestCredential:
    def test_repr_and_str_never_show_the_key(self):
        c = Credential(FAKE_KEY, "https://api.deepseek.com")
        assert FAKE_KEY not in repr(c) and FAKE_KEY not in str(c)

    def test_release_refuses_a_bare_string(self):
        with pytest.raises(KeyRouteError):
            release(FAKE_KEY, "https://api.deepseek.com")

    def test_release_refuses_another_destination(self):
        with pytest.raises(KeyRouteError) as e:
            release(Credential(FAKE_KEY, "https://api.deepseek.com"), "https://evil.example")
        assert FAKE_KEY not in str(e.value)

    def test_release_gives_the_key_to_the_bound_destination(self):
        assert release(Credential(FAKE_KEY, "https://api.deepseek.com"), "https://api.deepseek.com") == FAKE_KEY

    def test_destination_ignores_path_case_default_port(self):
        assert destination_of("openai", "https://API.DeepSeek.com:443/v1/") == "https://api.deepseek.com"
        assert destination_of("openai", "") == "https://api.openai.com"  # SDK 默认地址
        assert destination_of("gemini", "") == "provider:gemini"

    def test_destination_agrees_with_the_route_settings_binds(self):
        for provider, spec in __import__("core.providers", fromlist=["x"]).DRAFT_PROVIDERS.items():
            if spec.base:
                assert destination_of(spec.protocol, spec.base) == draft_route(provider)
        assert destination_of("gemini", "") == draft_route("gemini")
        assert jev_client.jev_destination("openrouter") == jev_route("openrouter")
        assert jev_client.jev_destination("typesafe") == jev_route("typesafe")


class TestLowLevelClientsRefuseWithoutTheGuard:
    """不经过 main，不调 require_key_route：直接打底层客户端。"""

    def test_llm_chat_with_a_bare_string_key_sends_nothing(self, transport):
        with pytest.raises(KeyRouteError):
            llm.chat("openai", "https://api.deepseek.com", FAKE_KEY, "m", "S", ["U"])
        assert transport.built == []

    def test_llm_chat_with_a_credential_for_another_origin_sends_nothing(self, transport):
        cred = Credential(FAKE_KEY, destination_of("openai", "https://api.deepseek.com"))
        with pytest.raises(KeyRouteError):
            llm.chat("openai", "https://evil.example/v1", cred, "m", "S", ["U"])
        assert transport.built == [] and transport.calls == 0

    def test_llm_list_models_with_a_credential_for_another_origin_sends_nothing(self, transport):
        cred = Credential(FAKE_KEY, destination_of("openai", "https://api.deepseek.com"))
        with pytest.raises(KeyRouteError):
            llm.list_models("openai", "https://evil.example/v1", cred)
        assert transport.built == []

    def test_llm_chat_to_the_bound_origin_sends_exactly_that_key(self, transport):
        cred = Credential(FAKE_KEY, destination_of("openai", "https://api.deepseek.com"))
        assert llm.chat("openai", "https://api.deepseek.com", cred, "m", "S", ["U"])
        assert transport.built[0]["api_key"] == FAKE_KEY
        assert transport.built[0]["base_url"] == "https://api.deepseek.com"

    def test_draft_candidates_bound_elsewhere_sends_nothing(self, cfg, transport):
        cfg(LLM_ENV, draft_route("deepseek"))
        with pytest.raises(KeyRouteError) as e:
            draft.draft_candidates(MSGS, "friends", provider="custom_openai", base_url="https://evil.example/v1")
        assert FAKE_KEY not in str(e.value)
        assert transport.built == []

    def test_draft_candidates_provider_switched_sends_nothing(self, cfg, transport):
        cfg(LLM_ENV, draft_route("deepseek"))
        with pytest.raises(KeyRouteError):
            draft.draft_candidates(MSGS, "friends", provider="openrouter")
        assert transport.built == []

    def test_draft_bilingual_bound_elsewhere_sends_nothing(self, cfg, transport):
        cfg(LLM_ENV, draft_route("deepseek"))
        with pytest.raises(KeyRouteError):
            draft.draft_bilingual([("her", "Wie geht's?", None)], "friends",
                                  provider="custom_openai", base_url="https://evil.example/v1")
        assert transport.built == []

    def test_draft_bilingual_is_not_swallowed_by_the_image_fallback(self, cfg, transport):
        """带图失败会去掉图重发；KeyRouteError 不能被这条重试吃掉。"""
        cfg(LLM_ENV, draft_route("deepseek"))
        with pytest.raises(KeyRouteError):
            draft.draft_bilingual([("her", "Wie geht's?", None)], "friends", provider="openrouter",
                                  image="AAAA")
        assert transport.built == []

    def test_draft_to_the_bound_origin_goes_through(self, cfg, transport):
        cfg(LLM_ENV, draft_route("deepseek"))
        out = draft.draft_candidates(MSGS, "friends", provider="deepseek")
        assert out == ["甲", "乙", "丙"]
        assert transport.built[0]["api_key"] == FAKE_KEY
        assert transport.built[0]["base_url"] == "https://api.deepseek.com"

    def test_an_unbound_key_sends_nothing_even_on_the_route_it_was_meant_for(self, cfg, transport):
        with pytest.raises(KeyRouteError):  # 没有绑定记录 = 证明不了该发往哪（S3.1 起不再放行）
            draft.draft_candidates(MSGS, "friends", provider="deepseek")
        assert transport.built == []

    def test_jev_ask_bound_to_the_other_source_sends_nothing(self, cfg, monkeypatch):
        cfg(JEV_ENV, jev_route("typesafe"))
        opened = []
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: opened.append(a))
        with pytest.raises(KeyRouteError):
            jev_client.ask({"chat": {}}, {}, provider="openrouter")
        assert opened == []

    def test_jev_ask_typesafe_bound_to_openrouter_never_builds_the_sdk_client(self, cfg, monkeypatch):
        import typesafe_sdk
        cfg(JEV_ENV, jev_route("openrouter"))
        built = []
        monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", lambda **kw: built.append(kw))
        with pytest.raises(KeyRouteError):
            jev_client.ask({"chat": {}}, {}, provider="typesafe")
        assert built == []

    def test_jev_list_models_with_a_foreign_credential_sends_nothing(self, monkeypatch):
        opened = []
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: opened.append(a))
        with pytest.raises(KeyRouteError):
            jev_client.list_models("openrouter", Credential(FAKE_KEY, "https://api.typesafe.ai"))
        with pytest.raises(KeyRouteError):
            jev_client.list_models("openrouter", FAKE_KEY)
        assert opened == []


class TestNoRawProviderErrorBody:
    def _assert_clean(self, text, *secrets):
        for s in secrets:
            assert s not in text, f"'{s}' leaked in: {text}"

    def _sdk_error(self, monkeypatch, status, message):
        import openai

        class Boom(Exception):
            pass

        boom = Boom(message)
        if status is not None:
            boom.status_code = status

        def explode(**_):
            raise boom
        monkeypatch.setattr(openai, "OpenAI", explode)

    @pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503, None])
    def test_sdk_error_text_never_reaches_the_message(self, monkeypatch, status):
        body = (f"{CHAT_MARKER} key={SHORT_KEY} {FAKE_KEY} sk-or-v1-abcdef123456 "
                "Bearer abcdef1234567890 AIzaSyA-fake-google-key")
        self._sdk_error(monkeypatch, status, body)
        cred = Credential(SHORT_KEY, destination_of("openai", "https://api.deepseek.com"))
        with pytest.raises(jev_client.JevError) as e:
            llm.chat("openai", "https://api.deepseek.com", cred, "m", "S", ["U"])
        self._assert_clean(str(e.value), "CHATMARKER", "老王", SHORT_KEY, FAKE_KEY, "abcdef", "AIza")
        assert e.value.status == status

    def test_a_401_still_tells_the_user_what_to_do(self, monkeypatch):
        self._sdk_error(monkeypatch, 401, f"Incorrect API key provided: {FAKE_KEY}")
        cred = Credential(FAKE_KEY, destination_of("openai", "https://api.deepseek.com"))
        with pytest.raises(jev_client.JevError) as e:
            llm.chat("openai", "https://api.deepseek.com", cred, "m", "S", ["U"])
        assert "密钥被拒" in str(e.value)

    def test_openrouter_http_error_body_is_not_read_into_the_message(self, cfg, monkeypatch):
        cfg(JEV_ENV, jev_route("openrouter"))
        body = f'{{"error":{{"message":"{CHAT_MARKER} {FAKE_KEY}"}}}}'.encode("utf-8")

        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, io.BytesIO(body))
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        with pytest.raises(jev_client.JevError) as e:
            jev_client.ask({"chat": {}}, {}, provider="openrouter")
        self._assert_clean(str(e.value), "CHATMARKER", "老王", FAKE_KEY)
        assert "HTTP 422" in str(e.value)

    def test_openrouter_key_probe_error_body_is_not_read(self, monkeypatch):
        body = f'{{"error":"{CHAT_MARKER}"}}'.encode("utf-8")

        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad", {}, io.BytesIO(body))
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        cred = Credential(FAKE_KEY, jev_route("openrouter"))
        with pytest.raises(jev_client.JevError) as e:
            jev_client.list_models("openrouter", cred)
        self._assert_clean(str(e.value), "CHATMARKER", "老王")

    def test_connection_failure_reports_the_type_only(self, cfg, monkeypatch):
        cfg(JEV_ENV, jev_route("openrouter"))

        def urlopen(req, timeout=None):
            raise urllib.error.URLError(OSError(f"cannot reach {CHAT_MARKER} {FAKE_KEY}"))
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(jev_client.time, "sleep", lambda s: None)
        with pytest.raises(jev_client.JevError) as e:
            jev_client.ask({"chat": {}}, {}, provider="openrouter")
        self._assert_clean(str(e.value), "CHATMARKER", "老王", FAKE_KEY)

    def test_a_model_reply_that_is_not_json_is_not_echoed(self):
        with pytest.raises(jev_client.JevError) as e:
            draft._parse_bilingual(f"我不给 JSON，但我复述一下：{CHAT_MARKER}")
        self._assert_clean(str(e.value), "CHATMARKER", "老王")

    def test_hint_for_is_always_our_own_text(self):
        for s in (400, 401, 402, 403, 404, 422, 429, 500, 529, 599, 418, None):
            assert jev_client.hint_for(s)

    def test_unparseable_draft_reply_is_not_echoed(self):
        with pytest.raises(jev_client.JevError) as e:
            draft._parse_candidates("")  # 解析不出任何候选
        self._assert_clean(str(e.value), "CHATMARKER", "老王")
        with pytest.raises(jev_client.JevError) as e:
            draft._parse_three(CHAT_MARKER)  # 只有一条：strict 版抛，消息里不能带原文
        self._assert_clean(str(e.value), "CHATMARKER", "老王")


class TestKeyWithControlCharacters:
    def test_a_key_with_a_newline_never_becomes_a_credential(self):
        with pytest.raises(KeyRouteError) as e:
            Credential("abc\ndef", "https://api.deepseek.com")
        assert "abc" not in str(e.value) and "def" not in str(e.value)
        with pytest.raises(KeyRouteError):
            keygate.typed_credential("abc\x00def", "https://api.deepseek.com")
