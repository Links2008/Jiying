"""VideoSubtitler —— 视频多分辨率下载 + 多语言字幕生成。"""

from importlib.resources import files

__version__ = files(__package__).joinpath("VERSION").read_text(encoding="utf-8").strip()
