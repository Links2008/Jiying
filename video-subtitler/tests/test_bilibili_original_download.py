"""旧原素材档退出后，站内画质和历史记录保持可用。"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.engines import ytdlp_engine


VIDEO_URL = 'https://www.bilibili.com/video/BV1MP4y1m7Ci'
ORIGINAL_ID = 'mixkit-40913-4k'


@pytest.fixture
def original_engine(monkeypatch):
    info = {
        'id': 'BV1MP4y1m7Ci', 'title': '无人机航拍城市高架', 'duration': 14.8,
        '_jiying_bilibili_cid': '890170031',
        'formats': [
            {'format_id': '30080', 'url': 'https://example.test/video.m4s',
             'width': 1920, 'height': 1080, 'vcodec': 'avc1', 'acodec': 'none', 'protocol': 'https'},
            {'format_id': '30280', 'url': 'https://example.test/audio.m4s', 'ext': 'm4a',
             'vcodec': 'none', 'acodec': 'aac', 'protocol': 'https'},
        ],
    }
    engine = ytdlp_engine.YtDlpEngine(Config({}))

    @contextmanager
    def youtube_dl(options):
        yield SimpleNamespace(params=options, cookiejar=None)

    monkeypatch.setattr(engine, '_youtube_dl', youtube_dl)
    monkeypatch.setattr(engine, '_extract_info', lambda *_args, **_kwargs: info)
    return engine, info


def test_original_link_lists_only_bilibili_formats(original_engine):
    engine, _ = original_engine
    result = engine.probe(VIDEO_URL)
    assert [(item.format_id, item.label, item.height) for item in result.formats] == [
        ('30080', '1080p', 1080),
    ]
    assert 'Mixkit' not in result.quality_notice


@pytest.mark.parametrize('changed', ['cid', 'other-video', 'audio-unavailable', 'native-4k'])
def test_original_is_only_advertised_for_matching_available_video(original_engine, changed):
    engine, info = original_engine
    url = VIDEO_URL
    if changed == 'cid':
        info['_jiying_bilibili_cid'] = '890170032'
    elif changed == 'other-video':
        url = 'https://www.bilibili.com/video/BV1MP4y1m7Cx'
    elif changed == 'audio-unavailable':
        info['formats'].pop()
    else:
        info['formats'][0].update(format_id='30120', width=3840, height=2160)
    assert all(item.format_id != ORIGINAL_ID for item in engine.probe(url).formats)


def test_legacy_selection_does_not_fetch_another_source(original_engine, monkeypatch, tmp_path):
    engine, _ = original_engine
    def unexpected(*args, **kwargs):
        pytest.fail("历史画质标识不应请求媒体或替换下载来源")
    monkeypatch.setattr(engine, '_source_info', unexpected)
    monkeypatch.setattr(engine, '_download_native_streams', unexpected)
    with pytest.raises(AppError, match='原素材') as exc:
        engine.download(VIDEO_URL, ORIGINAL_ID, tmp_path, task_id='original-download')
    assert exc.value.code == Codes.ENG_FAILED
    assert '重新解析' in exc.value.suggestion


def test_stale_original_selection_returns_unavailable_instead_of_1080p(original_engine, tmp_path):
    engine, info = original_engine
    info['_jiying_bilibili_cid'] = '890170032'
    with pytest.raises(AppError, match='原素材') as exc:
        engine.download(VIDEO_URL, ORIGINAL_ID, tmp_path)
    assert exc.value.code == Codes.ENG_FAILED
