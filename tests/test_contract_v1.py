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
