"""音频工具：读 WAV、拼 WAV、剪静音、转码。

为什么不用 ``audioop``：它在 Python 3.13 已被移除（本项目跑在 3.14）。
这里用 ``array('h')`` 直接操作 16bit PCM，够用且不依赖任何东西。

**剪静音是「不卡顿」的关键一环**：Edge TTS 每段合成结果的首尾都自带几十到
几百毫秒不等的静音。若直接拼接，拼接处的静音会是「两段自带静音 + 我们插入的
停顿」，长度忽长忽短，听感上就是忽快忽慢的「卡顿」。剪掉后，停顿完全由韵律
规划决定，节拍才稳定。
"""

from __future__ import annotations

import array
import math
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any, Sequence

from .errors import TTSError

#: 16bit PCM 的满量程，用于把 dBFS 阈值换算成样本幅度。
_FULL_SCALE = 32768.0


def wav_info(path: Path) -> tuple[int, int]:
    """返回 ``(sample_rate, channels)``。"""
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getframerate(), handle.getnchannels()
    except (wave.Error, OSError) as exc:
        raise TTSError(f"无法读取音频 {Path(path).name}: {exc}") from exc


def wav_duration(path: Path) -> float:
    """读取 WAV 时长（秒），只用标准库，不需要 ffprobe。"""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            return handle.getnframes() / float(rate)
    except (wave.Error, OSError) as exc:
        raise TTSError(f"无法读取音频 {Path(path).name}: {exc}") from exc


def write_silence_wav(path: Path, seconds: float, *, rate: int = 24000, channels: int = 1) -> None:
    """生成等长静音 WAV（``silence`` 引擎与 ``--dry-run`` 用）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, int(max(0.0, seconds) * rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00" * frames * channels * 2)


def _silence_bytes(milliseconds: int, params: Any) -> bytes:
    frames = int(params.framerate * max(0, int(milliseconds)) / 1000)
    return b"\x00" * frames * params.nchannels * params.sampwidth


def concat_wavs(
    parts: Sequence[Path],
    target: Path,
    *,
    pauses: Sequence[int] | None = None,
    pause_ms: int = 0,
    lead_in_ms: int = 0,
) -> None:
    """按顺序拼接同格式 WAV，并在片段之间插入停顿。

    ``pauses[i]`` 是第 ``i`` 段**之后**的停顿时长；``lead_in_ms`` 在最前面
    插一段留白（章首的「话说……」之前那口气）。所有片段格式必须一致 ——
    不一致说明合成链路出了问题，宁可报错也不要拼出一段噪音。
    """
    if not parts:
        raise TTSError("没有可拼接的音频片段")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()
        frames: list[bytes] = []
        if lead_in_ms > 0:
            frames.append(_silence_bytes(lead_in_ms, params))
        frames.append(first.readframes(first.getnframes()))

    def gap(index: int) -> int:
        if pauses is not None and index < len(pauses):
            return max(0, int(pauses[index]))
        return max(0, int(pause_ms))

    for index, part in enumerate(parts[1:]):
        with wave.open(str(part), "rb") as handle:
            if handle.getparams()[:3] != params[:3]:
                raise TTSError(f"音频片段格式不一致: {Path(part).name}")
            milliseconds = gap(index)
            if milliseconds > 0:
                frames.append(_silence_bytes(milliseconds, params))
            frames.append(handle.readframes(handle.getnframes()))

    with wave.open(str(target), "wb") as output:
        output.setparams(params)
        output.writeframes(b"".join(frames))


def trim_silence(path: Path, *, threshold_db: float = -45.0, keep_ms: int = 30) -> tuple[float, float]:
    """原地剪掉首尾静音，返回 ``(剪掉的开头秒数, 剪掉的结尾秒数)``。

    保留 ``keep_ms`` 的余量，避免把爆破音/气声的起音也削掉（那会听起来很「硬」）。
    """
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as handle:
            params = handle.getparams()
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, OSError) as exc:
        raise TTSError(f"无法读取音频 {path.name}: {exc}") from exc

    if params.sampwidth != 2 or not raw:
        return 0.0, 0.0

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()

    channels = max(1, params.nchannels)
    total_frames = len(samples) // channels
    if total_frames <= 1:
        return 0.0, 0.0

    threshold = max(1.0, _FULL_SCALE * (10.0 ** (float(threshold_db) / 20.0)))

    def frame_peak(frame_index: int) -> int:
        base = frame_index * channels
        return max(abs(samples[base + offset]) for offset in range(channels))

    first = 0
    while first < total_frames and frame_peak(first) < threshold:
        first += 1
    last = total_frames - 1
    while last > first and frame_peak(last) < threshold:
        last -= 1
    if first >= total_frames:
        # 整段都是静音：保持原样，交给上层判断（多半是合成失败）。
        return 0.0, 0.0

    margin = max(0, int(params.framerate * max(0, int(keep_ms)) / 1000))
    start_frame = max(0, first - margin)
    end_frame = min(total_frames, last + 1 + margin)

    trimmed = samples[start_frame * channels : end_frame * channels]
    if sys.byteorder == "big":
        trimmed.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setparams(params)
        handle.writeframes(trimmed.tobytes())

    rate = float(params.framerate or 1)
    return start_frame / rate, (total_frames - end_frame) / rate


def run_ffmpeg(args: Sequence[str], *, timeout: float = 240.0) -> None:
    """执行 ffmpeg，失败时抛出带 stderr 摘要的异常。"""
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise TTSError(f"ffmpeg 超时（{timeout}s）") from exc
    except FileNotFoundError as exc:
        raise TTSError("找不到 ffmpeg，请先安装（macOS: brew install ffmpeg）") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise TTSError(f"ffmpeg 失败（exit={proc.returncode}）: {tail}")


def to_wav(source: Path, target: Path, *, sample_rate: int = 24000, timeout: float = 240.0) -> None:
    """把任意音频（Edge TTS 输出的是 mp3）转成 16bit PCM WAV。

    指定 ``-ac 1`` 单声道：听书不需要立体声，单声道体积更小、拼接更稳。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-i", str(source),
            "-ar", str(int(sample_rate)),
            "-ac", "1",
            "-sample_fmt", "s16",
            "-acodec", "pcm_s16le",
            str(target),
        ],
        timeout=timeout,
    )


