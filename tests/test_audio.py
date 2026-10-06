"""音频工具测试：拼 WAV、插停顿、剪静音。

这些函数决定「停顿准不准」——停顿不准，听感就是忽快忽慢的卡顿。
"""

from __future__ import annotations

import array
import math
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import helpers  # noqa: F401

from autoread import audio
from autoread.errors import TTSError


def _write_tone(path: Path, seconds: float, *, rate: int = 24000, amplitude: int = 12000) -> None:
    frames = int(seconds * rate)
    samples = array.array(
        "h", (int(amplitude * math.sin(2 * math.pi * 220 * i / rate)) for i in range(frames))
    )
    if sys.byteorder == "big":
        samples.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())


def _write_silence(path: Path, seconds: float, *, rate: int = 24000) -> None:
    audio.write_silence_wav(path, seconds, rate=rate)


class SilenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_silence_duration(self) -> None:
        path = self.tmp / "s.wav"
        audio.write_silence_wav(path, 0.5, rate=24000)
        self.assertAlmostEqual(audio.wav_duration(path), 0.5, places=2)
        self.assertEqual(audio.wav_info(path), (24000, 1))


class ConcatTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pauses_are_inserted_between_parts(self) -> None:
        parts = []
        for index in range(3):
            path = self.tmp / f"p{index}.wav"
            _write_silence(path, 0.2)
            parts.append(path)
        target = self.tmp / "joined.wav"
        audio.concat_wavs(parts, target, pauses=[100, 300])
        # 0.2*3 + 0.1 + 0.3 = 1.0 秒
        self.assertAlmostEqual(audio.wav_duration(target), 1.0, places=2)

    def test_lead_in_is_added_at_the_front(self) -> None:
        parts = []
        for index in range(2):
            path = self.tmp / f"p{index}.wav"
            _write_silence(path, 0.2)
            parts.append(path)
        target = self.tmp / "joined.wav"
        audio.concat_wavs(parts, target, pauses=[0], lead_in_ms=500)
        self.assertAlmostEqual(audio.wav_duration(target), 0.9, places=2)

    def test_uniform_pause_fallback(self) -> None:
        parts = []
        for index in range(2):
            path = self.tmp / f"p{index}.wav"
            _write_silence(path, 0.1)
            parts.append(path)
        target = self.tmp / "joined.wav"
        audio.concat_wavs(parts, target, pause_ms=200)
        self.assertAlmostEqual(audio.wav_duration(target), 0.4, places=2)

    def test_empty_parts_raises(self) -> None:
        with self.assertRaises(TTSError):
            audio.concat_wavs([], self.tmp / "x.wav")

    def test_mismatched_format_raises(self) -> None:
        a = self.tmp / "a.wav"
        b = self.tmp / "b.wav"
        _write_silence(a, 0.1, rate=24000)
        _write_silence(b, 0.1, rate=16000)
        with self.assertRaises(TTSError):
            audio.concat_wavs([a, b], self.tmp / "x.wav")


class TrimSilenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_trims_leading_and_trailing_silence(self) -> None:
        path = self.tmp / "tone.wav"
        head = self.tmp / "h.wav"
        body = self.tmp / "b.wav"
        tail = self.tmp / "t.wav"
        _write_silence(head, 0.3)
        _write_tone(body, 0.4)
        _write_silence(tail, 0.3)
        audio.concat_wavs([head, body, tail], path)
        before = audio.wav_duration(path)
        removed_front, removed_back = audio.trim_silence(path, threshold_db=-45.0, keep_ms=30)
        after = audio.wav_duration(path)

        self.assertAlmostEqual(before, 1.0, places=2)
        self.assertGreater(removed_front, 0.2)
        self.assertGreater(removed_back, 0.2)
        # 保留 30ms 余量，所以剪完仍略长于纯语音的 0.4 秒。
        self.assertGreater(after, 0.4)
        self.assertLess(after, 0.6)

    def test_all_silence_is_left_alone(self) -> None:
        path = self.tmp / "quiet.wav"
        _write_silence(path, 0.2)
        removed = audio.trim_silence(path)
        self.assertEqual(removed, (0.0, 0.0))
        self.assertAlmostEqual(audio.wav_duration(path), 0.2, places=2)


class HelperTest(unittest.TestCase):
    def test_silence_dbfs_of_digital_silence_is_neg_inf(self) -> None:
        self.assertEqual(audio.silence_dbfs(b"\x00" * 200), -math.inf)

    def test_frame_count_of_missing_file_is_zero(self) -> None:
        self.assertEqual(audio.frame_count(Path("/nonexistent/none.wav")), 0)


if __name__ == "__main__":
    unittest.main()
