# -*- coding: utf-8 -*-
"""S5-A 单一接缝「一次图片回复」：假消息 + 假图 + 假服务商（模型列表和 chat 都是替身），只看外部行为——
发没发图、发了几次、图片什么时候不能再用。"""
import pytest

from core import capability, draft, engine, retry
from core.convo import Coordinator
from core.jev_client import JevError
from core.keygate import Credential, destination_of
from core.route import ReplyPlan, ReplyRoute

DEST = destination_of("openai", "https://api.deepseek.com")
ROUTE = ReplyRoute("deepseek", "openai", "https://api.deepseek.com", "m", DEST, Credential("k", DEST))
MSGS = [("her", "[图片]", None)]
IMG = "QkFTRTY0SU1H"
ENVELOPE = ('{"lang": "中文", "analysis": "一张图", "replies": ["a", "b", "c"]}')


class Provider:
    """能力查询和 chat 的替身；记下每次 chat 带没带图。"""

    def __init__(self, monkeypatch, state, replies):
        self.sent, self.cap_asked, self.replies = [], 0, list(replies)
        monkeypatch.setattr(draft.CAPABILITIES, "image_input", self._cap(state))
        monkeypatch.setattr(draft, "chat", self._chat)

    def _cap(self, state):
        def ask(route):
            self.cap_asked += 1
            return capability.Capability(state, capability.PROVIDER_DECLARED)
        return ask

    def _chat(self, *a, image=None, **k):
        self.sent.append(image)
        r = self.replies[min(len(self.sent) - 1, len(self.replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def _reply(**kw):
    return engine.analyze_bilingual(MSGS, ReplyPlan(ROUTE, "friends", 10, "", False, **kw.pop("plan", {})), **kw)


class TestOneImageReply:
    def test_supported_model_gets_the_picture_with_one_request(self, monkeypatch):
        p = Provider(monkeypatch, capability.SUPPORTED, [ENVELOPE])
        out = _reply(image=IMG)
        assert p.sent == [IMG] and out["image_use"] == "attached" and len(out["candidates"]) == 3

    def test_owner_switch_off_sends_no_picture_and_does_not_ask_the_provider(self, monkeypatch):
        p = Provider(monkeypatch, capability.SUPPORTED, [ENVELOPE])
        out = _reply(image=IMG, plan={"image_enabled": False})
        assert p.sent == [None] and p.cap_asked == 0 and out["image_use"] == "text_disabled"

    def test_unsupported_model_is_sent_text_only(self, monkeypatch):
        p = Provider(monkeypatch, capability.UNSUPPORTED, [ENVELOPE])
        out = _reply(image=IMG)
        assert p.sent == [None] and out["image_use"] == "text_unsupported"

    def test_unknown_capability_tries_the_picture_and_keeps_the_answer(self, monkeypatch):
        p = Provider(monkeypatch, capability.UNKNOWN, [ENVELOPE])
        out = _reply(image=IMG)
        assert p.sent == [IMG] and out["image_use"] == "attached"

    def test_unknown_and_explicit_refusal_falls_back_to_text_exactly_once(self, monkeypatch):
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("不支持", 400, image_unsupported=True), ENVELOPE])
        out = _reply(image=IMG)
        assert p.sent == [IMG, None] and out["image_use"] == "fell_back"

    @pytest.mark.parametrize("status", [401, 403, 429, 503])
    def test_other_failures_are_never_turned_into_a_text_fallback(self, monkeypatch, status):
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("x", status), ENVELOPE])
        with pytest.raises(JevError):
            _reply(image=IMG)
        assert p.sent == [IMG]

    def test_a_generation_that_went_stale_makes_no_fallback_call(self, monkeypatch):
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("不支持", 400, image_unsupported=True), ENVELOPE])
        with pytest.raises(JevError):
            _reply(image=IMG, egress=draft.ImageEgress(still_wanted=lambda: len(p.sent) == 0))
        assert p.sent == [IMG]

    def test_no_picture_leaves_the_text_request_alone(self, monkeypatch):
        p = Provider(monkeypatch, capability.SUPPORTED, [ENVELOPE])
        out = _reply()
        assert p.sent == [None] and p.cap_asked == 0 and out["image_use"] == "none"

    def test_the_chinese_path_reports_the_same_decision(self, monkeypatch):
        p = Provider(monkeypatch, capability.SUPPORTED, [ENVELOPE])
        out = engine.analyze_bilingual([("her", "看这个", None)], ReplyPlan(ROUTE, "friends", 10, "", False,
                                                                           image_enabled=False), image=IMG)
        assert p.sent == [None] and out["image_use"] == "text_disabled"


