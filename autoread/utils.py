"""通用工具：路径、文件名、JSON、中文文本归一化、时长格式化。

刻意全部用标准库：这个项目要求「系统 python3 就能跑」，不引入任何第三方包。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

#: 中日韩汉字、全角标点、全角字母数字。用来判断「空格该不该留」。
_CJK_CHARS = r"\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f"
_SPACE_RUN_RE = re.compile(r"[\s\u00a0\u3000]+")
_BETWEEN_CJK_SPACE_RE = re.compile(rf"(?<=[{_CJK_CHARS}])\s+(?=[{_CJK_CHARS}])")
_AFTER_CJK_OPEN_RE = re.compile(rf"([{_CJK_CHARS}])\s+(?=[，。！？；：、）】》”’…—])")
_BEFORE_CJK_CLOSE_RE = re.compile(r"(?<=[（【《“‘])\s+")

#: 文件名里不能出现的字符（macOS/Linux 都覆盖）。
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def ensure_dir(path: Path) -> Path:
    """创建目录（含父目录）并返回它。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_cjk(text: str) -> str:
    """把 HTML 里抽出来的文本整成「适合朗读的一行」。

    EPUB 的正文经常这样：一段话在源码里被硬换行拆成好几行，甚至每行都带
    缩进空格。若保留这些空白，TTS 要么在行尾停顿（用户听到的「换行卡顿」），
    要么把连续空格读成停顿。所以：

    1. 所有空白（含全角空格、不换行空格）压缩成一个半角空格；
    2. 汉字之间、汉字与全角标点之间的空格直接删掉 —— 中文不该有词间空格；
    3. 英文单词之间的空格保留（中英混排时仍然正确）。
    """
    if not text:
        return ""
    cleaned = _SPACE_RUN_RE.sub(" ", str(text))
    cleaned = _BETWEEN_CJK_SPACE_RE.sub("", cleaned)
    cleaned = _AFTER_CJK_OPEN_RE.sub(r"\1", cleaned)
    cleaned = _BEFORE_CJK_CLOSE_RE.sub("", cleaned)
    return cleaned.strip()


def normalize_title(text: str) -> str:
    """标题用的轻量归一：压缩空白但**保留汉字之间的词间空格**。

    ``normalize_cjk`` 会把「第一章 惊蛰」的空格删掉（正文里中文不该有词间空格），
    但标题恰恰需要这个空格来分隔章号与章名 —— 文件名与列表都靠它可读。
    """
    return _SPACE_RUN_RE.sub(" ", str(text or "")).strip()


def safe_filename(text: str, *, max_length: int = 80, fallback: str = "chapter") -> str:
    """把章节标题变成安全的文件名片段。

    章节标题形如 ``第一章 惊蛰``，含空格与全角标点；这里保留中文与全角标点，
    只清掉路径分隔符等危险字符，并把空格换成 ``_`` 以免命令行下游踩坑。
    """
    cleaned = _UNSAFE_FILENAME_RE.sub("", str(text or "")).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = fallback
    # 按「字符数」截断，超出部分丢弃（章节标题通常很短，很少触发）。
    return cleaned[:max_length] or fallback


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """原子写文本：先写临时文件再 ``os.replace``，避免中断留下半截文件。"""
    path = Path(path)
    ensure_dir(path.parent)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding=encoding, dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """原子写 JSON（``ensure_ascii=False`` 保留中文，便于人工核对）。"""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=indent) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    """读 JSON；文件不存在或损坏时返回 ``default``（元数据坏了不该中断出片）。"""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def parse_dotenv(text: str) -> dict[str, str]:
    """极简 ``.env`` 解析：``KEY=VALUE``、``#`` 注释、可选的引号包裹。

    只做需求范围内的事，不引入 ``python-dotenv``：值里的 ``$`` 不展开、
    不处理多行 —— 配置项都是 Key 或开关，不需要更复杂的能力。
    """
    result: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def load_dotenv(path: Path, *, override: bool = False) -> dict[str, str]:
    """把 ``.env`` 读进 ``os.environ``（默认不覆盖已存在的环境变量）。"""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        values = parse_dotenv(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def format_duration(seconds: float) -> str:
    """把秒数格式化成 ``H:MM:SS`` / ``M:SS``，用于日志与报告。"""
    total = max(0, int(round(float(seconds or 0))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_duration_human(seconds: float) -> str:
    """人话版时长：``2 天 5 小时`` / ``3 小时 20 分`` / ``4 分 10 秒``。

    整本书的预计时长是几百小时，``H:MM:SS`` 会变成 ``709:00:00`` 这种读不出
    量级的字符串 —— 评估规模时「700 小时」比「709:00:00」有用得多。
    """
    total = max(0, int(round(float(seconds or 0))))
    if total >= 86400:
        days, rem = divmod(total, 86400)
        hours = rem // 3600
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    if total >= 3600:
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    if total >= 60:
        minutes, secs = divmod(total, 60)
        return f"{minutes} 分 {secs} 秒" if secs else f"{minutes} 分"
    return f"{total} 秒"


_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_bytes(num_bytes: float) -> str:
    """把字节数格式化成人话（1024 进制，与 ``ls -h`` / Finder 的习惯一致）。"""
    value = float(num_bytes or 0)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for index, unit in enumerate(_BYTE_UNITS):
        if value < 1024 or index == len(_BYTE_UNITS) - 1:
            if unit == "B":
                return f"{sign}{value:.0f} B"
            return f"{sign}{value:.2f} {unit}"
        value /= 1024.0
    return f"{sign}{value:.2f} PB"


_RATE_RE = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*%")


def parse_rate_percent(value: str, default: float = 0.0) -> float:
    """解析 Edge 语速字符串：``"-12%"`` → ``-12.0``；解析不出来就用 ``default``。

    语速在三个地方要用同一个数（合成参数、时长估算、macOS ``say`` 的 wpm 换算），
    各写一份正则迟早会漂移，所以只留这一个解析入口。
    """
    match = _RATE_RE.match(str(value or ""))
    if not match:
        return float(default)
    return float(match.group(1))
