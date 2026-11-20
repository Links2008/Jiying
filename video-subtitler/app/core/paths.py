"""路径解析：兼容开发态、绿色版（portable.flag）、安装版（%LOCALAPPDATA%）三种布局。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

APP_NAME = "VideoSubtitler"


def replace_with_retry(source: Path, target: Path, *, before_retry=None) -> None:
    """仅对 Windows 临时共享冲突重试，保留原子替换及原始错误。"""
    for delay in (0.05, 0.1, 0.2, None):
        try:
            os.replace(source, target)
            return
        except OSError as error:
            code = getattr(error, "winerror", None)
            # Windows 替换已打开的普通文件也可能返回 5；只对可写的已有目标短暂重试。
            sharing_conflict = code in (32, 33) or (code == 5 and target.is_file() and os.access(target, os.W_OK))
            if delay is None or not sharing_conflict:
                raise
            time.sleep(delay)
            if before_retry is not None:
                before_retry()


def app_root() -> Path:
    """应用根目录：PyInstaller 冻结态取 exe 所在目录，开发态取项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def is_portable() -> bool:
    """绿色版判定：根目录存在 portable.flag 标记文件。"""
    return (app_root() / "portable.flag").exists()


def bin_dir() -> Path:
    """内置外部工具目录（ffmpeg/aria2c/yt-dlp）；开发态不存在时靠 PATH。

    PyInstaller 6 onedir 的数据文件统一落在 _internal/（即 sys._MEIPASS），
    冻结态需要额外检查那里。
    """
    d = app_root() / "bin"
    if d.exists():
        return d
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            d = Path(meipass) / "bin"
            if d.exists():
                return d
    return app_root()


def default_config_path() -> Path:
    """默认配置文件：项目根 config.yaml 优先，其次数据目录下的 config.yaml。"""
    dev = app_root() / "config.yaml"
    return dev if dev.exists() else data_dir() / "config.yaml"


def data_dir() -> Path:
    """数据目录：绿色版写根目录/data，安装版写 %LOCALAPPDATA%\\VideoSubtitler。"""
    if is_portable():
        d = app_root() / "data"
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        d = Path(base) / APP_NAME
    # 标准子目录一次性建齐（v3 文件库布局）
    for sub in ("library", "cache/probe", "cache/transcripts", "tmp", "models", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d
