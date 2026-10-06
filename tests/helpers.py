"""测试辅助：把项目根目录放进 ``sys.path``，让 ``python3 -m unittest`` 能找到包。

不安装包、不用 pytest：跟主程序一样保持零第三方依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

__all__ = ["ROOT"]
