"""官方发布更新必须完整校验，并在下载结束后才能替换运行模块。"""
import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from app.services import ytdlp_updater as updater


def package(version='2099.01.01', extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('yt_dlp/__init__.py', '')
        archive.writestr('yt_dlp/version.py', f"__version__ = '{version}'\n")
        if extra:
            archive.writestr(*extra)
    return output.getvalue()


class Response:
    def __init__(self, content=b'', data=None):
        self.content, self.data = content, data
        self.text = content.decode(errors='replace')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    def iter_content(self, size):
        yield self.content


def test_latest_release_uses_official_github(monkeypatch):
    calls = []
    digest = '1' * 64

    def get(url, **kwargs):
        calls.append(url)
        return Response(data={'tag_name': '2099.01.01', 'draft': False, 'prerelease': False,
                              'assets': [{'name': 'yt-dlp', 'digest': 'sha256:' + digest,
                                          'browser_download_url': 'https://github.com/yt-dlp/yt-dlp/releases/download/2099.01.01/yt-dlp'}]})

    monkeypatch.setattr('requests.get', get)
    assert updater._latest_release() == ('2099.01.01', calls[0].replace('api.github.com/repos/yt-dlp/yt-dlp/releases/latest',
                                         'github.com/yt-dlp/yt-dlp/releases/download/2099.01.01/yt-dlp'), digest)
    assert calls == ['https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest']


@pytest.mark.parametrize('extra', [('../escape.py', 'bad'), ('yt_dlp/../../escape.py', 'bad')])
def test_unsafe_archive_preserves_installed(monkeypatch, tmp_path, extra):
    vendor = tmp_path / 'vendor'
    active = vendor / 'yt_dlp'
    active.mkdir(parents=True)
    (active / 'version.py').write_text("__version__ = '2098.01.01'", encoding='utf-8')
    payload = package(extra=extra)
    monkeypatch.setattr(updater, '_vendor_dir', lambda: vendor)
    monkeypatch.setattr('requests.get', lambda *a, **k: Response(payload))
    with pytest.raises(RuntimeError):
        updater._install_release('https://github.com/yt-dlp/yt-dlp/releases/download/2099.01.01/yt-dlp',
                                 hashlib.sha256(payload).hexdigest(), '2099.01.01')
    assert '2098.01.01' in (active / 'version.py').read_text()
    assert not (tmp_path / 'escape.py').exists()


@pytest.mark.parametrize('digest,version', [('0' * 64, '2099.01.01'), ('', '2099.01.01'), (None, '2099.01.02')])
def test_integrity_or_version_failure_keeps_old(monkeypatch, tmp_path, digest, version):
    monkeypatch.setattr(updater, '_vendor_dir', lambda: tmp_path)
    payload = package()
    monkeypatch.setattr('requests.get', lambda *a, **k: Response(payload))
    with pytest.raises(RuntimeError):
        updater._install_release('https://github.com/yt-dlp/yt-dlp/releases/download/2099.01.01/yt-dlp',
                                 hashlib.sha256(payload).hexdigest() if digest is None else digest, version)
    assert not (tmp_path / 'pending/yt_dlp').exists()


def test_failed_activation_restores_previous_package(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, '_vendor_dir', lambda: tmp_path)
    for folder, version in [('yt_dlp', '2098.01.01'), ('pending/yt_dlp', '2099.01.01')]:
        path = tmp_path / folder
        path.mkdir(parents=True)
        (path / 'version.py').write_text(f"__version__ = '{version}'", encoding='utf-8')
    monkeypatch.setattr(updater, '_smoke_test', lambda *args: False)
    assert updater._activate_pending() == ''
    assert updater.vendored_version() == '2098.01.01'


def test_update_waits_for_inflight_download(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, '_vendor_dir', lambda: tmp_path)
    monkeypatch.setattr(updater, 'bundled_version', lambda: '2098.01.01')
    payload = package()
    monkeypatch.setattr(updater, '_latest_release', lambda: ('2099.01.01',
                        'https://github.com/yt-dlp/yt-dlp/releases/download/2099.01.01/yt-dlp', hashlib.sha256(payload).hexdigest()))
    monkeypatch.setattr('requests.get', lambda *args, **kwargs: Response(payload))
    monkeypatch.setattr(updater, '_smoke_test', lambda *args: True)
    with updater.runtime_session():
        updater._check_once(force=True)
        assert updater.pending_version() == '2099.01.01'
        assert not updater.vendored_version()
    assert updater.vendored_version() == '2099.01.01'
    assert not updater.pending_version()
