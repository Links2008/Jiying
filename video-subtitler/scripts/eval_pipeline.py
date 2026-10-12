"""流水线成功率评测。

方法：构造「应成功」与「应失败」两组用例，逐个走真实 PipelineRunner
（探测 → 下载 → 提取 → 识别 → 渲染 全链路），统计：
  - 正向用例完成率（任务成功率）
  - 负向用例的优雅失败率（给出统一错误码而非裸异常）
每个用例记录耗时与错误码，报告写入 eval/pipeline/report.json。

用法：py scripts/eval_pipeline.py [--model base]
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_asr import sapi_tts  # 复用 TTS 生成标准语音  # noqa: E402

from app.core.config import Config, DEFAULTS  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.models.db import Store  # noqa: E402
from app.pipeline.runner import PipelineRunner  # noqa: E402
from app.services.renderer import find_ffmpeg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "eval" / "pipeline"
FFMPEG = find_ffmpeg(Config(dict(DEFAULTS)))


def build_speech_video(name: str, voice: str, text: str, size: str) -> Path:
    """彩条画面 + TTS 语音 → 测试视频（无需外网）。"""
    wav = WORK / f"{name}.wav"
    video = WORK / f"{name}.mp4"
    sapi_tts(text, voice, wav)
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration=12:size={size}:rate=15",
         "-i", str(wav), "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         "-shortest", str(video)],
        check=True, capture_output=True)
    return video


def serve_dir(directory: Path) -> tuple[int, http.server.ThreadingHTTPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="base")
    args = ap.parse_args()

    setup_logging("WARNING")  # 评测输出保持干净
    WORK.mkdir(parents=True, exist_ok=True)

    print("== 1/2 准备测试视频 ==")
    v_en = build_speech_video("en-sample", "Microsoft Zira Desktop",
                              "Pipeline success rate test. The result should be reliable.",
                              "640x360")
    v_zh = build_speech_video("zh-sample", "Microsoft Huihui Desktop",
                              "流水线成功率测试，字幕应当正常生成。", "960x540")
    (WORK / "page.html").write_text("<html><body>这不是视频文件</body></html>")
    port, srv = serve_dir(WORK)
    base = f"http://127.0.0.1:{port}"
    print(f"  本地服务 :{port}，样本 {v_en.name} / {v_zh.name}")

    cases = [
        # ---- 应成功 ----
        dict(name="本地直链-软封装", pos=True, url=f"{base}/en-sample.mp4",
             langs=["en"], mode="softmux"),
        dict(name="本地直链-硬烧录", pos=True, url=f"{base}/zh-sample.mp4",
             langs=["zh"], mode="burn"),
        dict(name="多语言-null翻译跳过", pos=True, url=f"{base}/en-sample.mp4",
             langs=["en", "ja"], mode="softmux"),
        dict(name="同链接重复提交", pos=True, url=f"{base}/en-sample.mp4",
             langs=["en"], mode="softmux"),
        dict(name="远程直链-有语音", pos=True,
             url="https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
             langs=["en"], mode="softmux"),
        # ---- 应失败（要求优雅失败 + 统一错误码）----
        dict(name="端口未监听", pos=False, url="http://127.0.0.1:59999/x.mp4"),
        dict(name="文件不存在404", pos=False, url=f"{base}/notexist.mp4"),
        dict(name="网页非视频", pos=False, url=f"{base}/page.html"),
        # ---- 应失败且属于设计行为：内容无人声 → 明确报错而非空字幕 ----
        dict(name="远程内容无人声守卫", pos=False, expect_code="E-ASR-502",
             url="https://download.samplelib.com/mp4/sample-5s.mp4",
             langs=["en"], mode="softmux"),
    ]

    print(f"== 2/2 执行 {len(cases)} 个用例（模型 {args.model}，CPU）==")
    cfg = Config(dict(DEFAULTS))
    cfg.set("transcriber.model", args.model)
    cfg.set("transcriber.device", "cpu")
    cfg.set("transcriber.compute_type", "int8")
    store = Store(WORK / "tasks.db")
    runner = PipelineRunner(cfg, store)

    rows, t_all = [], time.monotonic()
    for c in cases:
        t0 = time.monotonic()
        row = {"name": c["name"], "expect": "成功" if c["pos"] else "优雅失败"}
        try:
            task = runner.create_task(c["url"], "best", c.get("langs", ["en"]),
                                      c.get("mode", "softmux"))
            task = runner.run(task)
            row["status"] = task["status"]
            row["error"] = task.get("error") or ""
        except AppError as e:
            row["status"] = "failed"
            row["error"] = e.code
        row["seconds"] = round(time.monotonic() - t0, 1)
        if c["pos"]:
            ok = row["status"] == "completed"
        elif c.get("expect_code"):
            ok = row["status"] == "failed" and row["error"] == c["expect_code"]
        else:
            ok = row["status"] == "failed" and row["error"].startswith("E-")
        row["verdict"] = "✓" if ok else "✗"
        rows.append(row)
        print(f"  [{row['verdict']}] {c['name']:<14} → {row['status']:<10}"
              f" {row['error'][:24]:<24} {row['seconds']}s")
    total = time.monotonic() - t_all

    pos = [r for r, c in zip(rows, cases) if c["pos"]]
    neg = [r for r, c in zip(rows, cases) if not c["pos"]]
    summary = {
        "model": args.model,
        "task_success_rate": round(sum(r["status"] == "completed" for r in pos) / len(pos), 4),
        "graceful_failure_rate": round(sum(r["verdict"] == "✓" for r in neg) / len(neg), 4),
        "avg_positive_seconds": round(sum(r["seconds"] for r in pos) / len(pos), 1),
        "total_seconds": round(total, 1),
        "rows": rows,
    }
    print(f"\n▶ 任务成功率：{summary['task_success_rate']:.0%}"
          f"（{sum(r['status'] == 'completed' for r in pos)}/{len(pos)}）")
    print(f"▶ 优雅失败率：{summary['graceful_failure_rate']:.0%}"
          f"（{sum(r['verdict'] == '✓' for r in neg)}/{len(neg)}）")
    print(f"▶ 正向用例平均耗时：{summary['avg_positive_seconds']}s（总 {summary['total_seconds']}s）")

    out = WORK / "report.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告已写入 {out}")
    srv.shutdown()


if __name__ == "__main__":
    main()
