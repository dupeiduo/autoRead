"""产出评估测试：大小换算、已产出统计、实测校准、整本评估。

这些数字是用户决定「要不要现在跑、磁盘够不够」的依据，所以每一步算法都要能算清楚：
码率怎么换算、样本怎么取、外推用哪个斜率。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from fixtures import build_epub

from autoread.config import Config
from autoread.epub import load_book
from autoread.report import (
    BookEstimate,
    build_estimate,
    calibrate_bytes_per_char,
    estimate_chapter,
    estimate_size_bytes,
    iter_audio_files,
    parse_bitrate,
    render_estimate,
    summarize_output,
)
from autoread.textsplit import chars_per_second


def _write_output(
    out_dir: Path,
    *,
    name: str,
    chapter_index: int,
    chars: int,
    duration: float,
    size: int,
    preview_pages: int | None = None,
    title: str = "第一章",
) -> Path:
    """造一个「已产出」的假文件 + 旁车元数据。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = out_dir / f"{name}.mp3"
    audio.write_bytes(b"\x00" * size)
    (out_dir / f"{name}.mp3.json").write_text(
        json.dumps(
            {
                "chapter_index": chapter_index,
                "chapter_title": title,
                "preview_pages": preview_pages,
                "chars": chars,
                "audio": {"duration": duration, "bytes": size},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return audio


class ParseBitrateTest(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(parse_bitrate("64k"), 64000)
        self.assertEqual(parse_bitrate("96K"), 96000)
        self.assertEqual(parse_bitrate("1m"), 1_000_000)
        self.assertEqual(parse_bitrate(128000), 128000)
        self.assertEqual(parse_bitrate("64000"), 64000)

    def test_bad_input_falls_back(self) -> None:
        self.assertEqual(parse_bitrate(""), 64000)
        self.assertEqual(parse_bitrate("高"), 64000)
        self.assertEqual(parse_bitrate("", default=48000), 48000)


class EstimateSizeTest(unittest.TestCase):
    def test_mp3_from_bitrate(self) -> None:
        # 100 秒 × 64kbps ÷ 8 = 800,000 字节（再乘 0.1% 容器开销）
        self.assertEqual(estimate_size_bytes(100, fmt="mp3", bitrate="64k"), 800800)

    def test_mp3_bitrate_scales(self) -> None:
        low = estimate_size_bytes(600, fmt="mp3", bitrate="48k")
        high = estimate_size_bytes(600, fmt="mp3", bitrate="96k")
        self.assertEqual(high, low * 2)

    def test_wav_is_uncompressed_pcm(self) -> None:
        # 24000 Hz × 单声道 × 16bit = 48,000 字节/秒
        self.assertEqual(
            estimate_size_bytes(100, fmt="wav", sample_rate=24000, channels=1),
            100 * 24000 * 2 + 64,
        )

    def test_wav_is_much_bigger_than_mp3(self) -> None:
        self.assertGreater(
            estimate_size_bytes(600, fmt="wav"),
            estimate_size_bytes(600, fmt="mp3", bitrate="64k") * 5,
        )

    def test_zero_duration(self) -> None:
        self.assertEqual(estimate_size_bytes(0, fmt="mp3"), 0)


class SummarizeOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_counts_files_bytes_and_duration(self) -> None:
        _write_output(self.tmp, name="0001_a", chapter_index=1, chars=1000, duration=200.0, size=1000)
        _write_output(self.tmp, name="0002_b", chapter_index=2, chars=1200, duration=250.0, size=2000)
        summary = summarize_output(self.tmp)
        self.assertEqual(summary.files, 2)
        self.assertEqual(summary.bytes, 3000)
        self.assertEqual(summary.duration, 450.0)
        self.assertEqual(summary.chapters, 2)
        self.assertEqual(summary.average_bytes, 1500)

    def test_preview_and_full_chapter_count_as_one_chapter(self) -> None:
        # 试听片段与整章产物会同时存在，按文件数报「已完成多少章」会翻倍。
        _write_output(self.tmp, name="0001_title_p1-3", chapter_index=1, chars=1000,
                      duration=230.0, size=1800, preview_pages=3)
        _write_output(self.tmp, name="0001_title", chapter_index=1, chars=20000,
                      duration=4500.0, size=36000)
        summary = summarize_output(self.tmp)
        self.assertEqual(summary.files, 2)
        self.assertEqual(summary.chapters, 1)
        self.assertEqual(summary.chapter_numbers, [1])

    def test_average_chapter_ignores_preview_samples(self) -> None:
        # 3 页试听只有 1.75 MB，混进「平均每章」会把整本大小估小几倍。
        _write_output(self.tmp, name="0001_title_p1-3", chapter_index=1, chars=1000,
                      duration=230.0, size=1_750_000, preview_pages=3)
        _write_output(self.tmp, name="0001_title", chapter_index=1, chars=20000,
                      duration=4500.0, size=34_440_000)
        summary = summarize_output(self.tmp)
        self.assertEqual(summary.full_chapters, 1)
        self.assertEqual(summary.average_chapter_bytes, 34_440_000)
        self.assertAlmostEqual(summary.average_chapter_duration, 4500.0, places=3)
        payload = summary.as_dict()
        self.assertEqual(payload["average_chapter_size"], "32.84 MB")

    def test_missing_dir_is_empty(self) -> None:
        summary = summarize_output(self.tmp / "nope")
        self.assertEqual((summary.files, summary.bytes, summary.duration), (0, 0, 0.0))

    def test_json_sidecars_are_not_counted_as_audio(self) -> None:
        _write_output(self.tmp, name="0001_a", chapter_index=1, chars=100, duration=20.0, size=500)
        self.assertEqual(len(iter_audio_files(self.tmp)), 1)


class CalibrateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_uses_full_chapters(self) -> None:
        # 两章共 20000 字 / 2,000,000 字节 → 100 字节/字
        _write_output(self.tmp, name="0001_a", chapter_index=1, chars=10000, duration=2200.0, size=1_000_000)
        _write_output(self.tmp, name="0002_b", chapter_index=2, chars=10000, duration=2200.0, size=1_000_000)
        calibrated = calibrate_bytes_per_char(self.tmp)
        self.assertIsNotNone(calibrated)
        per_char, samples = calibrated  # type: ignore[misc]
        self.assertAlmostEqual(per_char, 100.0, places=6)
        self.assertEqual(samples, 2)

    def test_preview_samples_are_excluded(self) -> None:
        # 试听片段带一次性的章首留白，摊到每字上的固定开销偏高，会系统性偏大。
        _write_output(self.tmp, name="0001_p1-3", chapter_index=1, chars=1000,
                      duration=230.0, size=1_000_000, preview_pages=3)
        self.assertIsNone(calibrate_bytes_per_char(self.tmp))

    def test_no_sidecar_means_no_calibration(self) -> None:
        (self.tmp / "0001_a.mp3").write_bytes(b"\x00" * 1000)
        self.assertIsNone(calibrate_bytes_per_char(self.tmp))


class EstimateChapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.book = load_book(
            build_epub(
                self.tmp / "b.epub",
                title="测试书",
                chapters=[
                    ("第一章 起风", ["风起了。" * 30, "雨来了。" * 30]),
                    ("第二章 落雨", ["雨停了。" * 30]),
                ],
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cues_are_counted_not_guessed(self) -> None:
        chapter = self.book.chapters[0]
        estimate = estimate_chapter(chapter, group_chars=60, rate_percent=-12)
        # 60 字一组，两段各 120 字 → 每段 2 组，加标题共 5 段
        self.assertEqual(estimate.cues, 5)

    def test_duration_is_speech_plus_pauses(self) -> None:
        chapter = self.book.chapters[0]
        estimate = estimate_chapter(chapter, group_chars=60, rate_percent=-12)
        speech_only = estimate.chars / chars_per_second(-12)
        # 时长必须严格长于「只算语音」——停顿、剪静音余量、章首留白都还没算。
        self.assertGreater(estimate.duration, speech_only)
        # 但也不能离谱：整章里停顿是配角（约几个百分点）。
        self.assertLess(estimate.duration, speech_only * 1.2)

    def test_longer_pauses_make_a_longer_file(self) -> None:
        # 这是「可调停顿」的前提：改了 base_pause_ms / paragraph_pause_ms，
        # 评估结果必须跟着变，而不是继续报同一个数。
        chapter = self.book.chapters[0]
        short = estimate_chapter(chapter, group_chars=60, rate_percent=-12,
                                 base_pause_ms=200, paragraph_pause_ms=400)
        long = estimate_chapter(chapter, group_chars=60, rate_percent=-12,
                                base_pause_ms=600, paragraph_pause_ms=1000)
        self.assertGreater(long.duration, short.duration)
        self.assertGreater(long.size_bytes, short.size_bytes)

    def test_lead_in_and_trim_margins_are_counted(self) -> None:
        chapter = self.book.chapters[0]
        base = estimate_chapter(chapter, group_chars=60, rate_percent=-12,
                                base_pause_ms=0, paragraph_pause_ms=0,
                                lead_in_ms=0, trim_keep_ms=0)
        padded = estimate_chapter(chapter, group_chars=60, rate_percent=-12,
                                  base_pause_ms=0, paragraph_pause_ms=0,
                                  lead_in_ms=1000, trim_keep_ms=100)
        # 多出来的是 1s 章首留白 + (段数-1)×2×100ms 的剪静音余量
        expected = 1.0 + max(0, base.cues - 1) * 0.2
        self.assertAlmostEqual(padded.duration - base.duration, expected, places=3)

    def test_zero_chars_is_zero(self) -> None:
        chapter = self.book.chapters[0]
        estimate = estimate_chapter(chapter, paragraphs=["   "], group_chars=60, rate_percent=-12)
        self.assertEqual(estimate.duration, 0.0)
        self.assertEqual(estimate.size_bytes, 0)
        self.assertEqual(estimate.cues, 0)

    def test_slower_rate_means_longer_and_bigger(self) -> None:
        chapter = self.book.chapters[0]
        fast = estimate_chapter(chapter, rate_percent=-8)
        slow = estimate_chapter(chapter, rate_percent=-20)
        self.assertGreater(slow.duration, fast.duration)
        self.assertGreater(slow.size_bytes, fast.size_bytes)

    def test_pages_subset_is_smaller(self) -> None:
        chapter = self.book.chapters[0]
        whole = estimate_chapter(chapter, group_chars=60)
        part = estimate_chapter(chapter, paragraphs=chapter.paragraphs[:1], group_chars=60)
        self.assertLess(part.chars, whole.chars)
        self.assertLess(part.cues, whole.cues)


class BuildEstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out_dir = self.tmp / "out"
        self.book = load_book(
            build_epub(
                self.tmp / "b.epub",
                title="测试书",
                chapters=[
                    ("第一章 起风", ["风起了。" * 30]),
                    ("第二章 落雨", ["雨来了。" * 30]),
                    ("第三章 天晴", ["天晴了。" * 30]),
                ],
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_totals_are_the_sum_of_chapters(self) -> None:
        estimate = build_estimate(self.book, Config())
        self.assertEqual(len(estimate.chapters), 3)
        self.assertEqual(estimate.total_chars, sum(c.chars for c in estimate.chapters))
        self.assertEqual(estimate.total_cues, sum(c.cues for c in estimate.chapters))
        self.assertEqual(estimate.total_bytes, sum(c.size_bytes for c in estimate.chapters))
        self.assertGreater(estimate.total_duration, 0)

    def test_subset_selection(self) -> None:
        estimate = build_estimate(self.book, Config(), chapters=self.book.chapters[:1])
        self.assertEqual(len(estimate.chapters), 1)
        self.assertLess(estimate.total_bytes, build_estimate(self.book, Config()).total_bytes)

    def test_volume_rows_aggregate(self) -> None:
        # 测试书的封面项会把书名当卷名，三章同属一卷。
        estimate = build_estimate(self.book, Config())
        rows = estimate.volume_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chapters"], 3)

    def test_calibration_extrapolates_to_the_whole_book(self) -> None:
        _write_output(self.out_dir, name="0001_a", chapter_index=1, chars=1000,
                      duration=225.0, size=1_798_000)
        estimate = build_estimate(self.book, Config(), out_dir=self.out_dir)
        self.assertIsNotNone(estimate.measured_bytes_per_char)
        self.assertEqual(
            estimate.calibrated_total_bytes,
            int(1798.0 * estimate.total_chars),
        )
        # 校准的意义是「贴近实测」，不是「一定更大」：两者应当同一个量级。
        ratio = estimate.calibrated_total_bytes / estimate.total_bytes  # type: ignore[operator]
        self.assertGreater(ratio, 0.9)
        self.assertLess(ratio, 1.1)

    def test_as_dict_is_json_serialisable(self) -> None:
        _write_output(self.out_dir, name="0001_a", chapter_index=1, chars=1000,
                      duration=225.0, size=1_798_000)
        estimate = build_estimate(self.book, Config(), out_dir=self.out_dir)
        payload = json.dumps(estimate.as_dict(), ensure_ascii=False)
        self.assertIn("total_size", payload)
        self.assertIn("calibrated_total_size", payload)

    def test_preview_scope_is_marked_partial_and_skips_calibration(self) -> None:
        # 试听范围不能拿实测外推整本：试听片段带一次性章首留白，外推没有意义。
        _write_output(self.out_dir, name="0001_a", chapter_index=1, chars=1000,
                      duration=225.0, size=1_000_000)
        estimate = build_estimate(self.book, Config(), out_dir=self.out_dir, complete_chapter=False)
        self.assertTrue(estimate.partial)
        self.assertIsNone(estimate.calibrated_total_bytes)
        self.assertIsNone(estimate.measured_bytes_per_char)
        self.assertNotIn("实测校准", render_estimate(estimate))

    def test_full_scope_is_not_partial(self) -> None:
        estimate = build_estimate(self.book, Config())
        self.assertFalse(estimate.partial)

    def test_remaining_duration_subtracts_done_work(self) -> None:
        _write_output(self.out_dir, name="0001_a", chapter_index=1, chars=1000,
                      duration=225.0, size=1_000_000)
        estimate = build_estimate(self.book, Config(), out_dir=self.out_dir)
        self.assertLess(estimate.remaining_duration, estimate.total_duration)
        self.assertGreaterEqual(estimate.remaining_duration, 0.0)


class RenderEstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.book = load_book(
            build_epub(self.tmp / "b.epub", title="测试书", chapters=[("第一章 起风", ["风起了。" * 30])])
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_text_mentions_the_headline_numbers(self) -> None:
        estimate = build_estimate(self.book, Config())
        text = render_estimate(estimate)
        self.assertIn("测试书", text)
        self.assertIn("文件总大小", text)
        self.assertIn("音频总时长", text)
        self.assertIn("按卷明细", text)

    def test_volume_rows_can_be_limited(self) -> None:
        estimate = build_estimate(self.book, Config())
        self.assertIsInstance(estimate, BookEstimate)
        text = render_estimate(estimate, max_volume_rows=1)
        self.assertIn("按卷明细", text)


if __name__ == "__main__":
    unittest.main()
