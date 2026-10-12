"""Split streams must merge with bundled FFmpeg when it is absent from PATH."""
import functools
import os
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import av
import pytest

from app.core.config import Config
from app.services import renderer
from app.services.engines.ytdlp_engine import YtDlpEngine


@pytest.mark.parametrize("configured", [False, True])
def test_split_streams_merge_without_system_ffmpeg(tmp_path, monkeypatch, configured):
    ffmpeg = renderer.find_ffmpeg(Config({}))
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=64x48:r=10:d=1.5", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=44100:duration=1.5",
        "-map", "0:v", "-an", "-c:v", "libx264", str(source / "video.mp4"),
        "-map", "1:a", "-vn", "-c:a", "aac", str(source / "audio.m4a"),
    ], check=True, capture_output=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        QuietHandler, directory=str(source)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        empty_path = tmp_path / "empty-path"
        empty_path.mkdir()
        monkeypatch.setenv("PATH", str(empty_path))
        monkeypatch.setattr(renderer, "bin_dir", lambda: Path(ffmpeg).parent)
        config = {"paths": {"ffmpeg": ffmpeg}} if configured else {}
        config["download"] = {"aria2c": {"enabled": False}, "proxy_mode": "direct"}
        engine = YtDlpEngine(Config(config))
        base = f"http://127.0.0.1:{server.server_port}"
        info = {"id": "split", "title": "split merged", "formats": [
            {"format_id": "video", "url": base + "/video.mp4", "ext": "mp4",
             "vcodec": "h264", "acodec": "none", "height": 48,
             "width": 64, "fps": 10, "protocol": "http"},
            {"format_id": "audio", "url": base + "/audio.m4a", "ext": "m4a",
             "vcodec": "none", "acodec": "aac", "abr": 64, "protocol": "http"},
        ]}
        monkeypatch.setattr(engine, "_head_direct_file", lambda *args: info)
        files = engine.download("https://example.com/split", "best", tmp_path / "output space")
        assert len(files) == 1 and files[0].is_file()
        with av.open(str(files[0])) as container:
            assert len(container.streams.video) == 1
            assert len(container.streams.audio) == 1
            assert (container.streams.video[0].width, container.streams.video[0].height) == (64, 48)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
