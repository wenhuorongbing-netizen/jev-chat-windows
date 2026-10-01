# -*- coding: utf-8 -*-
"""S4：模型路由快照 / 能力缓存 / 有界重试 / Jev 判断路径退役，在 Windows 这一侧的原生测试。
共享向量的消费者在 test_contract_v1.py；这里是向量表达不了的那部分（线程里读不读设置、过期代不重试……）。
全部不联网：模型调用、SDK、HTTP 都换成假的。"""
import inspect
import queue
import re
import threading
from pathlib import Path

import pytest

import main
from app import settings
from core import capability, draft, engine, jev_client, llm, retry
from core.convo import Coordinator
from core.jev_client import JevError
from core.keygate import Credential, KeyRouteError, destination_of
from core.providers import LLM_ENV
from core.route import ReplyPlan, ReplyRoute, snapshot_route

FAKE_KEY = "FAKEKEY-not-a-real-credential-123456"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
    store = {}
    monkeypatch.setattr(settings, "_mirror_key", lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(settings, "_read_env", lambda name: store.get(name, ""))
    monkeypatch.setattr(settings, "_notify_env", lambda: None)
    monkeypatch.setenv(LLM_ENV, FAKE_KEY)
    return store


def _run_bg(monkeypatch, fake):
    monkeypatch.setattr(main, "analyze_bilingual", fake)
    q = queue.Queue()
    monkeypatch.setattr(main, "results", q)
    monkeypatch.setattr(main, "coord", Coordinator())
    return q


class TestSnapshotIsImmutableForTheRequest:
    MSGS = [("her", "在吗", None)]

    def test_changing_settings_mid_flight_does_not_change_the_running_request(self, env, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY, draft_model_text="model-a",
                      style_text="短句", thinking_on=False)
        seen = []
        q = _run_bg(monkeypatch, lambda msgs, plan, **kw: seen.append(plan) or {})
        req = main.coord.begin("t", self.MSGS, plan=main.snapshot_plan("t", self.MSGS))
        # 请求发出之后用户改了来源、模型、风格、思考：这一次不受影响，只影响下一次
        settings.save(draft_provider_text="openrouter", llm_key_text="FAKEKEY-second", draft_model_text="model-b",
                      style_text="长句", thinking_on=True)
        main.analyze_bg(req)
        plan = seen[0]
        assert (plan.route.provider, plan.route.model, plan.style, plan.thinking) == ("deepseek", "model-a", "短句", False)
        assert plan.route.base_url == "https://api.deepseek.com"
        assert q.get_nowait()[0] == "ok"
        nxt = main.snapshot_plan("t", self.MSGS)  # 下一次才看到新设置
        assert (nxt.route.provider, nxt.route.model, nxt.thinking) == ("openrouter", "model-b", True)

    def test_credential_destination_is_the_snapshot_destination(self, env):
        settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY)
        route = main.snapshot_plan("t", self.MSGS).route
        assert route.credential is not None
        assert route.credential.route == route.destination == destination_of(route.protocol, route.base_url)

    def test_a_key_bound_elsewhere_gives_a_route_without_a_credential(self, env):
        settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY)
        settings.save(draft_provider_text="openrouter")  # 换来源没重填 key
        route = main.snapshot_plan("t", self.MSGS).route
        assert route.credential is None and route.blocked
        assert FAKE_KEY not in route.blocked
        with pytest.raises(KeyRouteError):
            route.require()

    def test_the_route_cannot_be_edited_after_the_snapshot(self, env):
        route = snapshot_route("deepseek", None, None)
        with pytest.raises(Exception):
            route.model = "other"
        assert "FAKEKEY" not in repr(route)  # credential 不进 repr

    def test_the_background_chain_reads_no_settings(self):
        for fn in (main.analyze_bg, main._reroll_bg, engine.analyze_bilingual, engine.reroll_candidate,
                   draft.draft_candidates, draft.draft_bilingual):
            assert not re.search(r"\bsettings\.", inspect.getsource(fn)), fn.__name__
        for name in ("engine", "route", "capability", "retry", "draft", "llm"):
            text = (ROOT / "core" / f"{name}.py").read_text(encoding="utf-8")
            assert "app.settings" not in text and "from app" not in text, name

    def test_a_blocked_route_sends_nothing_and_reports_without_the_key(self, env, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY)
        settings.save(draft_provider_text="openrouter")
        sent = []
        q = _run_bg(monkeypatch, lambda *a, **k: sent.append(a) or {})
        main.analyze_bg(main.coord.begin("t", self.MSGS, plan=main.snapshot_plan("t", self.MSGS)))
        kind, text, *_ = q.get_nowait()
        assert sent == [] and kind == "err" and FAKE_KEY not in text


