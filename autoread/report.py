"""产出评估与报告：**估算全书要占多少磁盘**，以及统计已经产出了多少。

为什么需要它：一本书几百章。``剑来`` 全书 483 章 / 1135 万字，按默认音色与语速
是 **709 小时**音频、约 **19 GB** mp3，生成要几十小时。这种规模不可能「先跑了再说」，
必须先能评估：

* **事前**：``estimate`` 按「字数 × 语速 → 时长 → 码率 → 字节」算出总量，
  并按卷/章给出明细，好判断磁盘够不够、要不要拆批跑。
* **事后**：``summarize_output`` 扫产物目录，给出真实文件数、真实占用。
* **校准**：只要已经产出过哪怕几章，就用**实测的字节/字**去外推全书 ——
  这比任何理论公式都准，因为它天然包含了编码器开销、剪静音带来的时长变化等因素。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .config import Config
from .epub import Book, Chapter
from .prosody import plan_prosody
from .textsplit import build_units, chars_per_second, count_chars, with_title
from .utils import format_bytes, format_duration_human, parse_rate_percent, read_json

logger = logging.getLogger(__name__)

#: mp3 容器开销（实测 0.04%，取 0.1% 留点余量）。
_MP3_OVERHEAD = 1.001
#: WAV 头（44 字节）+ 可能的 chunk 填充。
_WAV_HEADER_BYTES = 64
#: 生成吞吐（段/秒）的保守估计：并发 3 时实测约 1.1 段/秒。
DEFAULT_CUES_PER_SECOND = 1.0


def parse_bitrate(value: str | int, *, default: int = 64000) -> int:
    """``"64k"`` → ``64000``；``"96000"`` → ``96000``。"""
    if isinstance(value, int):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return default


def estimate_size_bytes(
    duration_seconds: float,
    *,
    fmt: str = "mp3",
    bitrate: str | int = "64k",
    sample_rate: int = 24000,
    channels: int = 1,
) -> int:
    """按「时长 × 每秒字节数」估算单个音频文件的大小。"""
    duration = max(0.0, float(duration_seconds))
    if fmt.lower() == "wav":
        # WAV 是无损 PCM：每秒字节数 = 采样率 × 声道数 × 2 字节（16bit）。
        return int(round(duration * sample_rate * channels * 2 + _WAV_HEADER_BYTES))
    # 取整：文件大小本来就是整数，且 1.001 这类系数在二进制浮点下不精确，
    # 不取整会得到「7207198 / 7207199」这种一位之差的噪音。
    return int(round(duration * parse_bitrate(bitrate) / 8.0 * _MP3_OVERHEAD))


# ==========================================================================
# 已完成产出的统计
# ==========================================================================
@dataclass
class OutputSummary:
    """产物目录的真实情况。"""

    files: int = 0
    bytes: int = 0
    duration: float = 0.0
    chapters: int = 0
    chapter_numbers: list[int] = field(default_factory=list)
    #: 整章产物（不含 ``_p1-3`` 这类试听片段）—— 只有它们能代表「一章有多大」。
    full_chapters: int = 0
    full_bytes: int = 0
    full_duration: float = 0.0

    @property
    def average_bytes(self) -> int:
        """平均每个**文件**的字节数（含试听片段）。"""
        return int(self.bytes / self.files) if self.files else 0

    @property
    def average_chapter_bytes(self) -> int:
        """平均每个**整章**的字节数；没有整章样本时为 0。

        别拿「含试听片段」的平均值去外推整本：3 页的试听只有 1.75 MB，
        会把平均拉到远低于真实整章的水平，据此估出的「整本多大」会小得离谱。
        """
        return int(self.full_bytes / self.full_chapters) if self.full_chapters else 0

    @property
    def average_chapter_duration(self) -> float:
        return self.full_duration / self.full_chapters if self.full_chapters else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "size": format_bytes(self.bytes),
            "duration": round(self.duration, 3),
            "duration_human": format_duration_human(self.duration),
            "chapters": self.chapters,
            "chapter_numbers": self.chapter_numbers,
            "average_bytes": self.average_bytes,
            "full_chapters": self.full_chapters,
            "full_bytes": self.full_bytes,
            "average_chapter_bytes": self.average_chapter_bytes,
            "average_chapter_size": (
                format_bytes(self.average_chapter_bytes) if self.full_chapters else None
            ),
            "average_chapter_duration": round(self.average_chapter_duration, 3),
        }


_AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac")


def iter_audio_files(out_dir: Path) -> list[Path]:
    """列出产物目录里的音频文件（忽略元数据 json）。"""
    if not Path(out_dir).is_dir():
        return []
    return sorted(
        path
        for path in Path(out_dir).iterdir()
        if path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES
    )


def summarize_output(out_dir: Path) -> OutputSummary:
    """扫描产物目录，给出文件数、真实占用与总时长。

    时长优先读旁车 ``.json``（合成时已经记下精确时长）；没有旁车就按文件大小
    除以码率反推 —— 目录被手工整理过时也不会算错量级。

    ``chapters`` 是**去重后的章节数**：试听片段（``_p1-3``）与整章产物会同时存在，
    按文件数报「已完成多少章」会翻倍，那是个会误导人判断进度错的数字。
    """
    summary = OutputSummary()
    for path in iter_audio_files(out_dir):
        summary.files += 1
        summary.bytes += path.stat().st_size
        sidecar = path.with_suffix(path.suffix + ".json")
        meta = read_json(sidecar)
        if isinstance(meta, dict):
            size = path.stat().st_size
            duration = (meta.get("audio") or {}).get("duration")
            index = meta.get("chapter_index")
            if not isinstance(duration, (int, float)):
                duration = size * 8.0 / parse_bitrate("64k")
            summary.duration += float(duration)
            if isinstance(index, int):
                summary.chapter_numbers.append(index)
            if not meta.get("preview_pages"):
                summary.full_chapters += 1
                summary.full_bytes += size
                summary.full_duration += float(duration)
        else:
            summary.duration += path.stat().st_size * 8.0 / parse_bitrate("64k")
    summary.chapter_numbers = sorted(set(summary.chapter_numbers))
    summary.chapters = len(summary.chapter_numbers)
    return summary


def calibrate_bytes_per_char(out_dir: Path) -> tuple[float, int] | None:
    """用已完成产出的「字节 / 字」外推全书大小。

    返回 ``(每字字节数, 样本章节数)``；没有可用样本时返回 ``None``。

    为什么要校准：理论公式只知道码率和估算时长，而实际文件大小还受编码器开销、
    剪静音导致的时长变化影响。已经跑过几章，就该用**实测**去推，而不是继续信任公式。

    **只用整章样本**：试听片段（``preview_pages``）带一次性的章首留白，摊到
    每字上的固定开销偏高，拿它外推整本会系统性偏大。宁可样本少，不要样本歪。
    """
    pairs: list[tuple[int, int]] = []
    for path in iter_audio_files(out_dir):
        meta = read_json(path.with_suffix(path.suffix + ".json"))
        if not isinstance(meta, dict):
            continue
        if meta.get("preview_pages"):
            continue
        chars = meta.get("chars")
        if isinstance(chars, int) and chars > 0 and path.stat().st_size > 0:
            pairs.append((chars, path.stat().st_size))
    if not pairs:
        return None
    total_chars = sum(chars for chars, _ in pairs)
    total_bytes = sum(size for _, size in pairs)
    if total_chars <= 0:
        return None
    return total_bytes / float(total_chars), len(pairs)


# ==========================================================================
# 逐章评估
# ==========================================================================
@dataclass
class ChapterEstimate:
    index: int
    title: str
    volume: str
    chars: int
    cues: int
    duration: float
    size_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "title": self.title,
            "volume": self.volume,
            "chars": self.chars,
            "cues": self.cues,
            "duration": round(self.duration, 3),
            "duration_human": format_duration_human(self.duration),
            "size_bytes": self.size_bytes,
            "size": format_bytes(self.size_bytes),
        }


@dataclass
class BookEstimate:
    book_title: str
    fmt: str
    bitrate: str
    sample_rate: int
    rate_percent: float
    chapters: list[ChapterEstimate]
    total_chars: int = 0
    total_cues: int = 0
    total_duration: float = 0.0
    total_bytes: int = 0
    existing: OutputSummary | None = None
    measured_bytes_per_char: float | None = None
    measured_sample_chapters: int = 0
    cues_per_second: float = DEFAULT_CUES_PER_SECOND
    #: 评估范围是否只是「每章前 N 页」的试听。是的话不能拿实测去外推整本 ——
    #: 试听片段带了章首留白等一次性开销，外推出来的数没有意义。
    partial: bool = False

    @property
    def calibrated_total_bytes(self) -> int | None:
        """按实测「字节/字」外推的全书大小；没有样本时为 ``None``。"""
        if not self.measured_bytes_per_char or not self.total_chars:
            return None
        return int(self.measured_bytes_per_char * self.total_chars)

    @property
    def estimated_generation_seconds(self) -> float:
        if self.cues_per_second <= 0:
            return 0.0
        return self.total_cues / self.cues_per_second

    @property
    def remaining_duration(self) -> float:
        """还没产出的部分还剩多少音频时长。"""
        done = self.existing.duration if self.existing else 0.0
        return max(0.0, self.total_duration - done)

    def volume_rows(self) -> list[dict[str, object]]:
        """按卷聚合，用于明细表。"""
        order: list[str] = []
        buckets: dict[str, list[ChapterEstimate]] = {}
        for chapter in self.chapters:
            key = chapter.volume or "(未分卷)"
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(chapter)
        rows: list[dict[str, object]] = []
        for key in order:
            items = buckets[key]
            rows.append(
                {
                    "volume": key,
                    "chapters": len(items),
                    "chars": sum(item.chars for item in items),
                    "cues": sum(item.cues for item in items),
                    "duration": sum(item.duration for item in items),
                    "size_bytes": sum(item.size_bytes for item in items),
                }
            )
        return rows

    def as_dict(self) -> dict[str, object]:
        return {
            "book_title": self.book_title,
            "format": self.fmt,
            "bitrate": self.bitrate,
            "sample_rate": self.sample_rate,
            "rate": f"{self.rate_percent:+.0f}%",
            "chapters": len(self.chapters),
            "total_chars": self.total_chars,
            "total_cues": self.total_cues,
            "total_duration": round(self.total_duration, 3),
            "total_duration_human": format_duration_human(self.total_duration),
            "total_bytes": self.total_bytes,
            "total_size": format_bytes(self.total_bytes),
            "partial": self.partial,
            "calibrated_bytes_per_char": self.measured_bytes_per_char,
            "calibrated_sample_chapters": self.measured_sample_chapters,
            "calibrated_total_bytes": self.calibrated_total_bytes,
            "calibrated_total_size": (
                format_bytes(self.calibrated_total_bytes)
                if self.calibrated_total_bytes is not None
                else None
            ),
            "estimated_generation_seconds": round(self.estimated_generation_seconds, 1),
            "estimated_generation_human": format_duration_human(self.estimated_generation_seconds),
            "existing": self.existing.as_dict() if self.existing else None,
            "volumes": [
                {
                    **row,
                    "duration": round(float(row["duration"]), 3),
                    "duration_human": format_duration_human(float(row["duration"])),
                    "size": format_bytes(float(row["size_bytes"])),
                }
                for row in self.volume_rows()
            ],
            "items": [chapter.as_dict() for chapter in self.chapters],
        }


def approximate_duration(
    chars: int,
    *,
    paragraphs: int = 1,
    rate_percent: float = -12.0,
    base_pause_ms: int = 500,
    paragraph_pause_ms: int = 800,
    group_chars: int = 60,
    lead_in_ms: int = 350,
    trim_keep_ms: int = 30,
) -> float:
    """不切正文的快速时长估算，供 ``list`` 这类要秒回的命令使用。

    整本 483 章逐章真切一遍要 20 多秒，而 ``list`` 只是给人扫一眼量级；
    ``estimate`` 走的是 ``estimate_chapter`` 的精确逐项累加。这里用
    「片段数 ≈ 字数 ÷ 每片段字数」近似，误差几个百分点，且**跟着停顿配置变**。
    """
    if chars <= 0:
        return 0.0
    per_cue = max(1, int(group_chars))
    cues = max(1, math.ceil(int(chars) / per_cue))
    speech = chars / chars_per_second(rate_percent)
    pauses = max(0, cues - 1) * max(0, int(base_pause_ms)) / 1000.0
    keeps = max(0, cues - 1) * 2 * max(0, int(trim_keep_ms)) / 1000.0
    return speech + pauses + keeps + max(0, int(lead_in_ms)) / 1000.0


def estimate_chapter(
    chapter: Chapter,
    *,
    paragraphs: Sequence[str] | None = None,
    announce_title: bool = True,
    group_chars: int = 60,
    hard_limit: int = 160,
    complete_chapter: bool = True,
    rate_percent: float = 0.0,
    base_pause_ms: int = 500,
    paragraph_pause_ms: int = 800,
    lead_in_ms: int = 350,
    trim_keep_ms: int = 30,
    fmt: str = "mp3",
    bitrate: str | int = "64k",
    sample_rate: int = 24000,
) -> ChapterEstimate:
    """评估单章：片段数、时长、文件大小。

    时长是**逐项加出来**的，不是「字数 ÷ 一个魔法速度」：

    ``时长 = 纯语音 + 段间停顿 + 剪静音余量 + 章首留白``

    * 纯语音：``字数 ÷ 语速速度``（速度只含语音，见 ``textsplit``）；
    * 段间停顿：真的跑一遍 ``plan_prosody`` 把每处停顿加出来，**最后一段的停顿
      不会插入**（拼接时它后面没有内容），所以不计；
    * 剪静音余量：剪静音每段保留 ``keep_ms`` 的头尾，落在拼接缝上就是
      ``2 × keep_ms`` 的额外静音，不加上会低估 1% 左右；
    * 章首留白：``lead_in_ms``。

    这样一改 ``base_pause_ms`` / ``paragraph_pause_ms``，评估结果立刻跟着变 ——
    而不是像固定常数那样悄悄失真。片段数同样是 ``build_units`` **数出来**的，
    它直接决定生成耗时。
    """
    body = list(paragraphs) if paragraphs is not None else chapter.paragraphs
    units = build_units(
        body, group_chars=group_chars, hard_limit=hard_limit, complete_chapter=complete_chapter
    )
    if announce_title:
        units = with_title(units, chapter.title)
    chars = sum(count_chars(unit.text) for unit in units)
    if not chars:
        return ChapterEstimate(
            index=chapter.index,
            title=chapter.title,
            volume=chapter.volume,
            chars=0,
            cues=0,
            duration=0.0,
            size_bytes=0,
        )

    plan = plan_prosody(units, base_pause_ms=base_pause_ms, paragraph_pause_ms=paragraph_pause_ms)
    speech_seconds = chars / chars_per_second(rate_percent)
    cue_count = len(plan.cues)
    pause_seconds = sum(cue.pause_after_ms for cue in plan.cues[:-1]) / 1000.0 if cue_count > 1 else 0.0
    keep_seconds = max(0, cue_count - 1) * 2 * max(0, int(trim_keep_ms)) / 1000.0
    duration = speech_seconds + pause_seconds + keep_seconds + max(0, int(lead_in_ms)) / 1000.0
    return ChapterEstimate(
        index=chapter.index,
        title=chapter.title,
        volume=chapter.volume,
        chars=chars,
        cues=cue_count,
        duration=duration,
        size_bytes=estimate_size_bytes(
            duration, fmt=fmt, bitrate=bitrate, sample_rate=sample_rate
        ),
    )


def build_estimate(
    book: Book,
    config: Config,
    *,
    chapters: Sequence[Chapter] | None = None,
    paragraphs_by_index: dict[int, list[str]] | None = None,
    complete_chapter: bool = True,
    out_dir: Path | None = None,
    cues_per_second: float | None = None,
) -> BookEstimate:
    """汇总整本（或选中的若干章）的产出规模。"""
    fmt = str(config.get("tts.format", "mp3"))
    bitrate = config.get("tts.mp3_bitrate", "64k")
    sample_rate = int(config.get("tts.sample_rate", 24000))
    rate_percent = parse_rate_percent(config.get("tts.edge_rate", "-12%"), default=-12.0)
    group_chars = int(config.get("text.tts_group_chars", 60))
    hard_limit = int(config.get("text.sentence_hard_limit", 160))

    prosody_base = int(config.get("prosody.base_pause_ms", 500))
    prosody_paragraph = int(config.get("prosody.paragraph_pause_ms", 800))
    lead_in = int(config.get("prosody.chapter_lead_in_ms", 350))
    trim_keep = int(config.get("tts.trim_keep_ms", 30))

    selected = list(chapters) if chapters is not None else list(book.chapters)
    items: list[ChapterEstimate] = []
    for chapter in selected:
        items.append(
            estimate_chapter(
                chapter,
                paragraphs=(paragraphs_by_index or {}).get(chapter.index),
                group_chars=group_chars,
                hard_limit=hard_limit,
                complete_chapter=complete_chapter,
                rate_percent=rate_percent,
                base_pause_ms=prosody_base,
                paragraph_pause_ms=prosody_paragraph,
                lead_in_ms=lead_in,
                trim_keep_ms=trim_keep,
                fmt=fmt,
                bitrate=bitrate,
                sample_rate=sample_rate,
            )
        )

    estimate = BookEstimate(
        book_title=book.title,
        fmt=fmt,
        bitrate=str(bitrate),
        sample_rate=sample_rate,
        rate_percent=rate_percent,
        chapters=items,
        total_chars=sum(item.chars for item in items),
        total_cues=sum(item.cues for item in items),
        total_duration=sum(item.duration for item in items),
        total_bytes=sum(item.size_bytes for item in items),
        cues_per_second=float(
            cues_per_second
            if cues_per_second is not None
            else config.get("estimate.cues_per_second", DEFAULT_CUES_PER_SECOND)
        ),
    )
    estimate.partial = not complete_chapter
    if out_dir is not None:
        estimate.existing = summarize_output(out_dir)
        if not estimate.partial:
            calibrated = calibrate_bytes_per_char(out_dir)
            if calibrated:
                estimate.measured_bytes_per_char, estimate.measured_sample_chapters = calibrated
    return estimate


# ==========================================================================
# 展示与落盘
# ==========================================================================
def render_estimate(estimate: BookEstimate, *, max_volume_rows: int = 0) -> str:
    """把评估结果排成人看的表。"""
    lines: list[str] = []
    lines.append(f"《{estimate.book_title}》")
    lines.append(
        f"共 {len(estimate.chapters)} 章 / {estimate.total_chars:,} 字 / "
        f"{estimate.total_cues:,} 个音频片段"
    )
    lines.append(
        f"配置：{estimate.fmt} {estimate.bitrate} · 语速 {estimate.rate_percent:+.0f}% · "
        f"{estimate.sample_rate} Hz"
    )
    lines.append("")
    lines.append("预计产出")
    lines.append(f"  音频总时长   {format_duration_human(estimate.total_duration)}")
    lines.append(f"  文件总大小   {format_bytes(estimate.total_bytes)}")
    lines.append(f"  文件数量     {len(estimate.chapters)} 个（每章一个）")
    if estimate.chapters:
        avg_bytes = estimate.total_bytes // len(estimate.chapters)
        avg_secs = estimate.total_duration / len(estimate.chapters)
        lines.append(
            f"  平均每章     {format_bytes(avg_bytes)} / {format_duration_human(avg_secs)}"
        )
    lines.append(
        f"  生成耗时     ≈{format_duration_human(estimate.estimated_generation_seconds)}"
        f"（按 {estimate.cues_per_second:.1f} 段/秒估算）"
    )

    calibrated = None if estimate.partial else estimate.calibrated_total_bytes
    if calibrated is not None and estimate.measured_bytes_per_char:
        delta = ""
        if estimate.total_bytes:
            delta = f"，较理论值 {(calibrated / estimate.total_bytes - 1) * 100:+.1f}%"
        lines.append("")
        lines.append(
            f"实测校准：{estimate.measured_sample_chapters} 章样本得出 "
            f"{estimate.measured_bytes_per_char:.1f} 字节/字 → 全书约 {format_bytes(calibrated)}{delta}"
        )

    if estimate.existing and estimate.existing.files:
        done = estimate.existing
        lines.append("")
        lines.append("已产出情况")
        lines.append(
            f"  已完成       {done.chapters} 章 / {done.files} 个文件，"
            f"实际占用 {format_bytes(done.bytes)}"
        )
        lines.append(
            f"  已完成时长   {format_duration_human(done.duration)}"
            f"（占总时长 {(done.duration / estimate.total_duration * 100) if estimate.total_duration else 0:.1f}%）"
        )
        lines.append(f"  还差         {format_duration_human(estimate.remaining_duration)} 音频")

    rows = estimate.volume_rows()
    if rows:
        lines.append("")
        lines.append("按卷明细")
        lines.append(
            f"  {'#':>3}  {'卷名':<24}  {'章':>3}  {'字数':>10}  {'时长':>14}  {'大小':>10}"
        )
        shown = rows[:max_volume_rows] if max_volume_rows and max_volume_rows > 0 else rows
        for position, row in enumerate(shown, start=1):
            name = str(row["volume"])
            display = name if len(name) <= 22 else name[:21] + "…"
            lines.append(
                f"  {position:>3}  {display:<24}  {row['chapters']:>3}  "
                f"{int(row['chars']):>10,}  {format_duration_human(float(row['duration'])):>14}  "
                f"{format_bytes(float(row['size_bytes'])):>10}"
            )
        if len(shown) < len(rows):
            lines.append(f"  …另有 {len(rows) - len(shown)} 卷")
    return "\n".join(lines)


def write_report(path: Path, estimate: BookEstimate) -> None:
    """把评估/产出报告写成 JSON（便于脚本消费，也是人工核对的凭据）。"""
    from .utils import write_json

    write_json(path, estimate.as_dict())


def total_chars_of(chapters: Iterable[Chapter]) -> int:
    return sum(sum(count_chars(paragraph) for paragraph in chapter.paragraphs) for chapter in chapters)


__all__ = [
    "approximate_duration",
    "ChapterEstimate",
    "BookEstimate",
    "OutputSummary",
    "build_estimate",
    "calibrate_bytes_per_char",
    "estimate_chapter",
    "estimate_size_bytes",
    "iter_audio_files",
    "parse_bitrate",
    "render_estimate",
    "summarize_output",
    "total_chars_of",
    "write_report",
]
