"""Bundled CUDA restoration must be offline, verified, and recoverable."""
import hashlib
import json
import lzma
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services.cuda_runtime import ensure_cuda_runtime


NAMES = ('cublas64_12.dll', 'cublasLt64_12.dll')


def make_bundle(tmp_path):
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    payloads, records = {}, []
    for i, name in enumerate(NAMES):
        data = bytes(range(256)) * (5000 + i)
        packed = lzma.compress(data)
        payloads[name] = data
        (bundle / (name + '.xz')).write_bytes(packed)
        records.append({'name': name, 'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest(),
                        'archive_sha256': hashlib.sha256(packed).hexdigest()})
    (bundle / 'runtime.json').write_text(json.dumps({'files': records}), encoding='utf-8')
    return bundle, tmp_path / 'cache', payloads


def test_restores_exact_bundled_files(tmp_path):
    bundle, cache, payloads = make_bundle(tmp_path)
    statuses = []
    restored = ensure_cuda_runtime(bundle, cache, on_status=statuses.append)
    assert restored.parent == cache
    assert restored.name == hashlib.sha256((bundle / 'runtime.json').read_bytes()).hexdigest()
    assert {p.name: p.read_bytes() for p in restored.glob('*.dll')} == payloads
    assert statuses
    assert not list(cache.rglob('*.tmp'))


def test_development_files_need_no_cache_or_manifest(tmp_path):
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    for name in NAMES:
        (bundle / name).write_bytes(b'development runtime')
    cache = tmp_path / 'unused'
    assert ensure_cuda_runtime(bundle, cache) == bundle
    assert not cache.exists()


def test_valid_cache_is_reused_without_decompression(tmp_path, monkeypatch):
    bundle, cache, _ = make_bundle(tmp_path)
    restored = ensure_cuda_runtime(bundle, cache)
    times = {name: (restored / name).stat().st_mtime_ns for name in NAMES}
    monkeypatch.setattr(lzma, 'LZMAFile', lambda *_a, **_kw: pytest.fail('cache was decompressed'))
    assert ensure_cuda_runtime(bundle, cache) == restored
    assert times == {name: (restored / name).stat().st_mtime_ns for name in NAMES}


def test_corrupt_cache_is_rebuilt_without_rewriting_good_file(tmp_path):
    bundle, cache, payloads = make_bundle(tmp_path)
    restored = ensure_cuda_runtime(bundle, cache)
    good_time = (restored / NAMES[1]).stat().st_mtime_ns
    (restored / NAMES[0]).write_bytes(b'damaged')
    assert ensure_cuda_runtime(bundle, cache) == restored
    assert (restored / NAMES[0]).read_bytes() == payloads[NAMES[0]]
    assert (restored / NAMES[1]).stat().st_mtime_ns == good_time


def test_same_length_cache_damage_is_detected(tmp_path):
    bundle, cache, payloads = make_bundle(tmp_path)
    restored = ensure_cuda_runtime(bundle, cache)
    damaged = bytearray(payloads[NAMES[0]])
    damaged[600] ^= 1
    (restored / NAMES[0]).write_bytes(damaged)
    ensure_cuda_runtime(bundle, cache)
    assert (restored / NAMES[0]).read_bytes() == payloads[NAMES[0]]


def test_corrupt_archive_never_becomes_a_dll(tmp_path):
    bundle, cache, _ = make_bundle(tmp_path)
    archive = bundle / (NAMES[0] + '.xz')
    archive.write_bytes(archive.read_bytes()[:-10])
    with pytest.raises(RuntimeError, match='CUDA'):
        ensure_cuda_runtime(bundle, cache)
    assert not list(cache.rglob('*.dll'))
    assert not list(cache.rglob('*.tmp'))


