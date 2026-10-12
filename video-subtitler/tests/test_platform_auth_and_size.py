"""Optional login and selected-stream size regressions; entirely offline."""
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.engines import ytdlp_engine


def fake_engine(monkeypatch, tmp_path, formats, cookie_error=False, remote_error=""):
    opened, extracted = [], []

    class DownloadError(Exception):
        pass

    class YoutubeDL:
        def __init__(self, options):
            self.options = dict(options)
            opened.append(self.options)

        @property
        def cookiejar(self):
            if cookie_error and (self.options.get("cookiesfrombrowser") or self.options.get("cookiefile")):
                raise DownloadError("Failed to decrypt browser cookies with DPAPI")
            return SimpleNamespace(get_cookies_for_url=lambda _: [])

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def extract_info(self, url, download):
            extracted.append((download, bool(self.options.get("cookiesfrombrowser") or self.options.get("cookiefile"))))
            if remote_error:
                raise DownloadError(remote_error)
            info = {"id": "offline", "title": "offline", "duration": 60, "formats": list(formats)}
            if download:
                media = tmp_path / "offline.mp4"
                media.write_bytes(b"offline-media")
                info["requested_downloads"] = [{"filepath": str(media)}]
            return info

    monkeypatch.setattr(ytdlp_engine, "_import_ytdlp", lambda: SimpleNamespace(
        YoutubeDL=YoutubeDL, utils=SimpleNamespace(DownloadError=DownloadError)))
    monkeypatch.setattr(ytdlp_engine.YtDlpEngine, "_head_direct_file", lambda *_: None)
    monkeypatch.setattr(ytdlp_engine.YtDlpEngine, "_find_aria2c", lambda *_: None)
    monkeypatch.setattr(ytdlp_engine, "find_ffmpeg", lambda _: "ffmpeg")
    return opened, extracted


def video(**kwargs):
    return {"format_id": "video", "height": 1080, "fps": 30, "vcodec": "avc1",
            "acodec": "none", "ext": "mp4", "protocol": "https", "url": "https://offline.test/video", **kwargs}


def audio(format_id="audio", **kwargs):
    return {"format_id": format_id, "vcodec": "none", "acodec": "aac", "ext": "m4a",
            "protocol": "https", "url": "https://offline.test/audio", **kwargs}


def test_youtube_auth_error_never_advises_bilibili(monkeypatch, tmp_path):
    fake_engine(monkeypatch, tmp_path, [], remote_error="Sign in to confirm you are not a bot. Use --cookies")
    with pytest.raises(AppError) as error:
        ytdlp_engine.YtDlpEngine(Config({})).probe("https://www.youtube.com/watch?v=offline")
    assert error.value.code == Codes.AUTH_NEED_COOKIES
    assert "B 站" not in error.value.suggestion and "扫码" not in error.value.suggestion


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=offline", "https://www.bilibili.com/video/offline"])
def test_missing_cookie_file_does_not_block_public_video(monkeypatch, tmp_path, url):
    opened, extracted = fake_engine(monkeypatch, tmp_path, [video(filesize=1000), audio(filesize=100)])
    result = ytdlp_engine.YtDlpEngine(Config({"cookies": str(tmp_path / "missing.txt")})).probe(url)
    assert result.formats and extracted == [(False, False)]
    assert all("cookiefile" not in item for item in opened)


