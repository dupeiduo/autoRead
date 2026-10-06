"""异常类型定义。

分层原则：

* ``ConfigError`` / ``BookError`` —— 输入本身就是错的（书打不开、章节号不存在），
  重试没有意义，直接中止并给出可操作的提示。
* ``TTSError`` —— 合成阶段出错（网络抖动、语音不存在）。**不致命**：
  一本书几百章，某一章失败不该让整本书作废，由上层决定重试/降级/跳过。
"""

from __future__ import annotations


class AutoReadError(Exception):
    """所有 autoRead 异常的基类。"""


class ConfigError(AutoReadError):
    """配置缺失或非法。"""


class BookError(AutoReadError):
    """电子书无法解析（不是合法 EPUB、章节不存在等）。"""


class TTSError(AutoReadError):
    """语音合成失败。"""
