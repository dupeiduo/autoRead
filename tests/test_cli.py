"""命令行测试：章节选择解析、list 命令端到端、引擎链的降级决策。

不碰网络：``make`` 只测「选择与规划」这些确定性部分，真正的合成用 ``--dry-run`` 覆盖。
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import helpers  # noqa: F401

from fixtures import build_epub

from autoread.cli import _book_dir_name, main, parse_chapter_spec, select_chapters, volume_order
from autoread.config import Config
from autoread.errors import BookError, ConfigError
from autoread.epub import load_book
from autoread.tts import SilenceEngine, _batch_timeout, build_engine_chain


class ParseChapterSpecTest(unittest.TestCase):
    def test_single(self) -> None:
        self.assertEqual(parse_chapter_spec("3"), [3])

    def test_list_and_range(self) -> None:
        self.assertEqual(parse_chapter_spec("1,5-7"), [1, 5, 6, 7])

    def test_reversed_range_is_normalised(self) -> None:
        self.assertEqual(parse_chapter_spec("7-5"), [5, 6, 7])

    def test_bad_input_raises(self) -> None:
        with self.assertRaises(ConfigError):
            parse_chapter_spec("一")


class SelectChaptersTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.book = load_book(
            build_epub(
                self.tmp / "book.epub",
                chapters=[
                    ("第一章 惊蛰", ["甲。"]),
                    ("第二章 稗草", ["乙。"]),
                    ("第三章 少女和飞剑", ["丙。"]),
                ],
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _args(**kwargs: object) -> argparse.Namespace:
        base = {"chapter": None, "volume": None, "match": None, "all": False}
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_by_index(self) -> None:
        chapters = select_chapters(self.book, self._args(chapter="2"))
        self.assertEqual([c.title for c in chapters], ["第二章 稗草"])

    def test_by_match(self) -> None:
        chapters = select_chapters(self.book, self._args(match="飞剑"))
        self.assertEqual([c.title for c in chapters], ["第三章 少女和飞剑"])

    def test_all(self) -> None:
        self.assertEqual(len(select_chapters(self.book, self._args(all=True))), 3)

    def test_out_of_range_raises(self) -> None:
        with self.assertRaises(BookError):
            select_chapters(self.book, self._args(chapter="99"))

    def test_no_selection_raises(self) -> None:
        with self.assertRaises(ConfigError):
            select_chapters(self.book, self._args())

    def test_by_volume(self) -> None:
        # 测试书把封面标题当卷名，三章同属第一卷。
        self.assertEqual(len(volume_order(self.book)), 1)
        self.assertEqual(len(select_chapters(self.book, self._args(volume="1"))), 3)

    def test_volume_out_of_range_raises(self) -> None:
        with self.assertRaises(BookError):
            select_chapters(self.book, self._args(volume="9"))

    def test_error_message_mentions_volume(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            select_chapters(self.book, self._args())
        self.assertIn("--volume", str(ctx.exception))


class ListCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.book_path = build_epub(
            self.tmp / "book.epub",
            title="测试书",
            chapters=[("第一章 起风", ["风起了。"]), ("第二章 落雨", ["雨停了。"])],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_json_output(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["list", "--book", str(self.book_path), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["title"], "测试书")
        self.assertEqual(payload["chapters"], 2)
        self.assertEqual(payload["items"][0]["title"], "第一章 起风")

    def test_human_output_mentions_chapters(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["list", "--book", str(self.book_path), "--limit", "2"])
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("第一章 起风", text)
        self.assertIn("第二章 落雨", text)

    def test_missing_book_returns_error_code(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["list", "--book", str(self.tmp / "nope.epub")])
        self.assertEqual(code, 2)


class BookDirNameTest(unittest.TestCase):
    def test_strips_source_id_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = load_book(
                build_epub(Path(tmp) / "b.epub", title="74886_剑来（1-49册）_烽火戏诸侯")
            )
            self.assertEqual(_book_dir_name(book), "剑来（1-49册）_烽火戏诸侯")

    def test_keeps_plain_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = load_book(build_epub(Path(tmp) / "b.epub", title="三体"))
            self.assertEqual(_book_dir_name(book), "三体")

    def test_does_not_strip_short_digit_title(self) -> None:
        # 只有「2 位以上数字 + 分隔符」才算编号前缀，避免误伤以数字开头的书名。
        with tempfile.TemporaryDirectory() as tmp:
            book = load_book(build_epub(Path(tmp) / "b.epub", title="24 个比利"))
            self.assertEqual(_book_dir_name(book), "24_个比利")


class EstimateCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.book_path = build_epub(
            self.tmp / "b.epub",
            title="测试书",
            chapters=[
                # 每章两个 800 字段落 → 分页后是 2 页，--pages 1 才会真的截断
                ("第一章 起风", ["风起了。" * 200, "风又起了。" * 200]),
                ("第二章 落雨", ["雨来了。" * 200, "雨又来了。" * 200]),
            ],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_to_whole_book_and_emits_json(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["estimate", "--book", str(self.book_path), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["chapters"], 2)
        self.assertGreater(payload["total_bytes"], 0)
        self.assertGreater(payload["total_duration"], 0)
        self.assertIsNone(payload["calibrated_total_bytes"])

    def test_scope_narrows_with_volume(self) -> None:
        whole = io.StringIO()
        with redirect_stdout(whole):
            main(["estimate", "--book", str(self.book_path), "--json"])
        one = io.StringIO()
        with redirect_stdout(one):
            main(["estimate", "--book", str(self.book_path), "--chapter", "1", "--json"])
        self.assertLess(
            json.loads(one.getvalue())["total_bytes"],
            json.loads(whole.getvalue())["total_bytes"],
        )

    def test_human_output_and_report_file(self) -> None:
        report = self.tmp / "estimate.json"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                ["estimate", "--book", str(self.book_path), "--report", str(report)]
            )
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("文件总大小", text)
        self.assertIn("按卷明细", text)
        self.assertTrue(report.is_file())
        self.assertIn("total_bytes", report.read_text(encoding="utf-8"))

    def test_bitrate_changes_the_estimate(self) -> None:
        low, high = io.StringIO(), io.StringIO()
        with redirect_stdout(low):
            main(["estimate", "--book", str(self.book_path), "--bitrate", "48k", "--json"])
        with redirect_stdout(high):
            main(["estimate", "--book", str(self.book_path), "--bitrate", "96k", "--json"])
        self.assertLess(
            json.loads(low.getvalue())["total_bytes"],
            json.loads(high.getvalue())["total_bytes"],
        )

    def test_pages_preview_is_smaller(self) -> None:
        whole, preview = io.StringIO(), io.StringIO()
        with redirect_stdout(whole):
            main(["estimate", "--book", str(self.book_path), "--json"])
        with redirect_stdout(preview):
            main(["estimate", "--book", str(self.book_path), "--pages", "1", "--json"])
        self.assertLess(
            json.loads(preview.getvalue())["total_bytes"],
            json.loads(whole.getvalue())["total_bytes"],
        )


class BatchTimeoutTest(unittest.TestCase):
    """批次超时必须按段数放大。

    固定 240s 在整章 470 段时会把「网络慢但仍在推进」的批次判死，24 段全废重来 ——
    每次重来都要重新连一遍，是「越慢越慢」的正反馈。
    """

    def test_scales_with_batch_size(self) -> None:
        self.assertEqual(_batch_timeout(24, 240), 288.0)
        self.assertEqual(_batch_timeout(40, 240), 480.0)

    def test_never_below_the_configured_base(self) -> None:
        self.assertEqual(_batch_timeout(1, 240), 240.0)
        self.assertEqual(_batch_timeout(24, 900), 900.0)

    def test_handles_degenerate_input(self) -> None:
        self.assertEqual(_batch_timeout(0, 240), 240.0)


class EngineChainTest(unittest.TestCase):
    def test_explicit_silence_is_available(self) -> None:
        chain = build_engine_chain(Config(), engine="silence")
        self.assertEqual(len(chain), 1)
        self.assertIsInstance(chain[0], SilenceEngine)

    def test_unknown_engine_raises(self) -> None:
        from autoread.errors import TTSError

        with self.assertRaises(TTSError):
            build_engine_chain(Config(), engine="不存在的引擎")

    def test_dry_run_make_produces_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = build_epub(
                root / "book.epub",
                chapters=[("第一章 起风", ["风起了。", "雨也来了。"])],
            )
            code = main(
                [
                    "make",
                    "--book", str(book_path),
                    "--chapter", "1",
                    "--dry-run",
                    "--out", str(root / "out"),
                    "--work", str(root / "work"),
                ]
            )
            self.assertEqual(code, 0)
            outputs = list((root / "out").rglob("*.mp3"))
            self.assertEqual(len(outputs), 1)
            self.assertGreater(outputs[0].stat().st_size, 1024)
            sidecar = outputs[0].with_suffix(".mp3.json")
            self.assertTrue(sidecar.is_file())
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(meta["chapter_title"], "第一章 起风")
            self.assertEqual(meta["audio"]["engine"], "silence")
            self.assertGreater(meta["audio"]["bytes"], 0)
            # 事后统计：产物目录里应当留下一份整本/本次范围的评估报告
            report = root / "out" / "测试书" / "report.json"
            self.assertTrue(report.is_file())
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["chapters"], 1)
            self.assertIsNotNone(payload["existing"])
            self.assertEqual(payload["existing"]["files"], 1)

    def test_pages_preview_marks_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = build_epub(
                root / "book.epub",
                chapters=[
                    ("第一章 长", ["这是很长的一段话。" * 40, "那又是很长的一段话。" * 40])
                ],
            )
            code = main(
                [
                    "make",
                    "--book", str(book_path),
                    "--chapter", "1",
                    "--pages", "1",
                    "--dry-run",
                    "--out", str(root / "out"),
                    "--work", str(root / "work"),
                ]
            )
            self.assertEqual(code, 0)
            outputs = list((root / "out").rglob("*_试听p1-1.mp3"))
            self.assertEqual(len(outputs), 1)
            meta = json.loads(outputs[0].with_suffix(".mp3.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["preview_pages"], 1)
            self.assertGreater(meta["pages_total"], 1)
            # 文件名要自解释：一眼看出这是试听，而不是记 _p1-3 这种暗号
            self.assertIn("试听", outputs[0].name)

    def test_preview_and_full_chapter_coexist_in_manifest(self) -> None:
        # 先试听、再整章：两条记录都要在，不能后写的把先写的覆盖掉。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = build_epub(
                root / "book.epub",
                chapters=[("第一章 长", ["很长的一段话。" * 200, "又一段很长的话。" * 200])],
            )
            common = ["--book", str(book_path), "--chapter", "1", "--dry-run",
                      "--out", str(root / "out"), "--work", str(root / "work")]
            self.assertEqual(main(["make", *common, "--pages", "1"]), 0)
            self.assertEqual(main(["make", *common]), 0)
            manifest = json.loads((root / "out" / "测试书" / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("1", manifest["chapters"])
            self.assertIn("1@p1", manifest["chapters"])
            self.assertIsNone(manifest["chapters"]["1"]["preview_pages"])
            self.assertEqual(manifest["chapters"]["1@p1"]["preview_pages"], 1)

    def test_untruncated_chapter_has_no_page_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = build_epub(root / "book.epub", chapters=[("第一章 短", ["很短。"])])
            code = main(
                [
                    "make", "--book", str(book_path), "--chapter", "1",
                    "--pages", "30", "--dry-run",
                    "--out", str(root / "out"), "--work", str(root / "work"),
                ]
            )
            self.assertEqual(code, 0)
            outputs = list((root / "out").rglob("*.mp3"))
            self.assertEqual(len(outputs), 1)
            self.assertFalse(outputs[0].stem.endswith("_试听p1-30"))


if __name__ == "__main__":
    unittest.main()