@pytest.mark.parametrize("download", [False, True])
def test_cookie_load_failure_uses_anonymous_before_extract(monkeypatch, tmp_path, download):
    from app.services import renderer
    from app.services.downloader import native_ranges

    opened, extracted = fake_engine(monkeypatch, tmp_path, [video(filesize=1000), audio(filesize=100)], cookie_error=True)
    transfers = []

    def download_ranges(ydl, target, dest_dir, on_progress=None, **kwargs):
        authenticated = bool(ydl.options.get("cookiesfrombrowser") or ydl.options.get("cookiefile"))
        assert not authenticated, "cookie loading must fall back before any media transfer"
        transfers.append((target.kind, authenticated))
        media = dest_dir / target.filename
        media.write_bytes(b"offline-media")
        return media

    def mux(config, video, audio, merged, **kwargs):
        merged.write_bytes(video.read_bytes())
        return merged

    monkeypatch.setattr(native_ranges, "download_ranges", download_ranges)
    monkeypatch.setattr(renderer, "mux_av", mux)
    engine = ytdlp_engine.YtDlpEngine(Config({"cookies_from_browser": "edge",
        "download": {"aria2c": {"enabled": False}}}))
    url = "https://www.bilibili.com/video/offline"
    result = engine.download(url, "video", tmp_path) if download else engine.probe(url)
    assert result
    assert any(item.get("cookiesfrombrowser") for item in opened)
    # Bili ranges resolve metadata first, then open a separate anonymous transfer context.
    assert extracted == [(False, False)]
    assert transfers == ([("video", False), ("audio", False)] if download else [])


def test_anonymous_access_still_respects_remote_login_requirement(monkeypatch, tmp_path):
    opened, extracted = fake_engine(monkeypatch, tmp_path, [], cookie_error=True, remote_error="Login required")
    with pytest.raises(AppError) as error:
        ytdlp_engine.YtDlpEngine(Config({"cookies_from_browser": "edge"})).probe("https://www.bilibili.com/video/offline")
    assert error.value.code == Codes.AUTH_NEED_COOKIES
    assert len(opened) == 2 and extracted == [(False, False)]


@pytest.mark.parametrize("video_fields,audio_fields,expected,estimated", [
    ({"filesize": 1000}, {"filesize": 100}, 1100, False),
    ({"filesize_approx": 1000}, {"filesize": 100}, 1100, True),
    ({"filesize": 1000}, {"filesize_approx": 100}, 1100, True),
    ({}, {"filesize": 100}, 0, False),
    ({"filesize": 1000}, {}, 0, False),
    ({"filesize": 1000, "filesize_approx": 9999}, {"filesize": 100}, 1100, False),
])
def test_size_requires_every_selected_stream_and_marks_estimates(monkeypatch, tmp_path, video_fields, audio_fields, expected, estimated):
    fake_engine(monkeypatch, tmp_path, [audio(**audio_fields), video(**video_fields)])
    result = ytdlp_engine.YtDlpEngine(Config({})).probe("https://www.youtube.com/watch?v=offline")
    assert result.formats[0].filesize == expected
    assert result.formats[0].size_is_estimate is estimated


def test_probe_and_split_follow_native_best_audio_order(monkeypatch, tmp_path):
    # yt-dlp returns worst to best; language/codec preference can outweigh bitrate or filesize.
    formats = [audio("large", filesize=900, abr=256), audio("preferred", filesize=200, abr=128, acodec="opus"),
               video(filesize=1000)]
    fake_engine(monkeypatch, tmp_path, formats)
    engine = ytdlp_engine.YtDlpEngine(Config({}))
    url = "https://www.youtube.com/watch?v=offline"
    assert engine.probe(url).formats[0].filesize == 1200
    assert engine._resolve_split_streams(url, "video")[1].filename.endswith(".fpreferred.m4a")


@pytest.mark.parametrize("protocol", ["m3u8_native", "http_dash_segments"])
def test_best_audio_with_fragments_keeps_native_selection(monkeypatch, tmp_path, protocol):
    fake_engine(monkeypatch, tmp_path, [audio("http", filesize=900),
        audio("preferred", filesize=200, protocol=protocol), video(filesize=1000)])
    engine = ytdlp_engine.YtDlpEngine(Config({}))
    assert engine._resolve_split_streams("https://www.youtube.com/watch?v=offline", "video") is None


def test_http_dash_segments_are_not_a_progressive_direct_file(monkeypatch, tmp_path):
    fake_engine(monkeypatch, tmp_path, [video(filesize=1000, acodec="aac", protocol="http_dash_segments")])
    engine = ytdlp_engine.YtDlpEngine(Config({}))
    assert engine._resolve_direct_stream("https://www.youtube.com/watch?v=offline", "video") is None


