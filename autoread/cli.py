"""命令行入口。

四条命令：

* ``list``     —— 看一眼这本书有哪些章、多少字、大约多长（不合成）。
* ``estimate`` —— 评估产出规模：总时长、总大小、生成要几小时（不合成）。
* ``make``     —— 生成音频。默认一章一个文件；``--pages N`` 只取前 N 页做试听。
* ``voices``   —— 列出可用音色，方便换音色。

用法示例::

    # 看目录
    python3 -m autoread list --book "剑来(烽火戏诸侯).epub" --limit 10

    # 评估整本规模（剑来：483 章 / 19 GB / 生成约 3 天）
    python3 -m autoread estimate --book "剑来(烽火戏诸侯).epub"

    # 试听：第一章的前三页（说书男声）
    python3 -m autoread make --book "剑来(烽火戏诸侯).epub" --chapter 1 --pages 3

    # 整本输出（几百章，建议分册跑；已完成章节会自动跳过，可随时续跑）
    python3 -m autoread make --book "剑来(烽火戏诸侯).epub" --volume 1
    python3 -m autoread make --book "剑来(烽火戏诸侯).epub" --all

    # 不联网、不花钱，只验证切分与流程
    python3 -m autoread make --book "剑来(烽火戏诸侯).epub" --chapter 1 --pages 3 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import Config
from .epub import Book, Chapter, load_book
from .errors import AutoReadError, BookError, ConfigError, TTSError
from .logging_setup import setup_logging
from .prosody import plan_prosody
from .report import (
    approximate_duration,
    build_estimate,
    render_estimate,
    summarize_output,
    write_report,
)
from .textsplit import build_units, count_chars, paginate, select_pages, with_title
from .tts import MacSayEngine, resolve_engine_preview, synthesize_plan
from .utils import (
    ensure_dir,
    format_bytes,
    format_duration,
    format_duration_human,
    parse_rate_percent,
    read_json,
    safe_filename,
    write_json,
)

logger = logging.getLogger("autoread.cli")

#: Edge TTS 里适合「说书」的中文男声，供 ``voices`` 命令展示。
_EDGE_STORYTELLER_VOICES = [
    ("zh-CN-YunjianNeural", "云健 · 成熟磁性男声，微软标注「小说/体育」，说书首选"),
    ("zh-CN-YunyangNeural", "云扬 · 新闻播报腔男声，庄重但偏念稿"),
    ("zh-CN-YunxiNeural", "云希 · 年轻活泼男声，适合轻快题材"),
    ("zh-CN-YunxiaNeural", "云夏 · 少年音，适合童书"),
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声，温暖，适合散文"),
]


# ==========================================================================
# 参数
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autoread",
        description="把 EPUB 电子书读成有声书：一章一个音频，逐句抑扬顿挫。",
    )
    parser.add_argument("--version", action="version", version=f"autoread {__version__}")
    parser.add_argument("--config", help="配置文件路径（默认 <root>/config/config.json）")
    parser.add_argument("--log-level", help="覆盖日志级别：DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("-v", "--verbose", action="store_true", help="等价于 --log-level DEBUG")
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- list ----
    list_parser = sub.add_parser("list", help="列出章节（不合成）")
    list_parser.add_argument("--book", required=True, help="EPUB 路径")
    list_parser.add_argument("--limit", type=int, default=0, help="只显示前 N 章（0=全部）")
    list_parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本处理")
    list_parser.add_argument("--chars-per-page", type=int, help="估算页长（字符/页）")

    # ---- make ----
    make_parser = sub.add_parser("make", help="生成章节音频")
    make_parser.add_argument("--book", required=True, help="EPUB 路径")
    select = make_parser.add_argument_group("章节选择")
    select.add_argument("--chapter", help="章节号，支持 1 / 1,3 / 1-5 / 1,5-7（按 list 输出的序号）")
    select.add_argument("--volume", help="按册选择，支持 1 / 1,3 / 1-5（册序号见 list 输出的【】顺序）")
    select.add_argument("--match", help="按标题包含的关键字选章（可多次用逗号分隔）")
    select.add_argument("--all", action="store_true", help="整本全部章节")
    preview = make_parser.add_argument_group("试听/输出")
    preview.add_argument("--pages", type=int, default=0, help="只生成每章的前 N 页（0=整章）")
    preview.add_argument("--chars-per-page", type=int, help="「一页」的字符预算（默认 500）")
    preview.add_argument("--out", help="输出目录（默认 data/output）")
    preview.add_argument("--work", help="中间文件目录（默认 data/work）")
    preview.add_argument("--name", help="输出子目录名（默认取书名）")
    preview.add_argument("--force", action="store_true", help="已存在也重新生成")
    voice_group = make_parser.add_argument_group("音色/韵律")
    voice_group.add_argument("--engine", help="auto | edge | macos_say | silence")
    voice_group.add_argument("--voice", help="Edge 音色，如 zh-CN-YunjianNeural")
    voice_group.add_argument("--rate", help="Edge 基准语速，如 -8%%")
    voice_group.add_argument("--pitch", help="Edge 基准音高，如 -2Hz")
    voice_group.add_argument("--format", choices=["mp3", "wav"], help="输出格式（默认 mp3）")
    voice_group.add_argument("--no-prosody", action="store_true", help="关闭逐句抑扬顿挫（对照用）")
    voice_group.add_argument("--no-announce-title", action="store_true", help="不朗读章节标题")
    voice_group.add_argument("--dry-run", action="store_true", help="生成等长静音，不联网不出声")

    # ---- estimate ----
    est = sub.add_parser("estimate", help="评估整本（或部分）的产出大小与耗时，不合成")
    est.add_argument("--book", required=True, help="EPUB 路径")
    est_select = est.add_argument_group("范围（默认整本）")
    est_select.add_argument("--chapter", help="章节号，支持 1 / 1,3 / 1-5")
    est_select.add_argument("--volume", help="按册选择，支持 1 / 1,3 / 1-5")
    est_select.add_argument("--match", help="按标题关键字选章")
    est_select.add_argument("--all", action="store_true", help="整本（默认行为，显式写出更清楚）")
    est_out = est.add_argument_group("口径")
    est_out.add_argument("--pages", type=int, default=0, help="只评估每章的前 N 页（0=整章）")
    est_out.add_argument("--chars-per-page", type=int, help="「一页」的字符预算（默认 500）")
    est_out.add_argument("--out", help="产物根目录（会自动附带书名子目录；用于对比「已产出 / 还没有」）")
    est_out.add_argument("--format", choices=["mp3", "wav"], help="按哪种格式评估（默认跟配置）")
    est_out.add_argument("--bitrate", help="mp3 码率，如 64k / 48k / 96k")
    est_out.add_argument("--volume-rows", type=int, default=0, help="按卷明细最多显示几卷（0=全部）")
    est_out.add_argument("--json", action="store_true", help="以 JSON 输出")
    est_out.add_argument("--report", help="同时把评估结果写成 JSON 文件")

    # ---- voices ----
    voices_parser = sub.add_parser("voices", help="列出可用音色")
    voices_parser.add_argument("--engine", default="auto", help="edge | macos_say | auto")
    return parser


def _config_from_args(args: argparse.Namespace) -> Config:
    root = Path(__file__).resolve().parents[1]
    overrides: dict[str, object] = {}
    if getattr(args, "log_level", None):
        overrides["log_level"] = args.log_level
    config = Config.load(getattr(args, "config", None), root=root)
    if overrides:
        config = config.with_overrides(overrides)
    return config


def _make_overrides(args: argparse.Namespace) -> dict[str, object]:
    mapping = {
        "out": "output_dir",
        "work": "work_dir",
        "voice": "tts.voice",
        "engine": "tts.engine",
        "rate": "tts.edge_rate",
        "pitch": "tts.edge_pitch",
        "format": "tts.format",
        "bitrate": "tts.mp3_bitrate",
        "chars_per_page": "text.chars_per_page",
    }
    overrides: dict[str, object] = {}
    for attr, dotted in mapping.items():
        value = getattr(args, attr, None)
        if value is not None:
            overrides[dotted] = value
    if getattr(args, "no_prosody", False):
        overrides["prosody.enabled"] = False
    return overrides


# ==========================================================================
# 章节选择
# ==========================================================================
def parse_chapter_spec(spec: str) -> list[int]:
    """把 ``1,3-5`` 解析成 ``[1,3,4,5]``。"""
    result: set[int] = set()
    for raw in str(spec or "").split(","):
        part = raw.strip()
        if not part:
            continue
        if "-" in part:
            start_text, _, end_text = part.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ConfigError(f"章节范围无法解析: {part!r}") from exc
            if start > end:
                start, end = end, start
            result.update(range(start, end + 1))
        else:
            try:
                result.add(int(part))
            except ValueError as exc:
                raise ConfigError(f"章节号无法解析: {part!r}") from exc
    return sorted(result)


def volume_order(book: Book) -> list[str]:
    """按首次出现顺序返回全书卷名（``list`` 输出里的【】顺序就是这个）。"""
    order: list[str] = []
    for chapter in book.chapters:
        name = chapter.volume or ""
        if name and name not in order:
            order.append(name)
    return order


def select_chapters(book: Book, args: argparse.Namespace) -> list[Chapter]:
    by_index = {chapter.index: chapter for chapter in book.chapters}
    selected: dict[int, Chapter] = {}

    if getattr(args, "volume", None):
        names = volume_order(book)
        if not names:
            raise BookError("这本书的目录里没有分卷信息，请改用 --chapter / --match / --all")
        chosen: set[str] = set()
        for position in parse_chapter_spec(args.volume):
            if not 1 <= position <= len(names):
                raise BookError(
                    f"册序号 {position} 不存在（本书共 {len(names)} 册，序号从 1 开始）"
                )
            chosen.add(names[position - 1])
        for chapter in book.chapters:
            if chapter.volume in chosen:
                selected[chapter.index] = chapter

    if args.chapter:
        for index in parse_chapter_spec(args.chapter):
            if index not in by_index:
                raise BookError(f"章节 {index} 不存在（本书共 {len(book.chapters)} 章，序号从 1 开始）")
            selected[index] = by_index[index]

    if args.match:
        keywords = [item.strip().lower() for item in str(args.match).split(",") if item.strip()]
        for chapter in book.chapters:
            title = chapter.title.lower()
            if any(keyword in title for keyword in keywords):
                selected[chapter.index] = chapter

    if getattr(args, "all", False):
        for chapter in book.chapters:
            selected[chapter.index] = chapter

    if not selected:
        raise ConfigError(
            "没有指定要生成哪些章节。请用 --chapter 1（单章）、--chapter 1,3-5、"
            "--volume 1（一册）、--match 关键字 或 --all（整本）。先跑 `list` 看目录。"
        )
    return [selected[index] for index in sorted(selected)]


# ==========================================================================
# 命令实现
# ==========================================================================
def command_list(args: argparse.Namespace, config: Config) -> int:
    book = load_book(args.book, strip_duplicate_heading=bool(config.get("text.strip_duplicate_heading", True)))
    chars_per_page = args.chars_per_page or int(config.get("text.chars_per_page", 500))
    rows = _chapter_rows(book, chars_per_page, config)
    shown = rows[: args.limit] if args.limit and args.limit > 0 else rows

    if args.json:
        import json

        print(json.dumps(
            {
                "book": str(book.path),
                "title": book.title,
                "author": book.author,
                "chapters": len(book.chapters),
                "items": shown,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    print(f"《{book.title}》  作者：{book.author or '未知'}")
    print(f"共 {len(book.chapters)} 章｜页长按 {chars_per_page} 字估算")
    print("（时长为「纯语音 + 停顿 + 剪静音余量」的近似值；精确值用 estimate）")
    print("-" * 72)
    volume = None
    for row in shown:
        if row["volume"] and row["volume"] != volume:
            volume = row["volume"]
            print(f"\n【{volume}】")
        print(
            f"{row['index']:>4}  {row['title']:<28.28}  "
            f"{row['chars']:>7} 字  {row['pages']:>3} 页  ≈{row['duration']}"
        )
    if args.limit and len(rows) > args.limit:
        print(f"\n（共 {len(rows)} 章，仅显示前 {args.limit} 章；去掉 --limit 看全部）")
    return 0


def _chapter_rows(book: Book, chars_per_page: int, config: Config) -> list[dict[str, object]]:
    """章节列表行。

    时长用 ``approximate_duration``（秒回）；精确值要跑 ``estimate``。
    两处都会跟着停顿/语速配置变，不会一个报新数一个报旧数。
    """
    rate = parse_rate_percent(config.get("tts.edge_rate", "-12%"), default=-12.0)
    rows: list[dict[str, object]] = []
    for chapter in book.chapters:
        paragraphs = chapter.paragraphs
        chars = sum(count_chars(item) for item in paragraphs)
        rows.append(
            {
                "index": chapter.index,
                "title": chapter.title,
                "volume": chapter.volume,
                "href": chapter.href,
                "chars": chars,
                "pages": len(paginate(paragraphs, chars_per_page)) if paragraphs else 0,
                "duration": format_duration(
                    approximate_duration(
                        chars,
                        paragraphs=len(paragraphs),
                        rate_percent=rate,
                        base_pause_ms=int(config.get("prosody.base_pause_ms", 500)),
                        paragraph_pause_ms=int(config.get("prosody.paragraph_pause_ms", 800)),
                        group_chars=int(config.get("text.tts_group_chars", 60)),
                        lead_in_ms=int(config.get("prosody.chapter_lead_in_ms", 350)),
                        trim_keep_ms=int(config.get("tts.trim_keep_ms", 30)),
                    )
                ),
            }
        )
    return rows


def command_voices(args: argparse.Namespace) -> int:
    engine = (args.engine or "auto").lower()
    if engine in {"auto", "edge"}:
        print("Edge TTS（需联网；装法：./.venv/bin/pip install edge-tts）")
        print("-" * 72)
        for name, note in _EDGE_STORYTELLER_VOICES:
            print(f"  {name:<26} {note}")
    if engine in {"auto", "macos_say", "say", "macos"}:
        print("\nmacOS say（离线兜底）")
        print("-" * 72)
        voices = MacSayEngine.list_voices()
        if not voices:
            print("  （未检测到中文语音，或用的是非 macOS 系统）")
        for name, locale in voices:
            marker = "  ← 自动选择" if name == MacSayEngine.resolve_voice() else ""
            print(f"  {name:<26} {locale}{marker}")
        print("  提示：macOS 简体中文通常只有女声；要男声请用 Edge 的 zh-CN-YunjianNeural。")
    return 0


_BOOK_ID_PREFIX_RE = re.compile(r"^\d{2,}[_\-]+")


def _book_dir_name(book: Book) -> str:
    """输出子目录名。

    很多电子书的 ``dc:title`` 带一个来源站的编号前缀（实测：
    ``74886_剑来（1-49册）_烽火戏诸侯``）。这个编号对听书的人没有意义，
    只留书名更清爽 —— 但只在「2 位以上数字 + 分隔符」这种明确的编号形态下才剥掉，
    不会误伤本来就以数字开头的书名。
    """
    title = str(book.title or "").strip()
    stripped = _BOOK_ID_PREFIX_RE.sub("", title).strip() or title or book.path.stem
    return safe_filename(stripped, fallback="book")


@dataclass
class _Planned:
    """一章的「待生成计划」：念什么、写到哪。

    先规划再生成，是为了能在动手前**统一评估产出规模**（多久、多大、磁盘够不够），
    而不是跑到一半才发现空间不足。
    """

    chapter: Chapter
    paragraphs: list[str]
    truncated: bool
    pages_total: int
    out_path: Path
    stem: str
    #: manifest 里的键。试听与整章必须分开记 —— 否则「先试听、后整章」时
    #: 后写入的会把前一条覆盖掉，索引里就只剩一个。
    key: str


def _plan_chapters(
    chapters: Sequence[Chapter],
    *,
    out_root: Path,
    chars_per_page: int,
    pages: int,
    fmt: str,
) -> list[_Planned]:
    planned: list[_Planned] = []
    for chapter in chapters:
        paragraphs = chapter.paragraphs
        if not paragraphs:
            logger.warning("%s 无正文，跳过", chapter.title)
            continue
        pages_total = len(paginate(paragraphs, chars_per_page))
        truncated = bool(pages and pages > 0 and pages < pages_total)
        selected = select_pages(paragraphs, pages, chars_per_page) if truncated else paragraphs
        name = f"{chapter.index:04d}_{safe_filename(chapter.title)}"
        key = str(chapter.index)
        if truncated:
            # 文件名里直接写「试听」：过几天回头看目录，还能一眼认出哪个是样本。
            name += f"_试听p1-{pages}"
            key = f"{chapter.index}@p{pages}"
        planned.append(
            _Planned(
                chapter=chapter,
                paragraphs=list(selected),
                truncated=truncated,
                pages_total=pages_total,
                out_path=out_root / f"{name}.{fmt}",
                stem=f"{chapter.index:04d}",
                key=key,
            )
        )
    return planned


def _has_selector(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "chapter", None)
        or getattr(args, "volume", None)
        or getattr(args, "match", None)
        or getattr(args, "all", False)
    )


def command_estimate(args: argparse.Namespace, config: Config) -> int:
    """评估产出规模：多久、多大、生成要几小时 —— 但一个音频都不合成。"""
    config = config.with_overrides(_make_overrides(args))
    setup_logging(config, verbose=bool(args.verbose))

    book = load_book(
        args.book, strip_duplicate_heading=bool(config.get("text.strip_duplicate_heading", True))
    )
    # 评估很便宜，所以默认直接评估整本 —— 用户问「这本书多大」时不必再写 --all。
    chapters = select_chapters(book, args) if _has_selector(args) else list(book.chapters)

    dir_name = _book_dir_name(book)
    out_root = config.path("output_dir") / dir_name

    chars_per_page = int(args.chars_per_page or config.get("text.chars_per_page", 500))
    pages = int(args.pages or 0)
    paragraphs_by_index: dict[int, list[str]] = {}
    if pages > 0:
        for chapter in chapters:
            if pages < len(paginate(chapter.paragraphs, chars_per_page)):
                paragraphs_by_index[chapter.index] = select_pages(
                    chapter.paragraphs, pages, chars_per_page
                )

    estimate = build_estimate(
        book,
        config,
        chapters=chapters,
        paragraphs_by_index=paragraphs_by_index,
        complete_chapter=pages <= 0,
        out_dir=out_root,
    )

    if args.json:
        import json

        print(json.dumps(estimate.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_estimate(estimate, max_volume_rows=int(args.volume_rows or 0)))
        print()
        print(f"产物目录：{out_root}")
        if estimate.existing is None or not estimate.existing.files:
            print("（该目录还没有产出；上面全是估算值）")

    if args.report:
        report_path = Path(args.report).expanduser()
        write_report(report_path, estimate)
        print(f"评估报告已写入：{report_path}")
    return 0


def command_make(args: argparse.Namespace, config: Config) -> int:
    config = config.with_overrides(_make_overrides(args))
    setup_logging(config, verbose=bool(args.verbose))

    book = load_book(args.book, strip_duplicate_heading=bool(config.get("text.strip_duplicate_heading", True)))
    chapters = select_chapters(book, args)

    dir_name = safe_filename(args.name, fallback="book") if args.name else _book_dir_name(book)
    out_root = ensure_dir(config.path("output_dir") / dir_name)
    work_root = ensure_dir(config.path("work_dir") / dir_name)
    fmt = str(config.get("tts.format", "mp3"))
    chars_per_page = int(config.get("text.chars_per_page", 500))
    group_chars = int(config.get("text.tts_group_chars", 60))
    hard_limit = int(config.get("text.sentence_hard_limit", 160))
    expressive = bool(config.get("prosody.enabled", True))
    announce = not args.no_announce_title

    logger.info("《%s》共 %d 章，本次生成 %d 章", book.title, len(book.chapters), len(chapters))
    logger.info("引擎：%s", resolve_engine_preview(config, engine=args.engine))
    logger.info("输出目录：%s", out_root)
    if args.dry_run:
        logger.warning("--dry-run：将生成等长静音，仅用于验证流程，不可当成品听")

    # ---- 规划 + 事前评估 ----
    planned = _plan_chapters(
        chapters,
        out_root=out_root,
        chars_per_page=chars_per_page,
        pages=int(args.pages or 0),
        fmt=fmt,
    )
    if not planned:
        logger.error("没有任何可生成的章节")
        return 1

    estimate = build_estimate(
        book,
        config,
        chapters=[item.chapter for item in planned],
        paragraphs_by_index={item.chapter.index: item.paragraphs for item in planned if item.truncated},
        complete_chapter=not (args.pages and args.pages > 0),
        out_dir=out_root,
    )
    # 逐章精确时长（含停顿/剪静音余量/章首留白），下面的进度日志直接用这个数，
    # 而不是「字数 ÷ 速度」的粗估 —— 两处报不一样的预计时长最让人困惑。
    duration_by_index = {item.index: item.duration for item in estimate.chapters}
    existing_bytes = estimate.existing.bytes if estimate.existing else 0
    remaining_bytes = max(0, estimate.total_bytes - existing_bytes)
    logger.info(
        "预计产出：%s 音频（%s）/ %s 个文件 / 生成约 %s",
        format_bytes(estimate.total_bytes),
        format_duration_human(estimate.total_duration),
        len(planned),
        format_duration_human(estimate.estimated_generation_seconds),
    )
    if existing_bytes:
        logger.info(
            "已存在 %d 章 / %s，本次还需约 %s",
            estimate.existing.chapters if estimate.existing else 0,
            format_bytes(existing_bytes),
            format_bytes(remaining_bytes),
        )
    try:
        free_bytes = shutil.disk_usage(out_root).free
    except OSError:
        free_bytes = None
    if free_bytes is not None and remaining_bytes > free_bytes:
        logger.warning(
            "磁盘空间可能不足：还需约 %s，可用 %s —— 可以先 --volume 1 分册生成",
            format_bytes(remaining_bytes),
            format_bytes(free_bytes),
        )

    manifest_path = out_root / "manifest.json"
    manifest = read_json(manifest_path, default={"book": str(book.path), "chapters": {}})
    if not isinstance(manifest, dict):
        manifest = {"book": str(book.path), "chapters": {}}
    manifest["book"] = str(book.path)
    manifest.setdefault("chapters", {})

    succeeded = 0
    skipped = 0
    failures: list[str] = []
    started = time.time()

    for position, item in enumerate(planned, start=1):
        chapter = item.chapter
        units = build_units(
            item.paragraphs,
            group_chars=group_chars,
            hard_limit=hard_limit,
            complete_chapter=not item.truncated,
        )
        if announce:
            units = with_title(units, chapter.title)
        if not units:
            logger.warning("[%d/%d] %s 无有效文本，跳过", position, len(planned), chapter.title)
            continue

        plan = plan_prosody(
            units,
            base_rate=str(config.get("tts.edge_rate", "-12%")),
            base_pitch=str(config.get("tts.edge_pitch", "-2Hz")),
            base_volume=str(config.get("tts.edge_volume", "+0%")),
            base_pause_ms=int(config.get("prosody.base_pause_ms", 220)),
            paragraph_pause_ms=int(config.get("prosody.paragraph_pause_ms", 560)),
            expressive=expressive,
            max_pitch_hz=float(config.get("prosody.max_pitch_hz", 10.0)),
            lead_in_ms=int(config.get("prosody.chapter_lead_in_ms", 350)),
        )

        out_path = item.out_path
        speech_chars = count_chars("".join(cue.text for cue in plan.cues))
        logger.info(
            "[%d/%d] %s｜%s 字｜%s 段｜预计 %s%s",
            position, len(planned), chapter.title, speech_chars, len(plan.cues),
            format_duration_human(duration_by_index.get(chapter.index, 0.0)),
            "（试听前 %d 页）" % args.pages if item.truncated else "",
        )

        if out_path.is_file() and out_path.stat().st_size > 1024 and not args.force:
            logger.info("    已存在，跳过（--force 可覆盖）：%s", out_path.name)
            skipped += 1
            continue

        chapter_started = time.time()
        try:
            result = synthesize_plan(
                config,
                plan,
                work_dir=work_root,
                stem=item.stem,
                out_path=out_path,
                engine=args.engine,
                force_silence=bool(args.dry_run),
            )
        except TTSError as exc:
            logger.error("    生成失败：%s", exc)
            failures.append(f"{chapter.title}: {exc}")
            continue

        elapsed = time.time() - chapter_started
        logger.info(
            "    完成 %s｜时长 %s｜%s｜引擎 %s｜耗时 %.1fs%s",
            out_path.name, format_duration(result.duration), format_bytes(out_path.stat().st_size),
            result.engine, elapsed, "｜⚠️ 已降级" if result.degraded else "",
        )

        write_json(
            out_path.with_suffix(out_path.suffix + ".json"),
            {
                "book": str(book.path),
                "book_title": book.title,
                "author": book.author,
                "chapter_index": chapter.index,
                "chapter_title": chapter.title,
                "volume": chapter.volume,
                "href": chapter.href,
                "preview_pages": args.pages if item.truncated else None,
                "pages_total": item.pages_total,
                "chars": speech_chars,
                "cues": len(plan.cues),
                "audio": {
                    "path": str(out_path),
                    "duration": round(result.duration, 3),
                    "bytes": out_path.stat().st_size,
                    "engine": result.engine,
                    "voice": result.voice,
                    "sample_rate": result.sample_rate,
                    "degraded": result.degraded,
                    "format": fmt,
                },
                "prosody": {
                    "enabled": expressive,
                    "base_rate": config.get("tts.edge_rate"),
                    "base_pitch": config.get("tts.edge_pitch"),
                    "base_pause_ms": config.get("prosody.base_pause_ms"),
                    "paragraph_pause_ms": config.get("prosody.paragraph_pause_ms"),
                    "announce_title": announce,
                },
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        manifest["chapters"][item.key] = {
            "title": chapter.title,
            "file": out_path.name,
            "preview_pages": args.pages if item.truncated else None,
            "duration": round(result.duration, 3),
            "bytes": out_path.stat().st_size,
            "engine": result.engine,
            "degraded": result.degraded,
        }
        write_json(manifest_path, manifest)
        succeeded += 1

    # ---- 事后统计：真实占用 + 报告 ----
    total_elapsed = time.time() - started
    summary = summarize_output(out_root)
    final = build_estimate(
        book,
        config,
        chapters=[item.chapter for item in planned],
        paragraphs_by_index={item.chapter.index: item.paragraphs for item in planned if item.truncated},
        complete_chapter=not (args.pages and args.pages > 0),
        out_dir=out_root,
    )
    logger.info(
        "完成：成功 %d，跳过 %d，失败 %d，总耗时 %s",
        succeeded, skipped, len(failures), format_duration(total_elapsed),
    )
    logger.info(
        "本次范围：%d 章（成功 %d，跳过 %d）",
        len(planned), succeeded, skipped,
    )
    logger.info(
        "产物目录累计：%d 章 / %d 个文件 / %s / 音频总时长 %s",
        summary.chapters,
        summary.files,
        format_bytes(summary.bytes),
        format_duration_human(summary.duration),
    )
    if summary.full_chapters:
        logger.info(
            "整章平均 %s / %s（%d 章样本）",
            format_bytes(summary.average_chapter_bytes),
            format_duration_human(summary.average_chapter_duration),
            summary.full_chapters,
        )
    calibrated = None if final.partial else final.calibrated_total_bytes
    if calibrated is not None:
        delta = ""
        if final.total_bytes:
            delta = f"（较理论估算 {(calibrated / final.total_bytes - 1) * 100:+.1f}%）"
        logger.info(
            "按实测外推：本次 %d 章全部完成约 %s%s",
            len(planned), format_bytes(calibrated), delta,
        )
    previews = [item for item in planned if item.truncated and item.out_path.is_file()]
    if previews:
        logger.info("试听文件（每章前 %d 页，可先听这个）：", args.pages)
        for item in previews:
            logger.info("    %s", item.out_path)
    report_path = out_root / "report.json"
    final.existing = summary
    write_report(report_path, final)
    logger.info("报告：%s", report_path)

    if failures:
        for item in failures:
            logger.error("失败：%s", item)
        return 1
    return 0


# ==========================================================================
# main
# ==========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _config_from_args(args)
        if args.command == "list":
            setup_logging(config, verbose=bool(args.verbose))
            return command_list(args, config)
        if args.command == "voices":
            return command_voices(args)
        if args.command == "estimate":
            return command_estimate(args, config)
        if args.command == "make":
            return command_make(args, config)
        parser.error(f"未知命令: {args.command}")
        return 2
    except (ConfigError, BookError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except AutoReadError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
