"""Website sessions belong to one platform and never replace global credentials."""
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import server
from app.core.config import Config
from app.core.errors import AppError, Codes
from app.core.paths import data_dir
from app.services.engines.ytdlp_engine import YtDlpEngine, _ytdlp_error

HOST = {'Host': '127.0.0.1:8766'}
SECRET = 'test-visitor-session-not-a-real-credential'
URL = 'https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9?xsec_token=A%2BB%3D'


def cookie(**changes):
    return dict(domain='.xiaohongshu.com', name='web_session', value=SECRET,
                path='/', secure=True, http_only=True, expires=0) | changes


def test_connect_keeps_bilibili_settings_and_hides_credentials():
    with TestClient(server.create_app()) as client:
        server._config.raw.update(cookies='preserved-bilibili.txt', cookies_from_browser='')
        response = client.post('/api/v1/platform/session', headers=HOST,
                               json={'platform': 'xiaohongshu', 'cookies': [cookie()]})
        assert response.status_code == 200
        assert response.json() == {'ok': True}
        assert SECRET not in response.text
        assert server._config.get('cookies') == 'preserved-bilibili.txt'
        jar = MozillaCookieJar(str(data_dir() / 'platform-sessions/xiaohongshu.txt'))
        jar.load(ignore_discard=True)
        assert [(c.name, c.value) for c in jar] == [('web_session', SECRET)]
        assert next(iter(jar)).has_nonstandard_attr('HTTPOnly')


@pytest.mark.parametrize('bad', [cookie(domain='.example.test'), cookie(value=SECRET + '\nforged'),
                                cookie(name='invalid\tname'), cookie(path='not-absolute')])
def test_invalid_session_is_rejected_without_echo_or_write(bad):
    with TestClient(server.create_app()) as client:
        response = client.post('/api/v1/platform/session', headers=HOST,
                               json={'platform': 'xiaohongshu', 'cookies': [bad]})
        assert response.status_code == 422
        assert SECRET not in response.text
        assert not (data_dir() / 'platform-sessions/xiaohongshu.txt').exists()


def test_platform_session_uses_existing_local_api_origin_checks():
    with TestClient(server.create_app()) as client:
        payload = {'platform': 'xiaohongshu', 'cookies': [cookie()]}
        assert client.post('/api/v1/platform/session', json=payload,
                           headers={'Host': 'evil.example:8766'}).status_code == 403
        assert client.post('/api/v1/platform/session', json=payload,
                           headers=HOST | {'Origin': 'https://evil.example'}).status_code == 403


@pytest.mark.parametrize('download', [False, True])
def test_probe_and_download_share_only_target_platform_cookies(monkeypatch, download):
    import yt_dlp
    from app.services.platform_sessions import save_session
    save_session('xiaohongshu', [cookie()])
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        def extract(url, **kwargs):
            assert kwargs['download'] == download
            assert any(c.name == 'web_session' and c.value == SECRET
                       for c in ydl.cookiejar.get_cookies_for_url(URL))
            assert not ydl.cookiejar.get_cookies_for_url('https://www.douyin.com/')
            return {'id': 'sample', 'formats': [{'url': 'https://example.test/video.mp4'}]}
        monkeypatch.setattr(ydl, 'extract_info', extract)
        YtDlpEngine._extract_info(ydl, URL, download)


def test_save_failure_preserves_previous_session(monkeypatch):
    from app.services import platform_sessions as sessions
    sessions.save_session('xiaohongshu', [cookie()])
    target = sessions.session_path('xiaohongshu')
    original = target.read_bytes()
    monkeypatch.setattr(sessions.os, 'replace', lambda *_: (_ for _ in ()).throw(OSError('disk test')))
    with pytest.raises(AppError) as caught:
        sessions.save_session('xiaohongshu', [cookie(value='replacement-test')])
    assert caught.value.code == Codes.DISK_FAILED
    assert target.read_bytes() == original
    assert not list(target.parent.glob('*.tmp'))


def test_untrusted_platform_cannot_choose_cookie_file_path():
    from app.services.platform_sessions import save_session
    with pytest.raises(AppError):
        save_session('../../outside', [cookie()])


def test_xhs_empty_streams_point_to_website_connection():
    error = _ytdlp_error('No video formats found!', '链接解析', URL)
    assert error.code == Codes.AUTH_NEED_COOKIES
    assert '连接小红书' in error.suggestion
    assert '已失效' not in error.message


def test_xhs_short_link_recovers_note_id_and_keeps_signed_query(monkeypatch):
    import yt_dlp
    from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
    from app.services.engines.xiaohongshu_public import ConnectedXiaoHongShuIE
    redirected = ('https://www.xiaohongshu.com/404?source=note&noteId=6411cf99000000001300b6d9'
                  '&xsec_token=A%2BB%3D&xsec_source=app_share')
    seen = []
    monkeypatch.setattr(XiaoHongShuIE, '_real_extract', lambda self, url: seen.append(url) or
                        {'id': 'sample', 'formats': [{'url': 'https://example.test/video.mp4'}]})
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        extractor = ConnectedXiaoHongShuIE(ydl)
        monkeypatch.setattr(extractor, '_request_webpage', lambda *_a, **_k: SimpleNamespace(url=redirected))
        extractor._real_extract('https://xhslink.com/o/public-sample')
    assert len(seen) == 1
    assert seen[0].startswith('https://www.xiaohongshu.com/discovery/item/6411cf99000000001300b6d9?')
    assert 'xsec_token=A%2BB%3D' in seen[0] and 'xsec_source=app_share' in seen[0]


def test_xhs_image_note_is_not_reported_as_failed_login(monkeypatch):
    import json
    import yt_dlp
    from yt_dlp.extractor.common import InfoExtractor
    from app.services.engines.xiaohongshu_public import ConnectedXiaoHongShuIE
    note_id = '6411cf99000000001300b6d9'
    webpage = 'window.__INITIAL_STATE__=' + json.dumps({'note': {'noteDetailMap': {
        note_id: {'note': {'type': 'normal', 'title': 'image note', 'imageList': []}}}}}) + ';</script>'
    monkeypatch.setattr(InfoExtractor, '_download_webpage', lambda *a, **k: webpage)
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        extractor = ConnectedXiaoHongShuIE(ydl)
        with pytest.raises(yt_dlp.utils.ExtractorError) as caught:
            extractor._real_extract(URL)
    error = _ytdlp_error(str(caught.value), '解析', URL)
    assert error.code == Codes.ENG_UNSUPPORTED and '图文' in error.message
