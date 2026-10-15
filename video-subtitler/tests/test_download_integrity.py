"""复现缺三秒画面、跳过分片和错误容器；所有媒体都在临时目录生成。"""
import shutil
import subprocess
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import av
import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services import renderer
from app.services.engines import ytdlp_engine


@pytest.fixture(scope="module")
def media_samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("download-integrity")
    ff = renderer.find_ffmpeg(Config({}))
    for name, seconds, codec in [("complete.mp4", 8, "libx264"),
                                  ("short.mp4", 5, "libx264"),
                                  ("complete.webm", 8, "libvpx-vp9")]:
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"color=c=blue:s=64x48:r=10:d={seconds}", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=8", "-c:v", codec,
                        "-threads", "2", "-c:a", "libopus" if name.endswith("webm") else "aac",
                        str(root / name)], check=True, capture_output=True, timeout=30,
                       creationflags=renderer._NO_WINDOW)
    return root


def native_engine(monkeypatch, result, captured=None):
    class FakeYoutubeDL:
        def __init__(self, opts):
            if captured is not None:
                captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def extract_info(self, *_args, **_kwargs):
            return result

    monkeypatch.setattr(ytdlp_engine, "_import_ytdlp", lambda: SimpleNamespace(
        YoutubeDL=FakeYoutubeDL, utils=SimpleNamespace(DownloadError=RuntimeError)))
    engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": False}}}))
    monkeypatch.setattr(engine, "_head_direct_file", lambda *_: None)
    return engine


def test_native_rejects_short_picture_despite_full_length_audio(tmp_path, monkeypatch, media_samples):
    media = tmp_path / "clip.mp4"
    shutil.copy2(media_samples / "short.mp4", media)
    original = media.read_bytes()
    engine = native_engine(monkeypatch, {"duration": 8,
        "requested_downloads": [{"filepath": str(media)}]})
    with pytest.raises(AppError, match="下载文件不完整") as error:
        engine.download("https://example.test/video", "hls-1080", tmp_path)
    assert error.value.code == Codes.ENG_FAILED
    assert not media.exists()
    saved = list(tmp_path.parent.glob(tmp_path.name + ".invalid-*/clip.mp4"))
    assert len(saved) == 1 and saved[0].read_bytes() == original


def test_native_requires_all_fragments_and_keeps_selected_format(tmp_path, monkeypatch, media_samples):
    media = tmp_path / "clip.mp4"
    shutil.copy2(media_samples / "complete.mp4", media)
    opts = {}
    engine = native_engine(monkeypatch, {"duration": 8,
        "requested_downloads": [{"filepath": str(media)}]}, opts)
    assert engine.download("https://example.test/video", "hls-1080", tmp_path) == [media]
    assert opts.get("skip_unavailable_fragments") is False
    assert opts.get("fragment_retries", 0) >= 3
    assert opts["format"] == "hls-1080[acodec=none]+ba/hls-1080"


@pytest.mark.parametrize('cancelled', [False, True])
def test_closed_fragment_stream_reports_failure_or_cancel_without_losing_partial(tmp_path, monkeypatch, cancelled):
    from app.core import cancel
    task_id = 'closed-fragment-stream'
    cancel.clear(task_id)
    partial = tmp_path / 'clip.mp4.part'
    partial.write_bytes(b'previous-fragments')
    engine = native_engine(monkeypatch, None)

    def fail(*_args, **_kwargs):
        if cancelled:
            cancel.cancel(task_id)
        raise ValueError('write to closed file')

    monkeypatch.setattr(engine, '_extract_info', fail)
    try:
        with pytest.raises(AppError) as error:
            engine.download('https://example.test/video', 'hls', tmp_path, task_id=task_id)
        assert error.value.code == (Codes.TASK_CANCELLED if cancelled else Codes.NET_FAILED)
        if not cancelled:
            assert error.value.retryable and '分片' in error.value.message
        assert partial.read_bytes() == b'previous-fragments'
        assert not list(tmp_path.glob('*.mp4'))
    finally:
        cancel.clear(task_id)


@pytest.mark.parametrize("extension", ["webm", "mp4"])
def test_native_returns_real_mp4_even_for_single_webm_stream(tmp_path, monkeypatch, media_samples, extension):
    media = tmp_path / ("clip." + extension)
    shutil.copy2(media_samples / "complete.webm", media)
    original = media.read_bytes()
    engine = native_engine(monkeypatch, {"duration": 8,
        "requested_downloads": [{"filepath": str(media)}]})
    files = engine.download("https://example.test/video", "best", tmp_path)
    assert len(files) == 1 and files[0].suffix == ".mp4"
    with av.open(str(files[0])) as container:
        assert "mp4" in container.format.name
        assert len(container.streams.video) == len(container.streams.audio) == 1
        assert container.streams.audio[0].codec_context.name in {"aac", "opus"}
    assert media.read_bytes() == original


