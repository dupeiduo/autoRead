"""韵律规划测试：角色判定与逐句变调/变停顿。

这些都是**纯函数**，不碰网络、不碰音频，所以结果必须完全确定 ——
有任何不确定，就说明「这句该怎么说」这件事没被定义清楚。
"""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from autoread.prosody import (
    ROLE_PAUSE_SCALE,
    _shift_percent,
    _shift_pitch,
    _terminal_factor,
    classify_role,
    has_number,
    is_dialogue,
    is_emphasis,
    narration_profile,
    plan_prosody,
    plan_text,
)
from autoread.textsplit import SpeechUnit, build_units


class TerminalFactorTest(unittest.TestCase):
    def test_punctuation_scales(self) -> None:
        self.assertEqual(_terminal_factor("他走了。"), 1.0)
        self.assertEqual(_terminal_factor("快跑！"), 1.25)
        self.assertEqual(_terminal_factor("是你吗？"), 1.20)
        self.assertEqual(_terminal_factor("他犹豫了……"), 1.30)
        self.assertEqual(_terminal_factor("甲，"), 0.70)

    def test_penetrates_closing_quotes(self) -> None:
        # 「站住！”」的收尾符号其实是叹号，不是引号。
        self.assertEqual(_terminal_factor("他喊道：“站住！”"), 1.25)

    def test_unknown_ending_uses_default(self) -> None:
        self.assertEqual(_terminal_factor("没有标点"), 0.9)


class DialogueTest(unittest.TestCase):
    def test_sentence_starting_with_quote_is_dialogue(self) -> None:
        self.assertTrue(is_dialogue("“你来了。”少年说。"))
        self.assertTrue(is_dialogue("「走。」"))

    def test_narration_with_long_quote_counts_as_dialogue(self) -> None:
        self.assertTrue(is_dialogue("他喊道：“你小子给我站住！”"))

    def test_short_quote_with_speech_verb_is_dialogue(self) -> None:
        # 「快回家」只有 3 字，占比不够；但有说话动词，仍应判为对话。
        self.assertTrue(is_dialogue("他喊道：“快回家！”"))

    def test_quoted_term_without_speech_verb_is_not_dialogue(self) -> None:
        # 引号里是「被引用的名词」，不是有人说话，不该用对话语调念。
        self.assertFalse(is_dialogue("就承担起“奉诏监烧献陵祭器”的重任"))

    def test_pure_narration_is_not_dialogue(self) -> None:
        self.assertFalse(is_dialogue("暮色里，小镇名叫泥瓶巷。"))


class EmphasisTest(unittest.TestCase):
    def test_short_exclamation_is_emphasis(self) -> None:
        self.assertTrue(is_emphasis("快跑！"))
        self.assertTrue(is_emphasis("不好！"))

    def test_long_exclamation_is_not_emphasis(self) -> None:
        self.assertFalse(is_emphasis("他" * 30 + "！"))

    def test_plain_short_sentence_is_not_emphasis(self) -> None:
        self.assertFalse(is_emphasis("他走了。"))


class NumberTest(unittest.TestCase):
    def test_digits_detected(self) -> None:
        self.assertTrue(has_number("他今年 38 岁了"))
        self.assertFalse(has_number("他走了。"))

    def test_chinese_numerals_are_not_digits(self) -> None:
        # 刻意只认阿拉伯数字：中文的「一」出现在「一个/一样/一起」里，
        # 一概当作数字会让太多句子被放慢，反而变成新的「节奏别扭」。
        self.assertFalse(has_number("十四岁的陈平安"))
        self.assertFalse(has_number("他一个人走了。"))


class ClassifyRoleTest(unittest.TestCase):
    def test_chapter_end_wins(self) -> None:
        unit = SpeechUnit("结束了。", chapter_end=True)
        self.assertEqual(classify_role(unit), "closing")

    def test_chapter_start_is_opening(self) -> None:
        self.assertEqual(classify_role(SpeechUnit("第一章 惊蛰", chapter_start=True)), "opening")

    def test_dialogue_beats_emphasis(self) -> None:
        self.assertEqual(classify_role(SpeechUnit("“快跑！”")), "dialogue")

    def test_normal_sentence(self) -> None:
        self.assertEqual(classify_role(SpeechUnit("暮色里，小镇名叫泥瓶巷。")), "normal")


class ShiftTest(unittest.TestCase):
    def test_shift_percent(self) -> None:
        self.assertEqual(_shift_percent("-8%", -3.0), "-11%")
        self.assertEqual(_shift_percent("+0%", 3.0), "+3%")
        self.assertEqual(_shift_percent("garbage", 4.0), "+4%")

    def test_shift_pitch(self) -> None:
        self.assertEqual(_shift_pitch("-2Hz", 4.0), "+2Hz")
        self.assertEqual(_shift_pitch("+0Hz", -2.0), "-2Hz")


