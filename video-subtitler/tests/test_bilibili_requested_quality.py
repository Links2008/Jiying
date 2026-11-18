"""B站高画质按具体档位返回，不能以请求不存在的8K替代4K。"""
import json

import pytest
import yt_dlp
from yt_dlp.extractor.bilibili import BilibiliBaseIE
from yt_dlp.utils import ExtractorError

from app.services.engines.bilibili_quality import BiliBiliIE


def playinfo(quality, available=(120, 112, 80)):
    return {'quality': quality, 'accept_quality': list(available), 'dash': {'video': [
        {'id': quality, 'width': 3840 if quality == 120 else 1920,
         'height': 2160 if quality == 120 else 1080, 'codecs': 'avc1',
         'baseUrl': f'https://example.test/{quality}.m4s'}],
        'audio': [{'id': 30280, 'baseUrl': 'https://example.test/audio.m4s'}]}}


@pytest.mark.parametrize('inline_present', [True, False])
def test_fetches_advertised_4k_instead_of_nonexistent_8k(monkeypatch, inline_present):
    requests = []
    monkeypatch.setattr(BiliBiliIE, 'is_logged_in', property(lambda _: True))
    def download(self, bvid, cid, headers=None, query=None, fatal=True):
        requests.append(query.get('qn'))
        return playinfo(120 if query.get('qn') == 120 else 80)
    monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo', download)
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ie = BiliBiliIE(ydl)
        if inline_present:
            ie._search_json('DATA=', 'DATA=' + json.dumps({'data': playinfo(80)}), 'play info', 'BVtest')
        result = ie._download_playinfo('BVtest', 123, query={'try_look': 1}, fatal=False)
    assert {v['id'] for v in result['dash']['video']} == {80, 120}
    assert requests == [None, 120]


def test_keeps_4k_when_secondary_request_fails(monkeypatch):
    monkeypatch.setattr(BiliBiliIE, 'is_logged_in', property(lambda _: True))
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ie = BiliBiliIE(ydl)
        ie._inline_playinfo = playinfo(120)
        monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo', lambda *a, **k: (_ for _ in ()).throw(ExtractorError('暂时失败')))
        assert ie._download_playinfo('BVtest', 123, fatal=False)['dash']['video'][0]['height'] == 2160


def test_explicit_quality_is_not_replaced(monkeypatch):
    monkeypatch.setattr(BiliBiliIE, 'is_logged_in', property(lambda _: True))
    requests = []
    monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo', lambda self, bvid, cid, **kw:
        requests.append(kw['query']['qn']) or playinfo(80))
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ie = BiliBiliIE(ydl)
        ie._inline_playinfo = playinfo(80)
        ie._download_playinfo('BVtest', 123, query={'qn': 80})
    assert requests == [80]


@pytest.mark.parametrize('logged_in', [False, True])
def test_available_uhd_qualities_are_requested_individually(monkeypatch, logged_in):
    requests = []
    advertised = (127, 126, 125, 120, 80)

    def download(self, bvid, cid, headers=None, query=None, fatal=True):
        quality = query.get('qn')
        requests.append((bvid, cid, quality))
        if quality == 126:
            raise ExtractorError('杜比视界本次不可用', expected=True)
        if quality == 125:
            return playinfo(80, advertised)  # 接口声明档位，实际仍只返回 1080p。
        return playinfo(quality or 80, advertised)

    monkeypatch.setattr(BiliBiliIE, 'is_logged_in', property(lambda _: logged_in))
    monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo', download)
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ie = BiliBiliIE(ydl)
        result = ie._download_playinfo('BV1oq4y1S7T1', 392681949, query={'try_look': 1}, fatal=False)
    assert {q for _, _, q in requests} == {None, 127, 126, 125, 120}
    assert len(requests) == 5
    assert all((bvid, cid) == ('BV1oq4y1S7T1', 392681949) for bvid, cid, _ in requests)
    assert {stream['id'] for stream in result['dash']['video']} == {80, 120, 127}


def test_4k_response_preserves_fps_variants_and_audio(monkeypatch):
    inline = playinfo(80, (125, 120, 80))
    inline['timelength'] = 15000
    inline['dash']['dolby'] = {'audio': [{'id': 30250, 'baseUrl': 'https://example.test/dolby.m4s'}]}
    fresh = playinfo(120)
    fresh['timelength'] = 15000
    fresh['dash']['video'][0]['frameRate'] = '30'
    fresh['dash']['video'].append({
        **fresh['dash']['video'][0], 'frameRate': '60', 'baseUrl': 'https://example.test/120-60.m4s',
    })
    preview = {**playinfo(125), 'timelength': 5000, 'is_preview': 1}

    def download(self, bvid, cid, headers=None, query=None, fatal=True):
        return {None: inline, 120: fresh, 125: preview}[query.get('qn')]

    monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo', download)
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ie = BiliBiliIE(ydl)
        result = ie._download_playinfo('BV1rp4y1e745', 244954665, query={'try_look': 1}, fatal=False)
    assert {(stream['id'], stream.get('frameRate')) for stream in result['dash']['video']} == {
        (80, None), (120, '30'), (120, '60'),
    }
    assert result['dash']['dolby'] == inline['dash']['dolby']


def test_page_2_does_not_reuse_page_1_4k(monkeypatch):
    inline = {**playinfo(120), 'cid': 244954665, 'timelength': 347000}
    second = {**playinfo(80), 'cid': 240776679, 'timelength': 955000}
    monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo', lambda *args, **kwargs: second)
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        ie = BiliBiliIE(ydl)
        ie._inline_playinfo = inline
        result = ie._download_playinfo('BV1rp4y1e745', 240776679, query={'try_look': 1}, fatal=False)
    assert [stream['id'] for stream in result['dash']['video']] == [80]


def test_preserves_full_progressive_4k_when_standard_response_uses_dash(monkeypatch):
    standard = {**playinfo(80), 'timelength': 15000}
    full_4k = {'quality': 120, 'timelength': 15000, 'durl': [
        {'url': 'https://example.test/full.mp4', 'length': 15000}],
    }
    monkeypatch.setattr(BilibiliBaseIE, '_download_playinfo',
        lambda self, bvid, cid, **kw: full_4k if kw['query'].get('qn') == 120 else standard)
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        result = BiliBiliIE(ydl)._download_playinfo('BV1oq4y1S7T1', 392681949)
    assert result['quality'] == 120 and result['durl'] == full_4k['durl']
    assert [stream['id'] for stream in result['dash']['video']] == [80]
