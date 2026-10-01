# -*- coding: utf-8 -*-
"""S3-E：config.json 的写是「临时文件 + 原子替换」，写不成就明说、原文件原样；重新读出来的永远是完整的旧版或完整的新版。

故障尽量用真的：临时文件的路径上放一个目录、被别的进程占着的文件（Windows 上替换会真的被拒）、半截的 .tmp、
内容损坏的老文件；其余（写到一半被打断）用注入。"""
import json
import os
import sys
import threading

import pytest

from app import settings


def _put(text: str) -> None:
    with open(settings._CONFIG, "w", encoding="utf-8") as f:
        f.write(text)


def _bytes() -> bytes:
    with open(settings._CONFIG, "rb") as f:
        return f.read()


def _tmp() -> str:
    return settings._CONFIG + ".tmp"


class TestFailedWritesDoNotLookSaved:
    def test_a_temp_path_that_is_a_directory_fails_the_write_and_the_old_file_survives(self):
        settings.set_chat_meta("a", ts=1)
        before = _bytes()
        os.mkdir(_tmp())                                  # 真的文件系统故障：临时文件根本建不出来

        assert settings.set_chat_meta("b", ts=2) is False
        with pytest.raises(settings.ConfigWriteError):
            settings.save(context_n=50)
        assert _bytes() == before
        assert settings.chat_meta("b") == {} and settings.context() != 50  # 重新读：没有「存了一半」

        os.rmdir(_tmp())
        assert settings.set_chat_meta("b", ts=2) is True  # 故障排除后恢复
        assert settings.chat_meta("b") == {"ts": 2}

    @pytest.mark.skipif(sys.platform != "win32", reason="占着文件不让替换是 Windows 的行为")
    def test_a_destination_held_open_by_someone_else_fails_the_replace_and_leaves_no_temp(self):
        settings.set_chat_meta("a", ts=1)
        before = _bytes()
        with open(settings._CONFIG, "rb"):                # 临时文件写成了，替换这一步被系统拒绝
            assert settings.set_chat_meta("b", ts=2) is False
            with pytest.raises(settings.ConfigWriteError):
                settings.save(context_n=50)
        assert not os.path.exists(_tmp())
        assert _bytes() == before
        assert settings.set_chat_meta("b", ts=2) is True  # 放手后正常

    def test_a_write_interrupted_halfway_leaves_the_old_file_whole(self, monkeypatch):
        settings.set_chat_meta("a", ts=1)
        before = _bytes()

        def dies_midway(data, f, **kw):
            f.write('{"chat_meta": {"a": {"ts"')          # 半截 JSON 已经进了临时文件
            raise OSError("disk full")

        with monkeypatch.context() as m:
            m.setattr(json, "dump", dies_midway)
            assert settings.set_chat_meta("b", ts=2) is False
        assert _bytes() == before
        assert not os.path.exists(_tmp())
        assert settings.chat_meta("a") == {"ts": 1}

    def test_a_leftover_half_temp_file_is_ignored_by_readers_and_replaced_by_the_next_commit(self):
        settings.set_chat_meta("a", ts=1)
        with open(_tmp(), "w", encoding="utf-8") as f:
            f.write('{"chat_meta": {"a": {"ts"')          # 上次被杀掉时留下的
        assert settings.chat_meta("a") == {"ts": 1}
        assert settings.set_chat_meta("b", ts=2) is True
        assert not os.path.exists(_tmp())
        assert settings.chat_meta("b") == {"ts": 2} and settings.chat_meta("a") == {"ts": 1}

    def test_the_ui_entry_point_tells_the_truth(self):
        os.mkdir(_tmp())
        assert settings.try_save(dock_on=False) is False
        os.rmdir(_tmp())
        assert settings.try_save(dock_on=False) is True
        assert settings.dock() is False

    def test_the_error_message_carries_no_setting_content(self, monkeypatch):
        with monkeypatch.context() as m:
            m.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("secret-path-detail")))
            with pytest.raises(settings.ConfigWriteError) as e:
                settings.save(style_text="我的私人说话风格")
        assert "私人" not in str(e.value) and "secret-path-detail" not in str(e.value)


