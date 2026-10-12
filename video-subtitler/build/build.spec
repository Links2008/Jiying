# -*- mode: python ; coding: utf-8 -*-
"""VideoSubtitler 打包配置（PyInstaller onedir）。

用法（项目根目录执行）：pyinstaller build/build.spec --noconfirm
产物：dist/VideoSubtitler/（含 exe + bin/ 外部工具）
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

root = os.path.abspath(os.path.join(SPECPATH, ".."))  # SPECPATH = build/ 所在目录

# bin/ 外部工具随包分发（ffmpeg/aria2c/yt-dlp，CI 或手工放入项目 bin/）
datas = [(str(Path(root) / "bin"), "bin"),
         (str(Path(root) / "app" / "web"), "app/web")]  # 内嵌 Web UI
# faster-whisper 的 tokenizer 资源文件
datas += collect_data_files("faster_whisper")

a = Analysis(
    [os.path.join(root, "cli.py")],
    pathex=[root],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "yt_dlp",
        "yt_dlp.extractor",           # 站点提取器动态加载，必须显式打入
        "faster_whisper",
        "yaml",
    ],
    excludes=[
        "torch", "tkinter", "matplotlib", "PyQt5", "PyQt6",  # 明确用不到的重依赖
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="VideoSubtitler",
    console=True,          # CLI 优先；pywebview 图形壳接入后改 windowed
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,             # UPX 会显著提高杀软误报率，宁大勿报
    name="VideoSubtitler",
)
