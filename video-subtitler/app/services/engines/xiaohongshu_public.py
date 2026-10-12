"""Resolve XHS share redirects and use the existing upstream stream parser."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
from yt_dlp.utils import ExtractorError, js_to_json


def is_xhs_url(url):
    return (urlsplit(url).hostname or '').lower() in {
        'xiaohongshu.com', 'www.xiaohongshu.com', 'm.xiaohongshu.com', 'xhslink.com', 'www.xhslink.com'}


def note_url(url):
    if not is_xhs_url(url):
        return None
    parts = urlsplit(url)
    match = re.fullmatch(r'/(?:explore|discovery/item)/([a-f0-9]{24})/?', parts.path, re.I)
    note_id = match[1] if match else None
    if not note_id and parts.path.rstrip('/') == '/404':
        candidate = (parse_qs(parts.query).get('noteId') or [''])[0]
        if re.fullmatch(r'[a-f0-9]{24}', candidate, re.I):
            note_id = candidate
    if note_id:
        return urlunsplit(('https', 'www.xiaohongshu.com', '/discovery/item/' + note_id, parts.query, ''))
    return None


class ConnectedXiaoHongShuIE(XiaoHongShuIE):
    IE_NAME = 'jiying:xiaohongshu'
    _VALID_URL = r'https?://(?:(?:www|m)\.)?(?:xiaohongshu|xhslink)\.com/(?P<id>.*)'

    @classmethod
    def _match_id(cls, url):
        return urlsplit(note_url(url)).path.rsplit('/', 1)[-1]

    def _download_webpage(self, url, video_id, *args, **kwargs):
        webpage = super()._download_webpage(url, video_id, *args, **kwargs)
        state = self._search_json(r'window\.__INITIAL_STATE__\s*=', webpage, 'initial state',
                                  video_id, transform_source=js_to_json, fatal=False, default={})
        note = ((state.get('note') or {}).get('noteDetailMap') or {}).get(video_id) or {}
        self._note_type = (note.get('note') or {}).get('type')
        return webpage

    def _real_extract(self, url):
        canonical = note_url(url)
        if not canonical:
            response = self._request_webpage(url, 'share', 'Resolving XHS share link')
            canonical = note_url(response.url)
        if not canonical:
            raise ExtractorError('小红书分享链接未指向具体笔记，请复制完整视频分享链接', expected=True)
        self._note_type = None
        info = super()._real_extract(canonical)
        if not info.get('formats'):
            if self._note_type == 'normal':
                raise ExtractorError('这是小红书图文笔记，请复制视频笔记的分享链接', expected=True)
            raise ExtractorError('小红书网页未返回视频地址，请连接小红书官网后重试', expected=True)
        info['webpage_url'] = canonical
        info['http_headers'] = (info.get('http_headers') or {}) | {'Referer': 'https://www.xiaohongshu.com/'}
        return info
