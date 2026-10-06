"""语音合成：把韵律方案渲染成一条完整的章节音频。

三种引擎，按 ``tts.engine=auto`` 自动择优：

1. ``edge``      —— 微软 Edge TTS（免费、中文音色接近真人，需联网 + ``edge-tts``）
2. ``macos_say`` —— macOS 自带 ``say``，完全离线、零成本（**音色明显更差**）
3. ``silence``   —— 生成等长静音，仅用于 ``--dry-run`` 联调，不产出可听的成品

**为什么一章要分很多次合成再拼**：只有逐段调用才能逐段改变语速/音高/停顿，
也就是「抑扬顿挫」；整体一次合成只能全场一个调，就是「机器朗读」。
拼接在 PCM 层完成，并在每段之间插入按韵律算好的停顿 —— 段与段之间不会出现
换页式的爆音或错位，因为段边界都是**句子边界**，不是「页边界」。

**降级必须显式**：edge 失败会按 ``tts.edge_attempts`` 先重试；确实不行才降到
``macos_say``，此时 ``AudioResult.degraded=True``、日志打 WARNING，
元数据里也记下来 —— 免得用户以为听到的男声换成女声是自己听错了。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import audio
from .config import Config
from .errors import TTSError
from .prosody import ProsodyCue, ProsodyPlan
from .textsplit import estimate_read_seconds
from .utils import ensure_dir, parse_rate_percent, write_json

logger = logging.getLogger(__name__)

_EDGE_WORKER = Path(__file__).resolve().parent / "edge_worker.py"

#: 每段合成给父进程的等待预算（秒）。网络抖动时单段可能要重连几次，
#: 按段数放大超时，比一个固定值更能区分「慢」和「卡死」。
_SECONDS_PER_CUE_BUDGET = 12.0


def _batch_timeout(batch_size: int, base_timeout: float) -> float:
    """一个批次的子进程等待上限：至少 ``base_timeout``，并按段数放大。"""
    return max(float(base_timeout), max(1, int(batch_size)) * _SECONDS_PER_CUE_BUDGET)


@dataclass
class AudioResult:
    """一次合成产出的结果（含“可信度”信息，便于上层写元数据）。"""

    path: Path
    duration: float
    engine: str
    voice: str
    sample_rate: int
    degraded: bool = False
    cues: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


# ==========================================================================
# 引擎基类
# ==========================================================================
class TTSEngine(ABC):
    """引擎只负责「把每段文本变成音频文件」，拼接/转码由基类统一处理。"""

    name = "base"
    display_name = "未命名引擎"
    #: 输出的中间音频格式（决定是否需要转 WAV）
    part_suffix = ".mp3"
    #: 是否是「好听」的引擎。降级到其它引擎时用于标记 degraded。
    is_preferred = False

    def __init__(self, config: Config) -> None:
        self.config = config
        self.voice = str(config.get("tts.voice", ""))
        self.timeout = float(config.get("tts.timeout_seconds", 240))
        self.sample_rate = int(config.get("tts.sample_rate", 24000))

    @abstractmethod
    def available(self) -> bool:
        """当前环境是否可用。"""

    @abstractmethod
    def render_parts(self, plan: ProsodyPlan, work_dir: Path, stem: str) -> list[Path]:
        """按韵律方案渲染出与 ``plan.cues`` 一一对应的音频文件。"""

    def describe(self) -> str:
        return f"{self.display_name}（{self.voice or '默认音色'}）"

    # ------------------------------------------------------------------
    def synthesize(self, plan: ProsodyPlan, work_dir: Path, stem: str, out_path: Path) -> AudioResult:
        """渲染 + 拼接 + 转码，产出 ``out_path``。"""
        if not plan.cues:
            raise TTSError("没有可合成的片段（文本为空）")

        work_dir = ensure_dir(Path(work_dir))
        out_path = Path(out_path)
        ensure_dir(out_path.parent)

        parts: list[Path] = []
        wav_parts: list[Path] = []
        combined = work_dir / f"{stem}.combined.wav"
        try:
            parts = self.render_parts(plan, work_dir, stem)
            if len(parts) != len(plan.cues):
                raise TTSError(f"合成片段数量不符：期望 {len(plan.cues)}，得到 {len(parts)}")

            trim = bool(self.config.get("tts.trim_silence", True))
            threshold = float(self.config.get("tts.trim_threshold_db", -45.0))
            keep_ms = int(self.config.get("tts.trim_keep_ms", 30))
            for index, part in enumerate(parts):
                if part.suffix.lower() == ".wav":
                    wav_part = part
                else:
                    wav_part = work_dir / f"{stem}.p{index:04d}.wav"
                    audio.to_wav(part, wav_part, sample_rate=self.sample_rate, timeout=self.timeout)
                if trim:
                    audio.trim_silence(wav_part, threshold_db=threshold, keep_ms=keep_ms)
                wav_parts.append(wav_part)

            audio.concat_wavs(
                wav_parts,
                combined,
                pauses=[cue.pause_after_ms for cue in plan.cues],
                lead_in_ms=plan.lead_in_ms,
            )
            duration = audio.wav_duration(combined)

            audio.encode_audio(
                combined,
                out_path,
                fmt=str(self.config.get("tts.format", "mp3")),
                bitrate=str(self.config.get("tts.mp3_bitrate", "64k")),
                sample_rate=self.sample_rate,
                timeout=self.timeout,
            )
            if not out_path.is_file() or out_path.stat().st_size < 1024:
                raise TTSError(f"成品音频为空: {out_path.name}")
        finally:
            for path in {*parts, *wav_parts}:
                if path != combined:
                    path.unlink(missing_ok=True)
            combined.unlink(missing_ok=True)

        return AudioResult(
            path=out_path,
            duration=duration,
            engine=self.name,
            voice=self.voice,
            sample_rate=self.sample_rate,
            # 是否「降级」由引擎链决定（见 ``synthesize_plan``）：显式选了
            # macos_say 就不是降级，edge 失败回落才是。
            degraded=False,
            cues=len(plan.cues),
            detail={"cues": len(plan.cues)},
        )


# ==========================================================================
# Edge TTS
# ==========================================================================
class EdgeTTSEngine(TTSEngine):
    """微软 Edge TTS：中文小说首选。

    音色 ``zh-CN-YunjianNeural``（云健）是微软为「小说 / 体育解说」训练的成熟
    男声，磁性、有讲述感，最接近「说书先生」；``zh-CN-YunyangNeural`` 是新闻
    播报腔，``zh-CN-YunxiNeural`` 偏年轻活泼，都不如云健适合讲长篇。

    合成走子进程（``edge_worker.py``）+ 项目 ``.venv`` 的解释器，
    主程序因此可以保持「零第三方依赖」。
    """

    name = "edge"
    display_name = "Edge TTS（神经音色）"
    part_suffix = ".mp3"
    is_preferred = True

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.rate = str(config.get("tts.edge_rate", "-8%"))
        self.pitch = str(config.get("tts.edge_pitch", "-2Hz"))
        self.volume = str(config.get("tts.edge_volume", "+0%"))
        self.concurrency = max(1, int(config.get("tts.edge_concurrency", 3)))
        self._python: str | None = None
        self._python_resolved = False

    # ------------------------------------------------------------------
    def available(self) -> bool:
        if shutil.which("ffmpeg") is None:
            return False
        return self._resolve_python() is not None

    def _resolve_python(self) -> str | None:
        """找一个能 ``import edge_tts`` 的解释器。"""
        if self._python_resolved:
            return self._python
        self._python_resolved = True

        candidates: list[str] = []
        configured = str(self.config.get("tts.edge_python", "") or "").strip()
        if configured:
            candidates.append(configured)
        env_python = os.environ.get("AUTOREAD_EDGE_PYTHON", "").strip()
        if env_python:
            candidates.append(env_python)
        for base in (Path.cwd(), Path(self.config.root)):
            candidates.append(str(base / ".venv" / "bin" / "python"))
            candidates.append(str(base / ".venv" / "Scripts" / "python.exe"))
        candidates.append(sys.executable)

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if candidate != sys.executable and not Path(candidate).exists():
                continue
            if self._can_import_edge(candidate):
                self._python = candidate
                logger.info("Edge TTS 使用解释器: %s", candidate)
                return candidate
        return None

    @staticmethod
    def _can_import_edge(python: str) -> bool:
        try:
            proc = subprocess.run(
                [python, "-c", "import edge_tts"],
                capture_output=True, text=True, timeout=40, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return proc.returncode == 0

    def describe(self) -> str:
        return f"Edge TTS（{self.voice}，语速 {self.rate}，音高 {self.pitch}）"

    # ------------------------------------------------------------------
    def render_parts(self, plan: ProsodyPlan, work_dir: Path, stem: str) -> list[Path]:
        python = self._resolve_python()
        if python is None:
            raise TTSError(
                "未找到可用的 edge-tts，请先执行："
                "python3 -m venv .venv && ./.venv/bin/pip install edge-tts"
            )

        cues = list(plan.cues)
        targets = [work_dir / f"{stem}.part{index:04d}{self.part_suffix}" for index in range(len(cues))]
        pending = list(range(len(cues)))
        attempts = max(1, int(self.config.get("tts.edge_attempts", 3)))
        backoff = max(0.0, float(self.config.get("tts.edge_retry_backoff_seconds", 1.5)))
        batch_size = max(1, int(self.config.get("tts.batch_size", 24)))
        temporary: list[Path] = []

        total = len(cues)
        for attempt in range(1, attempts + 1):
            if not pending:
                break
            logger.info(
                "Edge 合成：第 %d/%d 次尝试，共 %d 段%s",
                attempt, attempts, len(pending), "" if attempt == 1 else "（仅重试失败段）",
            )
            still_failed: list[int] = []
            batches = _chunks(pending, batch_size)
            # 进入本次尝试前已完成的段数（重试轮次里也成立）。
            completed = total - len(pending)
            for position, batch in enumerate(batches, start=1):
                failures, files = self._run_batch(
                    python, cues, targets, batch, work_dir, stem, attempt
                )
                temporary.extend(files)
                still_failed.extend(failures)
                completed += len(batch) - len(failures)
                # 整章动辄几百段、要跑几分钟，必须给心跳：否则用户看到的是
                # 长时间没有任何输出，分不清「在跑」和「卡死」。
                logger.info(
                    "    进度 %d/%d 段（第 %d/%d 批，本批失败 %d）",
                    completed,
                    total,
                    position,
                    len(batches),
                    len(failures),
                )
            pending = still_failed
            if pending and attempt < attempts:
                wait = backoff * (2 ** (attempt - 1))
                logger.warning("Edge 有 %d 段未成功，%.1fs 后重试", len(pending), wait)
                if wait > 0:
                    time.sleep(wait)

        if pending:
            # 失败时把任务/结果文件留在磁盘上，便于查是哪一段、什么错。
            logger.error(
                "Edge 连续 %d 次仍有 %d 段失败，任务文件保留在 %s",
                attempts, len(pending), work_dir,
            )
            raise TTSError(
                f"Edge TTS 连续 {attempts} 次仍有 {len(pending)} 段合成失败"
                f"（如持续失败请检查网络，或改用 tts.engine=macos_say）"
            )
        for path in temporary:
            path.unlink(missing_ok=True)
        return targets

    def _run_batch(
        self,
        python: str,
        cues: Sequence[ProsodyCue],
        targets: Sequence[Path],
        batch: Sequence[int],
        work_dir: Path,
        stem: str,
        attempt: int,
    ) -> tuple[list[int], list[Path]]:
        """跑一批合成。

        返回 ``(仍然失败的片段下标, 本批产生的临时文件)``。临时文件由调用方在
        **全部成功之后**才删；失败时保留，方便查是哪一段、什么错。
        """
        job_path = work_dir / f"{stem}.job{attempt}_{batch[0]:04d}.json"
        result_path = work_dir / f"{stem}.result{attempt}_{batch[0]:04d}.json"
        payload = {
            "voice": self.voice,
            "rate": self.rate,
            "pitch": self.pitch,
            "volume": self.volume,
            "concurrency": self.concurrency,
            "connect_timeout": float(self.config.get("tts.edge_connect_timeout", 10)),
            "receive_timeout": float(self.config.get("tts.edge_receive_timeout", 60)),
            "result_path": str(result_path),
            "items": [
                {
                    "id": index,
                    "text": cues[index].text,
                    "out_path": str(targets[index]),
                    "rate": cues[index].rate,
                    "pitch": cues[index].pitch,
                    "volume": cues[index].volume,
                }
                for index in batch
            ],
        }
        write_json(job_path, payload)
        result_path.unlink(missing_ok=True)

        budget = _batch_timeout(len(batch), self.timeout)
        try:
            proc = subprocess.run(
                [python, str(_EDGE_WORKER), str(job_path)],
                capture_output=True, text=True, timeout=budget, check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Edge 子进程超时（%.0fs），本批 %d 段视为失败", budget, len(batch))
            return list(batch), [job_path]
        except OSError as exc:
            logger.warning("无法启动 edge 工作器: %s", exc)
            return list(batch), [job_path]

        temporary = [job_path, result_path]
        if proc.returncode not in (0,):
            logger.warning(
                "edge 工作器退出码 %s: %s",
                proc.returncode, (proc.stderr or "").strip()[-200:],
            )

        results: dict[Any, bool] = {}
        for record in _read_results(result_path):
            results[record.get("id")] = bool(record.get("ok"))
            if not record.get("ok"):
                logger.warning("片段 %s 合成失败: %s", record.get("id"), record.get("error"))

        failures: list[int] = []
        for index in batch:
            ok = results.get(index)
            produced = targets[index].is_file() and targets[index].stat().st_size >= 512
            if not ok or not produced:
                failures.append(index)
        return failures, temporary


def _chunks(items: Sequence[int], size: int) -> list[list[int]]:
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def _read_results(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


# ==========================================================================
# macOS say（离线兜底）
# ==========================================================================
class MacSayEngine(TTSEngine):
    """macOS 自带 ``say``：完全离线、零成本，但**音色明显不如 Edge**。

    必须说实话：macOS 的简体中文语音目前只有女声 ``Ting-Ting``（本机实测：
    ``Mei-Jia``/``Sin-ji`` 分别是台湾国语与粤语）。所以一旦降级到这里，
    「成熟有磁性男声」这个要求就保不住了 —— 这正是 edge 失败要**先重试**
    的原因，也是 ``AudioResult.degraded=True`` 要一路记进元数据的原因。

    想要离线也有好音色：系统设置 → 辅助功能 → 朗读内容 → 系统声音，
    下载中文「增强/优质」语音，再把 ``tts.macos_voice`` 指过去。
    """

    name = "macos_say"
    display_name = "macOS say（离线兜底）"
    part_suffix = ".aiff"
    is_preferred = False

    #: 常见的 macOS 中文男声名（不同系统版本命名不一，命中即优先）。
    _MALE_HINTS = ("li-mu", "li mu", "yu-shu", "yushu", "bo-hao", "bohao", "shu", "hao")
    _CHINESE_MALE_HINTS = ("li-mu", "yu-shu", "bo-hao")

    #: 中文基准语速（字/分钟）。``say -r`` 的单位是「词/分钟」，中文按字近似。
    _BASE_WPM = 200

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        configured = str(config.get("tts.macos_voice", "") or "").strip()
        self.voice = configured or (self.resolve_voice() or "")

    # ------------------------------------------------------------------
    def available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("say") is not None

    @classmethod
    def list_voices(cls) -> list[tuple[str, str]]:
        """返回 ``[(名字, locale), ...]``，只列中文语音。"""
        if shutil.which("say") is None:
            return []
        try:
            proc = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=20, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return []
        voices: list[tuple[str, str]] = []
        for line in (proc.stdout or "").splitlines():
            match = re.match(r"^(.+?)\s{2,}([a-z]{2}[_-][A-Za-z]{2,4})\s+#", line.strip())
            if not match:
                continue
            name, locale = match.group(1).strip(), match.group(2).strip()
            if locale.lower().startswith("zh"):
                voices.append((name, locale))
        return voices

    @classmethod
    def resolve_voice(cls) -> str | None:
        """自动挑一个中文语音：优先「增强/优质」→ 优先男声（若有）→ 否则任意中文。"""
        voices = cls.list_voices()
        if not voices:
            return None

        def score(item: tuple[str, str]) -> tuple[int, int, int]:
            name, _locale = item
            lowered = name.lower()
            enhanced = 1 if ("增强" in name or "优质" in name or "premium" in lowered or "enhanced" in lowered) else 0
            male = 1 if any(hint in lowered for hint in cls._MALE_HINTS) else 0
            main = 1 if "zh_cn" in lowered.replace("-", "_") else 0
            return (enhanced, male, main)

        voices.sort(key=score, reverse=True)
        return voices[0][0]

    # ------------------------------------------------------------------
    def _wpm(self, cue: ProsodyCue) -> int:
        percent = parse_rate_percent(cue.rate)
        return max(120, min(320, int(round(self._BASE_WPM * (1.0 + percent / 100.0)))))

    def render_parts(self, plan: ProsodyPlan, work_dir: Path, stem: str) -> list[Path]:
        if not self.voice:
            raise TTSError("macOS 找不到可用的中文语音，请安装中文语音包或改用 tts.engine=edge")
        parts: list[Path] = []
        for index, cue in enumerate(plan.cues):
            aiff = work_dir / f"{stem}.p{index:04d}.aiff"
            wav = work_dir / f"{stem}.p{index:04d}.wav"
            try:
                proc = subprocess.run(
                    ["say", "-v", self.voice, "-r", str(self._wpm(cue)), "-o", str(aiff), cue.text],
                    capture_output=True, text=True, timeout=self.timeout, check=False,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise TTSError(f"macOS say 执行失败: {exc}") from exc
            if proc.returncode != 0 or not aiff.is_file():
                raise TTSError(f"macOS say 失败: {(proc.stderr or '').strip()[-200:]}")
            audio.to_wav(aiff, wav, sample_rate=self.sample_rate, timeout=self.timeout)
            aiff.unlink(missing_ok=True)
            parts.append(wav)
        return parts


# ==========================================================================
# 静音（dry-run）
# ==========================================================================
class SilenceEngine(TTSEngine):
    """生成等长静音，仅用于 ``--dry-run``：验证切分、进度与文件结构，不产出可听音频。"""

    name = "silence"
    display_name = "静音（dry-run，仅供联调）"
    part_suffix = ".wav"
    is_preferred = False

    def available(self) -> bool:
        return True

    def render_parts(self, plan: ProsodyPlan, work_dir: Path, stem: str) -> list[Path]:
        parts: list[Path] = []
        for index, cue in enumerate(plan.cues):
            percent = parse_rate_percent(cue.rate)
            seconds = max(0.3, estimate_read_seconds(cue.text, rate_percent=percent))
            path = work_dir / f"{stem}.p{index:04d}.wav"
            audio.write_silence_wav(path, seconds, rate=self.sample_rate)
            parts.append(path)
        return parts


# ==========================================================================
# 引擎链与降级
# ==========================================================================
def build_engine_chain(config: Config, *, engine: str | None = None) -> list[TTSEngine]:
    """按配置组装「首选 → 兜底」的引擎链。

    ``auto`` 的择优顺序：Edge（音色好）→ macOS say（离线）→ 静音（只保证流程能跑）。
    ``tts.fallback_engine=none`` 时去掉兜底 —— 宁可不产出，也不产出机器音。
    """
    requested = (engine or str(config.get("tts.engine", "auto"))).strip().lower()
    fallback_name = str(config.get("tts.fallback_engine", "macos_say")).strip().lower()
    # 中文别名，方便命令行/环境变量直接写。
    aliases = {
        "": "auto", "auto": "auto", "edge": "edge", "edge-tts": "edge",
        "say": "macos_say", "macos": "macos_say", "macos_say": "macos_say",
        "macos-say": "macos_say", "silence": "silence", "dry": "silence", "none": "none",
    }
    requested = aliases.get(requested, requested)
    fallback_name = aliases.get(fallback_name, fallback_name)

    def make(name: str) -> TTSEngine | None:
        if name == "edge":
            return EdgeTTSEngine(config)
        if name in {"macos_say", "say"}:
            return MacSayEngine(config)
        if name == "silence":
            return SilenceEngine(config)
        return None

    chain: list[TTSEngine] = []
    if requested == "auto":
        for name in ("edge", "macos_say"):
            engine_obj = make(name)
            if engine_obj and engine_obj.available():
                chain.append(engine_obj)
    elif requested == "none":
        raise TTSError("tts.engine=none 不产出任何音频")
    else:
        engine_obj = make(requested)
        if engine_obj is None:
            raise TTSError(f"未知的 TTS 引擎: {requested}")
        if not engine_obj.available() and requested != "silence":
            raise TTSError(f"TTS 引擎 {requested} 在当前环境不可用（缺 ffmpeg / edge-tts / say）")
        chain.append(engine_obj)
        # 只有「首选引擎」才配兜底：显式要求 macos_say / silence 时再降级，
        # 就违背了用户的明确选择。
        if engine_obj.is_preferred and fallback_name not in {"auto", "none", requested}:
            alt = make(fallback_name)
            if alt and alt.available():
                chain.append(alt)

    if not chain:
        chain.append(SilenceEngine(config))
        logger.warning("没有可用的语音引擎，退化为静音（只能验证流程，不能听）")
    return chain


def synthesize_plan(
    config: Config,
    plan: ProsodyPlan,
    *,
    work_dir: Path,
    stem: str,
    out_path: Path,
    engine: str | None = None,
    force_silence: bool = False,
) -> AudioResult:
    """沿引擎链依次尝试，返回首个成功的结果。

    降级时打 WARNING 并在结果里标 ``degraded`` —— 「音色悄悄变差」是最难发现、
    也最影响听感的故障。
    """
    chain = [SilenceEngine(config)] if force_silence else build_engine_chain(config, engine=engine)
    errors: list[str] = []
    for position, engine_obj in enumerate(chain):
        if not engine_obj.available():
            continue
        try:
            result = engine_obj.synthesize(plan, work_dir, stem, out_path)
        except TTSError as exc:
            errors.append(f"{engine_obj.name}: {exc}")
            logger.warning("引擎 %s 合成失败：%s", engine_obj.name, exc)
            continue
        if position > 0:
            logger.warning(
                "配音已降级：%s —— 音色与预期不同（期望：%s）",
                engine_obj.describe(),
                chain[0].display_name,
            )
            result.degraded = True
            result.detail["fallback_from"] = chain[0].name
        return result

    raise TTSError(
        ("所有可用引擎都失败：" + "；".join(errors)) if errors else "没有可用引擎"
    )


def resolve_engine_preview(config: Config, engine: str | None = None) -> str:
    """给日志/报告用：这一次实际会用什么引擎。"""
    try:
        chain = build_engine_chain(config, engine=engine)
    except TTSError as exc:
        return f"不可用（{exc}）"
    return chain[0].describe()


__all__ = [
    "AudioResult",
    "TTSEngine",
    "EdgeTTSEngine",
    "MacSayEngine",
    "SilenceEngine",
    "build_engine_chain",
    "synthesize_plan",
    "resolve_engine_preview",
]
