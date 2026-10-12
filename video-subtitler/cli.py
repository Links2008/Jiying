#!/usr/bin/env python3
"""VideoSubtitler 命令行入口 —— 下载视频并生成多语言字幕。

用法：
  python cli.py <链接> --list-formats                 # 先看有哪些分辨率
  python cli.py <链接> -r best --langs zh,en          # 下载 + 生成中英字幕（软封装）
  python cli.py <链接> -r 1080p --langs zh --burn     # 硬字幕烧录
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.config import load_config
from app.core.errors import AppError
from app.core.logging import setup_logging
from app.core.paths import data_dir
from app.models.db import Store
from app.pipeline.runner import PipelineRunner
from app.services.engines.router import EngineRouter


def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def _print_formats(result) -> None:
    print(f"  {'format_id':<14}{'分辨率':<10}{'帧率':<8}{'协议':<14}{'大小'}")
    for f in result.formats:
        size = _fmt_size(f.filesize) if f.filesize else "-"
        print(f"  {f.format_id:<14}{f.label:<10}"
              f"{(str(int(f.fps)) + 'fps') if f.fps else '-':<8}{f.protocol:<14}{size}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="VideoSubtitler",
        description="下载视频（多分辨率）并按语音生成多语言字幕",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：")[-1],
    )
    parser.add_argument("url", nargs="?", help="视频页面链接")
    parser.add_argument("-r", "--resolution",
                        help="分辨率：best / 1080p / --list-formats 中的 format_id")
    parser.add_argument("--langs", default="zh",
                        help="目标字幕语言，逗号分隔（如 zh,en,ja）；源语言自动检测")
    parser.add_argument("--burn", action="store_true",
                        help="硬字幕烧录进画面（默认软封装，不重编码）")
    parser.add_argument("--model", help="Whisper 模型：tiny/base/small/medium/large-v3")
    parser.add_argument("--list-formats", action="store_true",
                        help="仅列出可用分辨率，不下载")
    parser.add_argument("--config", help="配置文件路径（默认 config.yaml）")
    args = parser.parse_args(argv)

    setup_logging()
    config = load_config(args.config)
    if args.model:
        config.set("transcriber.model", args.model)
    store = Store(data_dir() / "tasks.db")
    router = EngineRouter(config, store)

    if not args.url:
        parser.print_help()
        print("\n未提供链接。示例：python cli.py \"https://...\" --list-formats")
        return 0

    # 两阶段交互第一步：探测（缓存 24h，同链接秒回）
    try:
        engine, result = router.probe(args.url)
    except AppError as e:
        print(f"✗ 探测失败 [{e.code}] {e.message}", file=sys.stderr)
        if e.suggestion:
            print(f"  建议：{e.suggestion}", file=sys.stderr)
        return 1
    if args.list_formats or not args.resolution:
        print(f"标题：{result.title}")
        print(f"时长：{_fmt_dur(result.duration)}    引擎：{engine.name}")
        print("可用分辨率：")
        _print_formats(result)
        if not args.resolution:
            print("\n→ 加 -r <format_id> 或 -r best / -r 1080p 开始下载")
            return 0
        return 0

    langs = [l.strip() for l in args.langs.split(",") if l.strip()]
    runner = PipelineRunner(config, store)
    mode = "burn" if args.burn else "softmux"
    task = runner.create_task(args.url, args.resolution, langs, mode, title=result.title)
    print(f"任务 {task['id']} 开始：{result.title} @ {args.resolution} "
          f"({'烧录' if args.burn else '软封装'}, 字幕语言 {args.langs})")

    try:
        task = runner.run(task)
    except AppError as e:
        print(f"\n✗ 任务失败 [{e.code}] {e.message}", file=sys.stderr)
        if e.suggestion:
            print(f"  建议：{e.suggestion}", file=sys.stderr)
        return 1

    print("\n✓ 完成，产物：")
    for a in store.list_artifacts(task["id"]):
        p = Path(a["path"])
        size = f"  ({_fmt_size(p.stat().st_size)})" if p.exists() else ""
        print(f"  [{a['kind']:<8}] {p}{size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
