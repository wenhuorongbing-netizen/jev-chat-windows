# -*- coding: utf-8 -*-
"""S3-D：key 只以 DPAPI 密文落盘；老版本的明文环境变量只当迁移来源，每一步失败都停在那一步、旧值原样留着。

DPAPI 用真的（只在 Windows 上有，CI 就是 windows-latest）；失败是注入的：加密失败、读回对不上、配置写不进去、
旧明文删不掉。注册表换成内存里的假的，不碰本机。"""
import json
import os
import sys

import pytest

from app import secretstore, settings
from core import jev_client, keygate
from core.providers import JEV_ENV, LEGACY, LLM_ENV, draft_route, jev_route

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 只在 Windows 上")

KEY = "FAKEKEY-not-a-real-credential-123456"
OTHER = "FAKEKEY-another-credential-654321"


def _cfg() -> dict:
    with open(settings._CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _cfg_text() -> str:
    with open(settings._CONFIG, encoding="utf-8") as f:
        return f.read()


def _fresh_process(monkeypatch):
    """模拟重启：本进程环境里解出来放着的 key 清掉，之后只能从磁盘上的加密存储读。"""
    for name in (JEV_ENV, LLM_ENV, *LEGACY.values()):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def reg(monkeypatch):
    """假的用户环境变量注册表：{名字: 明文}。"""
    values: dict[str, str] = {}
    monkeypatch.setattr(settings, "_registry_read", lambda name: values.get(name, ""))

    def delete(name):
        values.pop(name, None)
        return True

    monkeypatch.setattr(settings, "_registry_delete", delete)
    return values


class TestSecretStore:
    def test_round_trip_and_the_token_does_not_contain_the_key(self):
        token = secretstore.protect(KEY, LLM_ENV)
        assert token.startswith("dpapi1:") and KEY not in token
        assert secretstore.unprotect(token, LLM_ENV) == KEY

    def test_a_token_is_bound_to_its_slot(self):
        token = secretstore.protect(KEY, JEV_ENV)
        with pytest.raises(secretstore.SecretError):
            secretstore.unprotect(token, LLM_ENV)

    @pytest.mark.parametrize("bad", ["", "plain-text-key", "dpapi1:", "dpapi1:!!!not-base64", "dpapi1:AAAA", None])
    def test_garbage_is_an_error_not_a_key(self, bad):
        with pytest.raises(secretstore.SecretError):
            secretstore.unprotect(bad, JEV_ENV)

    def test_a_flipped_byte_is_rejected(self):
        token = secretstore.protect(KEY, JEV_ENV)
        head, body = token[:-8], token[-8:]
        tampered = head + ("A" if body[0] != "A" else "B") + body[1:]
        with pytest.raises(secretstore.SecretError):
            secretstore.unprotect(tampered, JEV_ENV)

    def test_two_threads_encrypting_and_decrypting_at_once_do_not_trip_over_each_other(self):
        import threading

        errors = []

        def work(n):
            try:
                for i in range(40):
                    k = f"{KEY}-{n}-{i}"
                    assert secretstore.unprotect(secretstore.protect(k, JEV_ENV), JEV_ENV) == k
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []

    def test_an_empty_key_is_not_encrypted(self):
        with pytest.raises(secretstore.SecretError):
            secretstore.protect("", JEV_ENV)


class TestSave:
    def test_the_key_reaches_the_disk_only_as_ciphertext_together_with_its_binding(self, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        data = _cfg()
        assert KEY not in _cfg_text()
        assert data["secrets"][LLM_ENV].startswith("dpapi1:")
        assert data["key_bindings"][LLM_ENV] == draft_route("deepseek")  # 同一份文件、同一次写

    def test_after_a_restart_the_key_comes_back_from_the_store(self, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        _fresh_process(monkeypatch)
        assert settings.llm_key() == KEY
        assert settings.key_for_route(LLM_ENV, draft_route("deepseek")) == KEY
        assert settings.key_for_route(LLM_ENV, draft_route("openrouter")) == ""

    def test_encryption_failure_saves_nothing_and_never_falls_back_to_plaintext(self, monkeypatch):
        settings.save(context_n=10)
        before = _cfg_text()

        def boom(*_a):
            raise secretstore.SecretError("DPAPI down")

        monkeypatch.setattr(secretstore, "protect", boom)
        with pytest.raises(secretstore.SecretError):
            settings.save(context_n=50, llm_key_text=KEY)
        assert _cfg_text() == before                  # 连同这次的其它设置，什么都没写
        assert KEY not in _cfg_text()
        assert settings.llm_key() == ""               # 本进程也没偷偷用上它
        assert settings.context() == 10

    def test_a_failed_config_write_saves_nothing_and_the_process_does_not_use_the_new_key(self, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        before = _cfg_text()
        with monkeypatch.context() as m:
            m.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
            with pytest.raises(settings.ConfigWriteError):
                settings.save(draft_provider_text="deepseek", llm_key_text=OTHER)
        assert _cfg_text() == before
        assert settings.llm_key() == KEY

    def test_a_failed_save_never_leaves_a_key_without_its_binding(self, monkeypatch):
        with monkeypatch.context() as m:
            m.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
            with pytest.raises(settings.ConfigWriteError):
                settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        assert not os.path.exists(settings._CONFIG)
        assert settings.bindings_state() == {}
        assert settings._read("secrets") is None

    def test_the_token_of_another_slot_does_not_unlock_this_one(self, monkeypatch):
        settings.save(jev_provider_text="openrouter", jev_key_text=KEY, draft_provider_text="deepseek", llm_key_text=OTHER)
        data = _cfg()
        data["secrets"][LLM_ENV] = data["secrets"][JEV_ENV]  # 手工把 A 槽的密文搬到 B 槽
        with open(settings._CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f)
        _fresh_process(monkeypatch)
        assert settings.llm_key() == ""
        assert any(LLM_ENV in m for m in settings.secret_issues())

    def test_other_writers_keep_the_secrets_and_bindings(self, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        settings.set_chat_meta("a", ts=1)
        settings.save_win_state(1, 2, 300)
        settings.set_chat_relationship("a", "同事")
        settings.save(dock_on=False)
        _fresh_process(monkeypatch)
        assert settings.llm_key() == KEY
        assert settings.bindings_state()[LLM_ENV] == draft_route("deepseek")


class TestMigration:
    def test_success_imports_binds_verifies_and_only_then_deletes_the_plaintext(self, reg, monkeypatch):
        reg[JEV_ENV] = KEY
        assert settings.migrate_legacy_keys() == []
        assert reg == {}                               # 旧明文没了
        assert KEY not in _cfg_text()
        assert _cfg()["key_bindings"][JEV_ENV] == jev_route("openrouter")  # 绑定跟密文同一次写
        _fresh_process(monkeypatch)
        assert settings.jev_key() == KEY               # 从加密存储读回

    def test_a_migrated_key_is_bound_so_it_is_no_longer_legacy_unbound(self, reg):
        reg[LLM_ENV] = KEY
        settings.migrate_legacy_keys()
        assert keygate.binding_state(LLM_ENV, draft_route("deepseek"))[0] is keygate.BindingState.BOUND_MATCH
        assert keygate.binding_state(LLM_ENV, draft_route("openrouter"))[0] is keygate.BindingState.BOUND_MISMATCH

    def test_encryption_failure_keeps_the_old_value_and_reports(self, reg, monkeypatch):
        reg[JEV_ENV] = KEY
        monkeypatch.setattr(secretstore, "protect", lambda *a: (_ for _ in ()).throw(secretstore.SecretError("x")))
        issues = settings.migrate_legacy_keys()
        assert reg == {JEV_ENV: KEY}
        assert not os.path.exists(settings._CONFIG)
        assert issues and KEY not in " ".join(issues)
        assert settings.jev_key() == ""                # 保留不等于使用：没迁成功的明文不拿去发请求（S3.1）

    def test_a_read_back_mismatch_keeps_the_old_value(self, reg, monkeypatch):
        reg[JEV_ENV] = KEY
        monkeypatch.setattr(secretstore, "unprotect", lambda *a: "something else")
        issues = settings.migrate_legacy_keys()
        assert reg == {JEV_ENV: KEY} and issues
        assert not os.path.exists(settings._CONFIG)

    def test_a_config_write_failure_keeps_the_old_value_and_leaves_no_half_state(self, reg, monkeypatch):
        reg[JEV_ENV] = KEY
        with monkeypatch.context() as m:
            m.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
            issues = settings.migrate_legacy_keys()
        assert reg == {JEV_ENV: KEY} and issues
        assert not os.path.exists(settings._CONFIG)    # 没有「有密文没绑定」或「有绑定没密文」
        assert settings.bindings_state() == {}

    def test_a_disk_read_back_mismatch_after_the_write_keeps_the_old_value_and_a_retry_completes(self, reg, monkeypatch):
        reg[JEV_ENV] = KEY
        real = secretstore.unprotect
        calls = {"n": 0}

        def flaky(token, slot):
            calls["n"] += 1
            return real(token, slot) if calls["n"] == 1 else "corrupted on disk"  # 第 1 次是加密后核对，之后是读盘

        monkeypatch.setattr(secretstore, "unprotect", flaky)
        assert settings.migrate_legacy_keys()
        assert reg == {JEV_ENV: KEY}                   # 磁盘上那份没验证过，不删旧的
        monkeypatch.setattr(secretstore, "unprotect", real)
        settings._ISSUES.clear()
        assert settings.migrate_legacy_keys() == []
        assert reg == {}

    def test_when_the_plaintext_cannot_be_deleted_the_secret_stays_and_the_next_start_retries(self, reg, monkeypatch):
        reg[JEV_ENV] = KEY
        monkeypatch.setattr(settings, "_registry_delete", lambda name: False)
        issues = settings.migrate_legacy_keys()
        assert reg == {JEV_ENV: KEY}
        assert any("没能删掉" in m for m in issues)
        assert _cfg()["secrets"][JEV_ENV].startswith("dpapi1:")  # 已经安全存下了
        token = _cfg()["secrets"][JEV_ENV]

        monkeypatch.setattr(settings, "_registry_delete", lambda name: reg.pop(name, None) or True)
        settings._ISSUES.clear()
        assert settings.migrate_legacy_keys() == []
        assert reg == {} and _cfg()["secrets"][JEV_ENV] == token  # 只清理，没有重新加密

    def test_generic_names_are_imported_but_not_deleted_behind_the_users_back(self, reg):
        reg[LEGACY[LLM_ENV]] = KEY                     # DEEPSEEK_API_KEY：别的工具也常用这个名字
        issues = settings.migrate_legacy_keys()
        assert settings.llm_key() == KEY and _cfg()["secrets"][LLM_ENV].startswith("dpapi1:")
        assert reg == {LEGACY[LLM_ENV]: KEY}
        assert any(LEGACY[LLM_ENV] in m for m in issues)

    def test_an_identical_plaintext_next_to_a_stored_key_is_cleaned_up(self, reg):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        reg[LLM_ENV] = KEY
        settings.migrate_legacy_keys()
        assert reg == {}
        assert settings.llm_key() == KEY

    def test_a_different_plaintext_next_to_a_stored_key_is_neither_deleted_nor_used_and_is_reported(self, reg):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        reg[LLM_ENV] = OTHER
        issues = settings.migrate_legacy_keys()
        assert reg == {LLM_ENV: OTHER}               # 没验证过的值不动
        assert settings.llm_key() == KEY             # 也不拿来用
        assert any("不同" in m for m in issues)
        assert settings._stored_key(LLM_ENV) == KEY

    def test_a_stale_inherited_variable_does_not_beat_the_stored_key_after_a_restart(self, reg, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        monkeypatch.setenv(LLM_ENV, OTHER)           # 启动时继承来的旧环境变量
        settings.migrate_legacy_keys()
        assert settings.llm_key() == KEY

    def test_a_generic_legacy_name_is_bound_to_its_own_provider_not_to_whatever_is_selected_now(self, reg):
        settings.save(draft_provider_text="openai")   # 现在选的是别家
        reg[LEGACY[LLM_ENV]] = KEY                    # DEEPSEEK_API_KEY：当年就是给 deepseek 填的
        settings.migrate_legacy_keys()
        assert _cfg()["key_bindings"][LLM_ENV] == draft_route("deepseek")
        assert keygate.binding_state(LLM_ENV, draft_route("openai"))[0] is keygate.BindingState.BOUND_MISMATCH

    def test_a_corrupt_config_postpones_the_migration_instead_of_guessing_the_route(self, reg):
        with open(settings._CONFIG, "w", encoding="utf-8") as f:
            f.write('{"draft_provider": "ope')
        reg[LLM_ENV] = KEY
        issues = settings.migrate_legacy_keys()
        assert reg == {LLM_ENV: KEY} and issues
        with open(settings._CONFIG, encoding="utf-8") as f:
            assert f.read() == '{"draft_provider": "ope'   # 没动用户的文件

    def test_a_secret_without_its_binding_is_refused_not_treated_as_legacy(self):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        data = _cfg()
        del data["key_bindings"][LLM_ENV]             # 手工改坏 / 半截状态
        with open(settings._CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f)
        assert keygate.binding_state(LLM_ENV, draft_route("deepseek"))[0] is keygate.BindingState.BINDING_UNAVAILABLE
        assert settings.key_for_route(LLM_ENV, draft_route("deepseek")) == ""
        with pytest.raises(settings.KeyRouteError):
            settings.require_key_route(LLM_ENV)

    def test_an_issue_goes_away_once_the_problem_does(self, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        data = _cfg()
        data["secrets"][LLM_ENV] = "dpapi1:AAAA"
        with open(settings._CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f)
        _fresh_process(monkeypatch)
        assert settings.llm_key() == "" and settings.secret_issues()
        settings.save(draft_provider_text="deepseek", llm_key_text=OTHER)   # 用户重新填了
        assert settings.secret_issues() == []

    def test_an_undecryptable_stored_token_is_replaced_by_the_legacy_value_not_trusted(self, reg):
        settings.save(draft_provider_text="deepseek", llm_key_text=OTHER)
        data = _cfg()
        data["secrets"][LLM_ENV] = "dpapi1:AAAA"
        with open(settings._CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f)
        reg[LLM_ENV] = KEY
        settings.migrate_legacy_keys()
        assert reg == {}
        assert settings._stored_key(LLM_ENV) == KEY

    def test_nothing_to_migrate_touches_nothing(self, reg):
        assert settings.migrate_legacy_keys() == []
        assert not os.path.exists(settings._CONFIG)

    def test_an_existing_binding_is_kept_not_replaced_by_the_current_route(self, reg):
        settings.save(draft_provider_text="openrouter")          # 现在选的是 openrouter
        with open(settings._CONFIG, encoding="utf-8") as f:
            data = json.load(f)
        data["key_bindings"] = {LLM_ENV: draft_route("deepseek")}  # 老版本把这把 key 绑在 deepseek 上
        with open(settings._CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f)
        reg[LLM_ENV] = KEY
        settings.migrate_legacy_keys()
        assert _cfg()["key_bindings"][LLM_ENV] == draft_route("deepseek")


def _no_credential(env, destination):
    """网络层此刻造不出这把 key 的 Credential（没有 key，或没有绑定）。"""
    with pytest.raises((jev_client.JevError, keygate.KeyRouteError)) as e:
        jev_client.credential_for(env, destination)
    assert KEY not in str(e.value)


class TestUnfinishedMigrationNeverSends:
    """S3.1-A：旧明文迁不成功时「原样保留 + 报告 + 不使用」。启动时用户环境变量会被继承进本进程，所以每个场景都把它放进进程环境。"""
    JEV_DEST = jev_route("openrouter")
    LLM_DEST = draft_route("deepseek")

    def _inherit(self, monkeypatch, reg, name, value=KEY):
        reg[name] = value
        monkeypatch.setenv(name, value)

    def test_dpapi_failure_keeps_the_registry_key_and_gives_the_network_no_credential(self, reg, monkeypatch):
        self._inherit(monkeypatch, reg, JEV_ENV)
        monkeypatch.setattr(secretstore, "protect", lambda *a: (_ for _ in ()).throw(secretstore.SecretError("x")))
        issues = settings.migrate_legacy_keys()
        assert reg == {JEV_ENV: KEY} and issues
        assert settings.has_jev_key() is False and os.environ.get(JEV_ENV) is None
        _no_credential(JEV_ENV, self.JEV_DEST)

    def test_config_write_failure_keeps_the_plaintext_and_sends_nothing(self, reg, monkeypatch):
        self._inherit(monkeypatch, reg, LLM_ENV)
        with monkeypatch.context() as m:
            m.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
            assert settings.migrate_legacy_keys()
        assert reg == {LLM_ENV: KEY}
        assert settings.has_llm_key() is False and os.environ.get(LLM_ENV) is None
        _no_credential(LLM_ENV, self.LLM_DEST)

    @pytest.mark.parametrize("damage", ["corrupt", "unreadable"])
    def test_a_corrupt_or_unreadable_config_keeps_the_plaintext_and_sends_nothing(self, reg, monkeypatch, damage):
        self._inherit(monkeypatch, reg, JEV_ENV)
        if damage == "corrupt":
            with open(settings._CONFIG, "w", encoding="utf-8") as f:
                f.write('{"jev_provider": "ope')
        else:
            monkeypatch.setattr(settings, "_load_raw", lambda: (None, "unreadable"))
        assert settings.migrate_legacy_keys()
        assert reg == {JEV_ENV: KEY}
        assert settings.has_jev_key() is False and os.environ.get(JEV_ENV) is None
        _no_credential(JEV_ENV, self.JEV_DEST)

    def test_a_generic_name_imported_ok_stays_in_place_and_jev_uses_its_own_encrypted_copy(self, reg, monkeypatch):
        self._inherit(monkeypatch, reg, LEGACY[LLM_ENV])
        settings.migrate_legacy_keys()
        assert reg == {LEGACY[LLM_ENV]: KEY}                         # 别的工具还要用，不删
        cred = jev_client.credential_for(LLM_ENV, self.LLM_DEST)     # deepseek 通用名当年就是给 deepseek 的
        assert keygate.release(cred, self.LLM_DEST) == KEY
        assert settings._stored_key(LLM_ENV) == KEY and KEY not in _cfg_text()

    def test_a_generic_name_whose_import_failed_is_left_untouched_and_jev_refuses(self, reg, monkeypatch):
        self._inherit(monkeypatch, reg, LEGACY[JEV_ENV])
        monkeypatch.setattr(secretstore, "protect", lambda *a: (_ for _ in ()).throw(secretstore.SecretError("x")))
        assert settings.migrate_legacy_keys()
        assert reg == {LEGACY[JEV_ENV]: KEY} and os.environ.get(LEGACY[JEV_ENV]) == KEY   # 通用名原样、不替用户摘
        assert settings.has_jev_key() is False
        _no_credential(JEV_ENV, self.JEV_DEST)                       # 通用名不是 Jev 的 key 来源

    def test_a_successful_migration_works_normally_through_the_network_layer(self, reg, monkeypatch):
        self._inherit(monkeypatch, reg, LLM_ENV)
        assert settings.migrate_legacy_keys() == []
        _fresh_process(monkeypatch)
        assert settings.has_llm_key() is True
        cred = jev_client.credential_for(LLM_ENV, self.LLM_DEST)
        assert keygate.release(cred, self.LLM_DEST) == KEY
        with pytest.raises(keygate.KeyRouteError):                   # 绑定还在：换个目的地照样拒
            jev_client.credential_for(LLM_ENV, draft_route("openrouter"))

    def test_typed_credentials_from_the_settings_page_are_unaffected(self, reg, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        cred = jev_client.credential_for(LLM_ENV, self.LLM_DEST)
        assert keygate.release(cred, self.LLM_DEST) == KEY
        _fresh_process(monkeypatch)
        assert settings.llm_key() == KEY

    def test_a_key_only_in_the_process_environment_is_not_used_either(self, reg, monkeypatch):
        monkeypatch.setenv(JEV_ENV, KEY)                             # 用户自己 set 的：证明不了该发往哪个接口
        issues = settings.migrate_legacy_keys()
        assert any(JEV_ENV in m for m in issues)
        assert settings.has_jev_key() is False and os.environ.get(JEV_ENV) is None
        _no_credential(JEV_ENV, self.JEV_DEST)

    def test_even_if_something_puts_an_unbound_key_back_into_the_environment_the_gate_refuses(self, reg, monkeypatch):
        monkeypatch.setenv(JEV_ENV, KEY)                             # 第二道：没有绑定记录 = 不发
        _no_credential(JEV_ENV, self.JEV_DEST)

    def test_an_undecryptable_stored_key_does_not_let_a_stale_inherited_value_through_its_binding(self, reg, monkeypatch):
        settings.save(draft_provider_text="deepseek", llm_key_text=KEY)
        data = _cfg()
        data["secrets"][LLM_ENV] = "dpapi1:AAAA"                     # 绑定还在、密文坏了
        with open(settings._CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f)
        _fresh_process(monkeypatch)
        monkeypatch.setenv(LLM_ENV, OTHER)                           # 启动时继承来的旧值
        settings.migrate_legacy_keys()
        assert os.environ.get(LLM_ENV) is None
        _no_credential(LLM_ENV, self.LLM_DEST)

    def test_saving_other_settings_does_not_bind_a_plaintext_legacy_key(self, reg, monkeypatch):
        self._inherit(monkeypatch, reg, LLM_ENV)
        settings.save(draft_provider_text="openrouter")
        assert settings.bindings_state() == {}                       # 以前这一步会把它绑到旧接口、再放行
        assert settings.llm_key() == ""


    def test_a_wrong_value_read_back_from_disk_does_not_leave_the_inherited_plaintext_matching_the_new_binding(self, reg, monkeypatch):
        self._inherit(monkeypatch, reg, LLM_ENV)
        real = secretstore.unprotect
        calls = {"n": 0}

        def flaky(token, slot):
            calls["n"] += 1
            return real(token, slot) if calls["n"] == 1 else "corrupted on disk"  # 第 1 次加密后核对，之后读盘读到别的值

        monkeypatch.setattr(secretstore, "unprotect", flaky)
        assert settings.migrate_legacy_keys()
        assert reg == {LLM_ENV: KEY}                                 # 旧明文没删
        assert os.environ.get(LLM_ENV) is None                       # 但绑定已经写进去了：继承来的明文不能留给网络层
        monkeypatch.setattr(secretstore, "unprotect", real)
        _no_credential_for_the_plaintext(LLM_ENV, self.LLM_DEST)


def _no_credential_for_the_plaintext(env, destination):
    """此刻网络层拿到的 key 不是旧明文（可以是空、或被拒，但绝不能是 KEY）。"""
    try:
        cred = jev_client.credential_for(env, destination)
    except (jev_client.JevError, keygate.KeyRouteError):
        return
    assert keygate.release(cred, destination) != KEY


def test_the_network_layer_does_not_read_generic_names_as_a_key(monkeypatch):
    for generic in (LEGACY[JEV_ENV], LEGACY[LLM_ENV]):
        monkeypatch.setenv(generic, KEY)             # 别的工具的变量：Jev 不拿来当自己的 key
    for env in (JEV_ENV, LLM_ENV):
        with pytest.raises(jev_client.JevError):
            jev_client._api_key(env)
