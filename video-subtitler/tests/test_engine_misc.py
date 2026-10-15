"""引擎辅助函数测试：限速解析 / 高度解析 / 滤镜字体转义。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import subprocess

import pytest

from app.core.config import Config
from app.core import cancel as task_cancel
from app.core.errors import AppError, Codes
from app.services.engines import ytdlp_engine
from app.services.engines.base import DirectTarget, DownloadProgress
from app.services.engines.ytdlp_engine import _display_thumbnail, _parse_rate, _safe_height
from app.services.renderer import _subtitle_filter, _write_positioned_ass, find_ffmpeg
from app.models.db import Store


def test_parse_rate():
    assert _parse_rate("5M") == 5 * 1024 * 1024
    assert _parse_rate("5MB") == 5 * 1024 * 1024   # 带 B 结尾也能解析
    assert _parse_rate("500K") == 500 * 1024
    assert _parse_rate("1048576") == 1048576
    assert _parse_rate("abc") == 0                 # 非法值 → 不限速


def test_safe_height():
    assert _safe_height("1080p") == 1080
    assert _safe_height("1080") == 1080
    assert _safe_height(" 720P ") == 720
    assert _safe_height("sb2") is None             # 非高度 id → None 而非崩溃
    assert _safe_height("hls-8") is None


def test_display_thumbnail_prefers_highest_supported_image():
    info = {
        "thumbnail": "https://img.example/max.webp",
        "thumbnails": [
            {"url": "https://img.example/low.jpg"},
            {"url": "https://img.example/max.jpg?token=1"},
            {"url": "https://img.example/max.webp"},
        ],
    }
    assert _display_thumbnail(info) == "https://img.example/max.jpg?token=1"


def test_subtitle_filter_escapes_font_name():
    """字体名里的 force_style 语法字符（, : ; '）必须被剔除（防滤镜注入）。"""
    vf = _subtitle_filter(Path("t.srt"),
                          font_name="x';drop,evil:1", font_size=18)
    style = vf.split("FontName=")[1].split(",")[0]
    assert style == "xdropevil1"                   # 仅剩安全字符
    for ch in ("'", ",", ":", ";"):
        assert ch not in style


def test_noto_burn_loads_bundled_font():
    vf = _subtitle_filter(Path("t.srt"), font_name="Noto Sans CJK SC")
    assert "FontName=Noto Sans CJK SC" in vf
    assert "fontsdir=" in vf


def test_subtitle_filter_position_mapping():
    """位置映射为实测经验值：顶部=6、中部=10、底部=2。"""
    srt = Path("t.srt")
    assert "Alignment=6" in _subtitle_filter(srt, position="top")
    assert "Alignment=10" in _subtitle_filter(srt, position="middle")
    assert "Alignment=2" in _subtitle_filter(srt, position="bottom")


def test_burn_color_and_middle_offset(tmp_path):
    vf = _subtitle_filter(Path("t.srt"), color="#FF8000")
    assert "PrimaryColour=&H000080FF" in vf
    srt = tmp_path / "sample.srt"
    ass = tmp_path / "sample.ass"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n示例字幕\n", encoding="utf-8")
    _write_positioned_ass(srt, ass, "middle", 30)
    text = ass.read_text(encoding="utf-8")
    assert r"\an5\pos(320,210)" in text
    assert "示例字幕" in text


def test_dragged_subtitle_position_survives_task_storage(tmp_path):
    srt = tmp_path / "sample.srt"
    ass = tmp_path / "sample.ass"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nDrag me\n", encoding="utf-8")
    _write_positioned_ass(srt, ass, "middle", 30, horizontal_offset=96)
    assert r"\an5\pos(416,210)" in ass.read_text(encoding="utf-8")
    store = Store(tmp_path / "tasks.db")
    task = store.create_task("https://example.com/a.mp4", "best", [], "burn",
                             horizontal_offset=96, vertical_offset=30)
    assert store.get_task(task["id"])["horizontal_offset"] == 96


def test_native_split_stream_progress_never_moves_backwards():
    state = {"done_before": 0}
    seen = []
    hook = ytdlp_engine.YtDlpEngine._wrap_aggregate(state)(
        ytdlp_engine.YtDlpEngine._make_hook(seen.append, state=state))
    hook({"status": "downloading", "downloaded_bytes": 99, "total_bytes": 100})
    hook({"status": "downloading", "downloaded_bytes": 100, "total_bytes": 100})
    hook({"status": "finished", "total_bytes": 100})
    hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 20})
    assert seen[1].fraction < 1.0
    assert seen[2].fraction >= seen[1].fraction


