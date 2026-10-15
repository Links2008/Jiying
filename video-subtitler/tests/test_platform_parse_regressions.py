"""Public platform regressions; no credentials, requests, or installed tasks."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.engines.ytdlp_engine import YtDlpEngine, _ytdlp_error


def engine_with_info(monkeypatch, formats):
    engine = YtDlpEngine(Config({}))
    @contextmanager
    def youtube_dl(_):
        yield SimpleNamespace(add_info_extractor=lambda *_: None, extract_info=lambda *_a, **_k: {
            "id": "sample", "title": "sample", "formats": formats})
    monkeypatch.setattr(engine, "_youtube_dl", youtube_dl)
    return engine


@pytest.mark.parametrize('url', [
    'https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9',
    'https://m.xiaohongshu.com/discovery/item/6411cf99000000001300b6d9',
    'https://xhslink.com/o/public-sample'])
def test_xhs_original_is_available_and_selected_first(monkeypatch, url):
    engine = engine_with_info(monkeypatch, [
        {"format_id": "720", "width": 1280, "height": 720, "vcodec": "h264", "acodec": "aac"},
        {"format_id": "1080", "width": 1920, "height": 1080, "vcodec": "h264", "acodec": "aac"},
        {"format_id": "direct", "url": "https://sns-video-bd.xhscdn.com/sample",
         "ext": "mp4", "quality": 1, "filesize": 9000000}])
    result = engine.probe(url)
    assert [f.format_id for f in result.formats] == ["direct", "1080", "720"]
    assert "原始" in result.formats[0].label
    assert result.formats[0].height == 0


def test_portrait_quality_uses_short_edge(monkeypatch):
    engine = engine_with_info(monkeypatch, [{"format_id": "0", "width": 720,
        "height": 1280, "vcodec": "h264", "acodec": "aac"}])
    result = engine.probe("https://example.test/video")
    assert result.formats[0].label == "720p · 720×1280"
    assert result.formats[0].height == 1280


def test_douyin_feed_is_rejected_before_network(monkeypatch):
    engine = engine_with_info(monkeypatch, [])
    with pytest.raises(AppError) as caught:
        engine.probe("https://www.douyin.com/?recommend=1")
    assert caught.value.code == Codes.ENG_UNSUPPORTED
    assert "推荐页" in caught.value.message and "分享" in caught.value.suggestion


def test_share_text_and_douyin_modal_id():
    from app.services.engines.platform_links import normalize_media_url
    assert normalize_media_url("复制打开抖音 https://v.douyin.com/abc/ 看视频！") == "https://v.douyin.com/abc/"
    assert normalize_media_url("https://www.douyin.com/?recommend=1&modal_id=6961737553342991651") == "https://www.douyin.com/video/6961737553342991651"
    url = "https://www.xiaohongshu.com/explore/abc?xsec_token=A%2BB%3D&xsec_source=pc"
    assert normalize_media_url("分享 " + url + " 。") == url
    assert normalize_media_url("https://douyin.com.example.test/?modal_id=123") == "https://douyin.com.example.test/?modal_id=123"


def test_empty_douyin_details_do_not_claim_login_expired():
    error = _ytdlp_error("Fresh cookies (not necessarily logged in) are needed", "解析",
                         "https://www.douyin.com/video/6961737553342991651")
    assert error.code == Codes.ENG_FAILED
    assert "登录态未能读取" not in error.message


def test_douyin_public_page_retries_shell_and_preserves_play_url(monkeypatch):
    import yt_dlp
    from app.services.engines.douyin_public import PublicDouyinIE
    video_id = "6961737553342991651"
    play_url = "https://www.iesdouyin.com/aweme/v1/playwm/?video_id=sample&ratio=720p"
    item = {"aweme_id": video_id, "desc": "public sample", "video": {
        "width": 1080, "height": 1920, "duration": 19782, "bit_rate": None,
        "play_addr": {"url_list": [play_url]}}}
    pages = iter(["window._ROUTER_DATA = {};</script>", "window._ROUTER_DATA = " +
        json.dumps({"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [item]}}}}) + ";</script>"])
    with yt_dlp.YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        extractor = PublicDouyinIE(ydl)
        monkeypatch.setattr(extractor, "_download_webpage", lambda *_a, **_k: next(pages))
        info = extractor._real_extract("https://www.douyin.com/video/" + video_id)
    assert info["id"] == video_id
    assert info["formats"][0]["url"] == play_url
    assert info["formats"][0]["height"] == 1280
    assert info["formats"][0]["width"] == 720
    assert info["duration"] == pytest.approx(19.782)


def test_douyin_official_ssr_page_preserves_formats_and_precise_duration(monkeypatch):
    import yt_dlp
    from app.services.engines.douyin_public import PublicDouyinIE
    video_id = "7672218225355062409"
    result = {"gid": video_id, "title": "公开视频", "video_model": json.dumps({
        "video_duration": 19.534, "video_list": [
            {"main_url": "https://media.example.test/high.mp4", "backup_url": "https://backup.example.test/high.mp4",
             "quality_type": 2, "video_meta": {"vwidth": 1080, "vheight": 1920, "codec_type": "h265", "size": 553485, "fps": 30}}]})}
    page = "window._SSR_DATA = " + json.dumps({"data": {"storeState": {"detail": {"videoData": {"result": result}}}}})
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        extractor = PublicDouyinIE(ydl)
        monkeypatch.setattr(extractor, "_download_webpage", lambda *_a, **_k: page)
        info = extractor._real_extract("https://www.douyin.com/video/" + video_id)
    assert info["duration"] == pytest.approx(19.534)
    assert info["formats"][0]["height"] == 1920
    assert info["formats"][0]["width"] == 1080
    assert {f["url"] for f in info["formats"]} == {"https://media.example.test/high.mp4", "https://backup.example.test/high.mp4"}


def test_douyin_keeps_authenticated_upstream_formats(monkeypatch, tmp_path):
    import yt_dlp
    from yt_dlp.extractor.tiktok import DouyinIE
    from app.services.engines.douyin_public import PublicDouyinIE
    expected = {'id': '123', 'formats': [{'format_id': 'original', 'height': 2160}]}
    monkeypatch.setattr(DouyinIE, '_real_extract', lambda self, url: expected)
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ydl.params['cookiefile'] = str(tmp_path / 'existing-cookie-setting')
        extractor = PublicDouyinIE(ydl)
        monkeypatch.setattr(extractor, '_download_webpage', lambda *a, **k: pytest.fail('Public fallback replaced existing formats'))
        assert extractor._real_extract('https://www.douyin.com/video/123') == expected
