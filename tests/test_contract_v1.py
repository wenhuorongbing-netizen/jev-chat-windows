# -*- coding: utf-8 -*-
"""共享契约 contracts/jev/v1 在 Windows 这一侧的消费者。向量文件跟 Android 仓库逐字节一份；
两边的测试各读同一批 JSON，谁改了行为谁就先红。改向量的规矩见 contracts/jev/v1/README.md。"""
import hashlib
import json
import pathlib

import pytest

from app import settings
from core import draft, jev_client, keygate
from core.fill_guard import FILL_SUPPORT, check_fresh
from core.keygate import BindingState, KeyRouteError, stored_credential
from core.providers import JEV_ENV, _origin

V1 = pathlib.Path(__file__).resolve().parent.parent / "contracts" / "jev" / "v1"


def _load(name):
    return json.loads((V1 / name).read_text(encoding="utf-8"))


def test_manifest_matches_files():
    """向量文件没被改坏、也没多出没登记的：清单里的 sha256 跟磁盘上逐字节一致。"""
    listed = {}
    for line in (V1 / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition(" *")
        listed[name] = digest
    on_disk = {p.name for p in V1.iterdir() if p.is_file() and p.name not in ("MANIFEST.sha256", ".gitattributes")}
    assert set(listed) == on_disk
    for name, digest in listed.items():
        assert hashlib.sha256((V1 / name).read_bytes()).hexdigest() == digest, name


class TestOrigin:
    @pytest.mark.parametrize("case", _load("origin.json")["origin_cases"], ids=lambda c: c["name"])
    def test_origin(self, case):
        assert (_origin(case["url"]) or None) == case["origin"]

    @pytest.mark.parametrize("case", _load("origin.json")["same_origin_cases"], ids=lambda c: c["name"])
    def test_same_origin(self, case):
        a, b = _origin(case["a"]), _origin(case["b"])
        assert bool(a and b and a == b) is case["same"]


@pytest.mark.parametrize("case", _load("http_hint.json")["cases"], ids=lambda c: str(c["status"]))
def test_http_hint(case):
    assert jev_client.hint_for(case["status"]) == case["hint"]


class TestReplyParse:
    DOC = _load("reply_parse.json")

    @pytest.mark.parametrize("case", DOC["cases"], ids=lambda c: c["name"])
    def test_case(self, case):
        if "error" in case:
            with pytest.raises(draft.JevError) as e:
                draft._parse_bilingual(case["content"])
            assert str(e.value) == self.DOC["errors"][case["error"]]
            if "must_not_leak" in case:
                assert case["must_not_leak"] not in str(e.value)
        else:
            got = draft._parse_bilingual(case["content"])
            assert {k: got[k] for k in case["expect"]} == case["expect"]


class TestFillVerdict:
    @pytest.mark.parametrize("case", _load("fill_verdict.json")["cases"], ids=lambda c: c["name"])
    def test_case(self, case):
        target, live = case["target"], case["live"]
        newest = tuple(target["messages"][-1]) if target["messages"] else None
        refusal = check_fresh(target["conversation"], newest, live["conversation"],
                              [tuple(m) for m in live["messages"]], live["on_chat_screen"])
        assert ("deny" if refusal else "allow") == case.get("expect_windows", case["expect"])

    def test_every_divergence_is_explained(self):
        for case in _load("fill_verdict.json")["cases"]:
            if "expect_android" in case or "expect_windows" in case:
                assert case.get("divergence"), case["name"]


class TestCredentialBinding:
    DOC = _load("credential_binding.json")

    def test_states_are_exactly_the_contract(self):
        assert {s.value for s in BindingState} == set(self.DOC["states"])

    @pytest.mark.parametrize("case", DOC["cases"], ids=lambda c: c["name"])
    def test_case(self, case, tmp_path, monkeypatch):
        binding = case["binding"]
        cfg = tmp_path / "c.json"
        monkeypatch.setattr(settings, "_CONFIG", str(cfg))
        if binding == "absent":
            cfg.write_text(json.dumps({"relationship": "auto"}), encoding="utf-8")
        elif binding == "corrupt_config":
            cfg.write_text("{not json", encoding="utf-8")
        elif binding == "settings_unavailable":
            monkeypatch.setattr(settings, "bindings_state", lambda: (_ for _ in ()).throw(ImportError("gone")))
        elif binding != "no_config_file":
            cfg.write_text(json.dumps({"key_bindings": {JEV_ENV: binding}}), encoding="utf-8")

        state, _ = keygate.binding_state(JEV_ENV, case["destination"])
        assert state.value == case["state"]
        assert keygate.SENDS[state] is case["send"]
        if case["send"]:
            assert stored_credential(JEV_ENV, "FAKEKEY", case["destination"]).route == case["destination"]
        else:
            with pytest.raises(KeyRouteError) as e:
                stored_credential(JEV_ENV, "FAKEKEY", case["destination"])
            assert "FAKEKEY" not in str(e.value)

    def test_wrong_typed_binding_is_unavailable_not_legacy(self, tmp_path, monkeypatch):
        """key_bindings 不是对象（被手改坏）算「读不了」，不能当成老 key 放行。"""
        cfg = tmp_path / "c.json"
        monkeypatch.setattr(settings, "_CONFIG", str(cfg))
        cfg.write_text(json.dumps({"key_bindings": ["x"]}), encoding="utf-8")
        assert keygate.binding_state(JEV_ENV, "https://a.example")[0] is BindingState.BINDING_UNAVAILABLE


class TestFillSupport:
    DOC = _load("fill_support.json")
    WINDOWS = {e["app"]: e["status"] for e in DOC["entries"] if e["platform"] == "windows"}

    def test_matches_the_contract_file(self):
        assert FILL_SUPPORT == self.WINDOWS

    def test_statuses_are_known(self):
        assert set(FILL_SUPPORT.values()) <= set(self.DOC["statuses"])

    def test_fresh_verified_means_there_is_a_uia_parser(self):
        """声明 fresh-verified 的 App 必须真有 UIA 读窗口的解析器（fill_uia 走的那条）；wechat 没有，所以不是。"""
        from app import uia
        for app, status in FILL_SUPPORT.items():
            assert (app in uia.PARSERS) == (status == "fresh-verified"), app


class TestMalformedBindingRecord:
    """有记录但格式不对（数字 / 列表 / 空串）是「读不了」，不是「没有记录」：不能被当成老 key 放行。"""

    @pytest.mark.parametrize("bad", [5, ["https://a.example"], {"x": 1}, "", True])
    def test_wrong_typed_entry_refuses(self, bad, tmp_path, monkeypatch):
        cfg = tmp_path / "c.json"
        monkeypatch.setattr(settings, "_CONFIG", str(cfg))
        cfg.write_text(json.dumps({"key_bindings": {JEV_ENV: bad}}), encoding="utf-8")
        assert keygate.binding_state(JEV_ENV, "https://a.example")[0] is BindingState.BINDING_UNAVAILABLE
        with pytest.raises(KeyRouteError):
            stored_credential(JEV_ENV, "FAKEKEY", "https://a.example")

    def test_null_entry_is_legacy(self, tmp_path, monkeypatch):
        cfg = tmp_path / "c.json"
        monkeypatch.setattr(settings, "_CONFIG", str(cfg))
        cfg.write_text(json.dumps({"key_bindings": {JEV_ENV: None}}), encoding="utf-8")
        assert keygate.binding_state(JEV_ENV, "https://a.example")[0] is BindingState.LEGACY_UNBOUND


# ---------------------------------------------------------------- S4：能力 / 重试 / 回复结局
from core import capability, llm, retry  # noqa: E402
from core.jev_client import JevError  # noqa: E402
from core.keygate import Credential  # noqa: E402
from core.route import ReplyRoute  # noqa: E402


def _route(base, model, key="k"):
    dest = _origin(base) or ""
    return ReplyRoute("p", "openai", base, model, dest, Credential(key, dest) if key else None)


class TestCapabilityContract:
    DOC = _load("capability.json")

    def test_the_evidence_list_is_exactly_the_contract(self):
        assert [capability.PROVIDER_DECLARED, capability.FIELD_ABSENT, capability.MODEL_NOT_LISTED,
                capability.UNKNOWN_SHAPE, capability.FETCH_FAILED, capability.OWNER_DISABLED] == self.DOC["evidence"]

    @pytest.mark.parametrize("case", DOC["parse_cases"], ids=lambda c: c["name"])
    def test_parse(self, case):
        cap = capability.parse_image(case["base"], case["body"], case["model"])
        assert (cap.state, cap.evidence) == (case["expect"]["state"], case["expect"]["evidence"])

    @pytest.mark.parametrize("case", DOC["fetch_cases"], ids=lambda c: c["name"])
    def test_fetch(self, case):
        outcome = case["outcome"]

        def fetch(route):
            if outcome.startswith("http_"):
                raise JevError("x", int(outcome[5:]))
            raise JevError("x", kind=outcome) if outcome in ("timeout", "transport") else AssertionError("never sent")

        caps = capability.ModelCapabilities(fetch)
        cap = caps.image_input(_route("https://api.example.com/v1", "m", key=None if outcome == "no_key" else "k"))
        assert (cap.state, cap.evidence) == (case["expect"]["state"], case["expect"]["evidence"])
        assert case["expect"]["cached"] is False
        assert caps._cache == {}

    @pytest.mark.parametrize("case", DOC["cache_key_cases"], ids=lambda c: c["name"])
    def test_cache_key(self, case):
        a = capability.cache_key(case["a"]["base"], case["a"]["model"])
        b = capability.cache_key(case["b"]["base"], case["b"]["model"])
        assert (a is not None and a == b) is case["same"]

    @pytest.mark.parametrize("case", DOC["image_decision_cases"], ids=lambda c: c["name"])
    def test_image_decision(self, case):
        d = capability.decide_image(case["capability"], case["owner_enabled"], case["client_can_attach"])
        assert {"effective": d.effective, "attach": d.attach, "fall_back_to_text": d.fall_back_to_text,
                "reason": d.reason} == case["expect"]


class TestRetryContract:
    DOC = _load("retry_policy.json")

    def test_the_caps_are_exactly_the_contract(self):
        assert retry.MAX_ATTEMPTS == {k: v["max_attempts"] for k, v in self.DOC["retry"].items()}
        assert sorted(retry.MAX_ATTEMPTS) == sorted(self.DOC["classes"])
        assert {k for k, v in self.DOC["retry"].items() if v["retry"]} == {
            k for k, n in retry.MAX_ATTEMPTS.items() if n > 1}

    @pytest.mark.parametrize("case", DOC["status_cases"], ids=lambda c: str(c["status"]))
    def test_status(self, case):
        assert retry.kind_of_status(case["status"]) == case["class"]
        assert JevError("x", case["status"]).kind == case["class"]

    @staticmethod
    def _failure(code):
        if code.startswith("http_"):
            return JevError("x", int(code[5:]))
        if code == "route_mismatch":
            return KeyRouteError("x")
        if code == "invalid":
            return draft.invalid_response("x")
        return JevError("x", kind=code)

    @pytest.mark.parametrize("case", DOC["failure_cases"], ids=lambda c: c["failure"])
    def test_failure(self, case):
        assert retry.kind_of(self._failure(case["failure"])) == case["class"]

    @pytest.mark.parametrize("case", DOC["flow_cases"], ids=lambda c: c["name"])
    def test_flow(self, case):
        live = list(case["live"])
        seen = {"calls": 0, "pauses": 0}

        def attempt(n):
            seen["calls"] += 1
            code = case["attempts"][n - 1]
            if code != "ok":
                raise self._failure(code)
            return "ok"

        def is_live():
            return live.pop(0) if live else True

        def pause(_):
            seen["pauses"] += 1

        try:
            outcome = retry.run(attempt, is_live, pause)
        except Exception as exc:
            outcome = retry.kind_of(exc)
        assert {"calls": seen["calls"], "outcome": outcome, "pauses": seen["pauses"]} == case["expect"]


class TestReplyOutcomeContract:
    DOC = _load("reply_outcome.json")

    def test_the_failure_texts_are_exactly_the_contract(self):
        assert self.DOC["errors"]["refused"] == llm.REFUSED
        assert {k: v for k, v in self.DOC["errors"].items() if k != "refused"} == {
            "no_json": "模型没有返回 JSON", "bad_json": "模型返回的 JSON 无法解析",
            "no_replies": "模型没有给出候选回复"}

    @pytest.mark.parametrize("case", DOC["cases"], ids=lambda c: c["name"])
    def test_case(self, case):
        env = case["envelope"]

        def go():
            return draft._parse_bilingual(llm.envelope_text(env.get("content"), env.get("refusal"),
                                                            env.get("finish_reason")))

        if "error" in case:
            with pytest.raises(JevError) as e:
                go()
            assert str(e.value) == self.DOC["errors"][case["error"]]
            assert e.value.kind == "invalid_response"
            if "must_not_leak" in case:
                assert case["must_not_leak"] not in str(e.value)
            return
        got = go()
        for key, want in case["expect"].items():
            assert got[key] == want, key
