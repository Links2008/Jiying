"""Resolve XHS share redirects and use the existing upstream stream parser."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
from yt_dlp.utils import ExtractorError, js_to_json, url_or_none

from .mp4_metadata import probe_mp4


def _https_cdn(url):
    # 官网浏览器会将这些媒体地址升级到 HTTPS；HTTP 在部分代理下返回 502。
    parts = urlsplit(url)
    if parts.scheme == 'http' and (parts.hostname or '').endswith('.xhscdn.com'):
        return urlunsplit(('https', parts.netloc, parts.path, parts.query, parts.fragment))
    return url


def is_xhs_url(url):
    return (urlsplit(url).hostname or '').lower() in {
        'xiaohongshu.com', 'www.xiaohongshu.com', 'm.xiaohongshu.com', 'xhslink.com', 'www.xhslink.com'}


def note_url(url):
    # 限制页可能嵌套保存原分享地址，保留完整查询参数并限制在平台域内。
    for _ in range(3):
        if not is_xhs_url(url):
            return None
        parts = urlsplit(url)
        if not parts.path.startswith('/404'):
            break
        query = parse_qs(parts.query)
        if re.fullmatch(r'[a-f0-9]{24}', (query.get('noteId') or [''])[0], re.I):
            break
        if 'redirectPath' in query:
            raw = parts.query.partition('redirectPath=')[2]
            url = raw if raw.startswith(('https://', 'http://')) else query['redirectPath'][0]
        elif 'source' in query:
            url = urljoin('https://www.xiaohongshu.com/', query['source'][0])
        else:
            break
    if not is_xhs_url(url):
        return None
    parts = urlsplit(url)
    match = re.fullmatch(r'/(?:explore|search_result|discovery/item|user/profile/[a-f0-9]{24})/([a-f0-9]{24})/?', parts.path, re.I)
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
        # yt-dlp 在重定向解析前就读取临时ID；短链接此时还没有笔记ID。
        return urlsplit(note_url(url) or url).path.rstrip('/').rsplit('/', 1)[-1] or 'share'

    def _download_webpage(self, url, video_id, *args, **kwargs):
        webpage = super()._download_webpage(url, video_id, *args, **kwargs)
        state = self._search_json(r'window\.__INITIAL_STATE__\s*=', webpage, 'initial state',
                                  video_id, transform_source=js_to_json, fatal=False, default={})
        note = ((state.get('note') or {}).get('noteDetailMap') or {}).get(video_id) or {}
        self._note_type = (note.get('note') or {}).get('type')
        self._note_video = (note.get('note') or {}).get('video') or {}
        return webpage

    def _real_extract(self, url):
        canonical = note_url(url)
        if not canonical:
            response = self._request_webpage(url, 'share', 'Resolving XHS share link')
            canonical = note_url(response.url)
        if not canonical:
            raise ExtractorError('小红书分享链接未指向具体笔记，请复制完整视频分享链接', expected=True)
        self._note_type = None
        self._note_video = {}
        self._source_size = (0, 0)
        info = super()._real_extract(canonical)
        for fmt in info.get('formats') or []:
            if fmt.get('url'):
                fmt['url'] = _https_cdn(fmt['url'])
        headers = (info.get('http_headers') or {}) | {'Referer': 'https://www.xiaohongshu.com/'}
        info.setdefault('formats', []).extend(self._extra_formats(info.get('formats') or [], headers))
        if not info.get('formats'):
            if self._note_type == 'normal':
                raise ExtractorError('这是小红书图文笔记，请复制视频笔记的分享链接', expected=True)
            raise ExtractorError('小红书网页未返回视频地址，请连接小红书官网后重试', expected=True)
        if not (info.get('title') or '').strip():
            info['title'] = (info.get('description') or '').strip().split('\n')[0][:80] or '小红书视频'
        info['webpage_url'] = canonical
        info['http_headers'] = headers
        candidates = [f for f in info['formats'] if str(f.get('vcodec') or '').startswith('EF')]
        best = max(candidates, key=lambda f: (min(f.get('width') or 0, f.get('height') or 0),
                    f.get('fps') or 0), default=None)
        for fmt in info['formats']:
            if (fmt.get('format_id') == 'direct' or fmt is best) and fmt.get('url'):
                headers = info['http_headers'] | (fmt.get('http_headers') or {})
                metadata = probe_mp4(self._downloader, fmt['url'], headers)
                fmt.update({key: value for key, value in metadata.items() if value})
                fmt['_jiying_complete_media'] = bool(metadata)
                if metadata:
                    fmt['quality'] = max(fmt.get('quality') or 0, 1)
        info['duration'] = max((f.get('duration') or 0 for f in info['formats']), default=0) or info.get('duration')
        info['_jiying_source_size'] = self._source_size
        return info

    def _extra_formats(self, existing, headers):
        # 新页面的高画质地址在 mediaV2 中；上传宽高不能替代流的实际宽高。
        try:
            media = self._note_video.get('mediaV2') or {}
            media = json.loads(media) if isinstance(media, str) else media
        except (TypeError, ValueError):
            return []
        if not isinstance(media, dict):
            return []
        source = media.get('video') or {}
        self._source_size = source.get('width') or 0, source.get('height') or 0
        formats, by_url = [], {f.get('url'): f for f in existing}
        seen = set(by_url)
        for codec, streams in (media.get('stream') or {}).items():
            for index, stream in enumerate(streams or []):
                url = url_or_none(stream.get('master_url'))
                if not url:
                    continue
                url = _https_cdn(url)
                stable_id = f'xhs-{stream.get("stream_type", codec + "-" + str(index))}'
                for backup_index, candidate in enumerate([url, *(stream.get('backup_urls') or [])]):
                    candidate = _https_cdn(candidate)
                    if candidate in by_url:
                        by_url[candidate]['format_id'] = stable_id + (f'-b{backup_index}' if backup_index else '')
                if url in seen:
                    continue
                seen.add(url)
                formats.append({'format_id': stable_id,
                    'url': url, 'ext': 'mp4' if stream.get('format') in ('mp4', 'fmp4') else stream.get('format') or 'mp4',
                    'width': stream.get('width'), 'height': stream.get('height'), 'fps': stream.get('fps'),
                    'vcodec': stream.get('video_codec'), 'acodec': stream.get('audio_codec'),
                    'filesize': stream.get('size'), 'duration': (stream.get('duration') or 0) / 1000})
        opaque = (media.get('video') or {}).get('opaque1') or {}
        url = url_or_none(opaque.get('hd_screencast_stream'))
        url = _https_cdn(url) if url else None
        if url and url not in seen:
            metadata = probe_mp4(self._downloader, url, headers)
            formats.append({'format_id': 'xhs-hd', 'url': url, 'ext': 'mp4',
                            '_jiying_complete_media': bool(metadata), **metadata})
        return formats
