# -*- coding: utf-8 -*-
"""S0-C：图片只来自这次授权的窗口截图，且只属于对方的最新一条。"""
import json
import os

import pytest

from app import settings
from core.image_policy import newest_image

HER, ME = "her", "me"


def _msgs(*whos):
    return [(w, f"t{i}", None) for i, w in enumerate(whos)]


class TestNewestImage:
    def test_image_on_the_newest_her_message_is_used(self):
        assert newest_image((3, "IMG"), _msgs(HER, ME, HER), True) == "IMG"

    def test_off_by_default_never_attaches(self):
        assert newest_image((3, "IMG"), _msgs(HER, ME, HER), False) is None

    def test_latest_message_from_me_never_attaches(self):
        assert newest_image((2, "IMG"), _msgs(HER, ME), True) is None

    def test_image_earlier_in_her_run_is_not_attached(self):
        # 旧口径：对方连发几条，里面有图就带。新口径：只认最后一条本身
        assert newest_image((1, "IMG"), _msgs(HER, HER, HER), True) is None

    def test_text_after_the_image_drops_it(self):
        assert newest_image((2, "IMG"), _msgs(ME, HER, HER), True) is None

    def test_no_image_or_no_messages(self):
        assert newest_image(None, _msgs(HER), True) is None
        assert newest_image((1, "IMG"), [], True) is None
        assert newest_image((1, ""), _msgs(HER), True) is None


class TestReadImagesIsOptIn:
    def test_default_is_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "_CONFIG", str(tmp_path / "c.json"))
        assert settings.read_images() is False

    def test_legacy_read_images_true_is_not_authorization(self, tmp_path, monkeypatch):
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"read_images": True}), encoding="utf-8")
        monkeypatch.setattr(settings, "_CONFIG", str(cfg))
        assert settings.read_images() is False

    def test_explicit_opt_in_round_trips(self, tmp_path, monkeypatch):
        cfg = tmp_path / "c.json"
        monkeypatch.setattr(settings, "_CONFIG", str(cfg))
        cfg.write_text(json.dumps({"read_images_optin": True}), encoding="utf-8")
        assert settings.read_images() is True


class TestNoLocalFileImages:
    def test_qq_file_reader_is_gone(self):
        from app import images

        assert not hasattr(images, "qq_file_since")

    def test_qq_picture_directory_is_never_opened(self, tmp_path, monkeypatch):
        """QQ 图片目录里有个修改时间刚好匹配的文件：不许被打开，图只能来自窗口截图。"""
        from app import images, uia_worker

        pic_dir = tmp_path / "Documents" / "Tencent Files" / "10001" / "nt_qq" / "nt_data" / "Pic"
        pic_dir.mkdir(parents=True)
        bait = pic_dir / "fresh.jpg"
        bait.write_bytes(b"not really a jpeg")
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        opened = []
        real_open = open
        monkeypatch.setattr("builtins.open",
                            lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1])
        monkeypatch.setattr(settings, "read_images", lambda: True)
        monkeypatch.setattr(images, "crop_window", lambda hwnd, rect: "FROM_WINDOW_SHOT")

        who, nm, text, img = uia_worker._with_image("qq", 1, (HER, None, "[图片]", (0, 0, 100, 100)), False)
        assert img == "FROM_WINDOW_SHOT"
        assert not any(os.path.basename(p) == "fresh.jpg" for p in opened)

    @pytest.mark.parametrize("enabled,first", [(False, False), (True, True)])
    def test_no_shot_when_off_or_first_read(self, monkeypatch, enabled, first):
        from app import images, uia_worker

        monkeypatch.setattr(settings, "read_images", lambda: enabled)
        monkeypatch.setattr(images, "crop_window", lambda *a: "SHOT")
        img = uia_worker._with_image("qq", 1, (HER, None, "[图片]", (0, 0, 100, 100)), first)[3]
        assert img is None