def encode_audio(
    source_wav: Path,
    target: Path,
    *,
    fmt: str = "mp3",
    bitrate: str = "64k",
    sample_rate: int = 24000,
    timeout: float = 240.0,
) -> None:
    """把成品 WAV 编码成目标格式（``mp3`` / ``wav``）。

    有声书用 ``libmp3lame`` + 64kbps 单声道：人声在这个码率下几乎听不出损失，
    而体积只有 WAV 的十分之一 —— 一本书 483 章，体积差别是「几 GB」和「几百 MB」。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt.lower() == "wav":
        if target.resolve() != Path(source_wav).resolve():
            run_ffmpeg(["-i", str(source_wav), "-ar", str(int(sample_rate)), "-ac", "1",
                        "-acodec", "pcm_s16le", str(target)], timeout=timeout)
        return
    run_ffmpeg(
        [
            "-i", str(source_wav),
            "-ar", str(int(sample_rate)),
            "-ac", "1",
            "-codec:a", "libmp3lame",
            "-b:a", str(bitrate),
            str(target),
        ],
        timeout=timeout,
    )


def silence_dbfs(raw: bytes) -> float:
    """估算一段 16bit PCM 的平均电平（dBFS），用于「别把静音当成功产出」的判断。"""
    if not raw:
        return -math.inf
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return -math.inf
    peak = max(abs(value) for value in samples)
    if peak <= 0:
        return -math.inf
    return 20.0 * math.log10(peak / _FULL_SCALE)


def frame_count(path: Path) -> int:
    """返回帧数（用于「长度是否为 0」的快速判断）。"""
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes()
    except (wave.Error, OSError):
        return 0


__all__ = [
    "wav_info",
    "wav_duration",
    "write_silence_wav",
    "concat_wavs",
    "trim_silence",
    "to_wav",
    "encode_audio",
    "run_ffmpeg",
    "silence_dbfs",
    "frame_count",
]