def test_bilibili_anonymous_notice_does_not_imply_required_login(monkeypatch, tmp_path):
    fake_engine(monkeypatch, tmp_path, [video(filesize=1000), audio(filesize=100)])
    result = ytdlp_engine.YtDlpEngine(Config({})).probe("https://www.bilibili.com/video/offline")
    assert result.auth_status == "none" and result.formats
    assert "可直接下载" in result.quality_notice


def test_real_ytdlp_bad_cookie_file_uses_anonymous_without_touching_source(monkeypatch, tmp_path):
    import yt_dlp

    cookies = tmp_path / "invalid-cookie-file.txt"
    cookies.write_bytes(b"invalid cookies\n")
    original = cookies.read_bytes()
    extracted = []

    def extract_info(ydl, url, download):
        extracted.append((download, ydl.params.get("cookiefile")))
        return {"id": "offline", "title": "offline", "formats":
                [audio(filesize=100), video(filesize=1000)]}

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    monkeypatch.setattr(ytdlp_engine.YtDlpEngine, "_head_direct_file", lambda *_: None)
    result = ytdlp_engine.YtDlpEngine(Config({"cookies": str(cookies)})).probe(
        "https://www.bilibili.com/video/offline")
    assert result.formats[0].filesize == 1100 and result.auth_status == "unknown"
    assert extracted == [(False, None)]
    assert cookies.read_bytes() == original


def test_direct_head_keeps_public_video_with_unreadable_cookie(monkeypatch, tmp_path):
    import yt_dlp

    cookies = tmp_path / "invalid-cookie-file.txt"
    cookies.write_bytes(b"invalid cookies\n")
    requests = []

    class Response:
        headers = {"Content-Type": "video/mp4", "Content-Length": "1100"}
        url = "https://offline.test/video.mp4"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def urlopen(ydl, request):
        ydl.cookiejar  # The real request director initializes the cookiejar before transport.
        requests.append(ydl.params.get("cookiefile"))
        return Response()

    monkeypatch.setattr(yt_dlp.YoutubeDL, "urlopen", urlopen)
    result = ytdlp_engine.YtDlpEngine(Config({"cookies": str(cookies)}))._head_direct_file(
        Response.url, "best")
    assert result and result["formats"][0]["filesize"] == 1100
    assert requests == [None] and cookies.read_bytes() == b"invalid cookies\n"


def test_audio_helper_agrees_with_real_ytdlp_best_audio_selector():
    import yt_dlp

    formats = [audio("large", filesize=900, abr=256),
               audio("preferred", filesize=200, abr=128, acodec="opus", ext="webm"),
               video(filesize=1000)]
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "check_formats": False}) as ydl:
        ydl.sort_formats({"formats": formats})
        selected = list(ydl.build_format_selector("ba")({
            "formats": formats, "has_merged_format": False, "incomplete_formats": False}))
    assert len(selected) == 1
    assert ytdlp_engine._best_audio(formats)["format_id"] == selected[0]["format_id"]
    assert selected[0]["format_id"] == "preferred"


@pytest.mark.parametrize("size,estimated", [(1100, False), (1100, True), (0, False)])
def test_probe_api_preserves_size_certainty(monkeypatch, size, estimated):
    from fastapi.testclient import TestClient
    from app.api import server
    from app.services.engines.base import FormatInfo, ProbeResult

    result = ProbeResult(video_id="offline", title="offline", formats=[FormatInfo(
        format_id="video", label="1080p", filesize=size, size_is_estimate=estimated)])
    with TestClient(server.create_app()) as client:
        monkeypatch.setattr(server._router, "probe", lambda *_, **__: (SimpleNamespace(name="test"), result))
        response = client.post("/api/v1/probe", json={"url": "https://offline.test/video"},
                               headers={"Host": "127.0.0.1:8766"})
    assert response.status_code == 200
    item = response.json()["formats"][0]
    assert item["filesize"] == size and item["size_is_estimate"] is estimated
