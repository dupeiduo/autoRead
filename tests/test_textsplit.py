"""文本切分测试：句子边界、闭引号归属、片段分组、分页。"""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from autoread.textsplit import (
    SpeechUnit,
    build_units,
    chars_per_second,
    count_chars,
    estimate_read_seconds,
    paginate,
    select_pages,
    split_sentences,
    with_title,
)


class SplitSentencesTest(unittest.TestCase):
    def test_splits_on_full_stop(self) -> None:
        self.assertEqual(split_sentences("二月二，龙抬头。暮色里。"), ["二月二，龙抬头。", "暮色里。"])

    def test_closing_quote_stays_with_previous_sentence(self) -> None:
        text = "他喊道：“你站住！”然后转身走了。"
        self.assertEqual(split_sentences(text), ["他喊道：“你站住！”", "然后转身走了。"])

    def test_closing_bracket_stays_with_previous_sentence(self) -> None:
        self.assertEqual(split_sentences("他说（真的吗？）就笑了。"), ["他说（真的吗？）", "就笑了。"])

    def test_ellipsis_splits_and_empty_pieces_dropped(self) -> None:
        self.assertEqual(split_sentences("他犹豫了……然后走了。"), ["他犹豫了……", "然后走了。"])

    def test_long_sentence_without_punctuation_is_hard_split(self) -> None:
        text = "甲" * 200
        parts = split_sentences(text, hard_limit=80)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)

    def test_long_sentence_splits_at_commas_when_possible(self) -> None:
        text = "第一句很长很长，" * 12
        parts = split_sentences(text, hard_limit=40)
        self.assertTrue(all(count_chars(p) <= 40 for p in parts))

    def test_whitespace_only_is_dropped(self) -> None:
        self.assertEqual(split_sentences("   \n  "), [])
        self.assertEqual(split_sentences("。。。"), [])


class BuildUnitsTest(unittest.TestCase):
    def test_short_sentences_are_grouped(self) -> None:
        # 每句 9 字，组上限 25 字 → 最多两句一组，4 句应切成 2 组。
        text = "甲乙丙丁戊己庚辛。" * 4
        units = build_units([text], group_chars=25)
        self.assertEqual(len(units), 2)
        self.assertEqual("".join(u.text for u in units), text)

    def test_group_limit_has_a_floor(self) -> None:
        # group_chars 有 20 字下限：比一次呼吸还短的请求只会让语气变碎。
        units = build_units(["一。二。三。四。"], group_chars=2)
        self.assertEqual(len(units), 1)

    def test_paragraph_end_flag_only_on_last_unit_of_paragraph(self) -> None:
        units = build_units(["第一段句子一。句子二。", "第二段。"], group_chars=4)
        ends = [i for i, u in enumerate(units) if u.paragraph_end]
        self.assertEqual(len(ends), 2)
        self.assertEqual(ends[-1], len(units) - 1)

    def test_chapter_flags(self) -> None:
        units = build_units(["一句话。", "两句话。"], group_chars=100)
        self.assertTrue(units[0].chapter_start)
        self.assertTrue(units[-1].chapter_end)

    def test_incomplete_chapter_has_no_closing_flag(self) -> None:
        # 试听片段是从中间截断的，不该用「收尾」语调，也不该打章末标记。
        units = build_units(["一句话。", "两句话。"], group_chars=100, complete_chapter=False)
        self.assertTrue(units[0].chapter_start)
        self.assertFalse(units[-1].chapter_end)

    def test_empty_paragraphs_produce_nothing(self) -> None:
        self.assertEqual(build_units([], group_chars=100), [])
        self.assertEqual(build_units(["   "], group_chars=100), [])


class WithTitleTest(unittest.TestCase):
    def test_title_becomes_first_unit(self) -> None:
        units = [SpeechUnit("正文一。"), SpeechUnit("正文二。")]
        result = with_title(units, "第一章 惊蛰")
        self.assertEqual(result[0].text, "第一章 惊蛰")
        self.assertTrue(result[0].chapter_start)
        self.assertTrue(result[0].paragraph_end)
        self.assertFalse(result[1].chapter_start)

    def test_empty_title_is_noop(self) -> None:
        units = [SpeechUnit("正文。")]
        self.assertEqual(with_title(units, ""), units)


class PaginateTest(unittest.TestCase):
    def test_respects_budget_without_splitting_paragraphs(self) -> None:
        paragraphs = ["甲" * 60, "乙" * 60, "丙" * 60, "丁" * 60]
        pages = paginate(paragraphs, chars_per_page=100)
        # 每页最多塞一段（60 + 60 > 100），共 4 页，且没有任何段落被切开。
        self.assertEqual(len(pages), 4)
        self.assertEqual([p for page in pages for p in page], paragraphs)

    def test_oversized_paragraph_gets_own_page(self) -> None:
        pages = paginate(["短。", "长" * 200, "短。"], chars_per_page=100)
        self.assertEqual(len(pages), 3)

    def test_page_floor_is_80_chars(self) -> None:
        # 小于 80 的预算会被抬到 80，避免配置笔误变成一堆奇怪的短文件。
        pages = paginate(["甲" * 70, "乙" * 70], chars_per_page=10)
        self.assertEqual(len(pages), 2)

    def test_select_pages_takes_first_n(self) -> None:
        paragraphs = ["一段六十个字的正文。" * 6 for _ in range(10)]
        selected = select_pages(paragraphs, 3, chars_per_page=100)
        self.assertEqual(len(selected), 3)
        self.assertEqual(selected, paragraphs[:3])

    def test_select_all_when_pages_zero(self) -> None:
        paragraphs = ["甲。", "乙。"]
        self.assertEqual(select_pages(paragraphs, 0), paragraphs)


class EstimateTest(unittest.TestCase):
    def test_count_chars_ignores_whitespace(self) -> None:
        self.assertEqual(count_chars("你好 世界\n啊"), 5)

    def test_estimate_is_positive_and_proportional(self) -> None:
        short = estimate_read_seconds("一句话。")
        long = estimate_read_seconds("一句话。" * 10)
        self.assertGreater(long, short)

    def test_slower_rate_reads_slower(self) -> None:
        self.assertLess(chars_per_second(-12), chars_per_second(0))
        self.assertLess(chars_per_second(-20), chars_per_second(-12))

    def test_extreme_rate_is_floored(self) -> None:
        # 语速再慢也不能算出 0 或负数，否则时长估算会除零/变负。
        self.assertGreater(chars_per_second(-500), 0)

    def test_estimate_follows_rate(self) -> None:
        text = "话" * 1000
        self.assertGreater(
            estimate_read_seconds(text, rate_percent=-20),
            estimate_read_seconds(text, rate_percent=0),
        )

    def test_explicit_speed_wins_over_rate(self) -> None:
        self.assertAlmostEqual(estimate_read_seconds("话" * 10, 10.0), 1.0, places=6)

    def test_default_rate_is_neutral(self) -> None:
        self.assertAlmostEqual(
            estimate_read_seconds("话" * 100),
            estimate_read_seconds("话" * 100, rate_percent=0),
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
