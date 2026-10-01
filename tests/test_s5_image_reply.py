# -*- coding: utf-8 -*-
"""S5-A 单一接缝「一次图片回复」：假消息 + 假图 + 假服务商（模型列表和 chat 都是替身），只看外部行为——
发没发图、发了几次、图片什么时候不能再用。"""
import pytest

from core import capability, draft, engine
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
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("不支持", 400), ENVELOPE])
        out = _reply(image=IMG)
        assert p.sent == [IMG, None] and out["image_use"] == "fell_back"

    @pytest.mark.parametrize("status", [401, 403, 429, 503])
    def test_other_failures_are_never_turned_into_a_text_fallback(self, monkeypatch, status):
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("x", status), ENVELOPE])
        with pytest.raises(JevError):
            _reply(image=IMG)
        assert p.sent == [IMG]

    def test_a_generation_that_went_stale_makes_no_fallback_call(self, monkeypatch):
        p = Provider(monkeypatch, capability.UNKNOWN, [JevError("不支持", 400), ENVELOPE])
        with pytest.raises(JevError):
            _reply(image=IMG, still_wanted=lambda: len(p.sent) == 0)
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
