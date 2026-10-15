"""公开分享链接的兼容性回归，使用模拟响应，不读取用户会话。"""
from types import SimpleNamespace

import pytest
import yt_dlp
from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE

from app.services.engines.platform_links import normalize_media_url
from app.services.engines.xiaohongshu_public import ConnectedXiaoHongShuIE, note_url


def test_xhs_short_link_reaches_redirect_resolution(monkeypatch):
    target = "https://www.xiaohongshu.com/discovery/item/674051740000000007027a15?xsec_token=sample%2Btoken"
    resolved = []

    def request(_self, url, *_args, **_kwargs):
        resolved.append(url)
        return SimpleNamespace(url=target)

    monkeypatch.setattr(ConnectedXiaoHongShuIE, "_request_webpage", request)
    monkeypatch.setattr(XiaoHongShuIE, "_real_extract", lambda _self, url: {
        "id": "674051740000000007027a15", "title": "公开测试视频", "formats": [
            {"format_id": "720", "url": "https://media.example.test/video.mp4", "ext": "mp4",
             "vcodec": "h264", "acodec": "aac", "height": 720}]})
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        ydl.add_info_extractor(ConnectedXiaoHongShuIE())
        result = ydl.extract_info("https://xhslink.com/o/public-video", download=False,
                                  ie_key=ConnectedXiaoHongShuIE.ie_key())
    assert resolved == ["https://xhslink.com/o/public-video"]
    assert result["webpage_url"] == target


@pytest.mark.parametrize("url", [
    "https://jingxuan.douyin.com/m/video/7672218225355062409",
    "https://open.douyin.com/player/video?vid=7672218225355062409"])
def test_douyin_official_link_variants_use_same_parser(url):
    assert normalize_media_url(url) == "https://www.douyin.com/video/7672218225355062409"


def test_xhs_profile_note_preserves_complete_query():
    url = "https://www.xiaohongshu.com/user/profile/63439913000000001901f49a/674051740000000007027a15?xsec_token=A%2BB%3D&xsec_source=pc_user"
    assert note_url(url) == "https://www.xiaohongshu.com/discovery/item/674051740000000007027a15?xsec_token=A%2BB%3D&xsec_source=pc_user"


def test_known_http_xhs_share_link_is_upgraded_to_https():
    assert normalize_media_url("分享 http://xhslink.com/o/public-video 。") == "https://xhslink.com/o/public-video"


def test_platform_like_external_hosts_remain_external():
    assert normalize_media_url("https://jingxuan.douyin.com.example.test/m/video/123") == "https://jingxuan.douyin.com.example.test/m/video/123"
    assert note_url("https://xiaohongshu.com.example.test/explore/674051740000000007027a15") is None


def test_xhs_restricted_redirect_keeps_original_note_and_token():
    from urllib.parse import quote
    target = "https://www.xiaohongshu.com/discovery/item/674051740000000007027a15?xsec_token=A%2BB%3D&xsec_source=app_share"
    url = "https://www.xiaohongshu.com/404?source=" + quote("/404/security?redirectPath=" + target, safe="") + "&error_code=300031"
    assert note_url(url) == target
    assert note_url("https://www.xiaohongshu.com/404?redirectPath=https://evil.example/discovery/item/674051740000000007027a15") is None
