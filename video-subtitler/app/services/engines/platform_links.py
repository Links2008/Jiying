"""Normalize copied share links without choosing an item from a feed."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from ...core.errors import AppError, Codes

_DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "m.douyin.com",
                 "v.douyin.com", "iesdouyin.com", "www.iesdouyin.com"}


def is_douyin_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in _DOUYIN_HOSTS


def douyin_video_id(url: str) -> str | None:
    if not is_douyin_url(url):
        return None
    parts = urlsplit(url)
    match = re.search(r"/(?:share/)?video/(\d+)(?:/|$)", parts.path)
    if match:
        return match[1]
    query = parse_qs(parts.query)
    return next((value for key in ("modal_id", "aweme_id", "vid", "item_id")
                 for value in query.get(key, []) if value.isdecimal()), None)


def normalize_media_url(value: str) -> str:
    value = value.strip()
    match = re.search(r"https?://[^\s<>\"“”]+", value, re.I)
    url = match[0].rstrip("。，；！、）】),;!") if match else value
    if url.lower().startswith("www."):
        url = "https://" + url
    if not is_douyin_url(url):
        return url
    video_id = douyin_video_id(url)
    if video_id:
        return "https://www.douyin.com/video/" + video_id
    parts = urlsplit(url)
    if parts.hostname != "v.douyin.com" and parts.path.rstrip("/") in ("", "/jingxuan", "/recommend"):
        raise AppError(Codes.ENG_UNSUPPORTED, "这是抖音推荐页，尚未指定视频",
                       suggestion="在目标视频中选择“分享 → 复制链接”，再粘贴到即影")
    return url
