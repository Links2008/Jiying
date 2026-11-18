"""补查当前 B站会话可用的完整画质，保留网页已有的有效流。"""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from yt_dlp.extractor.bilibili import BiliBiliIE as UpstreamBiliBiliIE
from yt_dlp.utils import ExtractorError


_UHD_QUALITIES = (120, 127, 125, 126)


def _advertised(info):
    return {quality for quality in _UHD_QUALITIES if quality in (info.get('accept_quality') or ())
            or any(item.get('quality') == quality for item in info.get('support_formats') or ()
                   if isinstance(item, dict))}


def _returned(info):
    qualities = {item.get('id') for item in (info.get('dash') or {}).get('video') or ()
                 if any(item.get(key) for key in ('baseUrl', 'base_url', 'url'))}
    if info.get('durl'):
        qualities.add(info.get('quality'))
    return qualities


def _same_playback(info, cid, baseline=None):
    if not info or info.get('is_preview') not in (None, 0, '0'):
        return False
    if info.get('cid') and str(info['cid']) != str(cid):
        return False
    expected, actual = (baseline or {}).get('timelength'), info.get('timelength')
    return not (isinstance(expected, (int, float)) and expected > 0
                and isinstance(actual, (int, float)) and actual > 0
                and abs(expected - actual) > max(500, expected * .02))


def _merge_playinfo(previous, fresh, *, supplement=False):
    if not previous or not fresh:
        return deepcopy(previous or fresh)
    merged = deepcopy(previous | fresh)
    if supplement:
        # 补查只追加媒体，主响应的片长和试看状态仍作为完整性依据。
        merged = deepcopy(previous)
        if fresh.get('durl') and not previous.get('durl'):
            merged.update(durl=deepcopy(fresh['durl']), quality=fresh.get('quality'))
    names = {item['quality']: item for info in (previous, fresh)
             for item in info.get('support_formats') or ()
             if isinstance(item, dict) and isinstance(item.get('quality'), int)}
    if names:
        merged['support_formats'] = deepcopy(list(names.values()))
    old_dash, new_dash = previous.get('dash') or {}, fresh.get('dash') or {}
    if not old_dash and not new_dash:
        return merged
    dash = merged['dash'] = {**deepcopy(old_dash), **deepcopy(new_dash)}
    for kind in ('dolby', 'flac'):
        if not dash.get(kind) and old_dash.get(kind):
            dash[kind] = deepcopy(old_dash[kind])
    for kind in ('video', 'audio'):
        streams = {}
        for stream in [*(old_dash.get(kind) or []), *(new_dash.get(kind) or [])]:
            if stream.get('baseUrl') or stream.get('base_url') or stream.get('url'):
                key = (stream.get('id'), stream.get('codecs'), stream.get('width'), stream.get('height'),
                       stream.get('frameRate') or stream.get('frame_rate'))
                streams[key] = deepcopy(stream)
        dash[kind] = list(streams.values())
    return merged


class BiliBiliIE(UpstreamBiliBiliIE):
    # 保持上游 key，短链接和分 P 重定向同样使用本适配。
    def _real_extract(self, url):
        self._inline_playinfo = None
        self._inline_playinfo_cid = None
        self._page_cid = None
        self._advertises_uhd = False
        self._requested_part = parse_qs(urlsplit(url).query).get('p', ['1'])[-1]
        result = super()._real_extract(url)
        if result.get('formats') and self._page_cid:
            result['_jiying_bilibili_cid'] = str(self._page_cid)
            result['_jiying_bilibili_advertises_uhd'] = self._advertises_uhd
        return result

    def _search_json(self, start_pattern, string, name, video_id, *args, **kwargs):
        result = super()._search_json(start_pattern, string, name, video_id, *args, **kwargs)
        if name == 'initial state' and isinstance(result, dict):
            video = result.get('videoData') or {}
            part = next((item for item in video.get('pages') or ()
                         if str(item.get('page')) == self._requested_part), None)
            self._page_cid = part.get('cid') if part else video.get('cid')
        if name == 'play info':
            self._inline_playinfo = result.get('data') if isinstance(result, dict) else None
            if getattr(self, '_page_cid', None):
                self._inline_playinfo_cid = str(self._page_cid)
            if self.is_logged_in:
                return None  # 让上游带正确 CID 和会话查询。
        return result

    def _download_playinfo(self, bvid, cid, headers=None, query=None, fatal=True):
        self._page_cid = cid
        params = {'fourk': 1, **(query or {})}
        request = super()._download_playinfo
        if params.get('qn'):
            return request(bvid, cid, headers=headers, query=params, fatal=fatal)

        inline = getattr(self, '_inline_playinfo', None)
        if inline and getattr(self, '_inline_playinfo_cid', None) not in (None, str(cid)):
            inline = None
        if not _same_playback(inline, cid):
            inline = None
        try:
            fresh = request(bvid, cid, headers=headers, query=params, fatal=fatal)
        except ExtractorError:
            if not inline:
                raise
            fresh = None
        if not _same_playback(inline, cid, fresh):
            inline = None
        result = _merge_playinfo(inline, fresh)
        if not result:
            return result
        offered = _advertised(inline or {}) | _advertised(fresh or {})
        self._advertises_uhd = bool(offered.intersection(_UHD_QUALITIES))
        if _same_playback(result, cid):
            for quality in _UHD_QUALITIES:
                if quality not in offered or quality in _returned(result):
                    continue
                try:
                    extra = request(bvid, cid, headers=headers, query=params | {'qn': quality}, fatal=False)
                except ExtractorError:
                    continue
                if _same_playback(extra, cid, result) and quality in _returned(extra):
                    result = _merge_playinfo(result, extra, supplement=True)
        return result
