"""正文切分：段落 → 句子 → TTS 片段（SpeechUnit）。

逐级切分各自解决一个问题：

1. **段落 → 换行 → 句子**：只在真正的句末标点（。！？；…）切，并且把闭引号/闭括号
   粘回上一句（``他喊：“站住！”`` 不能切成 ``…站住！`` + ``”``）。
   段落内部的换行（``<br>``，用 ``\\n`` 标记）是**独立的切点** ——
   换行处要有一个轻停顿，但绝不把一句话劈成两半。
2. **句子 → 片段**：把同一行内相邻的短句合成一个 ~60 字的合成请求。太碎会让语气被
   切断，太长则一段里塞进好几句话、韵律只能整段给一个调，起伏就没了。
   **片段不跨行**：跨行会把「换行停顿」吃掉。
3. **段落 → 页**：EPUB 是流式排版，没有真页码。用字符预算近似「一页」，
   只为 ``--pages`` 预览服务，切点统一落在段落边界，不会把一句话劈成两半。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 句末标点（含中英文）。用「后一个字符不再是句末标点」作为切点，
#: 这样 ``……`` / ``！！`` 这类**连续标点**会整体留在上一句，而不会被切两次。
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;…])(?![。！？!?；;…])")
#: 句内的次级停顿（逗号/顿号），只在单句超过 ``hard_limit`` 时才用来自救。
_SUBCLAUSE_SPLIT_RE = re.compile(r"(?<=[，,、])")
#: 闭引号/闭括号：句子在这里切开后，这些符号必须跟着上一句。
_CLOSERS = "”’」』）】》〉〕｝\"'"
_PURE_BLANK_RE = re.compile(r"^[\s\W_]+$")

#: Edge 中文神经语音在 ``rate=0%`` 时的**纯语音**速度（字/秒，**不含停顿**）。
#:
#: 由两个整章的实测反解得到（先减掉该章真实插入的停顿、剪静音余量与章首留白）：
#:
#: * 第一章：4277.1s / 20276 字 → 4.741 字/秒 @ ``rate=-12%``
#: * 第二章：4216.2s / 20032 字 → 4.751 字/秒 @ ``rate=-12%``
#:
#: 两章相差 0.2%，说明这个常数很稳；折算到 ``rate=0%`` 得 5.39。
#:
#: **为什么只算语音、不把停打包进来**：停顿是配置出来的（``base_pause_ms`` /
#: ``paragraph_pause_ms`` / 剪静音余量），用户随时会调。把停包含进「速度」里，
#: 一改停顿所有历史估算就全错。分开建模后，改停顿 → 估算自动跟着变。
SPEECH_CHARS_PER_SECOND_AT_ZERO = 5.39


@dataclass
class SpeechUnit:
    """一次 TTS 请求要念的内容，以及它在这一章里的位置。"""

    text: str
    paragraph_end: bool = False
    chapter_start: bool = False
    chapter_end: bool = False
    #: 本段内第几组（0 起）与本段共几组 —— 韵律规划靠它画出「段首扬、段末沉」的弧线。
    paragraph_pos: int = 0
    paragraph_groups: int = 1

    def __len__(self) -> int:
        return len(self.text)


#: ``\s`` 的编译版本。用 ``re.sub`` 而不是逐字符 ``isspace()`` ：
#: 整本书有 1100 万字，逐字符的 Python 循环要 2 秒多，正则走 C 层只要几十毫秒。
_WHITESPACE_RE = re.compile(r"\s")


def count_chars(text: str) -> int:
    """统计「要念的字符数」，忽略空白（空白不占朗读时长）。

    空白不发音，却会被 ``len()`` 算进去；一本书里换行与缩进能占掉可观的比例，
    用它算时长会系统性偏长。
    """
    body = str(text or "")
    if not body:
        return 0
    return len(_WHITESPACE_RE.sub("", body))


def chars_per_second(rate_percent: float = 0.0) -> float:
    """按语速偏移换算**纯语音**速度（字/秒，不含停顿）。

    ``rate=-12%`` → ``5.39 * 0.88 ≈ 4.74`` 字/秒。语速越负越慢，时长越长。
    """
    factor = max(0.4, 1.0 + float(rate_percent) / 100.0)
    return SPEECH_CHARS_PER_SECOND_AT_ZERO * factor


def estimate_read_seconds(
    text: str,
    chars_per_second: float | None = None,
    *,
    rate_percent: float = 0.0,
) -> float:
    """估算**纯语音**时长（秒），**不含停顿**。

    只用于「这一段大概要念多久」这类粗估（日志、dry-run 的静音时长）。
    整章的精确时长请走 ``report.estimate_chapter`` —— 它会把每个停顿、
    剪静音余量和章首留白都算进去，因而能跟着停顿配置变化。

    ``chars_per_second`` 显式给定时优先；否则按 ``rate_percent`` 换算。
    """
    speed = chars_per_second if chars_per_second and chars_per_second > 0 else globals()["chars_per_second"](rate_percent)
    return count_chars(text) / speed


def split_sentences(paragraph: str, *, hard_limit: int = 160) -> list[str]:
    """把一段话切成句子；超长句按逗号再切。

    ``hard_limit`` 是「一口气念不完」的阈值：一段 300 字的无标点长句若整段
    合成，语音引擎会自己随机断句，节奏不可控；主动在逗号处切开更自然。
    """
    text = str(paragraph or "").strip()
    if not text:
        return []

    parts = _SENTENCE_SPLIT_RE.split(text)
    merged: list[str] = []
    for part in parts:
        if not part:
            continue
        # 以闭引号/闭括号开头：只把**开头的闭符号**挪回上一句，
        # 剩下的文字仍要另起一句。整段并过去会把两句粘成一句。
        if merged and part[0] in _CLOSERS:
            cut = 0
            while cut < len(part) and part[cut] in _CLOSERS:
                cut += 1
            merged[-1] += part[:cut]
            remainder = part[cut:]
            if remainder:
                merged.append(remainder)
        else:
            merged.append(part)

    sentences: list[str] = []
    for sentence in merged:
        sentence = sentence.strip()
        if not sentence or _PURE_BLANK_RE.match(sentence):
            continue
        if count_chars(sentence) <= hard_limit:
            sentences.append(sentence)
            continue
        sentences.extend(_hard_split(sentence, hard_limit))
    return sentences


def _hard_split(sentence: str, hard_limit: int) -> list[str]:
    """超长句：优先在逗号处切，仍超长才等距硬切。"""
    pieces = [piece for piece in _SUBCLAUSE_SPLIT_RE.split(sentence) if piece]
    out: list[str] = []
    current = ""
    for piece in pieces:
        while count_chars(piece) > hard_limit:
            if current:
                out.append(current)
                current = ""
            out.append(piece[:hard_limit])
            piece = piece[hard_limit:]
        if count_chars(current) + count_chars(piece) <= hard_limit:
            current += piece
        else:
            if current:
                out.append(current)
            current = piece
    if current:
        out.append(current)
    return [item for item in out if item.strip()]


def build_units(
    paragraphs: list[str],
    *,
    group_chars: int = 60,
    hard_limit: int = 160,
    complete_chapter: bool = True,
) -> list[SpeechUnit]:
    """把正文段落编成 TTS 片段序列，并标注段末/章首/章末。

    ``complete_chapter=False``（例如 ``--pages 3`` 的试听）时不打「章末」标记：
    样本是从中间截断的，用「收尾」的语调念会让人误以为这一章就这么短。
    """
    units: list[SpeechUnit] = []
    limit = max(20, int(group_chars))
    for paragraph in paragraphs:
        sentences = split_sentences(paragraph, hard_limit=max(20, int(hard_limit)))
        if not sentences:
            continue
        current = ""
        chunks: list[str] = []
        for sentence in sentences:
            if not current:
                current = sentence
            elif count_chars(current) + count_chars(sentence) <= limit:
                current += sentence
            else:
                chunks.append(current)
                current = sentence
        if current:
            chunks.append(current)
        for position, chunk in enumerate(chunks):
            units.append(SpeechUnit(text=chunk, paragraph_pos=position, paragraph_groups=len(chunks)))
        units[-1].paragraph_end = True

    if units:
        units[0].chapter_start = True
        if complete_chapter:
            units[-1].chapter_end = True
    return units


def with_title(units: list[SpeechUnit], title: str) -> list[SpeechUnit]:
    """在正文前插入「播报章节标题」的一段。

    说书先生开讲前先报回目（「第一章 惊蛰」），听书的人才知道自己在哪一章。
    标题单独成段，不与正文首句并进同一次合成请求 —— 否则「第一章 惊蛰二月二，
    龙抬头」会连成一口气，题目就不成题目了。
    """
    label = str(title or "").strip()
    if not label or not units:
        return units
    units[0].chapter_start = False
    head = SpeechUnit(text=label, paragraph_end=True, chapter_start=True)
    return [head, *units]


def paginate(paragraphs: list[str], chars_per_page: int = 500) -> list[list[str]]:
    """按字符预算把段落分页；切点只在段落边界，绝不切开一句话。

    页长有 80 字的下限：比这更小的「页」在听感上没有意义，只会把配置笔误
    变成一堆奇怪的短文件。
    """
    budget = max(80, int(chars_per_page))
    pages: list[list[str]] = []
    current: list[str] = []
    used = 0
    for paragraph in paragraphs:
        length = count_chars(paragraph)
        # 单段就超过一页 → 自己占一页，不与其他段落混排。
        if current and used + length > budget:
            pages.append(current)
            current = []
            used = 0
        current.append(paragraph)
        used += length
    if current:
        pages.append(current)
    return pages


def total_pages(paragraphs: list[str], chars_per_page: int = 500) -> int:
    return len(paginate(paragraphs, chars_per_page))


def select_pages(paragraphs: list[str], page_count: int, chars_per_page: int = 500) -> list[str]:
    """取前 ``page_count`` 页的段落（用于 ``--pages`` 预览）。"""
    if page_count <= 0:
        return list(paragraphs)
    pages = paginate(paragraphs, chars_per_page)
    selected: list[str] = []
    for page in pages[:page_count]:
        selected.extend(page)
    return selected
