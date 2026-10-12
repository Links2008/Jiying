"""日志：控制台 + 滚动文件（data/logs/）。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import data_dir

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str = "INFO") -> None:
    """初始化根日志器：控制台 + data/logs/videosubtitler.log（5MB × 3）。"""
    root = logging.getLogger()
    if root.handlers:  # 避免重复初始化
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT, datefmt="%H:%M:%S"))
    root.addHandler(console)
    try:
        log_dir = data_dir() / "logs"
        fh = RotatingFileHandler(
            log_dir / "videosubtitler.log", maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fh)
    except OSError:  # 数据目录不可写时降级为仅控制台
        pass
