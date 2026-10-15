"""User-triggered, hash-pinned NVIDIA runtime download; inference stays offline."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .cuda_runtime import _CHUNK, _check, _digest, _records, _runtime_lock, _valid


def component_paths(bundle: Path, cache: Path):
    manifest = (bundle / 'runtime.json').read_bytes()
    return _records(manifest), cache / hashlib.sha256(manifest).hexdigest()


def runtime_present(bundle: Path, cache: Path) -> bool:
    try:
        records, target = component_paths(bundle, cache)
        requirements = tuple((r['name'], r['size'], r['sha256']) for r in records)
        for directory in (bundle, target):
            try:
                stamps = tuple((directory / r['name']).stat() for r in records)
                if any(stamp.st_size != record['size'] for stamp, record in zip(stamps, records)):
                    continue
                signature = tuple((s.st_size, s.st_mtime_ns, s.st_ctime_ns) for s in stamps)
                if _verified_runtime(str(directory), requirements, signature):
                    return True
            except OSError:
                continue
        return False
    except (OSError, ValueError):
        return False


@lru_cache(maxsize=8)
def _verified_runtime(directory, requirements, signature):
    return all(_valid(Path(directory) / name, {'size': size, 'sha256': digest}, None)
               for name, size, digest in requirements)


def download_gpu_runtime(bundle: Path, cache: Path, on_progress=None, check_cancel=None) -> Path:
    records, target = component_paths(bundle, cache)
    meta = json.loads((bundle / 'download.json').read_bytes())
    parsed = urlparse(meta['url'])
    if parsed.scheme != 'https' or parsed.hostname != 'files.pythonhosted.org' or parsed.username:
        raise ValueError('GPU 组件下载地址无效')
    if type(meta['size']) is not int or meta['size'] <= 0 or len(meta['sha256']) != 64:
        raise ValueError('GPU 组件下载清单无效')
    cache.mkdir(parents=True, exist_ok=True)
    def report(fraction, message):
        _check(check_cancel)
        if on_progress:
            on_progress(fraction, message)
    with _runtime_lock(cache / (target.name + '.lock'), check_cancel):
        for directory in (bundle, target):
            if all(_valid(directory / r['name'], r, check_cancel) for r in records):
                report(1.0, 'GPU 组件已校验，后续识别自动使用')
                return directory
        target.mkdir(exist_ok=True)
        wheel = None
        try:
            report(0, '正在连接 NVIDIA 组件仓库…')
            digest, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=cache, suffix='.tmp', delete=False) as output:
                wheel = Path(output.name)
                request = Request(meta['url'], headers={'User-Agent': 'Jiying-GPU-Component/1.0'})
                with urlopen(request, timeout=30) as response:
                    while chunk := response.read(_CHUNK):
                        size += len(chunk)
                        if size > meta['size']:
                            raise RuntimeError('GPU 组件体积校验失败')
                        digest.update(chunk)
                        output.write(chunk)
                        report(.8 * size / meta['size'], f'正在下载 GPU 组件 {size / meta["size"]:.0%}')
            if size != meta['size'] or digest.hexdigest() != meta['sha256']:
                raise RuntimeError('GPU 组件下载校验失败，请重试')
            report(.8, '正在校验并安装 GPU 组件…')
            _install_wheel(wheel, target, records, check_cancel)
            report(1, 'GPU 组件已校验，后续识别自动使用')
            return target
        finally:
            if wheel is not None:
                wheel.unlink(missing_ok=True)


def _install_wheel(wheel, target, records, check_cancel):
    with zipfile.ZipFile(wheel) as archive:
        for record in records:
            destination = target / record['name']
            if _valid(destination, record, check_cancel):
                continue
            name = 'nvidia/cublas/bin/' + record['name']
            if archive.getinfo(name).file_size != record['size']:
                raise RuntimeError('GPU 组件内容体积校验失败')
            temporary = None
            try:
                size, digest = 0, hashlib.sha256()
                with tempfile.NamedTemporaryFile(dir=target, suffix='.tmp', delete=False) as output:
                    temporary = Path(output.name)
                    with archive.open(name) as source:
                        while chunk := source.read(_CHUNK):
                            _check(check_cancel)
                            size += len(chunk)
                            if size > record['size']:
                                raise RuntimeError('GPU 组件内容体积校验失败')
                            digest.update(chunk)
                            output.write(chunk)
                if size != record['size'] or digest.hexdigest() != record['sha256']:
                    raise RuntimeError('GPU 组件内容校验失败')
                _check(check_cancel)
                os.replace(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
