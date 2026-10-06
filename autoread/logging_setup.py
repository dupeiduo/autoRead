"""日志：控制台 + 文件双写。控制台保持简洁，文件里留全量细节供事后排查。"""

from __future__ import annotations

import logging
import sys

from .config import Config
from .utils import ensure_dir

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(config: Config, *, verbose: bool = False) -> None:
    level_name = "DEBUG" if verbose else str(config.get("log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = config.path("log_file")
    try:
        ensure_dir(log_file.parent)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        # 日志文件写不了（只读目录等）不该让主流程起不来。
        pass

    logging.basicConfig(level=level, format=_FORMAT, datefmt=_DATEFMT, handlers=handlers, force=True)
    # 第三方库的 DEBUG 太吵，压低。
    for noisy in ("urllib3", "asyncio", "edge_tts"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
