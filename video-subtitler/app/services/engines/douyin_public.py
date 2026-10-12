"""Read publicly returned Douyin share-page data using the existing HTTP session."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs, urlparse

from yt_dlp.extractor.tiktok import DouyinIE
from yt_dlp.utils import ExtractorError

from .platform_links import douyin_video_id

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")


def public_video(page: str, video_id: str) -> dict | None:
    match = re.search(r"window\._ROUTER_DATA\s*=\s*", page or "")
    if not match:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(page[match.end():].lstrip())
        for loader in (data.get("loaderData") or {}).values():
            if not isinstance(loader, dict):
                continue
            for item in (loader.get("videoInfoRes") or {}).get("item_list", []):
                if isinstance(item, dict) and str(item.get("aweme_id")) == video_id and item.get("video"):
                    return item
    except (ValueError, TypeError, AttributeError):
        pass
    return None


class PublicDouyinIE(DouyinIE):
    IE_NAME = "jiying:douyin"
    _VALID_URL = r"https?://(?:(?:www|m|v)\.)?(?:douyin|iesdouyin)\.com/(?P<id>.*)"

    def _real_extract(self, url):
        video_id = douyin_video_id(url)
        headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/"}
        if not video_id:
            response = self._request_webpage(url, "share", "Resolving share link", headers=headers)
            video_id = douyin_video_id(response.url)
        if not video_id:
            raise ExtractorError("抖音分享链接未指向具体视频，请复制目标视频的分享链接", expected=True)
        if any(self._downloader.params.get(key) for key in ('cookiefile', 'cookiesfrombrowser', '_jiying_platform_session')):
            try:
                return DouyinIE(self._downloader)._real_extract('https://www.douyin.com/video/' + video_id)
            except ExtractorError:
                pass
        share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
        # The first public response may only set a visitor session. Reuse it once.
        for attempt in range(2):
            page = self._download_webpage(share_url, video_id, "Reading public share page",
                                          headers=headers, query={"from_ssr": "1"})
            item = public_video(page, video_id)
            if item:
                item = dict(item, video=dict(item["video"], bit_rate=item["video"].get("bit_rate") or []))
                info = self._parse_aweme_video_app(item)
                if info.get("formats"):
                    # Share data describes the upload; the playback URL may be a smaller rendition.
                    for fmt in info['formats']:
                        ratio = (parse_qs(urlparse(fmt['url']).query).get('ratio') or [''])[0]
                        match = re.fullmatch(r'(\d+)p', ratio)
                        width, height = fmt.get('width'), fmt.get('height')
                        if match and width and height and min(width, height) > int(match[1]):
                            scale = int(match[1]) / min(width, height)
                            fmt['width'], fmt['height'] = round(width * scale), round(height * scale)
                    info["webpage_url"] = "https://www.douyin.com/video/" + video_id
                    info["http_headers"] = headers | {"Referer": share_url}
                    return info
            if attempt == 0:
                time.sleep(2)
        raise ExtractorError("抖音分享页未返回可下载的视频，请确认作品仍可公开访问后重试", expected=True)