class PlanProsodyTest(unittest.TestCase):
    def test_opening_is_slower_and_ends_with_long_pause(self) -> None:
        plan = plan_prosody([SpeechUnit("第一章 惊蛰", chapter_start=True, paragraph_end=True)])
        cue = plan.cues[0]
        self.assertEqual(cue.role, "opening")
        self.assertEqual(cue.rate, "-15%")  # -12% 基准 + -3% 角色偏移
        self.assertEqual(cue.pitch, "-3Hz")  # -2Hz 基准 + -1Hz 角色偏移
        self.assertGreater(cue.pause_after_ms, 220)

    def test_dialogue_is_faster_and_higher(self) -> None:
        plan = plan_prosody([SpeechUnit("“你来了。”")])
        cue = plan.cues[0]
        self.assertEqual(cue.role, "dialogue")
        self.assertEqual(cue.rate, "-9%")
        self.assertEqual(cue.pitch, "+2Hz")

    def test_only_paragraph_breaks_pause_not_every_sentence(self) -> None:
        """核心契约：停顿留给「段落换行」，普通句末只做呼吸级停顿。

        段落换行要明显长于普通句末 —— 否则听不出换段；而段落内部的排版换行
        在解析层就已归并，不会在这里产生任何停顿。
        """
        sentence = plan_prosody([SpeechUnit("甲。")], base_pause_ms=220, paragraph_pause_ms=560)
        paragraph = plan_prosody(
            [SpeechUnit("甲。", paragraph_end=True)], base_pause_ms=220, paragraph_pause_ms=560
        )
        # 段落换行至少拿到「段落停顿」这个下限
        self.assertGreaterEqual(paragraph.cues[0].pause_after_ms, 560)
        # 且明显长于普通句末的呼吸停顿
        self.assertGreater(paragraph.cues[0].pause_after_ms, sentence.cues[0].pause_after_ms)
        self.assertGreaterEqual(paragraph.cues[0].pause_after_ms, int(sentence.cues[0].pause_after_ms * 1.5))

    def test_sentence_pause_never_reaches_the_paragraph_pause(self) -> None:
        """层级契约：句末停顿不得盖过段落停顿。

        没有这条，一个「！+ 强调」的句末（0.5s × 1.35 × 1.6 = 1.08s）会比段落换行
        （0.8s）还长，段落分节感就被句末盖掉了 —— 而「只有段落换行才停顿」正是初衷。
        """
        for text in ("甲。", "快跑！", "是你吗？", "他犹豫了……", "“你来了。”"):
            with self.subTest(text=text):
                plan = plan_prosody(
                    [SpeechUnit(text)], base_pause_ms=500, paragraph_pause_ms=800
                )
                self.assertLess(plan.cues[0].pause_after_ms, 800)

    def test_ordinary_sentence_pause_is_the_configured_base(self) -> None:
        # 普通长度的句子以句号收尾 → 正好是 base_pause_ms，配置的数字「所见即所得」。
        plan = plan_prosody(
            [SpeechUnit("他" * 40 + "。")], base_pause_ms=500, paragraph_pause_ms=800
        )
        self.assertEqual(plan.cues[0].pause_after_ms, 500)

    def test_short_sentence_pauses_a_bit_more_than_base(self) -> None:
        # 短句是「顿」：比基准多停一点，但仍要明显短于段落换行。
        plan = plan_prosody([SpeechUnit("他走了。")], base_pause_ms=500, paragraph_pause_ms=800)
        self.assertGreater(plan.cues[0].pause_after_ms, 500)
        self.assertLess(plan.cues[0].pause_after_ms, 800)

    def test_sentence_to_paragraph_ladder_is_monotonic(self) -> None:
        # 台阶：逗号 < 普通句末 < 短句/叹句 ≤ 段落换行 < 章末
        def pause(text: str, paragraph_end: bool = False, chapter_end: bool = False) -> int:
            plan = plan_prosody(
                [SpeechUnit(text, paragraph_end=paragraph_end, chapter_end=chapter_end)],
                base_pause_ms=500,
                paragraph_pause_ms=800,
            )
            return plan.cues[0].pause_after_ms

        comma = pause("他" * 40 + "，")
        ordinary = pause("他" * 40 + "。")
        short = pause("他走了。")
        shout = pause("快跑！")
        paragraph = pause("他" * 40 + "。", paragraph_end=True)
        chapter = pause("他" * 40 + "。", chapter_end=True)

        self.assertLess(comma, ordinary)
        self.assertLess(ordinary, short)
        self.assertLessEqual(shout, paragraph)
        self.assertLess(short, paragraph)
        self.assertLess(paragraph, chapter)

    def test_defaults_match_the_shipped_config(self) -> None:
        from autoread.config import Config

        config = Config()
        plan = plan_prosody([SpeechUnit("他" * 40 + "。")])
        self.assertEqual(plan.cues[0].pause_after_ms, config.get("prosody.base_pause_ms"))

    def test_paragraph_end_enforces_floor(self) -> None:
        plan = plan_prosody(
            [SpeechUnit("甲。", paragraph_end=True)],
            base_pause_ms=200,
            paragraph_pause_ms=500,
        )
        self.assertGreaterEqual(plan.cues[0].pause_after_ms, 500)

    def test_chapter_end_pause_is_longest(self) -> None:
        plan = plan_prosody(
            [SpeechUnit("完。", chapter_end=True)],
            base_pause_ms=200,
            paragraph_pause_ms=400,
        )
        self.assertGreaterEqual(plan.cues[0].pause_after_ms, 600)

    def test_pitch_is_clamped(self) -> None:
        plan = plan_prosody([SpeechUnit("“喊！”")], base_pitch="+0Hz", max_pitch_hz=2.0)
        self.assertEqual(plan.cues[0].pitch, "+2Hz")

    def test_not_expressive_means_single_tone(self) -> None:
        units = [
            SpeechUnit("第一章", chapter_start=True, paragraph_end=True),
            SpeechUnit("“快跑！”"),
        ]
        plan = plan_prosody(units, expressive=False)
        self.assertTrue(all(cue.role == "normal" for cue in plan.cues))

    def test_empty_plan_has_no_lead_in(self) -> None:
        plan = plan_prosody([])
        self.assertEqual(len(plan), 0)
        self.assertEqual(plan.lead_in_ms, 0)

    def test_role_pause_scale_is_complete(self) -> None:
        # 每个可能出现的角色都必须有停顿倍率，否则会静默退化成 1.0。
        for role in ("opening", "dialogue", "emphasis", "number", "closing", "normal"):
            with self.subTest(role=role):
                self.assertIn(role, ROLE_PAUSE_SCALE)


