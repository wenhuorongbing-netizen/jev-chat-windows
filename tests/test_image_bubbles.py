# -*- coding: utf-8 -*-
"""图片泡泡检测（app/image_bubbles.py）：合成 numpy 帧，纯逻辑，无 OCR 依赖。"""
import numpy as np

from app.image_bubbles import ahash, find_her_image

PANE = (245, 245, 245)  # 消息区底色


def _frame(h=600, w=800):
    return np.full((h, w, 3), PANE, dtype=np.uint8)


def _noise(frame, x0, y0, x1, y1, seed=7):
    rng = np.random.default_rng(seed)
    frame[y0:y1, x0:x1] = rng.integers(0, 256, (y1 - y0, x1 - x0, 3), dtype=np.uint8)


class TestFindHerImage:
    def test_noise_block_detected_at_right_place(self):
        """噪声块 = 照片：被检出且位置正确；旁边的细文字条（高 < 48）不得入选。"""
        frame = _frame()
        _noise(frame, 150, 100, 350, 250)
        frame[400:408, 80:300] = 30  # 两行细字
        frame[420:428, 80:260] = 30
        found = find_her_image(frame, PANE)
        assert found is not None
        (x0, y0, x1, y1), h = found
        assert abs(x0 - 150) <= 1 and abs(y0 - 100) <= 1
        assert abs(x1 - 350) <= 1 and abs(y1 - 250) <= 1
        assert isinstance(h, bytes) and h

    def test_avatar_strip_excluded(self):
        """60×60 头像：右缘 70 < 0.14W(112) 且宽 60 < 0.12W(96) → 头像条，排除。"""
        frame = _frame()
        _noise(frame, 10, 100, 70, 160)
        assert find_her_image(frame, PANE) is None

    def test_my_side_excluded(self):
        """中心 x=700 > 0.75W(600)：我自己发的图，排除。"""
        frame = _frame()
        _noise(frame, 650, 100, 750, 220)
        assert find_her_image(frame, PANE) is None

    def test_white_link_card_not_reported(self):
        """白底链接卡片 + 三行细灰字：白底不进掩码、字条太矮，不误报。"""
        frame = _frame()
        frame[100:220, 80:320] = 255
        frame[120:128, 96:280] = 40
        frame[140:148, 96:240] = 40
        frame[160:168, 96:200] = 40
        assert find_her_image(frame, PANE) is None

    def test_wechat_green_bubble_excluded(self):
        """微信绿泡（g > r+40 且 g > b+40）：不进掩码，即便在左侧也不报。"""
        frame = _frame()
        frame[100:220, 150:290] = (149, 236, 105)
        assert find_her_image(frame, PANE) is None

    def test_ahash_stable_and_distinct(self):
        """同一帧两次检测同 hash（worker 靠它去重）；换一张图 hash 不同。"""
        frame = _frame()
        _noise(frame, 150, 100, 350, 250, seed=7)
        _, h1 = find_her_image(frame, PANE)
        _, h2 = find_her_image(frame, PANE)
        assert h1 == h2
        _noise(frame, 150, 100, 350, 250, seed=99)
        _, h3 = find_her_image(frame, PANE)
        assert h3 != h1
