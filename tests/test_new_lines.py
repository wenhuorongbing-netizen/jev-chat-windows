# -*- coding: utf-8 -*-
"""Reader.new_lines 的去重/吞没逻辑（纯逻辑：不加载 OCR 引擎，直接构造行喂入）。

Bug 1 场景：同一方连发两条，行间距 < 0.6*lh 被合并成一行（y = 老消息顶部，text = 老+新）。
合并行被 floor（y 不增）滤掉、又被 _seen（≥0.75 相似）吞掉——修复后按「增长后缀」放行。"""
from app.ocr import Reader


def _reader():
    """不实例化 RapidOCR 引擎的 Reader：new_lines 只用 seen/lh 这几个属性。"""
    r = Reader.__new__(Reader)
    r.seen = []
    r.lh = 20.0
    r.last_boxes = []
    r.last_ms = 0
    return r


class TestGrowthSuffix:
    def test_consecutive_short_messages_not_swallowed(self):
        """「我也在瞪」→「22」合并成「我也在瞪22」（y=100 老位置，相似度 0.83）：
        修复后后缀「22」必须发出；同一合并行再来一帧不重复报。"""
        r = _reader()
        assert r.new_lines([("her", None, "我也在瞪", 100)]) == [("her", None, "我也在瞪")]
        assert r.new_lines([("her", None, "我也在瞪22", 100)]) == [("her", None, "22")]
        assert r.new_lines([("her", None, "我也在瞪22", 100)]) == []

    def test_split_then_merged_not_reported_twice(self):
        """frame1 分行报过「今天天气不错」「适合出门」，frame2 合并成「今天天气不错适合出门」
        （与前者相似度 0.75，被 floor 拦下）：后缀「适合出门」已在 seen，一条都别再发。"""
        r = _reader()
        first = r.new_lines([("her", None, "今天天气不错", 100), ("her", None, "适合出门", 140)])
        assert first == [("her", None, "今天天气不错"), ("her", None, "适合出门")]
        assert r.new_lines([("her", None, "今天天气不错适合出门", 100)]) == []

    def test_suffix_goes_to_seen_immediately(self):
        """后缀一旦发出立刻进 seen：下一帧还是这条合并行，不能每帧都当新消息刷。
        （「今天在加班」5 字 → 合并行 8 字，相似度 10/13≈0.77 ≥ 0.75，走 floor+suffix 路径）"""
        r = _reader()
        r.new_lines([("her", None, "今天在加班", 100)])
        assert r.new_lines([("her", None, "今天在加班晚点回", 100)]) == [("her", None, "晚点回")]
        assert r.new_lines([("her", None, "今天在加班晚点回", 100)]) == []
        assert r.new_lines([("her", None, "今天在加班晚点回", 100)]) == []


class TestExistingDedup:
    def test_exact_repeat_still_swallowed(self):
        """文档化现状：同一条消息一模一样的两帧只报一次。"""
        r = _reader()
        assert r.new_lines([("her", None, "你好", 100)]) == [("her", None, "你好")]
        assert r.new_lines([("her", None, "你好", 100)]) == []

    def test_scrolled_up_old_messages_not_repeated(self):
        """往上滚翻出来的旧消息在已知行上方（y ≤ floor），不算新。"""
        r = _reader()
        r.new_lines([("her", None, "后面的消息", 200), ("her", None, "最新的", 240)])
        out = r.new_lines([("her", None, "更早的旧消息", 100), ("her", None, "后面的消息", 200)])
        assert out == []
