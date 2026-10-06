"""配置：内置默认值 ← JSON 配置文件 ← 环境变量，后者覆盖前者。

与 AutoVideo 同源的做法：**默认值必须能直接跑**。一份没改过的配置，
执行 ``python3 -m autoread make --book 剑来.epub --chapter 1 --pages 3``
就应该能产出一段像样的试听。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .utils import load_dotenv

#: 内置默认值。改这里就能改「开箱即用」的行为。
DEFAULTS: dict[str, Any] = {
    # ---------- 输出 ----------
    "output_dir": "data/output",
    "work_dir": "data/work",
    "log_level": "INFO",
    "log_file": "logs/autoread.log",

    # ---------- 文本切分 ----------
    "text": {
        #: 「一页」的近似字符数。EPUB 是流式的，没有真正的页码，
        #: 用字符预算近似，只为 ``--pages`` 预览与进度估算服务。
        "chars_per_page": 500,
        #: 单次 TTS 请求的最大字符数（在句子边界切，不会切断一句话）。
        #: 太小 → 请求数暴涨且语气被切碎；太大 → 一段里塞进好几句话，
        #: 韵律只能按整段给一个调，「抑扬顿挫」就退化成「一段一个调」。
        #: 实测 60 字（约 2~3 句）是听感与请求数的平衡点。
        "tts_group_chars": 60,
        #: 单条句子超过这个长度就按逗号再切，避免一句念到气都不换。
        "sentence_hard_limit": 160,
        #: 丢掉与「章节标题」重复的正文首行（很多 EPUB 会在正文里再排一次标题）。
        "strip_duplicate_heading": True,
    },

    # ---------- 语音合成 ----------
    "tts": {
        #: auto | edge | macos_say | silence
        "engine": "auto",
        #: 云健：微软给「小说/体育解说」训练的成熟男声，磁性、有讲述感，
        #: 最贴近「说书先生」。云希偏年轻活泼、云扬是新闻播报腔，都不如它。
        "voice": "zh-CN-YunjianNeural",
        #: edge 失败时怎么办。macos_say=降级保底；none=宁可不产也不发机器音。
        "fallback_engine": "macos_say",
        #: macOS 兜底音色，留空则自动挑一个中文男声（优先「增强/优质」）。
        "macos_voice": "",
        #: 基准语速/音高。说书是「稳」的，比默认略慢、略沉一点更好听。
        #: -12% ≈ 4.2 字/秒（新闻播报约 5.5~6 字/秒），是「适中偏慢」的讲书速度。
        "edge_rate": "-12%",
        "edge_pitch": "-2Hz",
        "edge_volume": "+0%",
        #: 合成失败重试次数（含首次）与退避基数。网络抖一下不值得换成机器音。
        "edge_attempts": 3,
        "edge_retry_backoff_seconds": 1.5,
        #: 能 ``import edge_tts`` 的解释器；留空则自动发现项目里的 ``.venv``。
        "edge_python": "",
        "timeout_seconds": 240,
        #: 单个子进程最多合成多少段，超过就分批（避免一次调用过久、失败重来代价大）。
        "batch_size": 24,
        #: 子进程内的并发请求数。整本书动辄几百段，而公开端点偶尔会卡住一个连接；
        #: 并发留一点余量（5）能把这些停顿掩盖掉，又不至于把服务端惹毛。
        "edge_concurrency": 5,
        #: 单次连接/整段接收的超时（秒）。短一点更好：失败了上层会重试，
        #: 而卡在那里的连接会一直占着一个并发槽。
        "edge_connect_timeout": 10,
        "edge_receive_timeout": 60,
        #: 输出格式：mp3（体积小，听书首选）/ wav（无损，体积大）。
        "format": "mp3",
        "mp3_bitrate": "64k",
        #: Edge TTS 原生就是 24kHz 单声道，保持原采样率可避免重采样损耗。
        "sample_rate": 24000,
        #: 剪掉每段首尾的静音，让「停顿」完全由韵律规划决定，而不是
        #: 拼接处两段自带的静音叠加成忽长忽短的间隙（听感上就是「卡顿」）。
        "trim_silence": True,
        "trim_threshold_db": -45.0,
        "trim_keep_ms": 30,
    },

    # ---------- 产出评估 ----------
    "estimate": {
        #: 生成吞吐（音频片段/秒）。实测区间很宽，取决于端点网络：
        #: 网络差时 0.65 段/秒（并发 3、连接频频超时），网络好时 2.1 段/秒
        #: （并发 5，283 段里只失败 6 段）。取 1.5 是中间偏保守 ——
        #: 宁可估悲观，也不能让人以为几小时就能跑完一本书。
        "cues_per_second": 1.5,
    },

    # ---------- 韵律（说书口吻） ----------
    "prosody": {
        "enabled": True,
        #: 句末基准停顿（普通句号收尾 ≈ 这个值）。
        #: 重点句、对话、省略号会在此基础上按倍率放大，但**不会**盖过段落停顿。
        "base_pause_ms": 500,
        #: **段落换行**处的停顿下限。这是整章里唯一「必须听出来」的停顿：
        #: 段落内部的排版换行一律归并、不留停顿，只有段落换行才停一下。
        #: 800ms 是「听得出来换段了、又不会拖成冷场」的量。放大它就等于把
        #: 段落分节感调强，是听感上最有效的单一旋钮。
        "paragraph_pause_ms": 800,
        #: 音高抖动上限（Hz），防止逐句变调时跑出人声范围。
        "max_pitch_hz": 10.0,
        #: 是否在章首加一句「有声书开场」的停顿（不读任何文字，只是留白）。
        "chapter_lead_in_ms": 350,
    },
}

#: 环境变量映射：``AUTOREAD_TTS_VOICE`` → ``tts.voice``。
_ENV_MAP: dict[str, str] = {
    "AUTOREAD_OUTPUT_DIR": "output_dir",
    "AUTOREAD_WORK_DIR": "work_dir",
    "AUTOREAD_LOG_LEVEL": "log_level",
    "AUTOREAD_CHARS_PER_PAGE": "text.chars_per_page",
    "AUTOREAD_TTS_ENGINE": "tts.engine",
    "AUTOREAD_TTS_VOICE": "tts.voice",
    "AUTOREAD_TTS_FALLBACK_ENGINE": "tts.fallback_engine",
    "AUTOREAD_MACOS_VOICE": "tts.macos_voice",
    "AUTOREAD_EDGE_RATE": "tts.edge_rate",
    "AUTOREAD_EDGE_PITCH": "tts.edge_pitch",
    "AUTOREAD_BASE_PAUSE_MS": "prosody.base_pause_ms",
    "AUTOREAD_PARAGRAPH_PAUSE_MS": "prosody.paragraph_pause_ms",
    "AUTOREAD_EDGE_PYTHON": "tts.edge_python",
    "AUTOREAD_EDGE_CONCURRENCY": "tts.edge_concurrency",
    "AUTOREAD_TTS_FORMAT": "tts.format",
    "AUTOREAD_TTS_MP3_BITRATE": "tts.mp3_bitrate",
    "AUTOREAD_PROSODY": "prosody.enabled",
}

#: 这些配置项即使从环境变量来，也要转成数字/布尔，而不是留成字符串。
_INT_KEYS = {
    "text.chars_per_page",
    "text.tts_group_chars",
    "text.sentence_hard_limit",
    "tts.edge_attempts",
    "tts.batch_size",
    "tts.edge_concurrency",
    "tts.sample_rate",
    "tts.trim_keep_ms",
    "prosody.base_pause_ms",
    "prosody.paragraph_pause_ms",
    "prosody.chapter_lead_in_ms",
}
_FLOAT_KEYS = {
    "tts.edge_retry_backoff_seconds",
    "tts.trim_threshold_db",
    "prosody.max_pitch_hz",
}
_BOOL_KEYS = {
    "text.strip_duplicate_heading",
    "tts.trim_silence",
    "prosody.enabled",
}
_BOOL_TRUE = {"1", "true", "yes", "on", "y", "t"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并：``override`` 里的嵌套 dict 逐键覆盖，而不是整块替换。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _coerce(dotted: str, value: Any) -> Any:
    """把环境变量来的字符串转成配置期望的类型。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    try:
        if dotted in _BOOL_KEYS:
            return text.lower() in _BOOL_TRUE
        if dotted in _INT_KEYS:
            return int(float(text))
        if dotted in _FLOAT_KEYS:
            return float(text)
    except ValueError as exc:
        raise ConfigError(f"环境变量对应的配置项 {dotted} 不是合法数值: {value!r}") from exc
    return value