def test_invalid_xz_with_matching_archive_digest_is_rejected(tmp_path):
    bundle, cache, _ = make_bundle(tmp_path)
    archive = bundle / (NAMES[0] + '.xz')
    archive.write_bytes(b'invalid xz stream')
    manifest = json.loads((bundle / 'runtime.json').read_text(encoding='utf-8'))
    manifest['files'][0]['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    (bundle / 'runtime.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(RuntimeError, match='CUDA'):
        ensure_cuda_runtime(bundle, cache)
    assert not list(cache.rglob('*.dll'))
    assert not list(cache.rglob('*.tmp'))


def test_decoded_data_cannot_exceed_declared_size(tmp_path):
    bundle, cache, _ = make_bundle(tmp_path)
    manifest = json.loads((bundle / 'runtime.json').read_text(encoding='utf-8'))
    manifest['files'][0]['size'] = 1
    (bundle / 'runtime.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(RuntimeError, match='CUDA'):
        ensure_cuda_runtime(bundle, cache)
    assert not list(cache.rglob('*.dll'))
    assert not list(cache.rglob('*.tmp'))


def test_valid_archive_with_wrong_content_digest_is_rejected(tmp_path):
    bundle, cache, _ = make_bundle(tmp_path)
    manifest = json.loads((bundle / 'runtime.json').read_text(encoding='utf-8'))
    manifest['files'][0]['sha256'] = '0' * 64
    (bundle / 'runtime.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(RuntimeError, match='CUDA'):
        ensure_cuda_runtime(bundle, cache)
    assert not list(cache.rglob('*.dll'))
    assert not list(cache.rglob('*.tmp'))


def test_cancel_during_restoration_leaves_no_partial_dll(tmp_path):
    bundle, cache, _ = make_bundle(tmp_path)
    statuses, calls = [], []

    def cancel():
        if statuses:
            calls.append(1)
            if len(calls) == 2:
                raise InterruptedError('cancelled')

    with pytest.raises(InterruptedError, match='cancelled'):
        ensure_cuda_runtime(bundle, cache, check_cancel=cancel, on_status=statuses.append)
    assert not list(cache.rglob('*.dll'))
    assert not list(cache.rglob('*.tmp'))
    assert ensure_cuda_runtime(bundle, cache).is_dir()


def test_concurrent_restore_produces_only_complete_files(tmp_path):
    bundle, cache, payloads = make_bundle(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ensure_cuda_runtime(bundle, cache), range(4)))
    assert len(set(results)) == 1
    assert {p.name: p.read_bytes() for p in results[0].glob('*.dll')} == payloads
    assert not list(cache.rglob('*.tmp'))


def test_separate_processes_share_the_verified_runtime(tmp_path):
    bundle, cache, payloads = make_bundle(tmp_path)
    code = ("import sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
            "from app.services.cuda_runtime import ensure_cuda_runtime; "
            f"print(ensure_cuda_runtime(Path({str(bundle)!r}), Path({str(cache)!r})))")
    children = [subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for _ in range(3)]
    results = [child.communicate(timeout=20) for child in children]
    assert all(child.returncode == 0 for child in children), results
    assert len({stdout.strip() for stdout, stderr in results}) == 1
    restored = Path(results[0][0].strip())
    assert {p.name: p.read_bytes() for p in restored.glob('*.dll')} == payloads
    assert not list(cache.rglob('*.tmp'))


@pytest.mark.parametrize('change', [
    {'name': '../outside.dll'}, {'name': NAMES[1]}, {'size': -1},
    {'sha256': 'wrong'}, {'archive_sha256': 'wrong'},
])
def test_invalid_manifest_is_rejected_before_any_restoration(tmp_path, change):
    bundle, cache, _ = make_bundle(tmp_path)
    manifest = json.loads((bundle / 'runtime.json').read_text(encoding='utf-8'))
    manifest['files'][0].update(change)
    (bundle / 'runtime.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='CUDA'):
        ensure_cuda_runtime(bundle, cache)
    assert not cache.exists()