class NarrationProfileTest(unittest.TestCase):
    def test_short_sentence_is_slower_and_lower(self) -> None:
        rate, pitch, pause = narration_profile(SpeechUnit("他走了。"))
        self.assertLess(rate, 0)
        self.assertLess(pitch, 0)
        self.assertGreater(pause, 1.0)

    def test_long_sentence_is_faster_and_brighter(self) -> None:
        rate, pitch, pause = narration_profile(SpeechUnit("他" * 80))
        self.assertGreater(rate, 0)
        self.assertGreater(pitch, 0)
        self.assertLess(pause, 1.0)

    def test_paragraph_arc_goes_from_high_to_low(self) -> None:
        first = SpeechUnit("甲" * 40, paragraph_pos=0, paragraph_groups=2)
        last = SpeechUnit("甲" * 40, paragraph_pos=1, paragraph_groups=2)
        self.assertGreater(narration_profile(first)[1], narration_profile(last)[1])

    def test_single_group_paragraph_has_no_arc(self) -> None:
        plain = SpeechUnit("甲" * 40, paragraph_pos=0, paragraph_groups=1)
        # 单组段落没有对照，弧线必须是 0；否则每个短段都会被硬拉出起伏。
        self.assertEqual(narration_profile(plain)[1], 0.0)


class PlanTextTest(unittest.TestCase):
    def test_plan_text_splits_lines_into_paragraphs(self) -> None:
        plan = plan_text("第一段第一句。第一段第二句。\n第二段。", group_chars=10)
        self.assertGreaterEqual(len(plan.cues), 2)
        self.assertTrue(plan.cues[0].text.startswith("第一段第一句"))
        # 段末组应拿到段落级别的停顿
        self.assertGreaterEqual(plan.cues[-1].pause_after_ms, 1)


class NarrationVariationTest(unittest.TestCase):
    """「有抑扬顿挫」的回归护栏：纯叙述也必须产生多种念法。"""

    def test_pure_narration_is_not_single_tone(self) -> None:
        units = build_units(
            [
                "二月二，龙抬头。",
                "暮色里，小镇名叫泥瓶巷的僻静地方，有个孤苦伶仃的清瘦少年，"
                "此时他正按照习俗一手持蜡烛一手持桃枝照耀房梁墙壁木床等处。",
                "少年姓陈，名平安，爹娘早逝。",
            ],
            group_chars=60,
        )
        plan = plan_prosody(units)
        self.assertGreater(len(plan.cues), 2)
        rates = {cue.rate for cue in plan.cues}
        pitches = {cue.pitch for cue in plan.cues}
        pauses = {cue.pause_after_ms for cue in plan.cues}
        # 语速、音高、停顿至少有两档，否则就是「念稿」而不是「说书」。
        self.assertGreater(len(rates), 1, "语速没有变化")
        self.assertGreater(len(pitches), 1, "音高没有变化")
        self.assertGreater(len(pauses), 1, "停顿没有变化")


if __name__ == "__main__":
    unittest.main()
