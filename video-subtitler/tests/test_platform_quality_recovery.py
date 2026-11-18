"""平台最高画质回归：只用模拟会话和本地媒体。"""
import copy
import io
import json
import os
import subprocess
from types import SimpleNamespace

import pytest
import yt_dlp

from app.core.config import Config
from app.models.db import Store
from app.services.engines.base import ProbeResult
from app.services.engines.router import EngineRouter
from app.services.engines.ytdlp_engine import YtDlpEngine


def test_xhs_search_result_link_keeps_signature():
    from app.services.engines.xiaohongshu_public import note_url
    assert note_url('https://www.xiaohongshu.com/search_result/6a8e38620000000025039dde?xsec_token=A%2BB=') == (
        'https://www.xiaohongshu.com/discovery/item/6a8e38620000000025039dde?xsec_token=A%2BB=')


def test_xhs_cdn_uses_browser_https_scheme_and_preserves_signature(monkeypatch):
    from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
    from app.services.engines.xiaohongshu_public import ConnectedXiaoHongShuIE
    old = {'formats': [{'url': 'http://sns-video-v2.xhscdn.com/stream/source.mp4?sign=A%2BB&t=123'},
                       {'url': 'http://unrelated.example.test/source.mp4'}]}
    monkeypatch.setattr(XiaoHongShuIE, '_real_extract', lambda *a: copy.deepcopy(old))
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        result = ConnectedXiaoHongShuIE(ydl)._real_extract('https://www.xiaohongshu.com/explore/6a8e38620000000025039dde')
    assert result['formats'][0]['url'] == 'https://sns-video-v2.xhscdn.com/stream/source.mp4?sign=A%2BB&t=123'
    assert result['formats'][1]['url'] == old['formats'][1]['url']


def test_xhs_stable_format_ids_and_optional_more_quality(monkeypatch):
    from yt_dlp.extractor.common import InfoExtractor
    from app.services.engines import xiaohongshu_public as module
    note_id = '6a8e38620000000025039dde'
    video = {'media': {'stream': {'h264': [{'streamType': 258, 'width': 1280, 'height': 720,
        'videoCodec': 'h264', 'masterUrl': 'https://cdn.example.test/720.mp4'}]}},
        'mediaV2': json.dumps({'video': {'width': 3840, 'height': 2160}, 'stream': {'h264': [{
            'stream_type': 258, 'width': 1280, 'height': 720, 'video_codec': 'h264',
            'master_url': 'https://cdn.example.test/720.mp4'}]}})}
    page = 'window.__INITIAL_STATE__=' + json.dumps({'note': {'noteDetailMap': {
        note_id: {'note': {'title': '公开视频', 'video': video}}}}})
    monkeypatch.setattr(InfoExtractor, '_download_webpage', lambda *a, **k: page)
    monkeypatch.setattr(module, 'probe_mp4', lambda *a: {})
    result = YtDlpEngine(Config({})).probe('https://www.xiaohongshu.com/explore/' + note_id)
    assert result.formats[0].format_id == 'xhs-258'
    assert result.auth_status == 'none'
    assert '更多画质' in result.quality_notice
    assert '4K' not in result.quality_notice  # 上传尺寸只提示可能更多，不假称已取得 4K。


@pytest.mark.parametrize('faststart', [False, True])
def test_original_metadata_reads_mp4_without_downloading_payload(tmp_path, faststart):
    from app.services.engines.mp4_metadata import probe_mp4
    ffmpeg = os.environ.get('JIYING_TEST_FFMPEG')
    if not ffmpeg:
        pytest.skip('需要真实 FFmpeg')
    path = tmp_path / 'source.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=60',
        '-f', 'lavfi', '-i', 'sine', '-t', '2', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-c:a', 'aac', *(['-movflags', '+faststart'] if faststart else []), str(path)], check=True)
    payload = path.read_bytes()
    reads = []
    class Response(io.BytesIO):
        status = 206
        def __init__(self, start, end):
            super().__init__(payload[start:end + 1])
            self.headers = {'Content-Range': f'bytes {start}-{min(end, len(payload)-1)}/{len(payload)}'}
        def read(self, size=-1):
            result = super().read(size)
            reads.append(len(result))
            return result
    def open_request(request):
        assert request.headers['Referer'] == 'https://www.xiaohongshu.com/'
        start, end = map(int, request.headers['Range'].removeprefix('bytes=').split('-'))
        return Response(start, end)
    result = probe_mp4(SimpleNamespace(urlopen=open_request), 'https://media.example/source',
                       {'Referer': 'https://www.xiaohongshu.com/'})
    assert (result['width'], result['height'], result['fps']) == (320, 240, 60)
    assert result['vcodec'] == 'h264' and result['acodec'] == 'aac'
    assert sum(reads) < len(payload) / 4