class TestDamagedConfig:
    def test_a_corrupt_config_reads_as_defaults_and_bindings_as_unavailable(self):
        _put('{"relationship": "同')
        assert settings.relationship() == "auto"
        assert settings.bindings_state() is None          # S1：读不了 ≠ 没绑，调用方必须拒发

    def test_a_non_object_config_counts_as_corrupt_too(self):
        _put("[1, 2, 3]")
        assert settings.relationship() == "auto"
        assert settings.bindings_state() is None
        assert settings.set_chat_meta("a", ts=1) is True
        assert settings.chat_meta("a") == {"ts": 1}

    def test_writing_over_a_corrupt_config_keeps_the_original_bytes_aside(self):
        raw = '{"chat_meta": {"a": {"ts": 1}}, "relationship": "同'
        _put(raw)

        assert settings.set_chat_meta("b", ts=2) is True

        kept = [f for f in os.listdir(os.path.dirname(settings._CONFIG)) if ".corrupt." in f]
        assert len(kept) == 1
        with open(os.path.join(os.path.dirname(settings._CONFIG), kept[0]), encoding="utf-8") as f:
            assert f.read() == raw                        # 用户的东西没被抹掉
        assert settings.chat_meta("b") == {"ts": 2}       # 新文件是完整合法的 JSON
        assert settings.bindings_state() == {}

    def test_if_the_original_cannot_be_kept_aside_nothing_is_overwritten(self, monkeypatch):
        _put('{"broken')
        with monkeypatch.context() as m:
            m.setattr("shutil.copyfile", lambda *a: (_ for _ in ()).throw(OSError("no space")))
            assert settings.set_chat_meta("b", ts=2) is False
        assert _bytes() == b'{"broken'

    def test_a_config_that_cannot_be_opened_right_now_is_never_written_over(self, monkeypatch):
        settings.save(context_n=40)
        before = _bytes()
        monkeypatch.setattr(settings, "_load_raw", lambda: (None, "unreadable"))  # 被占用：不是「损坏」
        assert settings.set_chat_meta("a", ts=1) is False
        with pytest.raises(settings.ConfigWriteError):
            settings.save(context_n=50)
        assert _bytes() == before


class TestNothingIsLostOrHalfWritten:
    def test_save_keeps_fields_it_does_not_own(self):
        _put(json.dumps({"future_field": {"x": 1}, "chat_rel": {"a": "同事"}, "win_pos": [3, 4], "win_width": 500}))
        settings.save(dock_on=False, context_n=12)
        data = json.load(open(settings._CONFIG, encoding="utf-8"))
        assert data["future_field"] == {"x": 1}
        assert data["chat_rel"] == {"a": "同事"} and data["win_pos"] == [3, 4] and data["win_width"] == 500
        assert data["context"] == 12 and data["dock"] is False

    def test_a_single_switch_save_does_not_clear_other_settings(self):
        settings.save("朋友", 20, style_text="短句", draft_model_text="m1")
        settings.save(dock_on=False)
        assert (settings.relationship(), settings.context(), settings.style(), settings.draft_model()) == ("朋友", 20, "短句", "m1")

    def test_concurrent_writers_do_not_overwrite_each_other(self):
        errors = []

        def chat_writer(n):
            try:
                for i in range(15):
                    assert settings.set_chat_meta(f"chat-{n}-{i}", ts=i) is True
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def settings_writer():
            try:
                for i in range(15):
                    settings.save(context_n=10 + i)
                    settings.save_win_state(i, i, 300 + i)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=chat_writer, args=(n,)) for n in range(4)] + [threading.Thread(target=settings_writer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        data = json.load(open(settings._CONFIG, encoding="utf-8"))   # 合法 JSON，不是交错的半截
        assert len(data["chat_meta"]) == 4 * 15                      # 没有哪个线程的更新被吞掉
        assert data["context"] == 24 and data["win_width"] == 314
        assert not os.path.exists(_tmp())

    def test_readers_never_see_a_half_state_nor_make_a_writer_fail_while_writers_run(self):
        settings.save(context_n=10)
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                if settings.bindings_state() is None:      # 读不了 = 会让 keygate 拒发
                    bad.append("bindings unreadable")
                if settings._read("context") is None:
                    bad.append("context vanished")

        readers = [threading.Thread(target=reader) for _ in range(3)]
        for t in readers:
            t.start()
        try:
            for i in range(40):
                assert settings.set_chat_meta(f"c{i}", ts=i) is True   # 有人在读，写也不能被拒
        finally:
            stop.set()
            for t in readers:
                t.join()
        assert bad == []
