# -*- coding: utf-8 -*-
"""S0-D：key 只发往它被绑定的接口。换来源 / Base URL 之后不盲目复用旧 key。
配置、环境变量、注册表全部换成临时假的，不碰真实 config.json 和 HKCU\\Environment。"""
import json
import queue
from unittest import mock

import pytest

import main
from core.convo import Coordinator
from app import settings
from core import jev_client
from core.providers import JEV_ENV, LEGACY, LLM_ENV, draft_route, jev_route

FAKE_KEY = "FAKEKEY-not-a-real-credential-123456"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """假配置文件 + 假环境（key 只在这个 dict 里）；返回这个 dict。"""
    monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
    store = {}
    monkeypatch.setattr(settings, "_mirror_key", lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(settings, "_read_env", lambda name: store.get(name, ""))
    monkeypatch.setattr(settings, "_notify_env", lambda: None)
    return store


def _bindings(tmp_path_cfg):
    with open(tmp_path_cfg, encoding="utf-8") as f:
        return json.load(f).get("key_bindings", {})


class TestRouteIds:
    def test_same_origin_ignores_path_case_and_default_port(self):
        a = draft_route("custom_openai", "https://API.Example.com/v1/")
        assert a == draft_route("custom_openai", "https://api.example.com:443/other")
        assert a != draft_route("custom_openai", "http://api.example.com/v1")
        assert a != draft_route("custom_openai", "https://api.example.com:8443/v1")

    def test_named_providers_have_distinct_routes(self):
        assert draft_route("deepseek") != draft_route("openrouter")
        assert draft_route("deepseek") == "https://api.deepseek.com"

    def test_provider_without_a_base_falls_back_to_its_name(self):
        assert draft_route("gemini") == "provider:gemini"
        assert draft_route("custom_openai", "") == "provider:custom_openai"

    def test_jev_routes(self):
        assert jev_route("openrouter") != jev_route("typesafe")


class TestBindingOnSave:
    def test_typed_key_is_bound_to_the_saved_route(self, env, tmp_path):
        settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY)
        assert _bindings(settings._CONFIG)[LLM_ENV] == "https://api.deepseek.com"
        settings.require_key_route(LLM_ENV)  # 不抛
        assert FAKE_KEY not in open(settings._CONFIG, encoding="utf-8").read()

    def test_switching_provider_without_retyping_blocks_the_old_key(self, env):
        settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY)
        settings.save(draft_provider_text="openrouter")
        with pytest.raises(settings.KeyRouteError) as e:
            settings.require_key_route(LLM_ENV)
        assert FAKE_KEY not in str(e.value)
        assert settings.key_for_route(LLM_ENV, draft_route("openrouter")) == ""
        assert settings.key_for_route(LLM_ENV, draft_route("deepseek")) == FAKE_KEY

    def test_retyping_the_key_for_the_new_provider_unblocks(self, env):
        settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY)
        settings.save(draft_provider_text="openrouter", llm_key_text="FAKEKEY-second")
        settings.require_key_route(LLM_ENV)

    def test_changing_a_custom_base_url_blocks_the_old_key(self, env):
        settings.save(draft_provider_text="custom_openai", draft_base_url_text="https://a.example/v1",
                      llm_key_text=FAKE_KEY)
        settings.save(draft_base_url_text="https://b.example/v1")
        with pytest.raises(settings.KeyRouteError):
            settings.require_key_route(LLM_ENV)

    def test_a_plaintext_legacy_key_is_never_bound_or_used_by_saving_other_settings(self, env):
        # 升级前存的明文 key：没有迁成功之前不使用，保存别的设置也不会把它悄悄绑到某个接口上（S3.1）
        env[LEGACY[LLM_ENV]] = FAKE_KEY
        settings.save(draft_provider_text="openrouter")
        assert _bindings(settings._CONFIG) == {}
        assert settings.llm_key() == ""


class TestNothingIsSentAfterASwitch:
    """走 main.analyze_bg：路由对不上时，模型调用一次都不发生，界面只收到不含 key 的错误。"""

    def _run(self, monkeypatch):
        sent = []
        monkeypatch.setattr(main, "analyze_bilingual", lambda *a, **k: sent.append((a, k)) or {})
        q = queue.Queue()
        monkeypatch.setattr(main, "results", q)
        monkeypatch.setattr(main, "coord", Coordinator())
        main.analyze_bg(main.coord.begin("t", [("her", "hi", None)]))
        return sent, q

    def test_blocked_after_provider_switch(self, env, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY, bilingual_on=True)
        settings.save(draft_provider_text="openrouter")
        sent, q = self._run(monkeypatch)
        assert sent == []
        kind, text, *_ = q.get_nowait()
        assert kind == "err" and FAKE_KEY not in text

    def test_allowed_when_route_matches(self, env, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY, bilingual_on=True)
        sent, q = self._run(monkeypatch)
        assert len(sent) == 1
        assert q.get_nowait()[0] == "ok"


class TestErrorBodiesStayClean:
    def test_key_echoed_by_a_fake_server_is_redacted(self, monkeypatch):
        monkeypatch.setenv(LLM_ENV, FAKE_KEY)
        exc = RuntimeError(f"Incorrect API key provided: {FAKE_KEY}")
        with pytest.raises(jev_client.JevError) as e:
            jev_client._fail(exc, "起草")
        assert FAKE_KEY not in str(e.value)