def test_mux_rejects_three_second_picture_loss_in_short_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "ffmpeg")
    monkeypatch.setattr(renderer, "probe_duration", lambda *_args, **_kwargs: 30.708)
    monkeypatch.setattr(renderer, "_video_packet_duration", lambda *_: 27.708, raising=False)

    def run(_args, **kwargs):
        if kwargs.get("on_time"):
            kwargs["on_time"](27.708)

    monkeypatch.setattr(renderer, "_run", run)
    with pytest.raises(AppError, match="下载文件不完整"):
        renderer.mux_av(Config({}), Path("video"), Path("audio"), tmp_path / "merged.mp4")


def test_low_frame_rate_b_frames_are_not_mistaken_for_truncation(tmp_path):
    media = tmp_path / "low-fps.mp4"
    subprocess.run([renderer.find_ffmpeg(Config({})), "-v", "error", "-f", "lavfi", "-i",
                    "color=s=64x48:r=1:d=3", "-c:v", "libx264", str(media)],
                   capture_output=True, check=True, timeout=30, creationflags=renderer._NO_WINDOW)
    assert renderer.validate_download(Config({}), media, 3) == media


def test_source_duration_rejects_equally_short_video_and_audio(tmp_path, media_samples):
    video, audio = tmp_path / "video.mp4", tmp_path / "audio.m4a"
    for target, selection in [(video, "0:v:0"), (audio, "0:a:0")]:
        subprocess.run([renderer.find_ffmpeg(Config({})), "-v", "error", "-i",
                        str(media_samples / "complete.mp4"), "-t", "5", "-map", selection,
                        "-c", "copy", str(target)], capture_output=True, check=True,
                       timeout=30, creationflags=renderer._NO_WINDOW)
    with pytest.raises(AppError, match="下载文件不完整"):
        renderer.mux_av(Config({}), video, audio, tmp_path / "merged.mp4", expected_duration=8)
    assert video.is_file() and audio.is_file()
    assert not (tmp_path / "merged.mp4").exists()


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1])
def test_invalid_source_duration_is_rejected(tmp_path, media_samples, duration):
    media = tmp_path / "clip.mp4"
    shutil.copy2(media_samples / "complete.mp4", media)
    with pytest.raises(AppError):
        renderer.validate_download(Config({}), media, duration)


def test_packet_probe_preserves_cancel_during_open(monkeypatch, tmp_path):
    from app.core import cancel
    tid = "packet-open-cancel"
    cancel.clear(tid)

    def open_cancelled(*_args, **_kwargs):
        cancel.cancel(tid)
        raise ValueError("damaged input")

    monkeypatch.setattr(av, "open", open_cancelled)
    try:
        with pytest.raises(AppError) as error:
            renderer._video_packet_duration(tmp_path / "clip.mp4", tid)
        assert error.value.code == Codes.TASK_CANCELLED
    finally:
        cancel.clear(tid)


def test_real_hls_missing_fragment_fails_then_resumes_completely(tmp_path, monkeypatch, media_samples):
    source = tmp_path / "hls"
    source.mkdir()
    subprocess.run([renderer.find_ffmpeg(Config({})), "-hide_banner", "-loglevel", "error",
                    "-i", str(media_samples / "complete.mp4"), "-c:v", "libx264", "-g", "20",
                    "-sc_threshold", "0", "-c:a", "copy", "-hls_time", "2", "-hls_list_size", "0",
                    "-hls_segment_filename", str(source / "seg%d.ts"), str(source / "index.m3u8")],
                   check=True, capture_output=True, timeout=30, creationflags=renderer._NO_WINDOW)
    missing = {"enabled": True, "requests": 0}

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/seg1.ts" and missing["enabled"]:
                missing["requests"] += 1
                self.send_error(404)
            else:
                super().do_GET()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(source)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/index.m3u8"
        engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": False},
                                                               "proxy_mode": "direct"}}))
        monkeypatch.setattr(engine, "_head_direct_file", lambda *_: {
            "id": "fragment-check", "title": "fragment-check", "duration": 8, "formats": [
                {"format_id": "hls", "url": url, "ext": "mp4", "height": 48, "width": 64,
                 "vcodec": "h264", "acodec": "aac", "protocol": "m3u8_native"}]})
        output = tmp_path / "output"
        with pytest.raises(AppError):
            engine.download(url, "hls", output)
        assert missing["requests"] >= 3
        assert not list(output.glob("*.mp4"))
        assert any(p.name.endswith(".part") for p in output.iterdir())
        missing["enabled"] = False
        files = engine.download(url, "hls", output)
        with av.open(str(files[0])) as container:
            video = container.streams.video[0]
            assert float(video.duration * video.time_base) == pytest.approx(8, abs=.25)
            assert "mp4" in container.format.name
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
