from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.pipeline.runner import PipelineRunner
from app.services import renderer
from app.services.engines import ytdlp_engine
from app.services.translator import LLMTranslator


def test_long_translation_reports_completed_batches(monkeypatch):
    translator = LLMTranslator(Config({"translator": {"llm": {"batch_size": 2}}}))
    monkeypatch.setattr(translator, "_translate_chunk",
                        lambda chunk, _src, _dst, offset: [s.upper() for s in chunk])
    seen = []
    assert translator.translate(["a", "b", "c", "d", "e"], "en", "zh",
                                on_progress=seen.append) == ["A", "B", "C", "D", "E"]
    assert seen == [0.4, 0.8, 1.0]


def test_translation_stage_reports_real_progress(tmp_path, monkeypatch):
    class Bus:
        events = []

        def publish(self, event):
            self.events.append(event)

    bus = Bus()
    runner = PipelineRunner(Config({}), Store(tmp_path / "tasks.db"), bus=bus)
    runner._data = tmp_path
    task = runner.create_task("https://example.test/clip", "best", ["zh"],
                              "burn", title="clip")

    class Translator:
        def translate(self, texts, src, dst, on_progress=None):
            on_progress(0.5)
            on_progress(1.0)
            return ["译文" for _ in texts]

    monkeypatch.setattr("app.pipeline.runner.build_translator",
                        lambda *_: Translator())
    transcript = SimpleNamespace(language="en", segments=[
        SimpleNamespace(start=0, end=1, text="hello")])
    result = runner._stage_translate(task, transcript, ["zh"])
    assert result["zh"].exists()
    assert runner.live_progress(task["id"])["fraction"] == 1.0
    assert [e.fraction for e in bus.events if e.type == "progress"] == [0.5, 1.0]


def test_cancelled_hardware_burn_never_starts_cpu_fallback(tmp_path, monkeypatch):
    calls = []
    (tmp_path / "sub.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n")
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "ffmpeg")
    monkeypatch.setattr(renderer, "audio_args", lambda *_: ["-c:a", "copy"])
    monkeypatch.setattr(renderer, "pick_encoder", lambda _: "h264_nvenc")

    def cancelled(args, **_kwargs):
        calls.append(args)
        raise AppError(Codes.TASK_CANCELLED, "cancelled")

    monkeypatch.setattr(renderer, "_run", cancelled)
    with pytest.raises(AppError) as error:
        renderer.burn(Config({}), tmp_path / "video.mp4", tmp_path / "sub.srt",
                      tmp_path / "out.mp4", font_name="Arial", task_id="burn")
    assert error.value.code == Codes.TASK_CANCELLED
    assert len(calls) == 1


@pytest.mark.parametrize("format_id", ["123", "hls-1605"])
def test_selected_format_downloads_one_playlist_entry(tmp_path, monkeypatch, format_id):
    # 本测试只验证选定画质与播放列表范围，真实媒体另有完整性回归。
    monkeypatch.setattr(renderer, "validate_download", lambda _cfg, path, *_: path)
    opts = []
    media = tmp_path / "first.mp4"
    media.write_bytes(b"video")

    class FakeYoutubeDL:
        def __init__(self, options):
            opts.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def extract_info(self, _url, download):
            assert download
            return {"entries": [{"requested_downloads": [{"filepath": str(media)}]}]}

    monkeypatch.setattr(ytdlp_engine, "_import_ytdlp",
                        lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL,
                                                utils=SimpleNamespace(DownloadError=Exception)))
    engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": False}}}))
    monkeypatch.setattr(engine, "_find_aria2c", lambda: None)
    assert engine.download("https://example.test/playlist", format_id, tmp_path) == [media]
    assert opts[0]["playlist_items"] == "1"
    assert opts[0]["format"] == f"{format_id}[acodec=none]+ba/{format_id}"
    assert not opts[0].get("writesubtitles")