class TestImageEgressClosure:
    """S5.1：一次生成里图片最多出去一次（被明确拒绝后粘住纯文字）；含糊的 4xx 不当成「不认图」。"""

    @pytest.mark.parametrize("status", [400, 404, 413, 422])
    def test_an_ambiguous_4xx_is_never_read_as_the_model_taking_no_images(self, monkeypatch, status):
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("x", status), ENVELOPE])
        with pytest.raises(JevError):
            _reply(image=IMG)
        assert p.sent == [IMG]

    def _run_with_retry(self, p, is_live=lambda: True):
        egress = draft.ImageEgress(still_wanted=is_live)
        value = retry.run(lambda _n: _reply(image=IMG, egress=egress), is_live, pause=lambda n: None)
        return value, egress

    def test_after_an_explicit_refusal_a_transient_text_failure_never_uploads_the_picture_again(self, monkeypatch):
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("no", 400, image_unsupported=True),
                                                       JevError("busy", 503), ENVELOPE])
        out, egress = self._run_with_retry(p)
        assert p.sent == [IMG, None, None] and egress.uploads == 1 and out["image_use"] == "fell_back"

    def test_a_stale_generation_makes_no_text_retry_after_the_refusal(self, monkeypatch):
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("no", 400, image_unsupported=True),
                                                       JevError("busy", 503), ENVELOPE])
        live = iter([True, False])  # 回退那次问一次（还要），重试前问一次（已过期）
        with pytest.raises(JevError):
            self._run_with_retry(p, is_live=lambda: next(live, False))
        assert p.sent == [IMG, None]

    def test_a_transient_failure_before_any_refusal_still_retries_with_the_picture(self, monkeypatch):
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("busy", 429), ENVELOPE])
        out, egress = self._run_with_retry(p)
        assert p.sent == [IMG, IMG] and out["image_use"] == "attached"


class TestImageLifetimeIsTheReplySession:
    def _chat_with_image(self):
        c = Coordinator()
        c.messages("WhatsApp · A", [("her", None, "[图片]", IMG)])
        return c

    def test_a_new_message_ends_the_session(self):
        c = self._chat_with_image()
        c.messages("WhatsApp · A", [("her", None, "在吗")])
        assert c.chat("WhatsApp · A").image is None

    def test_cancel_ends_the_session(self):
        c = self._chat_with_image()
        c.cancel("WhatsApp · A")
        assert c.chat("WhatsApp · A").image is None

    def test_switching_the_open_chat_ends_the_old_session(self):
        c = self._chat_with_image()
        c.set_open_chat("whatsapp", "WhatsApp · A")
        c.set_open_chat("whatsapp", "WhatsApp · B")
        assert c.chat("WhatsApp · A").image is None

    def test_reopening_the_same_chat_keeps_it(self):
        c = self._chat_with_image()
        c.set_open_chat("whatsapp", "WhatsApp · A")
        c.set_open_chat("whatsapp", "WhatsApp · A")
        assert c.chat("WhatsApp · A").image is not None

    def test_capture_disconnect_and_teardown_release_every_picture(self):
        c = self._chat_with_image()
        c.messages("QQ · B", [("her", None, "[图片]", IMG)])
        c.cancel_all()
        assert c.chat("WhatsApp · A").image is None and c.chat("QQ · B").image is None

    def test_the_accepted_result_does_not_keep_the_picture(self):
        c = self._chat_with_image()
        req = c.begin("WhatsApp · A", [("her", "[图片]", None)], image=IMG)
        assert req.image == IMG
        c.finish(req, {"candidates": ["a"]})
        assert c.chat("WhatsApp · A").result_req.image is None

    def test_a_new_generation_cannot_pick_up_the_old_session_image_after_a_new_message(self):
        c = self._chat_with_image()
        c.messages("WhatsApp · A", [("her", None, "又一条")])
        import main
        assert main.newest_image(c.chat("WhatsApp · A").image, [("her", "x", None)] * 2, True) is None