class Config:
    """点号访问的配置容器：``config.get("tts.voice")``。"""

    def __init__(self, data: dict[str, Any] | None = None, *, root: Path | None = None) -> None:
        self._data: dict[str, Any] = _deep_merge(DEFAULTS, data or {})
        self.root = Path(root) if root else Path.cwd()

    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        root: Path | None = None,
        env: dict[str, str] | None = None,
        use_env: bool = True,
    ) -> "Config":
        """加载配置：JSON 文件 → 环境变量（后者覆盖）。

        ``path`` 为空时尝试 ``<root>/config/config.json``；不存在就用默认值。
        """
        import os

        root = Path(root) if root else Path.cwd()
        data: dict[str, Any] = {}
        config_path = Path(path) if path else root / "config" / "config.json"
        if config_path.is_file():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConfigError(f"配置文件无法解析: {config_path} ({exc})") from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"配置文件顶层必须是对象: {config_path}")
            data = loaded

        config = cls(data, root=root)
        if use_env:
            if env is None:
                load_dotenv(root / ".env")
                env = dict(os.environ)
            for env_key, dotted in _ENV_MAP.items():
                raw = (env or {}).get(env_key)
                if raw is None or raw == "":
                    continue
                config.set(dotted, _coerce(dotted, raw))
        return config

    # ------------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value

    def with_overrides(self, overrides: dict[str, Any]) -> "Config":
        """返回叠加了 ``overrides`` 的新配置（不修改自身）。"""
        clone = Config(self._data, root=self.root)
        for dotted, value in (overrides or {}).items():
            if value is not None:
                clone.set(dotted, value)
        return clone

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def path(self, dotted: str) -> Path:
        """把配置里的相对路径解析成相对项目根目录的绝对路径。"""
        raw = str(self.get(dotted, "") or "")
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_absolute() else (self.root / candidate)
