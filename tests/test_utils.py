"""工具函数与配置测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from autoread.config import Config, DEFAULTS
from autoread.errors import ConfigError
from autoread.utils import (
    format_bytes,
    format_duration,
    format_duration_human,
    normalize_cjk,
    normalize_title,
    parse_dotenv,
    parse_rate_percent,
    read_json,
    safe_filename,
    write_json,
)


class NormalizeTest(unittest.TestCase):
    def test_cjk_spaces_are_removed(self) -> None:
        self.assertEqual(normalize_cjk("你好 世界"), "你好世界")

    def test_latin_spaces_are_kept(self) -> None:
        self.assertEqual(normalize_cjk("hello world 你好"), "hello world 你好")

    def test_full_width_and_newlines_collapse(self) -> None:
        self.assertEqual(normalize_cjk("甲\n\n乙\u3000丙"), "甲乙丙")

    def test_spaces_around_cjk_punctuation_removed(self) -> None:
        self.assertEqual(normalize_cjk("你好 ， 世界 ！"), "你好，世界！")

    def test_title_keeps_the_separator_space(self) -> None:
        self.assertEqual(normalize_title("第一章   惊蛰"), "第一章 惊蛰")


class SafeFilenameTest(unittest.TestCase):
    def test_slashes_and_colons_removed(self) -> None:
        self.assertEqual(safe_filename("第一章 惊蛰/上: 下"), "第一章_惊蛰上_下")

    def test_empty_falls_back(self) -> None:
        self.assertEqual(safe_filename("   ", fallback="chapter"), "chapter")

    def test_length_is_capped(self) -> None:
        self.assertLessEqual(len(safe_filename("字" * 200)), 80)


class DotenvTest(unittest.TestCase):
    def test_parses_keys_comments_and_quotes(self) -> None:
        values = parse_dotenv(
            "# comment\nAUTOREAD_TTS_VOICE=zh-CN-YunjianNeural\n\nEMPTY=\nQUOTED=\"a b\"\n"
        )
        self.assertEqual(values["AUTOREAD_TTS_VOICE"], "zh-CN-YunjianNeural")
        self.assertEqual(values["EMPTY"], "")
        self.assertEqual(values["QUOTED"], "a b")


class JsonTest(unittest.TestCase):
    def test_round_trip_keeps_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.json"
            write_json(path, {"title": "第一章 惊蛰"})
            self.assertIn("惊蛰", path.read_text(encoding="utf-8"))
            self.assertEqual(read_json(path)["title"], "第一章 惊蛰")

    def test_missing_file_returns_default(self) -> None:
        self.assertEqual(read_json(Path("/nope/x.json"), default={"a": 1}), {"a": 1})


class DurationTest(unittest.TestCase):
    def test_formats(self) -> None:
        self.assertEqual(format_duration(59), "0:59")
        self.assertEqual(format_duration(60), "1:00")
        self.assertEqual(format_duration(3661), "1:01:01")


class FormatBytesTest(unittest.TestCase):
    def test_units_are_binary(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1023), "1023 B")
        self.assertEqual(format_bytes(1024), "1.00 KB")
        self.assertEqual(format_bytes(1536), "1.50 KB")
        self.assertEqual(format_bytes(1024 ** 2), "1.00 MB")
        self.assertEqual(format_bytes(1024 ** 3), "1.00 GB")

    def test_negative_keeps_sign(self) -> None:
        self.assertTrue(format_bytes(-1024).startswith("-"))


class FormatDurationHumanTest(unittest.TestCase):
    def test_scales(self) -> None:
        self.assertEqual(format_duration_human(45), "45 秒")
        self.assertEqual(format_duration_human(250), "4 分 10 秒")
        self.assertEqual(format_duration_human(3 * 3600 + 20 * 60), "3 小时 20 分")
        self.assertEqual(format_duration_human(2 * 86400 + 5 * 3600), "2 天 5 小时")

    def test_exact_day_has_no_hours(self) -> None:
        self.assertEqual(format_duration_human(2 * 86400), "2 天")


class ParseRatePercentTest(unittest.TestCase):
    def test_parses_signed_percent(self) -> None:
        self.assertEqual(parse_rate_percent("-12%"), -12.0)
        self.assertEqual(parse_rate_percent("+3%"), 3.0)
        self.assertEqual(parse_rate_percent(" +8 % "), 8.0)

    def test_falls_back_on_garbage(self) -> None:
        self.assertEqual(parse_rate_percent("很快"), 0.0)
        self.assertEqual(parse_rate_percent(None, default=-12.0), -12.0)


class ConfigTest(unittest.TestCase):
    def test_defaults_are_usable(self) -> None:
        config = Config()
        self.assertEqual(config.get("tts.voice"), "zh-CN-YunjianNeural")
        self.assertEqual(config.get("tts.engine"), "auto")
        self.assertEqual(config.get("text.chars_per_page"), 500)

    def test_json_config_overrides_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "config.json").write_text(
                json.dumps({"tts": {"voice": "zh-CN-YunyangNeural"}}), encoding="utf-8"
            )
            config = Config.load(root=root)
            # 只覆盖给定字段，同层其它默认值保留（深合并）。
            self.assertEqual(config.get("tts.voice"), "zh-CN-YunyangNeural")
            self.assertEqual(config.get("tts.edge_rate"), "-12%")

    def test_env_overrides_and_coercion(self) -> None:
        config = Config.load(env={"AUTOREAD_TTS_VOICE": "zh-CN-YunxiNeural"}, use_env=True)
        self.assertEqual(config.get("tts.voice"), "zh-CN-YunxiNeural")

        numeric = Config.load(env={"AUTOREAD_CHARS_PER_PAGE": "300"}, use_env=True)
        self.assertEqual(numeric.get("text.chars_per_page"), 300)
        self.assertIsInstance(numeric.get("text.chars_per_page"), int)

    def test_bad_numeric_env_raises(self) -> None:
        with self.assertRaises(ConfigError):
            Config.load(env={"AUTOREAD_CHARS_PER_PAGE": "abc"}, use_env=True)

    def test_broken_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "config.json").write_text("{ not json", encoding="utf-8")
            with self.assertRaises(ConfigError):
                Config.load(root=root)

    def test_with_overrides_does_not_mutate_original(self) -> None:
        base = Config()
        changed = base.with_overrides({"tts.voice": "x"})
        self.assertEqual(base.get("tts.voice"), "zh-CN-YunjianNeural")
        self.assertEqual(changed.get("tts.voice"), "x")

    def test_path_resolves_against_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config(root=root)
            self.assertEqual(config.path("output_dir"), root / "data/output")

    def test_none_overrides_are_skipped(self) -> None:
        config = Config().with_overrides({"tts.voice": None})
        self.assertEqual(config.get("tts.voice"), DEFAULTS["tts"]["voice"])


class ExampleConfigTest(unittest.TestCase):
    def test_example_config_stays_in_sync_with_defaults(self) -> None:
        """``config/config.example.json`` 必须与内置默认值完全一致。

        它是给人抄的范本；一旦和默认值漂移，用户「照抄」得到的行为就和
        文档描述不一样了 —— 这种偏差最难排查。
        """
        path = helpers.ROOT / "config" / "config.example.json"
        example = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(example, Config().as_dict())

    def test_example_config_is_loadable(self) -> None:
        path = helpers.ROOT / "config" / "config.example.json"
        config = Config(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(config.get("tts.voice"), "zh-CN-YunjianNeural")


if __name__ == "__main__":
    unittest.main()
