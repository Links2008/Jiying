"""GPU status is read-only and downloads require a supported machine."""
from fastapi.testclient import TestClient

from app.api import server
from app.core import paths
from app.services import gpu_component, transcriber


def test_status_never_downloads_components(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'bin_dir', lambda: tmp_path / 'bin')
    monkeypatch.setattr(transcriber, '_has_cuda', lambda: False)
    monkeypatch.setattr(server, '_gpu_job', {})
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
    monkeypatch.setattr(transcriber, '_has_cuda', lambda: False)
    client = TestClient(server.create_app())
    path = '/api/v1/gpu/download'
    assert client.post(path, headers={'Host': '127.0.0.1:8766'}).status_code == 400
    assert client.post(path, headers={'Host': '127.0.0.1:8766', 'Origin': 'https://example.com'}).status_code == 403