class TestNoBackdoorsToTheOldPicture:
    def _running_with_queue(self):
        """一次生成在跑、又排了一份带图的（生成期间对方又发了图）。"""
        c = Coordinator()
        c.messages("WhatsApp · A", [("her", None, "[图片]", IMG)])
        first = c.begin("WhatsApp · A", [("her", "[图片]", None)], image=IMG)
        assert c.begin("WhatsApp · A", [("her", "[图片]", None)], image=IMG, plan="p") is None  # 排队
        return c, first

    def _queued_image_after(self, c, first):
        _, nxt = c.finish(first, None)
        return nxt.image if nxt else "no-next"

    def test_a_queued_request_loses_the_picture_when_a_new_message_ends_the_session(self):
        c, first = self._running_with_queue()
        c.messages("WhatsApp · A", [("her", None, "在吗")])
        assert self._queued_image_after(c, first) in (None, "no-next")

    def test_a_queued_request_loses_the_picture_when_the_chat_is_switched_away(self):
        c, first = self._running_with_queue()
        c.set_open_chat("whatsapp", "WhatsApp · A")
        c.set_open_chat("whatsapp", "WhatsApp · B")
        assert self._queued_image_after(c, first) in (None, "no-next")

    def test_a_queued_request_loses_the_picture_on_disconnect(self):
        c, first = self._running_with_queue()
        c.set_open_chat("whatsapp", "WhatsApp · A")
        c.forget_open_chats()
        assert self._queued_image_after(c, first) in (None, "no-next")

    def _shown(self, c):
        req = c.begin("WhatsApp · A", [("her", "[图片]", None)], image=IMG)
        c.finish(req, {"candidates": ["a", "b", "c"]})

    def test_a_reroll_ticket_dies_when_the_session_picture_was_released(self):
        c = self._chat_with_image()
        self._shown(c)
        t = c.reroll_begin("WhatsApp · A", 0, image=IMG)
        assert t is not None and c.reroll_valid(t)
        c.set_open_chat("whatsapp", "WhatsApp · A")
        c.set_open_chat("whatsapp", "WhatsApp · B")
        assert not c.reroll_valid(t)

    def test_a_reroll_ticket_without_a_picture_survives_the_switch(self):
        c = Coordinator()
        c.messages("WhatsApp · A", [("her", None, "hi")])
        self._shown(c)
        t = c.reroll_begin("WhatsApp · A", 0)
        c.set_open_chat("whatsapp", "WhatsApp · A")
        c.set_open_chat("whatsapp", "WhatsApp · B")
        assert c.reroll_valid(t)

    def _chat_with_image(self):
        c = Coordinator()
        c.messages("WhatsApp · A", [("her", None, "[图片]", IMG)])
        return c


class TestImageIsOnlyForAnExplicitUserAction:
    """S5.1：自动生成永远不带图；只有用户点「立即生成」那一次才发图。设置里的开关只是「允许手动用」。"""

    @pytest.fixture
    def app(self, monkeypatch):
        import types

        import main
        c = Coordinator()
        c.messages("WhatsApp · A", [("her", None, "[图片]", IMG)])
        monkeypatch.setattr(main, "coord", c)
        monkeypatch.setattr(main.settings, "read_images", lambda: True)
        monkeypatch.setattr(main.settings, "has_llm_key", lambda: True)
        monkeypatch.setattr(main, "snapshot_plan", lambda t, m: ReplyPlan(ROUTE, "friends", 10, "", False))
        monkeypatch.setattr(main, "target_of", lambda t: None)
        monkeypatch.setattr(main.settings, "reply_target", lambda: False)
        monkeypatch.setattr(main, "ov", types.SimpleNamespace(
            set_busy=lambda *a: None, current_chat=lambda: "WhatsApp · A", set_status=lambda *a, **k: None,
            set_card_pending=lambda *a: None), raising=False)
        started = []
        monkeypatch.setattr(main.threading, "Thread", lambda target, args, daemon: types.SimpleNamespace(
            start=lambda: started.append(args[0])))
        return main, c, started

    def test_an_automatic_generation_carries_no_picture(self, app):
        main, c, started = app
        main.start_analyze("WhatsApp · A", list(c.chat("WhatsApp · A").history))
        assert started[0].image is None

    def test_the_user_pressing_generate_now_may_carry_the_picture(self, app):
        main, c, started = app
        main.generate_now("WhatsApp · A")
        assert started[0].image == IMG

    def test_the_switch_off_still_wins_over_a_manual_request(self, app, monkeypatch):
        main, c, started = app
        monkeypatch.setattr(main.settings, "read_images", lambda: False)
        main.generate_now("WhatsApp · A")
        assert started[0].image is None

    def test_a_reroll_of_a_result_that_never_carried_the_picture_does_not_carry_it(self, app):
        main, c, started = app
        req = c.begin("WhatsApp · A", [("her", "[图片]", None)])
        c.finish(req, {"candidates": ["a", "b", "c"], "image_use": "none"})
        main.reroll_reply(0)
        assert started[0].image is None

    def test_a_reroll_of_a_result_that_did_carry_the_picture_keeps_using_the_session(self, app):
        main, c, started = app
        req = c.begin("WhatsApp · A", [("her", "[图片]", None)], image=IMG)
        c.finish(req, {"candidates": ["a", "b", "c"], "image_use": "attached"})
        main.reroll_reply(0)
        assert started[0].image == IMG
