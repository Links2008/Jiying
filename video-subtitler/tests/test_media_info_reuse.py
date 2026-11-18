"""解析页和下载任务之间只短时复用同一账号范围的媒体信息。"""
from types import SimpleNamespace
import pytest
import threading
from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.engines import ytdlp_engine as module


def fake_source(monkeypatch):
    calls = []
    class Ydl:
        def __init__(self, options):
            self.cookiejar = None
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def add_info_extractor(self, extractor):
            assert extractor.ie_key() == 'BiliBili'
        def extract_info(self, url, download=False):
            calls.append(url)
            return {'id': 'sample', 'title': 'sample', 'formats': [
                {'format_id': 'v1', 'url': 'https://cdn.example.test/video', 'height': 1080,
                 'vcodec': 'h264', 'acodec': 'none', 'protocol': 'https'},
                {'format_id': 'a1', 'url': 'https://cdn.example.test/audio',
                 'vcodec': 'none', 'acodec': 'aac', 'protocol': 'https'}]}
    monkeypatch.setattr(module, '_import_ytdlp', lambda: SimpleNamespace(YoutubeDL=Ydl,
        utils=SimpleNamespace(DownloadError=RuntimeError)))
    return calls


def test_ui_probe_and_download_router_share_recent_extraction(monkeypatch):
    calls = fake_source(monkeypatch)
    url = 'https://www.bilibili.com/video/BVreuse'
    module.YtDlpEngine(Config({})).probe(url)
    streams = module.YtDlpEngine(Config({}))._resolve_split_streams(url, 'v1')
    assert len(streams) == 2 and calls == [url]


def test_new_probe_always_refreshes_source(monkeypatch):
    calls = fake_source(monkeypatch)
    engine = module.YtDlpEngine(Config({}))
    url = 'https://www.bilibili.com/video/BVrefresh'
    engine.probe(url)
    engine.probe(url)
    assert calls == [url, url]


def test_changed_cookie_file_cannot_reuse_previous_account_result(monkeypatch, tmp_path):
    calls = fake_source(monkeypatch)
    cookies = tmp_path / 'isolated-cookie.txt'
    cookies.write_text('old', encoding='utf-8')
    config = Config({'cookies': str(cookies)})
    url = 'https://www.bilibili.com/video/BVsession'
    module.YtDlpEngine(config).probe(url)
    cookies.write_text('new-session', encoding='utf-8')
    module.YtDlpEngine(config)._resolve_split_streams(url, 'v1')
    assert len(calls) == 2


def test_expired_source_info_is_extracted_again(monkeypatch):
    calls = fake_source(monkeypatch)
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    url = 'https://www.bilibili.com/video/BVexpired'
    engine = module.YtDlpEngine(Config({}))
    engine.probe(url)
    now[0] += 120
    engine._resolve_split_streams(url, 'v1')
    assert len(calls) == 2


def test_failed_download_discards_its_resolved_addresses(monkeypatch, tmp_path):
    calls = fake_source(monkeypatch)
    engine = module.YtDlpEngine(Config({}))
    url = 'https://www.bilibili.com/video/BVfailed'
    engine.probe(url)
    def fail(*args):
        raise AppError(Codes.NET_FAILED, '临时地址失效')
    monkeypatch.setattr(engine, '_download', fail)
    with pytest.raises(AppError):
        engine.download(url, 'v1', tmp_path)
    engine._resolve_split_streams(url, 'v1')
    assert len(calls) == 2


def test_browser_sessions_are_not_cached_without_a_stable_identity(monkeypatch):
    calls = fake_source(monkeypatch)
    engine = module.YtDlpEngine(Config({'cookies_from_browser': 'edge'}))
    url = 'https://www.bilibili.com/video/BVbrowser'
    engine.probe(url)
    engine._resolve_split_streams(url, 'v1')
    assert len(calls) == 2


def test_account_change_during_probe_cannot_label_old_response_as_new_account(monkeypatch, tmp_path):
    calls = fake_source(monkeypatch)
    cookies = tmp_path / 'changing-cookie.txt'
    cookies.write_text('old', encoding='utf-8')
    engine = module.YtDlpEngine(Config({'cookies': str(cookies)}))
    original = engine._extract_info
    def extract(ydl, url, download):
        result = original(ydl, url, download)
        cookies.write_text('new-session-after-request-start', encoding='utf-8')
        return result
    monkeypatch.setattr(engine, '_extract_info', extract)
    url = 'https://www.bilibili.com/video/BVchanging'
    engine.probe(url)
    engine._resolve_split_streams(url, 'v1')
    assert len(calls) == 2


def test_optional_cache_cannot_break_an_uncopyable_extractor_result(monkeypatch):
    fake_source(monkeypatch)
    engine = module.YtDlpEngine(Config({}))
    original = engine._extract_info
    def extract(ydl, url, download):
        return original(ydl, url, download) | {'extractor_private_state': threading.Lock()}
    monkeypatch.setattr(engine, '_extract_info', extract)
    assert engine.probe('https://www.bilibili.com/video/BVcopy').formats
