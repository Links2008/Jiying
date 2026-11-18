"""解析与加速下载的画质决策应一致。"""
import pytest

from app.core.config import Config
from app.services.engines.ytdlp_engine import YtDlpEngine
from test_platform_parse_regressions import engine_with_info


def video(identifier, width, height, fps, *, audio="aac", protocol="https", **extra):
    return dict(format_id=identifier, url=f"https://cdn.example.test/{identifier}",
                width=width, height=height, fps=fps, protocol=protocol,
                vcodec="h264", acodec=audio, ext="mp4", **extra)


def test_preview_selects_resolution_first_then_frame_rate_in_portrait(monkeypatch):
    entries = [
        video("small-tall", 720, 2304, 120),
        video("4k30", 2160, 3840, 30),
        video("4k60", 2160, 3840, 60),
        video("1080p60", 1080, 1920, 60),
    ]
    engine = engine_with_info(monkeypatch, entries)
    result = engine.probe("https://example.test/video")
    assert [item.format_id for item in result.formats] == ["4k60", "4k30", "1080p60", "small-tall"]


@pytest.mark.parametrize("url", ["https://example.test/video", "https://www.bilibili.com/video/BVtop"])
def test_http_acceleration_leaves_a_better_hls_format_for_native_download(monkeypatch, url):
    entries = [
        video("4k-hls", 3840, 2160, 60, protocol="m3u8_native"),
        video("1080-split", 1920, 1080, 60, audio="none"),
        video("1080-direct", 1920, 1080, 30, filesize=8000000),
        dict(format_id="audio", url="https://cdn.example.test/audio", vcodec="none",
             acodec="aac", protocol="https", ext="m4a"),
    ]
    engine = YtDlpEngine(Config({}))
    monkeypatch.setattr(engine, "_source_info", lambda _: {"formats": entries, "title": "test"})
    assert engine._resolve_split_streams(url, "best") is None
    assert engine._resolve_direct_stream(url, "best") is None
    explicit = engine._resolve_split_streams(url, "1080-split")
    assert explicit and explicit[0].url.endswith("/1080-split")


def test_combined_stream_selection_prefers_spatial_quality_over_size(monkeypatch):
    entries = [
        video("4k", 3840, 2160, 30, filesize=300000),
        video("lower", 1920, 1080, 120, filesize=900000),
    ]
    engine = YtDlpEngine(Config({}))
    monkeypatch.setattr(engine, "_source_info", lambda _: {"formats": entries})
    assert engine._resolve_direct_stream("https://example.test/v", "best").url.endswith("/4k")


def test_native_selector_keeps_60_fps_when_joining_the_audio():
    four_k = video("4k30", 3840, 2160, 30, audio="none", format="4k30")
    sixty = video("4k60", 3840, 2160, 60, audio="none", protocol="m3u8_native", format="4k60")
    lower = video("1080p", 1920, 1080, 120, format="1080p")
    audio = dict(format_id="audio", url="https://cdn.example.test/audio", vcodec="none",
                 acodec="aac", protocol="https", ext="m4a", format="audio")
    selection = YtDlpEngine._format_selector("best")({
        "formats": [four_k, sixty, lower, audio], "has_merged_format": True, "incomplete_formats": False})
    assert [item["format_id"] for item in next(selection)["requested_formats"]] == ["4k60", "audio"]


def test_usable_combined_format_survives_when_higher_video_has_no_separate_audio():
    silent = video("silent", 3840, 2160, 60, audio="none")
    combined = video("complete", 1920, 1080, 60)
    assert list(YtDlpEngine._format_selector("best")({"formats": [silent, combined]})) == [combined]


@pytest.mark.parametrize("url", [
    "https://www.bilibili.com/video/BV1rp4y1e745?p=1",
    "https://m.bilibili.com/video/BV1oq4y1S7T1?p=2",
    "https://b23.tv/example",
])
def test_bilibili_ultrawide_4k_stays_selected_after_media_refresh(monkeypatch, url):
    frames = [
        video("cdn-id-1", 3840, 1920, 60, audio="none", quality=120),
        video("smaller", 2880, 2160, 30, audio="none", quality=112),
        dict(format_id="audio", url="https://cdn.example.test/audio", vcodec="none",
             acodec="aac", protocol="https", ext="m4a"),
    ]
    engine = engine_with_info(monkeypatch, frames)
    result = engine.probe(url)
    choice = result.formats[0]
    assert choice.label == "4K · 3840×1920"
    assert "4K" in result.quality_notice

    updated = [{**frames[0], "format_id": "another-cdn-id", "url": "https://backup.example.test/4k"},
               frames[1], frames[2]]
    monkeypatch.setattr(engine, "_source_info", lambda _: {"formats": updated})
    targets = engine._resolve_split_streams(url, choice.format_id)
    assert targets and targets[0].url == "https://backup.example.test/4k"
    updated.pop(0)
    assert engine._resolve_split_streams(url, choice.format_id) is None
