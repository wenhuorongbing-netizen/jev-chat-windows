# -*- coding: utf-8 -*-
"""reply_rules 纯逻辑：gloss（候选下的中文意思）和 translation（「对方说」译文行）的显示规则。"""
from app.reply_rules import shown_gloss, shown_translation


class TestShownGloss:
    def test_normal_foreign_keeps_gloss(self):
        assert shown_gloss("Wie geht es dir?", "你最近怎么样", "德语") == "你最近怎么样"

    def test_empty_gloss(self):
        assert shown_gloss("hello", "", "英语") == ""

    def test_none_gloss(self):
        assert shown_gloss("hello", None, "英语") == ""

    def test_blank_gloss(self):
        assert shown_gloss("hello", "   ", "英语") == ""

    def test_gloss_same_as_text(self):
        assert shown_gloss("22", "22", "外语") == ""

    def test_gloss_same_as_text_with_whitespace(self):
        assert shown_gloss("  22 ", "22", "英语") == ""
        assert shown_gloss("22", " 22\n", "英语") == ""

    def test_chinese_lang_hides_gloss(self):
        for lang in ("中文", "Chinese", "zh", "ZH", "Zh", " chinese "):
            assert shown_gloss("好的", "好的", lang) == "", lang
            assert shown_gloss("好的", "不一样的意思", lang) == "", lang

    def test_numeric_text_with_real_gloss_shows(self):
        # 纯数字正文但模型给了不同 gloss、且语言是外语：照显示
        assert shown_gloss("22", "二十二", "英语") == "二十二"

    def test_empty_lang_treated_as_foreign(self):
        assert shown_gloss("Hallo", "你好", "") == "你好"
        assert shown_gloss("Hallo", "你好", None) == "你好"


class TestShownTranslation:
    def test_foreign_keeps_translation(self):
        assert shown_translation("今晚一起吃饭吗", "德语") == "今晚一起吃饭吗"

    def test_empty_translation(self):
        assert shown_translation("", "德语") == ""

    def test_none_translation(self):
        assert shown_translation(None, "德语") == ""

    def test_blank_translation(self):
        assert shown_translation("   ", "德语") == ""

    def test_chinese_lang_hides_translation(self):
        for lang in ("中文", "Chinese", "zh", "ZH", " chinese "):
            assert shown_translation("今晚一起吃饭吗", lang) == "", lang

    def test_empty_lang_treated_as_foreign(self):
        assert shown_translation("今晚一起吃饭吗", "") == "今晚一起吃饭吗"