def test_aria2c_partial_failure_does_not_redownload(monkeypatch, tmp_path):
    class FailingBackend:
        def __init__(self, config): pass
        def ping(self): return True
        def download_many(self, targets, dest_dir, on_progress, cancel_check=None):
            on_progress(DownloadProgress(fraction=0.7))
            raise AppError(Codes.ENG_FAILED, "传输中断")

    monkeypatch.setattr(ytdlp_engine, "Aria2cBackend", FailingBackend)
    engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": True}}}))
    monkeypatch.setattr(engine, "_resolve_split_streams", lambda url, resolution: [
        DirectTarget(url="https://example.com/video", filename="video.mp4"),
        DirectTarget(url="https://example.com/audio", filename="audio.m4a")])
    monkeypatch.setattr(engine, "_resolve_direct_stream", lambda *args: (_ for _ in ()).throw(AssertionError("unexpected fallback")))
    with pytest.raises(AppError, match="传输中断"):
        engine.download("https://example.com/video", "best", tmp_path, on_progress=lambda _: None)


def test_aria2c_partial_file_without_progress_does_not_redownload(monkeypatch, tmp_path):
    class FailingBackend:
        def __init__(self, config): pass
        def ping(self): return True
        def download_many(self, targets, dest_dir, on_progress, cancel_check=None):
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / targets[0].filename).write_bytes(b"partial")
            raise AppError(Codes.ENG_FAILED, "传输中断")

    monkeypatch.setattr(ytdlp_engine, "Aria2cBackend", FailingBackend)
    engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": True}}}))
    monkeypatch.setattr(engine, "_resolve_split_streams", lambda url, resolution: [
        DirectTarget(url="https://example.com/video", filename="video.mp4"),
        DirectTarget(url="https://example.com/audio", filename="audio.m4a")])
    monkeypatch.setattr(engine, "_resolve_direct_stream", lambda *args: (_ for _ in ()).throw(AssertionError("unexpected fallback")))
    with pytest.raises(AppError, match="传输中断"):
        engine.download("https://example.com/video", "best", tmp_path)


def test_native_http_does_not_repeat_nonresumable_bytes(tmp_path):
    # 现场生成公开视频测试图样，不依赖开发电脑的私有评测媒体。
    source = tmp_path / "generated-sample.mp4"
    subprocess.run([
        find_ffmpeg(Config({})), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24", "-t", "2",
        "-c:v", "libx264", "-crf", "0", str(source),
    ], check=True, capture_output=True, timeout=30)
    payload = source.read_bytes()
    assert len(payload) > 150_000, "测试图样必须超过服务器的截断长度"
    counts = {"range": 0, "bytes": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Range"):
                counts["range"] += 1
            self.send_response(200)  # 模拟不支持 Range 的服务器
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            part = payload[:150_000]
            self.wfile.write(part)
            counts["bytes"] += len(part)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/sample.mp4"
        engine = ytdlp_engine.YtDlpEngine(Config({
            "download": {"aria2c": {"enabled": False}, "proxy_mode": "direct"}}))
        with pytest.raises(AppError, match="下载失败"):
            engine.download(url, "best", tmp_path)
        assert counts["range"] == 0
        assert counts["bytes"] <= len(payload) * 2  # 允许一次解析探测，禁止整段重复下载
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("split", [True, False])
def test_cancelled_aria2c_never_falls_back(monkeypatch, tmp_path, split):
    task_id = "cancelled-download-test"

    class CancelledBackend:
        def __init__(self, config): pass
        def ping(self): return True
        def download_many(self, targets, dest_dir, on_progress, cancel_check=None):
            raise AppError(Codes.TASK_CANCELLED, "任务已取消")
        def download(self, target, dest_dir, on_progress, cancel_check=None):
            task_cancel.cancel(task_id)
            raise AppError(Codes.ENG_FAILED, "取消导致连接失败")

    def unexpected(*args):
        raise AssertionError("取消后不应启动另一路下载")

    monkeypatch.setattr(ytdlp_engine, "Aria2cBackend", CancelledBackend)
    monkeypatch.setattr(ytdlp_engine, "_import_ytdlp", unexpected)
    engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": True}}}))
    streams = [DirectTarget(url="https://example.com/video", filename="video.mp4"),
               DirectTarget(url="https://example.com/audio", filename="audio.m4a")]
    monkeypatch.setattr(engine, "_resolve_split_streams", lambda *_: streams if split else None)
    monkeypatch.setattr(engine, "_resolve_direct_stream", unexpected if split else
                        lambda *_: streams[0])
    try:
        with pytest.raises(AppError) as error:
            engine.download("https://example.com/video", "best", tmp_path, task_id=task_id)
        assert error.value.code == Codes.TASK_CANCELLED
    finally:
        task_cancel.clear(task_id)