class TestStaleGenerationDoesNotRetry:
    MSGS = [("her", "在吗", None)]

    def _plan(self):
        dest = "https://api.deepseek.com"
        return ReplyPlan(ReplyRoute("deepseek", "openai", dest, "m", dest, Credential("k", dest)), "friends", 10, "", False)

    def test_a_cancelled_generation_stops_after_the_first_failure(self, monkeypatch):
        calls = []

        def failing(msgs, plan, **kw):
            calls.append(1)
            main.coord.cancel("t")  # 第一次失败的同时用户取消了
            raise JevError("服务繁忙", 503)

        q = _run_bg(monkeypatch, failing)
        monkeypatch.setattr(retry, "backoff_pause", lambda n: None)
        main.analyze_bg(main.coord.begin("t", self.MSGS, plan=self._plan()))
        assert len(calls) == 1
        assert q.get_nowait()[0] == "err"

    def test_a_live_generation_retries_a_transport_failure(self, monkeypatch):
        calls = []

        def flaky(msgs, plan, **kw):
            calls.append(1)
            if len(calls) < 3:
                raise JevError("服务繁忙", 503)
            return {"candidates": ["a"]}

        q = _run_bg(monkeypatch, flaky)
        monkeypatch.setattr(retry.time, "sleep", lambda s: None)
        main.analyze_bg(main.coord.begin("t", self.MSGS, plan=self._plan()))
        assert len(calls) == 3
        assert q.get_nowait()[0] == "ok"

    def test_an_unusable_answer_is_not_asked_again(self, monkeypatch):
        calls = []

        def bad(msgs, plan, **kw):
            calls.append(1)
            raise draft.invalid_response("模型没有返回 JSON")

        q = _run_bg(monkeypatch, bad)
        monkeypatch.setattr(retry.time, "sleep", lambda s: None)
        main.analyze_bg(main.coord.begin("t", self.MSGS, plan=self._plan()))
        assert len(calls) == 1 and q.get_nowait()[0] == "err"

    def test_the_backoff_doubles_from_half_a_second(self, monkeypatch):
        slept = []
        monkeypatch.setattr(retry.time, "sleep", slept.append)
        retry.backoff_pause(1)
        retry.backoff_pause(2)
        assert slept == [1.0, 2.0]


class TestCapabilityCache:
    def _route(self, base="https://api.example.com/v1", model="m"):
        return ReplyRoute("custom_openai", "openai", base, model, destination_of("openai", base),
                          Credential("k", destination_of("openai", base)))

    BODY = '{"data":[{"id":"m","input_modalities":["text","image"]}]}'

    def test_a_provider_answer_is_cached_and_does_not_leak_to_another_base_or_model(self):
        asked = []
        caps = capability.ModelCapabilities(lambda r: asked.append((r.base_url, r.model)) or self.BODY)
        assert caps.image_input(self._route()).state == capability.SUPPORTED
        assert caps.image_input(self._route()).state == capability.SUPPORTED
        assert len(asked) == 1
        caps.image_input(self._route(base="https://api.other.com/v1"))
        caps.image_input(self._route(model="n"))
        assert len(asked) == 3  # 换地址 / 换模型各问各的，不吃别人的缓存

    def test_unknown_is_never_cached_and_never_becomes_unsupported(self):
        answers = iter([None, '{"data":[{"id":"other"}]}', '{"data":[{"id":"m"}]}', self.BODY])
        caps = capability.ModelCapabilities(lambda r: next(answers))
        for evidence in (capability.FETCH_FAILED, capability.MODEL_NOT_LISTED, capability.FIELD_ABSENT):
            got = caps.image_input(self._route())
            assert (got.state, got.evidence) == (capability.UNKNOWN, evidence)  # 每次都重新问，没有缓存「不知道」
        assert caps.image_input(self._route()).state == capability.SUPPORTED

    def test_the_cache_expires(self):
        clock = [1000.0]
        asked = []
        caps = capability.ModelCapabilities(lambda r: asked.append(1) or self.BODY, now=lambda: clock[0], ttl_s=60)
        caps.image_input(self._route())
        clock[0] += 59
        caps.image_input(self._route())
        assert len(asked) == 1
        clock[0] += 2
        caps.image_input(self._route())
        assert len(asked) == 2

    def test_a_blocked_route_is_never_fetched(self):
        asked = []
        caps = capability.ModelCapabilities(lambda r: asked.append(1) or self.BODY)
        blocked = ReplyRoute("custom_openai", "openai", "https://api.example.com/v1", "m",
                             "https://api.example.com", None, "x")
        assert caps.image_input(blocked).evidence == capability.FETCH_FAILED
        assert asked == []

    def test_no_model_name_decides_anything(self):
        """能力只来自服务商的声明：名字里带 vision / gpt-4o 之类的字样不改变结论。"""
        text = (ROOT / "core" / "capability.py").read_text(encoding="utf-8").lower()
        for word in ("vision", "gpt-4", "claude", "gemini", "qwen-vl"):
            assert word not in text, word


