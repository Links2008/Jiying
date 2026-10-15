"""Bilibili quality and authentication regressions; no account or network required."""
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.engines import ytdlp_engine


def fake_ytdlp(monkeypatch, formats=(), session=False, error=""):
    class DownloadError(Exception):
        pass

    class YoutubeDL:
        def __init__(self, options):
            self.cookiejar = SimpleNamespace(get_cookies_for_url=lambda _: (
                [SimpleNamespace(name="SESSDATA", value="test-session")] if session else []))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def extract_info(self, url, download):
            if error:
                raise DownloadError(error)
            return {"id": "sample", "title": "sample", "formats": list(formats)}

    monkeypatch.setattr(ytdlp_engine, "_import_ytdlp", lambda: SimpleNamespace(
        YoutubeDL=YoutubeDL, utils=SimpleNamespace(DownloadError=DownloadError)))


def video(format_id, fps=30, dynamic_range="SDR", filesize=1000, quality=120):
    return {"format_id": format_id, "height": 2160, "fps": fps,
            "dynamic_range": dynamic_range, "filesize": filesize, "quality": quality,
            "vcodec": "avc1", "acodec": "none", "ext": "mp4"}


def test_4k_keeps_distinct_frame_rate_and_dynamic_range(monkeypatch):
    fake_ytdlp(monkeypatch, [video("sdr30"), video("sdr60", fps=60, filesize=500),
                            video("hdr30", dynamic_range="HDR10", filesize=400)])
    result = ytdlp_engine.YtDlpEngine(Config({})).probe("https://www.bilibili.com/video/sample")
    assert {f.format_id for f in result.formats} == {"sdr30", "sdr60", "hdr30"}
    assert all(f.label == "4K / 2160p" for f in result.formats)
    assert {f.dynamic_range for f in result.formats} == {"SDR", "HDR10"}


def test_same_variant_prefers_extractor_quality_over_file_size(monkeypatch):
    fake_ytdlp(monkeypatch, [video("large", filesize=2000, quality=112),
                            video("high-quality", filesize=1000, quality=120)])
    result = ytdlp_engine.YtDlpEngine(Config({})).probe("https://www.bilibili.com/video/sample")
    assert result.formats[0].format_id == "high-quality"


@pytest.mark.parametrize("configured,session,expected", [
    (False, False, "none"), (True, False, "unknown"), (True, True, "loaded")])
def test_auth_status_requires_bilibili_session_cookie(monkeypatch, configured, session, expected):
    fake_ytdlp(monkeypatch, [dict(video("1080"), height=1080)], session=session)
    cfg = Config({"cookies_from_browser": "edge" if configured else ""})
    result = ytdlp_engine.YtDlpEngine(cfg).probe("https://www.bilibili.com/video/sample")
    assert result.auth_status == expected
    assert "4K" in result.quality_notice
    assert "test-session" not in result.quality_notice


@pytest.mark.parametrize("message", ["Could not copy Chrome cookie database",
                                     "Failed to decrypt with DPAPI", "Login required"])
def test_auth_failure_gives_in_app_recovery(monkeypatch, message):
    fake_ytdlp(monkeypatch, error=message)
    with pytest.raises(AppError) as caught:
        ytdlp_engine.YtDlpEngine(Config({})).probe("https://www.bilibili.com/video/sample")
    assert caught.value.code == Codes.AUTH_NEED_COOKIES
    assert "登录态" in caught.value.message
    assert "刷新画质" in caught.value.suggestion
    assert "连接 B 站" in caught.value.suggestion and "扫码登录" in caught.value.suggestion
    assert "自动刷新画质" in caught.value.suggestion
    assert "config.yaml" not in caught.value.suggestion
    assert "pip" not in caught.value.suggestion


def test_missing_cookie_file_keeps_public_formats_available(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, [video("4k")])
    result = ytdlp_engine.YtDlpEngine(Config({"cookies": str(tmp_path / "missing.txt")})).probe(
        "https://www.bilibili.com/video/sample")
    assert result.formats[0].format_id == "4k"
    assert result.auth_status == "unknown" and "可直接下载" in result.quality_notice


@pytest.mark.parametrize("browser", ["", "edge"])
def test_missing_4k_login_notice_prefers_scan_and_automatic_refresh(monkeypatch, browser):
    fake_ytdlp(monkeypatch, [dict(video("1080"), height=1080)])
    result = ytdlp_engine.YtDlpEngine(Config({"cookies_from_browser": browser})).probe(
        "https://www.bilibili.com/video/sample")
    assert "连接 B 站" in result.quality_notice
    assert "扫码登录" in result.quality_notice and "自动刷新画质" in result.quality_notice
    assert result.quality_notice.index("扫码登录") < result.quality_notice.index("浏览器")


def test_expired_selected_format_requests_quality_refresh(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, error="Requested format is not available")
    monkeypatch.setattr(ytdlp_engine, "find_ffmpeg", lambda _: "ffmpeg")
    engine = ytdlp_engine.YtDlpEngine(Config({}))
    monkeypatch.setattr(engine, "_find_aria2c", lambda: None)
    with pytest.raises(AppError) as caught:
        engine.download("https://www.bilibili.com/video/sample", "120-7", tmp_path)
    assert "所选画质" in caught.value.message
    assert "刷新画质" in caught.value.suggestion
