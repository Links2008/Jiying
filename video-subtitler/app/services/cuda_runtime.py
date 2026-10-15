"""Restore the exact bundled CUDA libraries locally, without any downloads."""
from __future__ import annotations

import hashlib
import json
import lzma
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_NAMES = {'cublas64_12.dll', 'cublasLt64_12.dll'}
_CHUNK = 1024 * 1024


def _check(callback):
    if callback is not None:
        callback()


def _digest(path, check_cancel):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(_CHUNK):
            _check(check_cancel)
            digest.update(chunk)
    return digest.hexdigest()


def _valid(path, record, check_cancel):
    try:
        return path.stat().st_size == record['size'] and _digest(path, check_cancel) == record['sha256']
    except InterruptedError:
        raise
    except OSError:
        return False


@contextmanager
def _runtime_lock(path, check_cancel):
    """A process crash releases the OS lock; no stale lock directory remains."""
    with path.open('a+b') as handle:
        if not path.stat().st_size:
            handle.write(b'\0')
            handle.flush()
        if os.name == 'nt':
            import msvcrt

            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        deadline = time.monotonic() + 180
        while True:
            _check(check_cancel)
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in (11, 13, 35, 36) or time.monotonic() >= deadline:
                    raise RuntimeError('等待随包 CUDA 库还原超时，请重试') from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            release()


def _records(manifest):
    try:
        records = json.loads(manifest)['files']
        if len(records) != 2 or {r['name'] for r in records} != _NAMES:
            raise ValueError('unexpected CUDA files')
        for record in records:
            if type(record['size']) is not int or record['size'] <= 0:
                raise ValueError('invalid CUDA size')
            for key in ('sha256', 'archive_sha256'):
                digest = record[key]
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                    raise ValueError('invalid CUDA digest')
        return records
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError('随包 CUDA 库校验清单无效') from exc


def _restore(bundle, target, record, check_cancel):
    archive = bundle / (record['name'] + '.xz')
    temporary = None
    try:
        if _digest(archive, check_cancel) != record['archive_sha256']:
            raise RuntimeError('随包 CUDA 压缩库校验失败，请重新安装')
        digest, size = hashlib.sha256(), 0
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.' + target.name + '.', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            with lzma.LZMAFile(archive, 'rb') as source:
                while chunk := source.read(_CHUNK):
                    _check(check_cancel)
                    size += len(chunk)
                    if size > record['size']:
                        raise RuntimeError('随包 CUDA 库解压体积校验失败')
                    digest.update(chunk)
                    output.write(chunk)
        if size != record['size'] or digest.hexdigest() != record['sha256']:
            raise RuntimeError('随包 CUDA 库内容校验失败，请重新安装')
        _check(check_cancel)
        os.replace(temporary, target)
    except InterruptedError:
        raise
    except (OSError, lzma.LZMAError) as exc:
        raise RuntimeError('随包 CUDA 库还原失败，请重试或重新安装') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ensure_cuda_runtime(bundle_dir: Path, cache_root: Path, check_cancel=None, on_status=None) -> Path:
    """Return a verified runtime directory; callers register it before CUDA use."""
    _check(check_cancel)
    if all((bundle_dir / name).is_file() for name in _NAMES):
        return bundle_dir
    try:
        manifest = (bundle_dir / 'runtime.json').read_bytes()
    except OSError as exc:
        raise RuntimeError('安装包缺少随包 CUDA 库校验清单，请重新安装') from exc
    records = _records(manifest)
    identity = hashlib.sha256(manifest).hexdigest()
    cache_root.mkdir(parents=True, exist_ok=True)
    target_dir = cache_root / identity
    with _runtime_lock(cache_root / (identity + '.lock'), check_cancel):
        target_dir.mkdir(exist_ok=True)
        for record in records:
            target = target_dir / record['name']
            if not _valid(target, record, check_cancel):
                if on_status is not None:
                    on_status('正在准备随包 GPU 运行库（仅首次使用）')
                _restore(bundle_dir, target, record, check_cancel)
    return target_dir
