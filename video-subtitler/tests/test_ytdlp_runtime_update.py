"""解析器更新须连同匹配的脚本原子切换，公共 API 限流可走官方发布页。"""
import hashlib
import io
import zipfile
from types import SimpleNamespace
import requests
from app.services import ytdlp_updater as updater
from test_ytdlp_github_update import package, Response, isolated_update_imports


def test_release_page_is_used_when_public_api_is_rate_limited(monkeypatch):
    digest, calls = 'b' * 64, []
    def get(url, **kwargs):
        calls.append(url)
        if url == updater.GITHUB_API:
            response = requests.Response()
            response.status_code = 429
            return response
        if url.endswith('/latest'):
            return SimpleNamespace(url='https://github.com/yt-dlp/yt-dlp/releases/tag/2099.01.01',
                                   raise_for_status=lambda: None)
        return Response((digest + '  yt-dlp\n').encode())
    monkeypatch.setattr(requests, 'get', get)
    version, url, actual = updater._latest_release()
    assert version == '2099.01.01' and actual == digest
    assert url == updater.RELEASE_ROOT + version + '/yt-dlp'
    assert all(call.startswith(('https://api.github.com/', 'https://github.com/yt-dlp/yt-dlp/')) for call in calls)


def test_solver_travels_inside_the_atomic_engine_directory(monkeypatch, tmp_path):
    payload = io.BytesIO(package())
    with zipfile.ZipFile(payload, 'a') as archive:
        for filename in ('__init__.py', '_version.py', 'yt/__init__.py',
                         'yt/solver/__init__.py', 'yt/solver/core.min.js', 'yt/solver/lib.min.js'):
            archive.writestr('yt_dlp_ejs/' + filename, '/* isolated script */' if filename.endswith('.js') else '')
    data = payload.getvalue()
    monkeypatch.setattr(updater, '_vendor_dir', lambda: tmp_path)
    monkeypatch.setattr(requests, 'get', lambda *a, **k: Response(data))
    updater._install_release(updater.RELEASE_ROOT + '2099.01.01/yt-dlp', hashlib.sha256(data).hexdigest(), '2099.01.01')
    script = 'yt_dlp/_jiying_ejs/yt_dlp_ejs/yt/solver/core.min.js'
    assert (tmp_path / 'pending' / script).is_file()
    monkeypatch.setattr(updater, '_smoke_test', lambda _: True)
    assert updater._activate_pending() == '2099.01.01'
    assert (tmp_path / script).is_file()