def test_xhs_original_metadata_controls_default_and_actual_target(monkeypatch):
    from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
    from app.services.engines import xiaohongshu_public as module
    original = {'id': '6411cf99000000001300b6d9', 'title': '样本', 'formats': [
        {'format_id': '1080', 'url': 'https://example.test/1080.mp4', 'width': 1920,
         'height': 1080, 'vcodec': 'h264', 'acodec': 'aac', 'protocol': 'https'},
        {'format_id': 'direct', 'url': 'https://example.test/original.mp4', 'ext': 'mp4', 'quality': 1}]}
    monkeypatch.setattr(XiaoHongShuIE, '_real_extract', lambda *_: copy.deepcopy(original))
    monkeypatch.setattr(module, 'probe_mp4', lambda *a: {
        'width': 3840, 'height': 2160, 'fps': 60, 'vcodec': 'h264', 'acodec': 'aac'})
    engine = YtDlpEngine(Config({}))
    url = 'https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9'
    result = engine.probe(url)
    assert result.formats[0].format_id == 'direct'
    assert result.formats[0].height == 2160 and result.formats[0].fps == 60
    target = engine._resolve_direct_stream(url, 'best')
    assert target.url == original['formats'][1]['url']


@pytest.mark.parametrize('failed', [False, True])
def test_bilibili_logged_in_refresh_retains_inline_formats(monkeypatch, failed):
    from yt_dlp.extractor.bilibili import BilibiliBaseIE
    from yt_dlp.utils import ExtractorError
    from app.services.engines.bilibili_quality import BiliBiliIE
    monkeypatch.setattr(BiliBiliIE, 'is_logged_in', property(lambda _: True))
    inline = {'dash': {'video': [{'id': 80, 'codecs': 'avc1', 'baseUrl': 'https://old/1080'},
                               {'id': 64, 'codecs': 'avc1', 'baseUrl': 'https://old/720'}],
                       'audio': [{'id': 30280, 'baseUrl': 'https://old/audio'}]}}
    def api(self, bvid, cid, headers=None, query=None, fatal=True):
        assert 'qn' not in query and query['fourk'] == 1
        if failed:
            raise ExtractorError('模拟临时网络失败')
        return {'dash': {'video': [{'id': 120, 'codecs': 'avc1', 'baseUrl': 'https://new/4k'},
                                  {'id': 80, 'codecs': 'avc1', 'baseUrl': 'https://new/1080'}],
                         'audio': [{'id': 30280, 'baseUrl': 'https://new/audio'}]}}
    monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo', api)
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ie = BiliBiliIE(ydl)
        assert ie._search_json('DATA=', 'DATA=' + json.dumps({'data': inline}), 'play info', 'BVtest') is None
        result = ie._download_playinfo('BVtest', 123, fatal=False)
    assert {v['id'] for v in result['dash']['video']} == ({64, 80} if failed else {64, 80, 120})
    if not failed:
        assert next(v for v in result['dash']['video'] if v['id'] == 80)['baseUrl'] == 'https://new/1080'


def test_bilibili_anonymous_keeps_upstream_path(monkeypatch):
    from app.services.engines.bilibili_quality import BiliBiliIE
    monkeypatch.setattr(BiliBiliIE, 'is_logged_in', property(lambda _: False))
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ie = BiliBiliIE(ydl)
        assert ie.ie_key() == 'BiliBili'
        assert ie._search_json('DATA=', 'DATA={"data":{"quality":80}}', 'play info', 'BVtest') == {'data': {'quality': 80}}


def test_bilibili_empty_dash_keeps_valid_inline(monkeypatch):
    from yt_dlp.extractor.bilibili import BilibiliBaseIE
    from app.services.engines.bilibili_quality import BiliBiliIE
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ie = BiliBiliIE(ydl)
        ie._inline_playinfo = {'dash': {'video': [{'id': 80, 'baseUrl': 'https://example.test/video'}]}}
        monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo', lambda *a, **k: {'dash': None})
        assert ie._download_playinfo('BVtest', 123)['dash']['video'][0]['id'] == 80


