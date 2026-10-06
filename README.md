# AutoRead

把 EPUB 电子书读成有声书：**一章一个音频**，逐句抑扬顿挫，说书先生口吻。

与同根目录的 [AutoVideo](../AutoVideo) 同源：核心**零第三方依赖**（`zipfile` /
`html.parser` / `wave` / `subprocess` 全部来自标准库），只有想要「好音色」时才装
可选的 [`edge-tts`](https://github.com/rany2/edge-tts)。

## 1. 它解决什么问题

| 需求 | 实现 |
| --- | --- |
| 各个章节都有音频 | 每个章节产出一个音频文件（EPUB spine 里的每篇正文 = 一个文件） |
| **不在换行/换页处卡顿** | 解析时把「源码里被硬换行拆散的同一段话」还原成一段；段内不留停顿，只在真正的段落之间留 |
| 阅读速度适中 | Edge 基准语速默认 `-12%`：纯语音约 4.7 字/秒，含停顿整章约 4.3 字/秒（新闻播报约 5.5~6 字/秒） |
| **有抑扬顿挫** | 逐句规划语速/音高/停顿：短句「顿」、长句「扬」、对话活、惊叹挑、段末留白 |
| 说书先生语调 | Edge `zh-CN-YunjianNeural`（云健）：微软为「小说/体育解说」训练的成熟磁性男声 |
| 可以传不同的电子书 | `--book` 接受任意 EPUB，章节顺序读 spine，标题读 EPUB3 nav / EPUB2 NCX |
| 整本规模可先评估 | `estimate` 算出总时长/总大小/生成耗时，不合成；首行就能答「要不要现在跑」 |

## 2. 安装

要求：macOS 或 Linux、Python 3.10+、FFmpeg（Edge 输出是 mp3，拼接前要转 wav）。

```bash
cd /path/to/autoRead
brew install ffmpeg          # macOS；Ubuntu: sudo apt install ffmpeg
```

核心程序用系统 `python3` 就能跑。**想要按需求里的「成熟有磁性男声」，必须装 edge-tts**：

```bash
python3 -m venv .venv
./.venv/bin/pip install edge-tts
```

装好后**不需要**用 venv 启动主程序：程序会自动发现项目里的 `.venv` 并调用它。
没装 edge-tts 时会回落到 macOS `say`（离线可用，但**只有女声**，见第 6 节）。

可选：复制环境变量模板，改默认音色/语速等。

```bash
cp .env.example .env
```

## 3. 快速开始

```bash
# 看目录（不合成，先确认章节号）
python3 -m autoread list --book "剑来(烽火戏诸侯).epub" --limit 10

# 评估整本要占多大、要跑多久（不合成）
python3 -m autoread estimate --book "剑来(烽火戏诸侯).epub"

# 试听：第一章的前三页
python3 -m autoread make --book "剑来(烽火戏诸侯).epub" --chapter 1 --pages 3

# 换一本书：只改 --book 就行
python3 -m autoread make --book "另一本书.epub" --chapter 1 --pages 3
```

产物：

```text
data/output/<书名>/
    0001_第一章_惊蛰_试听p1-3.mp3       试听（每章前 3 页）
    0001_第一章_惊蛰_试听p1-3.mp3.json  元数据（音色/时长/韵律/是否降级）
    0001_第一章_惊蛰.mp3               整章（去掉 --pages 生成）
    manifest.json                      本书已生成章节索引
```

`--pages 3` 的产物文件名里带「试听」（如 `0001_第一章_惊蛰_试听p1-3.mp3`），
**不会**和整章产物混淆，也不用记 `_p1-3` 这种暗号。跑完时命令行会直接把试听文件
的完整路径打出来。去掉 `--pages` 就是整章。

生成整本（示例：483 章，耗时以小时计，建议先 `--pages` 试听）：

```bash
python3 -m autoread make --book "剑来(烽火戏诸侯).epub" --all
```

不联网、不花钱，只验证切分与流程（产出等长静音）：

```bash
python3 -m autoread make --book "剑来(烽火戏诸侯).epub" --chapter 1 --pages 3 --dry-run
```

## 4. 整本输出与规模评估

### 一章一个音频

`make` 每章产出一个音频 + 一个同名旁车 `.json`。整本输出：

```bash
python3 -m autoread make --book "剑来(烽火戏诸侯).epub" --all        # 483 章（几十小时）
python3 -m autoread make --book "剑来(烽火戏诸侯).epub" --volume 1   # 先跑一册（9 章）
python3 -m autoread make --book "另一本书.epub" --all                # 换书
```

* **可续跑**：已存在且大于 1KB 的产物默认跳过（`--force` 覆盖）。中途中断、隔天再跑，
  只补没做完的章 —— 几百章的活不该因为一次断网从头再来。
* **分册跑**：`--volume 1-3` 比 `--all` 可控，一册约 0.4 GB。
* **产物索引**：`manifest.json`（已生成章节；试听与整章分开记，键为 `1` 与 `1@p3`）、
  `report.json`（本次范围 + 全书评估）。
* 每次 `make` 结束都会打印**真实**产出统计：

```text
完成：成功 2，跳过 0，失败 0，总耗时 9 分 12 秒
产出统计：本次范围内已生成 2/2 章，累计 2 个文件 / 69.1 MB / 音频总时长 2 小时 30 分
平均每章 34.5 MB / 1 小时 15 分
按实测外推：本次 2 章全部完成约 19.0 GB（较理论估算 -0.3%）
```

### 先评估，再动手

一本书几百章、几十 GB，不能「先跑了再说」。`estimate` 不合成任何音频，只算清楚规模：

```bash
python3 -m autoread estimate --book "剑来(烽火戏诸侯).epub"
```

```text
《剑来（1-49册）_烽火戏诸侯》
共 483 章 / 11,355,424 字 / 268,382 个音频片段
配置：mp3 64k · 语速 -12% · 24000 Hz

预计产出
  音频总时长   30 天 2 小时
  文件总大小   19.41 GB
  文件数量     483 个（每章一个）
  平均每章     41.15 MB / 1 小时 29 分
  生成耗时     ≈2 天 1 小时（按 1.5 段/秒估算）

已产出情况
  已完成       2 章 / 3 个文件，实际占用 70.4 MB
  还差         30 天 音频

按卷明细
    #  卷名                     章        字数          时长        大小
    1  剑来1：少年起微末           9    226,173   14 小时 23 分   395.55 MB
    2  剑来2：忽为远行客          11    268,138   16 小时 57 分   466.26 MB
    ...
```

评估口径（算法都在 `autoread/report.py`）：

| 环节 | 怎么算 |
| --- | --- |
| 时长 | **逐项累加**：`纯语音 + 段间停顿 + 剪静音余量 + 章首留白`。纯语音 = 字数 ÷ 语速速度（`rate=0%` 时 5.39 字/秒，实测反解）；停顿真的跑一遍韵律规划逐个加出来，最后一段的停顿不插入所以不计 |
| 大小 | mp3：`时长 × 码率 ÷ 8 × 1.001`；wav：`时长 × 采样率 × 声道 × 2`（无压缩 PCM） |
| 跟着停顿变 | 改 `base_pause_ms` / `paragraph_pause_ms` 后估算立刻跟着变；把停包含进「速度常数」会让历史估算全部失真 |
| 片段数 | 真的跑一遍切分**数出来**，不是「字数 ÷ 60」估的 —— 片段数决定生成耗时 |
| 生成耗时 | 片段数 ÷ `estimate.cues_per_second`（默认 1.5）。实测吞吐区间很宽：网络差 0.65 段/秒、网络好 2.1 段/秒，所以取中间偏保守 |
| 实测校准 | 只要已产出过整章，就用**实测「字节/字」**外推全书，与理论值并列显示 |

时长模型对两个实测样本的误差都在 0.3% 以内（语速 `-8%`：估 219.8s / 实测 219.3s；
`-12%`：估 229.7s / 实测 229.9s）。整章实测同样对得上：第一章预估 1:15、实测
**1:15:14**；预估 34.8 MB、实测 **34.44 MB**（误差约 1%）。

试听片段（`_试听p1-3`）**不参与校准** —— 它带一次性的章首留白，摊到每字上的固定开销偏高，
拿它外推会系统性偏大；「平均每章」也只用整章样本。

### 网络抖动怎么办

公开的 Edge TTS 端点偶尔连不上（实测一次 470 段的整章里，有 7 段首次失败）。
处理方式是**片段级重试**，而不是整章重来：

* 合成按 24 段一批提交，每段单独记录成败；
* 失败的段在下一轮**只重试它们**（`tts.edge_attempts`，默认 3 轮，指数退避）；
* 连接/接收超时显式设置（`tts.edge_connect_timeout` / `edge_receive_timeout`），
  卡住的连接尽快让位，而不是占着并发槽；
* 并发默认 5（`tts.edge_concurrency`），留一点余量掩盖偶发卡顿；
* 一个批次的等待上限按段数放大，避免把「慢但仍在推进」的批次误判为卡死。

口径可覆盖：`--format wav`、`--bitrate 48k`、`--pages 3`（只评估试听范围）、
`--volume 1`、`--json`、`--report out.json`。

## 5. 为什么听起来是「说书」而不是「念稿」

一次性把整章丢给 TTS，只能得到一个调 —— 那就是念稿。本工具**逐段调用**合成，
每段单独设语速/音高/停顿，再在 PCM 层拼接（`autoread/prosody.py`）。

角色判定只用**结构信号**（位置、标点、引号、长度），不猜语义 —— 猜错会让同样结构
的句子念法前后不一致，反而更别扭：

| 角色 | 判定 | 语速 | 音高 | 停顿 |
| --- | --- | --- | --- | --- |
| `opening` | 章首 | −3% | −1Hz | ×1.5 |
| `dialogue` | 引号开头 / 引语够长 / 引语+说话动词 | +3% | +4Hz | ×0.85 |
| `emphasis` | ≤16 字且以 `！？…` 收尾 | −6% | +5Hz | ×1.6 |
| `number` | 含阿拉伯数字 | −5% | −1Hz | ×1.35 |
| `closing` | 章末 | −3% | −2Hz | ×1.5 |
| `normal` | 其余 | 见下 | 见下 | 见下 |

**纯叙述也要有起伏**，否则大段描写就是平的。`normal` 叠加两个确定性来源：

* **长度分档**：短句（≤12 字）慢而沉、停顿长（「顿」）；长句（>60 字）略快略亮（「扬」）。
* **段内弧线**：一段是一口气，段首 +1.5Hz、段末 −1.5Hz，画出起落。

**停顿只留给「段落换行」**。分两层，界线必须听得出：

句子级停顿 = `base_pause_ms` × 标点倍率 × 角色倍率，倍率范围刻意收窄，
好让台阶清楚（数值为含 60ms 剪静音余量的实测口径）：

| 台阶 | 配置值 | 实测（含余量） | 出现位置 |
| --- | --- | --- | --- |
| 逗号 / 顿号 | ×0.7 / ×0.6 | ~0.30~0.41s | 长句被切开的分句处 |
| **普通句末（。+ 普通句）** | **500ms** | **~0.56s** | 句号收尾，最常见 |
| 短句（≤12 字） | ×1.20 | ~0.66s | 「少年姓陈。」这类短促句，是「顿」 |
| 叹号 / 省略号 | ×1.25 / ×1.30 | ~0.69~0.71s | 情绪句 |
| **段落换行** | **800ms**（`prosody.paragraph_pause_ms`） | **~0.86~0.90s** | 整章唯一的分节信号 |
| 章末 | ×1.5 | ~1.26s | 收尾留白 |

句末停顿**永远不会**盖过段落停顿（`plan_prosody` 里有硬上限）—— 否则一个「！+ 强调」
的句末会比段落换行还长，段落分节感就被自己吃掉了。

段落**内部**的排版换行（源码硬换行、`<br>`）在解析层就被归并，**不产生任何停顿** ——
按它们停顿就是「一句话一顿」的换行卡顿。所以想把分节感调强/调弱，
只需要动 `paragraph_pause_ms` 这一个旋钮。

实测（第一章前三页，1023 字）：语速在 `-16%`~`-10%`、音高在 `-4Hz`~`+0Hz` 之间变化；
停顿实测 0.25~0.90s，其中分句处 ~0.3~0.4s、普通句末 ~0.56s、段落换行 ~0.86~0.90s。
全部是确定性计算，同一段文字每次生成的念法一致。

不想要这些起伏（例如做对照实验）：`--no-prosody`。

## 6. 为什么不会在换行/换页处卡顿

两个根因，分别修在解析层和合成层：

1. **解析层**（`autoread/epub.py`）：EPUB 正文常把一段话硬换行成好几行
   （Sigil/Calibre 转换后尤甚），作者也可能用 `<br>` 主动折行。若按「源码里的行」
   朗读，每行末尾都会被当成句子结束而停顿 —— 这就是「换行卡顿」。本工具用 `<p>` /
   `<li>` / `<blockquote>` 等**块级元素**界定段落，块内的 `<br>` 与所有空白一律归并
   （`normalize_cjk`）：**只有段落换行才留停顿（800ms），其它换行不留**。
2. **合成层**：一章合成成**一个** PCM 流，中间没有任何「页」的概念，自然没有换页爆音或
   错位。片段切点都在句子边界；每段自带的静音会被剪掉（`trim_silence`），停顿完全由
   韵律规划决定 —— 否则拼接处会出现「两段自带静音 + 插入停顿」的忽长忽短间隙。

想看效果：`list` 出来的「页」是按字符预算（默认 500 字）近似的，只为 `--pages` 预览服务，
切点只在段落边界，不会把一句话劈成两半。

## 7. 音色

```bash
python3 -m autoread voices            # 看 Edge 与 macOS 可用音色
python3 -m autoread voices --engine macos_say
```

| 音色 | 特点 |
| --- | --- |
| `zh-CN-YunjianNeural`（云健，默认） | 成熟磁性男声，微软标注「小说/体育」，**说书首选** |
| `zh-CN-YunyangNeural`（云扬） | 新闻播报腔男声，庄重但偏念稿 |
| `zh-CN-YunxiNeural`（云希） | 年轻活泼男声 |
| `zh-CN-YunxiaNeural`（云夏） | 少年音，适合童书 |
| `zh-CN-XiaoxiaoNeural`（晓晓） | 女声，温暖，适合散文 |

换音色：`--voice zh-CN-YunyangNeural`，或写进 `.env` 的 `AUTOREAD_TTS_VOICE`。

**关于降级（必须说清楚）**：macOS 的简体中文语音目前只有女声 `Ting-Ting`
（`Mei-Jia` 是台湾国语、`Sin-ji` 是粤语）。所以一旦 edge 失败降级到 `say`，
「成熟有磁性男声」这个要求就保不住了。因此：

* edge 失败会**先重试**（`tts.edge_attempts`，默认 3 次、指数退避），网络抖一下不换音色；
* 真降级了会打 WARNING，并写进元数据 `audio.degraded=true` 与 `audio.detail.fallback_from`；
* 想「宁可不产出也不发机器音」：`.env` 里设 `AUTOREAD_TTS_FALLBACK_ENGINE=none`。

## 8. 代码地图

```text
autoread/
    cli.py              命令行：list / make / estimate / voices
    epub.py             EPUB 解析：spine 顺序、nav/NCX 标题、按段落抽正文  ← 第 5 节(1)
    textsplit.py        段落 → 句子 → TTS 片段；分页（--pages 用）
    prosody.py          逐句语速/音高/停顿规划                            ← 第 4 节
    tts.py              引擎链（edge → say → silence）、拼接、降级
    audio.py            WAV 读写、插停顿拼接、剪静音、ffmpeg 转码
    edge_worker.py      edge-tts 子进程工作器（由 .venv 解释器执行）
    report.py           产出规模评估（时长/大小/生成耗时）与产出统计   ← 第 4 节
    config.py           默认值 ← config.json ← 环境变量
    utils.py            文本归一、文件名、JSON、.env
tests/                  113 个用例，零第三方依赖（python3 -m unittest）
```

## 9. 配置

优先级：**内置默认值 ← `config/config.json` ← 环境变量**（后者覆盖前者）。

```bash
cp config/config.example.json config/config.json
```

常用环境变量（写进 `.env` 即可）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AUTOREAD_TTS_ENGINE` | `auto` | `auto` / `edge` / `macos_say` / `silence` |
| `AUTOREAD_TTS_VOICE` | `zh-CN-YunjianNeural` | Edge 音色 |
| `AUTOREAD_EDGE_RATE` | `-12%` | 基准语速（越负越慢） |
| `AUTOREAD_PARAGRAPH_PAUSE_MS` | `800` | **段落换行**的停顿（唯一的分节信号） |
| `AUTOREAD_BASE_PAUSE_MS` | `500` | 句末基准停顿（普通句号收尾 ≈ 此值） |
| `AUTOREAD_EDGE_PITCH` | `-2Hz` | 基准音高（负值更沉） |
| `AUTOREAD_EDGE_PYTHON` | 自动 | 能 `import edge_tts` 的解释器路径 |
| `AUTOREAD_TTS_FALLBACK_ENGINE` | `macos_say` | 兜底引擎；`none` = 宁可不产出 |
| `AUTOREAD_CHARS_PER_PAGE` | `500` | 「一页」的字符预算 |
| `AUTOREAD_OUTPUT_DIR` | `data/output` | 输出目录 |
| `AUTOREAD_LOG_LEVEL` | `INFO` | 日志级别 |

命令行参数优先级最高，例如 `--voice` / `--rate` / `--pitch` / `--pages` / `--out`。

## 10. 测试

```bash
python3 -m unittest discover -s tests        # 180 个用例，全部离线
```

覆盖：软换行合段、闭引号归属、超长句切分、分页边界、角色判定、韵律变调、
WAV 拼接与停顿、剪静音、配置合并、章节/分册选择、**大小换算与实测校准**、
`--dry-run` 与 `estimate` 端到端。

## 11. 常见问题

* **`未找到可用的 edge-tts`** —— 忘了建虚拟环境：
  `python3 -m venv .venv && ./.venv/bin/pip install edge-tts`。
* **`CERTIFICATE_VERIFY_FAILED`** —— python.org 的 Python 不带系统 CA：
  `./.venv/bin/pip install certifi`（程序会优先用它）。
* **某一章失败** —— 日志会写清哪一段失败；重跑会跳过已完成的章节
  （想重生成加 `--force`）。一本书几百章，不会因为某一章失败全盘作废。
* **整本太慢** —— 一本 483 章的书要几小时（每章约 200 段的网络请求）。先
  `--pages` 确认音色与韵律满意，再整本跑；也可以先只跑一卷。

## 12. 许可与边界

仅供个人学习与自用。请自行确认对所用电子书拥有合法使用权，不要将生成的音频
用于分发或商业用途。
