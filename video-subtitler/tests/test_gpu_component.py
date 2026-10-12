"""Optional GPU downloads must be pinned, recoverable, and never automatic."""
import hashlib
import io
import json
import zipfile
from contextlib import contextmanager

import pytest

from app.services.gpu_component import download_gpu_runtime, runtime_present
from app.services.cuda_runtime import ensure_cuda_runtime


def fixture(tmp_path, monkeypatch, broken=False):
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    wheel = io.BytesIO()
    records = []
    with zipfile.ZipFile(wheel, 'w') as archive:
        for name in ('cublas64_12.dll', 'cublasLt64_12.dll'):
            payload = name.encode() * 100
            records.append(dict(name=name, size=len(payload), sha256=hashlib.sha256(payload).hexdigest(), archive_sha256='0' * 64))
            archive.writestr('nvidia/cublas/bin/' + name, b'bad' if broken else payload)
        archive.writestr('../outside.txt', b'must never extract this')
    content = wheel.getvalue()
    (bundle / 'runtime.json').write_text(json.dumps({'files': records}))
    (bundle / 'download.json').write_text(json.dumps(dict(url='https://files.pythonhosted.org/test.whl',
        size=len(content), sha256=hashlib.sha256(content).hexdigest(), version='test')))
    @contextmanager
    def response(*args, **kwargs):
        yield io.BytesIO(content)
    monkeypatch.setattr('app.services.gpu_component.urlopen', response)
    return bundle, tmp_path / 'cache'


def test_downloaded_runtime_is_verified_and_reuses_offline_cache(tmp_path, monkeypatch):
    bundle, cache = fixture(tmp_path, monkeypatch)
    runtime = download_gpu_runtime(bundle, cache)
    assert len(list(runtime.glob('*.dll'))) == 2
    assert ensure_cuda_runtime(bundle, cache) == runtime
    assert not (tmp_path / 'outside.txt').exists()
    monkeypatch.setattr('app.services.gpu_component.urlopen', lambda *_a, **_k: pytest.fail('duplicate download'))
    assert download_gpu_runtime(bundle, cache) == runtime


def test_bad_dll_is_never_installed(tmp_path, monkeypatch):
    bundle, cache = fixture(tmp_path, monkeypatch, broken=True)
    with pytest.raises(RuntimeError, match='校验'):
        download_gpu_runtime(bundle, cache)
    assert not list(cache.rglob('*.dll'))
    assert not list(cache.rglob('*.tmp'))


def test_bad_wheel_hash_is_never_installed(tmp_path, monkeypatch):
    bundle, cache = fixture(tmp_path, monkeypatch)
    meta = json.loads((bundle / 'download.json').read_bytes())
    meta['sha256'] = '0' * 64
    (bundle / 'download.json').write_text(json.dumps(meta))
    with pytest.raises(RuntimeError, match='校验'):
        download_gpu_runtime(bundle, cache)
    assert not list(cache.rglob('*.dll'))


def test_cancellation_removes_temporary_files(tmp_path, monkeypatch):
    bundle, cache = fixture(tmp_path, monkeypatch)
    def cancel():
        raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        download_gpu_runtime(bundle, cache, check_cancel=cancel)
    assert not list(cache.rglob('*.dll'))
    assert not list(cache.rglob('*.tmp'))


def test_untrusted_download_origin_is_rejected(tmp_path, monkeypatch):
    bundle, cache = fixture(tmp_path, monkeypatch)
    meta = json.loads((bundle / 'download.json').read_bytes())
    meta['url'] = 'https://files.pythonhosted.org.example.com/runtime.whl'
    (bundle / 'download.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError, match='地址'):
        download_gpu_runtime(bundle, cache)
    assert not cache.exists()


def test_status_detects_corruption_without_rehashing_unchanged_files(tmp_path, monkeypatch):
    from app.services import gpu_component
    bundle, cache = fixture(tmp_path, monkeypatch)
    target = download_gpu_runtime(bundle, cache)
    assert runtime_present(bundle, cache)
    verify = gpu_component._valid
    monkeypatch.setattr(gpu_component, '_valid', lambda *_a: pytest.fail('unchanged files were rehashed'))
    assert runtime_present(bundle, cache)
    monkeypatch.setattr(gpu_component, '_valid', verify)
    library = target / 'cublas64_12.dll'
    library.write_bytes(b'!' * library.stat().st_size)
    assert not runtime_present(bundle, cache)