class TestJevJudgmentRuntimeIsRetired:
    def test_modules_and_entry_points_are_gone(self):
        assert not (ROOT / "core" / "questions.py").exists()
        for name in ("ask", "list_models", "jev_destination", "_ask_typesafe", "_ask_openrouter"):
            assert not hasattr(jev_client, name), name
        for name in ("analyze", "_add_usage"):
            assert not hasattr(engine, name), name

    def test_no_production_file_calls_the_old_path(self):
        banned = re.compile(r"core\.questions|from \.?questions|jev_client\.ask|jev_client\.list_models|"
                            r"typesafe_sdk|api/alpha/decisions|JEV_PROVIDERS|OPENROUTER_DECISIONS")
        for path in list((ROOT / "core").glob("*.py")) + list((ROOT / "app").glob("*.py")) + [ROOT / "main.py"]:
            assert not banned.search(path.read_text(encoding="utf-8")), path.name

    def test_old_config_with_jev_fields_still_loads(self, env):
        import json
        Path(settings._CONFIG).write_text(json.dumps({
            "jev_provider": "typesafe", "jev_model": "jev-latest", "bilingual": False,
            "draft_provider": "deepseek"}), encoding="utf-8")
        assert settings.draft_provider() == "deepseek" and settings.jev_provider() == "typesafe"
        settings.save(draft_provider_text="deepseek")  # 保存不崩，老字段原样留着
        assert json.loads(Path(settings._CONFIG).read_text(encoding="utf-8"))["jev_model"] == "jev-latest"


class TestFetchModelsStaysOnTheBoundRoute:
    def test_non_openai_protocols_have_no_capability_adapter(self):
        dest = destination_of("anthropic", "")
        with pytest.raises(JevError) as e:
            llm.fetch_models_json("anthropic", "", Credential("k", dest))
        assert e.value.kind == "unsupported"
        caps = capability.ModelCapabilities(lambda r: llm.fetch_models_json(
            r.protocol, r.base_url, r.credential))
        route = ReplyRoute("anthropic", "anthropic", "", "claude-x", dest, Credential("k", dest))
        cap = caps.image_input(route)
        assert cap.state == capability.UNKNOWN  # 不知道，不是不支持


def test_threads_do_not_share_a_mutable_route(env):
    """快照被几个线程同时读，拿到的都是同一个不可变对象。"""
    settings.save(draft_provider_text="deepseek", llm_key_text=FAKE_KEY)
    plan = main.snapshot_plan("t", [("her", "x", None)])
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(plan.route.model)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(seen)) == 1


class TestChinesePathFollowsTheReplyContract:
    """中文起草与外语路径同一个严格解析、不追问补条数（reply_outcome.json 规则 3 / reply_parse.json）。"""

    def _route(self):
        dest = destination_of("openai", "https://api.deepseek.com")
        return ReplyRoute("deepseek", "openai", "https://api.deepseek.com", "m", dest, Credential(FAKE_KEY, dest))

    def _draft(self, monkeypatch, reply):
        calls = []
        monkeypatch.setattr(draft, "chat", lambda *a, **k: calls.append(1) or reply)
        try:
            return draft.draft_candidates([("her", "明天几点", None)], "friends", self._route()), calls
        except JevError as e:
            return e, calls

    def test_fewer_than_three_stay_fewer_with_exactly_one_provider_call(self, monkeypatch):
        out, calls = self._draft(monkeypatch, '{"analysis": "约时间", "replies": ["三点", "四点"]}')
        assert out == ["三点", "四点"] and len(calls) == 1

    @pytest.mark.parametrize("reply,kind", [
        ("好的，我来回复", "invalid_response"),
        ('{"replies":[{"text":"abc"}', "invalid_response"),
        ('{"analysis": "对方在约饭"}', "invalid_response"),
        ('["a", "b", "c"]', "invalid_response"),
    ])
    def test_prose_malformed_or_analysis_only_never_becomes_a_candidate(self, monkeypatch, reply, kind):
        out, calls = self._draft(monkeypatch, reply)
        assert isinstance(out, JevError) and out.kind == kind and len(calls) == 1
