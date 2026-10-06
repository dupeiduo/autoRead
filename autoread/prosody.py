"""韵律规划：让整章听起来是「说书先生」在讲，而不是一台匀速朗读机。

**为什么不去改 SSML**：Edge TTS 的免费端点对 ``mstts:express-as`` 这类风格标签
支持并不可靠，一旦不被识别，要么整段被忽略、要么把标签当正文念出来。
而 ``Communicate(text, voice, rate=, pitch=, volume=)`` 是**逐次调用**就能变的，
所以「逐句变调」才是这里可靠又可测的着力点。

说书的听感来自四件事，本模块一一对应：

============================  ==========================================
说书的特征                    实现方式
============================  ==========================================
开篇慢、把听众领进来          ``opening``：语速 -3%、段末多留一拍
叙述平、对话活                ``dialogue``：语速 +3%、音高 +4Hz
惊叹/短促句要「扬」           ``emphasis``：语速 -6%、音高 +5Hz、停顿 ×1.6
段末收得住、章末留白          ``paragraph_end`` / ``chapter_end`` 抬停顿下限
============================  ==========================================

停顿不是常数：句末标点本身决定基准（句号 1.0、叹号 1.35、省略号 1.5、
逗号 0.6），再乘角色的倍率。这正是「抑扬顿挫」里「顿」的部分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .textsplit import SpeechUnit, count_chars

#: 相对基准语速的偏移（百分点）。说书是「稳」的，基准已由配置调慢。
ROLE_RATE_DELTA = {
    "opening": -3.0,
    "dialogue": 3.0,
    "emphasis": -6.0,
    "number": -5.0,  # 报数目放慢，让人听清
    "closing": -3.0,
    "normal": 0.0,
}
#: 相对基准音高的偏移（Hz）。全场一个音高，正是「机器感」的主要来源。
ROLE_PITCH_DELTA = {
    "opening": -1.0,
    "dialogue": 4.0,
    "emphasis": 5.0,
    "number": -1.0,  # 数字句略沉，抬高会显得轻浮
    "closing": -2.0,
    "normal": 0.0,
}
#: 停顿倍率（相对句末基准停顿）。
#:
#: 数值范围刻意收窄：``base_pause_ms`` 已经是 0.5s、段落停顿 0.8s，
#: 两者只差 1.6×。如果句末还能按 1.6× 放大，句末就会追平段落停顿，
#: 「段落换行才停顿」的分节感就被句末自己吃掉了。收窄后台阶是清楚的：
#: 逗号 ~0.35s → 普通句末 0.5s → 短句/叹句 0.6~0.65s → 段落换行 0.8s。
ROLE_PAUSE_SCALE = {
    "opening": 1.25,
    "dialogue": 0.85,
    "emphasis": 1.35,
    "number": 1.15,
    "closing": 1.30,
    "normal": 1.0,
}

#: 句末标点 → 停顿倍率。数值不是拍的：逗号要「连」，省略号要「悬」。
_TERMINAL_PAUSE_FACTOR = {
    "。": 1.0,
    "！": 1.25, "!": 1.25,
    "？": 1.20, "?": 1.20,
    "；": 1.05, ";": 1.05,
    "…": 1.30,
    "：": 0.95, ":": 0.95,
    "，": 0.70, ",": 0.70,
    "、": 0.60,
}

#: 闭引号/闭括号：判断句末标点时要穿透它们看真正的收尾符号。
_TRAILING_CLOSERS = "”’」』）】》〉〕｝\"'"
#: 引号起始符：判定对话角色的主力信号。
_QUOTE_OPENERS = "「『“‘\"'"
#: 短促句的阈值（字数）。短句 + 叹号/省略号 = 强调。
_EMPHASIS_MAX_CHARS = 16

#: 叙述句按长度分档：``(字数上限, 语速偏移, 音高偏移, 停顿倍率)``。
#:
#: 这是「抑扬顿挫」在**纯叙述**里的落点。一段小说里大部分句子既不是对话、
#: 也没有叹号，如果全用同一个调，就是「念稿」。真人讲故事时：
#:
#: * 短句是「顿」—— 慢下来、沉下去、后面多留一拍（``少年姓陈，名平安。``）
#: * 长句是「扬」—— 略快、略亮，让长串描写走得起来（大段环境描写）
#:
#: 档位是**确定性**的（按字数），不随机；同一句话每次生成的念法都一样。
_LENGTH_PROFILES: tuple[tuple[int, float, float, float], ...] = (
    (12, -4.0, -2.0, 1.20),
    (22, -2.0, -1.0, 1.08),
    (60, 0.0, 0.0, 1.00),
)
#: 超过 60 字的长句：略快、略亮，避免长描写拖沓。
_LONG_PROFILE = (2.0, 1.0, 0.92)

#: 段落内部的音高弧线（Hz）：段首扬、段末沉。
#: 一个段落是一口气，这口气本身有起伏 —— 这是「顿挫」里最像人的部分。
_PARAGRAPH_ARC_HZ = 1.5

_QUOTED_RE = re.compile(r"[「『“‘]([^」』”’]{1,120})[」』”’]")

#: 说话动词。引语 + 说话动词 = 对话（``他喊道：“快回家！”``）。
#: 这是**词法信号**而不是语义猜测：命中条件必须同时有引语，所以「知道」「道理」
#: 这类含「道」的词不会被误判 —— 它们不会和引号一起出现。
_SPEECH_VERB_RE = re.compile(r"说道|说|道|喊|问|叫|答|嚷|骂|喝|吼|嘟囔|嘀咕|念叨")

_NUMBER_UNIT_RE = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class ProsodyCue:
    """一段文本该怎么念。"""

    text: str
    role: str
    rate: str
    pitch: str
    volume: str
    pause_after_ms: int


@dataclass(frozen=True)
class ProsodyPlan:
    """整章的朗读方案：逐段指令 + 章首留白。"""

    cues: tuple[ProsodyCue, ...]
    lead_in_ms: int = 0

    def __len__(self) -> int:
        return len(self.cues)


def _terminal_factor(text: str) -> float:
    """看最后一个「有效」字符（穿透闭引号）决定句末停顿倍率。"""
    body = (text or "").rstrip()
    while body and body[-1] in _TRAILING_CLOSERS:
        body = body[:-1].rstrip()
    if not body:
        return 0.9
    return _TERMINAL_PAUSE_FACTOR.get(body[-1], 0.9)


def is_dialogue(text: str) -> bool:
    """判断是否以对话为主。

    三个信号，命中任一即为对话：

    1. 整句以引号开头 —— 叙述句一般以主语开头，这个信号最稳；
    2. 引语够长（≥6 字）且占比过半 —— ``他说：“我今天就去城里。”``；
    3. 有引语 + 有说话动词 —— 覆盖 ``他喊道：“快回家！”`` 这种引语很短的句子。

    用引号占比而不是固定阈值，是因为切句粒度会变：句子被合进不同长度的片段里，
    按绝对字数设阈值会随分组配置漂移（这也是 ``classify_role`` 不按字数判角色的原因）。
    """
    body = (text or "").strip()
    if not body:
        return False
    if body[0] in _QUOTE_OPENERS:
        return True
    if not _QUOTED_RE.search(body):
        return False
    quoted = sum(len(match.group(1)) for match in _QUOTED_RE.finditer(body))
    if quoted >= 6 and quoted * 2 >= count_chars(body):
        return True
    return bool(_SPEECH_VERB_RE.search(body))


def is_emphasis(text: str) -> bool:
    """短促句 + 强标点 = 该「扬」起来的一句。"""
    body = (text or "").strip()
    if not body or count_chars(body) > _EMPHASIS_MAX_CHARS:
        return False
    stripped = body.rstrip(_TRAILING_CLOSERS)
    return bool(stripped) and stripped[-1] in "！!？?…"


def has_number(text: str) -> bool:
    """含数字的句子放慢一点，让听众听清（说书人报数目也会放慢）。"""
    return bool(_NUMBER_UNIT_RE.search(text or ""))


def classify_role(unit: SpeechUnit) -> str:
    """决定这一段的角色。优先级：章末 > 章首 > 对话 > 强调 > 数字 > 普通。

    位置与形式都是**结构信号**，不猜语义 —— 猜语义会引入不确定性，
    同一段文字在不同章里念法不一致，就成了「忽快忽慢」的另一种别扭。
    """
    if unit.chapter_end:
        return "closing"
    if unit.chapter_start:
        return "opening"
    if is_dialogue(unit.text):
        return "dialogue"
    if is_emphasis(unit.text):
        return "emphasis"
    if has_number(unit.text):
        return "number"
    return "normal"


def narration_profile(unit: SpeechUnit) -> tuple[float, float, float]:
    """纯叙述句的 ``(语速偏移, 音高偏移, 停顿倍率)``。

    两个来源叠加：

    1. **长度**：短句顿、长句扬（见 ``_LENGTH_PROFILES``）。
    2. **段内位置**：段首略微扬、段末略微沉，画出这一口气的起落
       （见 ``_PARAGRAPH_ARC_HZ``）。单组段落没有对照，弧线取 0，
       免得把每个短段都硬拉出一个起伏。

    这里**刻意不按语义猜**：不判断「这句是写景还是抒情」——猜错会让念法
    在同样结构的句子上前后不一致，反而更别扭。
    """
    length = count_chars(unit.text)
    profile = _LONG_PROFILE
    for limit, rate, pitch, pause in _LENGTH_PROFILES:
        if length <= limit:
            profile = (rate, pitch, pause)
            break

    arc = 0.0
    if unit.paragraph_groups > 1:
        ratio = unit.paragraph_pos / float(unit.paragraph_groups - 1)
        arc = _PARAGRAPH_ARC_HZ - 2.0 * _PARAGRAPH_ARC_HZ * ratio
    return profile[0], profile[1] + arc, profile[2]


def _normalize_zero(value: float) -> float:
    """把 ``-0.0`` 与「四舍五入后是 0」的极小负数都归一成 ``0.0``。

    否则会输出 ``-0Hz`` / ``-0%``：功能上没问题，但日志和元数据里看着像 bug，
    排查时容易先怀疑一个不存在的问题。这里按最终展示的整数精度取整，
    保证「显示成 0」的值就是 ``+0``。
    """
    rounded = round(float(value))
    return 0.0 if rounded == 0 else float(rounded)


def _shift_percent(value: str, delta: float) -> str:
    """``"-8%"`` + ``-3`` → ``"-11%"``；解析不出来就退回 ``delta`` 本身。"""
    match = re.match(r"\s*([+-]?\d+(?:\.\d+)?)\s*%", str(value or ""))
    base = float(match.group(1)) if match else 0.0
    return f"{_normalize_zero(base + delta):+.0f}%"


def _shift_pitch(value: str, delta: float) -> str:
    """``"-2Hz"`` + ``4`` → ``"+2Hz"``。"""
    match = re.match(r"\s*([+-]?\d+(?:\.\d+)?)\s*Hz", str(value or ""), re.IGNORECASE)
    base = float(match.group(1)) if match else 0.0
    return f"{_normalize_zero(base + delta):+.0f}Hz"


def plan_prosody(
    units: Sequence[SpeechUnit],
    *,
    base_rate: str = "-12%",
    base_pitch: str = "-2Hz",
    base_volume: str = "+0%",
    base_pause_ms: int = 500,
    paragraph_pause_ms: int = 800,
    expressive: bool = True,
    max_pitch_hz: float = 10.0,
    lead_in_ms: int = 350,
) -> ProsodyPlan:
    """把 SpeechUnit 序列变成逐段的朗读指令。"""
    cues: list[ProsodyCue] = []
    for unit in units:
        role = classify_role(unit) if expressive else "normal"
        if not expressive:
            rate_delta = pitch_delta = 0.0
            pause_scale = 1.0
        elif role == "normal":
            # 纯叙述也要有起伏：靠长度分档 + 段内弧线，而不是整段一个调。
            rate_delta, pitch_delta, pause_scale = narration_profile(unit)
        else:
            rate_delta = ROLE_RATE_DELTA.get(role, 0.0)
            pitch_delta = ROLE_PITCH_DELTA.get(role, 0.0)
            pause_scale = ROLE_PAUSE_SCALE.get(role, 1.0)

        pitch_delta = max(-max_pitch_hz, min(max_pitch_hz, pitch_delta))
        pause = base_pause_ms * _terminal_factor(unit.text) * pause_scale
        if unit.paragraph_end or unit.chapter_end:
            # 章末即使没被标成段末，也必须按「收尾」处理 —— 顺序耦合（依赖
            # build_units 一定同时打上 paragraph_end）迟早会被别处调用踩坏。
            pause = max(pause, paragraph_pause_ms)
            if unit.chapter_end:
                pause = max(pause, paragraph_pause_ms * 1.5)
        else:
            # 句末停顿不得盖过段落停顿。没有这条，一个「！+ 强调」的句末
            # 算出来会比段落换行还长，段落分节感就被自己盖掉了 ——
            # 而「只有段落换行才停顿」正是这条规则的初衷。
            ceiling = max(base_pause_ms, int(round(paragraph_pause_ms * 0.9)))
            pause = min(pause, ceiling)

        cues.append(
            ProsodyCue(
                text=unit.text,
                role=role,
                rate=_shift_percent(base_rate, rate_delta),
                pitch=_shift_pitch(base_pitch, pitch_delta),
                volume=base_volume,
                pause_after_ms=int(round(max(0.0, pause))),
            )
        )
    return ProsodyPlan(cues=tuple(cues), lead_in_ms=max(0, int(lead_in_ms)) if cues else 0)


def plan_text(
    text: str,
    *,
    group_chars: int = 60,
    hard_limit: int = 160,
    **kwargs: object,
) -> ProsodyPlan:
    """便捷入口：给一段文本（每行一段）直接算出朗读方案。"""
    from .textsplit import build_units

    paragraphs = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    units = build_units(paragraphs, group_chars=group_chars, hard_limit=hard_limit)
    return plan_prosody(units, **kwargs)  # type: ignore[arg-type]
