"""GPU status is read-only and downloads require a supported machine."""
import json

from fastapi.testclient import TestClient

from app.api import server
from app.core import paths
from app.services import gpu_component, speech_cuda


def test_status_never_downloads_components(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'bin_dir', lambda: tmp_path / 'bin')
    monkeypatch.setattr(speech_cuda, '_has_cuda', lambda: False)
    def unexpected(*args, **kwargs):
        raise AssertionError('Status must not download')
    monkeypatch.setattr(gpu_component, 'urlopen', unexpected)
    client = TestClient(server.create_app())
    response = client.get('/api/v1/gpu/status', headers={'Host': '127.0.0.1:8766'})
    assert response.status_code == 200
    assert response.json()['downloaded'] is False
    assert response.json()['supported'] is False
    assert not list(tmp_path.rglob('*.dll'))


def test_unsupported_gpu_and_cross_origin_downloads_are_rejected(monkeypatch):
    monkeypatch.setattr(speech_cuda, '_has_cuda', lambda: False)
    client = TestClient(server.create_app())
    path = '/api/v1/gpu/download'
    assert client.post(path, headers={'Host': '127.0.0.1:8766'}).status_code == 400
    assert client.post(path, headers={'Host': '127.0.0.1:8766', 'Origin': 'https://example.com'}).status_code == 403


def test_gpu_status_reads_size_from_download_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'bin_dir', lambda: tmp_path)
    monkeypatch.setattr(speech_cuda, '_has_cuda', lambda: True)
    bundle = tmp_path / 'cuda'
    bundle.mkdir()
    manifest = bundle / 'download.json'
    manifest.write_text(json.dumps({'url': 'https://files.pythonhosted.org/example.whl',
                                    'size': 234567, 'sha256': 'a' * 64}), encoding='utf-8')
    client = TestClient(server.create_app(), base_url='http://127.0.0.1')
    assert client.get('/api/v1/gpu/status').json()['size_bytes'] == 234567
    manifest.write_text('{}', encoding='utf-8')
    assert client.get('/api/v1/gpu/status').json()['size_bytes'] == 0