def test_bilibili_normal_probe_uses_logged_in_quality_adapter(monkeypatch):
    from yt_dlp.extractor.bilibili import BilibiliBaseIE
    from yt_dlp.extractor.common import InfoExtractor
    from app.services.engines.bilibili_quality import BiliBiliIE
    def stream(qn, width, height):
        return {'id': qn, 'width': width, 'height': height, 'frameRate': '60',
                'codecs': 'avc1.640032', 'baseUrl': f'https://example.test/{qn}.m4s'}
    inline = {'dash': {'video': [stream(80, 1920, 1080)], 'audio': []}}
    fresh = {'dash': {'video': [stream(120, 3840, 2160)], 'audio': []}}
    state = {'videoData': {'bvid': 'BV13u411t7cx', 'title': '测试', 'cid': 123, 'aid': 456}}
    page = 'window.__INITIAL_STATE__=' + json.dumps(state) + ';window.__playinfo__=' + json.dumps({'data': inline})
    monkeypatch.setattr(BiliBiliIE, '_download_webpage_handle', lambda self, url, *a, **k: (page, SimpleNamespace(url=url)))
    monkeypatch.setattr(BiliBiliIE, 'is_logged_in', property(lambda _: True))
    monkeypatch.setattr(BiliBiliIE, '_download_json', lambda *a, **k: {'data': []})
    monkeypatch.setattr(BiliBiliIE, '_get_chapters', lambda *a: None)
    monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo', lambda *a, **k: fresh)
    result = YtDlpEngine(Config({})).probe('https://www.bilibili.com/video/BV13u411t7cx')
    assert [fmt.height for fmt in result.formats] == [2160, 1080]


@pytest.mark.parametrize('browser', [False, True])
def test_router_respects_proxy_and_unstable_browser_session(tmp_path, monkeypatch, browser):
    config = Config({'cookies_from_browser': 'edge' if browser else ''})
    router = EngineRouter(config, Store(tmp_path / 'state.db'))
    calls = []
    def probe(url):
        calls.append(url)
        return ProbeResult(video_id='sample', title=str(len(calls)))
    monkeypatch.setattr(router._engines[0], 'probe', probe)
    router.probe('https://example.test/video')
    if not browser:
        config.set('download.proxy_mode', 'custom')
        config.set('download.custom_proxy', 'http://127.0.0.1:9999')
    assert router.probe('https://example.test/video')[1].title == '2'


@pytest.mark.parametrize('legacy_stream', [False, True])
def test_xhs_media_v2_hd_stream_is_read_and_measured(monkeypatch, legacy_stream):
    from yt_dlp.extractor.common import InfoExtractor
    from app.services.engines import xiaohongshu_public as module
    note_id = '6a8e38620000000025039dde'
    stream = {'width': 1280, 'height': 720, 'fps': 25, 'videoCodec': 'h264',
              'masterUrl': 'https://cdn.example.test/720.mp4'}
    video = {'media': {'stream': {'h264': [stream] if legacy_stream else []}}, 'mediaV2': json.dumps({
        'video': {'width': 7680, 'height': 4320, 'opaque1': {
            'hd_screencast_stream': 'https://cdn.example.test/hd.mp4'}}})}
    page = 'window.__INITIAL_STATE__=' + json.dumps({'note': {'noteDetailMap': {
        note_id: {'note': {'title': '公开视频', 'video': video}}}}})
    monkeypatch.setattr(InfoExtractor, '_download_webpage', lambda *a, **k: page)
    monkeypatch.setattr(module, 'probe_mp4', lambda *a: {
        'width': 3840, 'height': 2160, 'fps': 25, 'duration': 29.96, 'vcodec': 'h264', 'acodec': 'none'})
    engine = YtDlpEngine(Config({}))
    result = engine.probe('https://www.xiaohongshu.com/explore/' + note_id)
    assert result.formats[0].height == 2160  # 不能把上传信息的 8K 冒充实际流的 4K。
    assert result.duration == 29.96
    assert result.formats[0].acodec == 'none'  # 静音原片保留无音轨信息，不当成不完整的分离流。
    target = engine._resolve_direct_stream(result.webpage_url, 'best')
    assert target.url == 'https://cdn.example.test/hd.mp4'
    source = engine._source_info(result.webpage_url)
    selected = list(engine._format_selector('best')({'formats': source['formats'], 'incomplete_formats': False}))
    assert selected[0]['format_id'] == result.formats[0].format_id


@pytest.mark.parametrize('during_request', [False, True])
def test_router_does_not_reuse_other_session_results(tmp_path, monkeypatch, during_request):
    cookie = tmp_path / 'fake-cookies.txt'
    cookie.write_text('模拟账号A', encoding='utf-8')
    router = EngineRouter(Config({'cookies': str(cookie)}), Store(tmp_path / 'state.db'))
    calls = []
    def probe(url):
        calls.append(url)
        if during_request and len(calls) == 1:
            cookie.write_text('模拟账号B，已切换', encoding='utf-8')
        return ProbeResult(video_id='sample', title=str(len(calls)))
    monkeypatch.setattr(router._engines[0], 'probe', probe)
    router.probe('https://www.bilibili.com/video/BVtest')
    if not during_request:
        cookie.write_text('模拟账号B，已切换', encoding='utf-8')
    assert router.probe('https://www.bilibili.com/video/BVtest')[1].title == '2'
    assert router.probe('https://www.bilibili.com/video/BVtest')[1].title == '2'
    assert len(calls) == 2