def test_selected_hls_id_never_substitutes_progressive_or_dash(tmp_path, monkeypatch):
    info = {"title": "clip", "formats": [
        {"format_id": "hls-1605", "url": "https://x/hls", "protocol": "m3u8_native",
         "height": 720, "vcodec": "avc1", "acodec": "aac"},
        {"format_id": "progressive-1080", "url": "https://x/mp4", "protocol": "https",
         "height": 1080, "vcodec": "avc1", "acodec": "aac"},
        {"format_id": "dash-1080", "url": "https://x/dash", "protocol": "https",
         "height": 1080, "vcodec": "avc1", "acodec": "none"},
        {"format_id": "audio", "url": "https://x/audio", "protocol": "https",
         "vcodec": "none", "acodec": "aac"},
    ]}

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def extract_info(self, _url, download):
            assert not download
            return info

    monkeypatch.setattr(ytdlp_engine, "_import_ytdlp",
                        lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    engine = ytdlp_engine.YtDlpEngine(Config({}))
    assert engine._resolve_direct_stream("https://x/page", "hls-1605") is None
    assert engine._resolve_split_streams("https://x/page", "hls-1605") is None


def test_direct_top_level_stream_must_match_selected_format(monkeypatch):
    info = {"title": "clip", "format_id": "low", "url": "https://x/low.mp4",
            "protocol": "https", "ext": "mp4", "formats": [
                {"format_id": "high", "url": "https://x/high.m3u8",
                 "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "aac"}]}

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def extract_info(self, _url, download):
            return info

    monkeypatch.setattr(ytdlp_engine, "_import_ytdlp",
                        lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    engine = ytdlp_engine.YtDlpEngine(Config({}))
    assert engine._resolve_direct_stream("https://x/page", "high") is None


def test_probe_does_not_add_audio_size_to_already_merged_format(monkeypatch):
    info = {"id": "clip", "title": "clip", "formats": [
        {"format_id": "merged", "height": 720, "filesize": 100,
         "vcodec": "avc1", "acodec": "aac", "ext": "mp4"},
        {"format_id": "video", "height": 1080, "filesize": 200,
         "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
        {"format_id": "audio", "filesize": 30,
         "vcodec": "none", "acodec": "aac", "ext": "m4a"},
    ]}

    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def extract_info(self, _url, download):
            return info

    monkeypatch.setattr(ytdlp_engine, "_import_ytdlp",
                        lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    sizes = {fmt.format_id: fmt.filesize for fmt in
             ytdlp_engine.YtDlpEngine(Config({})).probe("https://x/page").formats}
    assert sizes == {"video": 230, "merged": 100}


@pytest.mark.parametrize("aria2_enabled", [False, True])
def test_plain_mp4_uses_head_then_one_media_get(tmp_path, monkeypatch, aria2_enabled):
    # HTTP 请求次数测试的载荷是模拟字节；媒体有效性另由真实样本验证。
    monkeypatch.setattr(renderer, "validate_download", lambda _cfg, path, *_: path)
    class MediaHandler(BaseHTTPRequestHandler):
        methods = []
        body = b"video-bytes"

        def respond(self, with_body):
            self.methods.append(self.command)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            if with_body:
                self.wfile.write(self.body)

        def do_HEAD(self):
            self.respond(False)

        def do_GET(self):
            self.respond(True)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        engine = ytdlp_engine.YtDlpEngine(Config({"download": {
            "proxy_mode": "direct", "aria2c": {"enabled": aria2_enabled}}}))
        monkeypatch.setattr(engine, "_find_aria2c", lambda: None)
        url = f"http://127.0.0.1:{server.server_port}/clip.mp4"
        assert engine.probe(url).formats[0].format_id == "mp4"
        assert MediaHandler.methods == ["HEAD"]
        if aria2_enabled:
            class FakeAria2c:
                def __init__(self, _config):
                    pass

                def ping(self):
                    return True

                def download(self, target, dest, _progress, cancel_check=None):
                    from urllib.request import ProxyHandler, build_opener
                    dest.mkdir(parents=True, exist_ok=True)
                    path = dest / target.filename
                    with build_opener(ProxyHandler({})).open(target.url) as response:
                        path.write_bytes(response.read())
                    return path

            monkeypatch.setattr(ytdlp_engine, "Aria2cBackend", FakeAria2c)
        files = engine.download(url, "mp4", tmp_path)
        assert len(files) == 1 and files[0].read_bytes() == MediaHandler.body
        assert MediaHandler.methods == ["HEAD", "HEAD", "GET"]
    finally:
        server.shutdown()
        server.server_close()
