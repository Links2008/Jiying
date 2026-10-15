"""Exercise the release spec without building the application."""
import runpy
import json
import hashlib
import lzma
import sys
from pathlib import Path
from types import SimpleNamespace


def run_spec(tmp_path, monkeypatch, binaries=()):
    cuda = tmp_path / 'bin' / 'cuda'
    cuda.mkdir(parents=True)
    for name in ('cublas64_12.dll', 'cublasLt64_12.dll', 'NVIDIA-cublas-LICENSE.txt',
                 'cudnn64_9.dll', 'NVIDIA-cudnn-LICENSE.txt'):
        (cuda / name).write_bytes(b'test')
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(__file__=str(tmp_path / 'fw.py')))
    captured = {}

    def analysis(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pure=[], scripts=[], binaries=list(binaries), datas=kwargs['datas'])

    spec = Path(__file__).parents[1] / 'JiyingEngine.spec'
    result = runpy.run_path(str(spec), init_globals={
        'SPECPATH': str(tmp_path), 'Analysis': analysis,
        'PYZ': lambda *a, **kw: None, 'EXE': lambda *a, **kw: None,
        'COLLECT': lambda *a, **kw: a,
    })
    return captured, result


def test_base_package_contains_no_cuda_payload(tmp_path, monkeypatch):
    captured, _ = run_spec(tmp_path, monkeypatch)
    assert not any(Path(source).suffix in ('.xz', '.dll') and destination == str(Path('bin') / 'cuda')
                   for source, destination in captured['datas'])


def test_only_required_cuda_runtime_is_collected(tmp_path, monkeypatch):
    monkeypatch.setenv('JIYING_BUNDLE_CUDA', '1')
    captured, _ = run_spec(tmp_path, monkeypatch)
    names = set()
    for source, destination in captured['datas'] + captured['binaries']:
        if destination == str(Path('bin') / 'cuda'):
            path = Path(source)
            names.update(p.name for p in path.rglob('*') if p.is_file()) if path.is_dir() else names.add(path.name)
    assert names == {'cublas64_12.dll.xz', 'cublasLt64_12.dll.xz',
                     'runtime.json', 'NVIDIA-cublas-LICENSE.txt'}
    manifest_source = next(source for source, _ in captured['datas']
                           if Path(source).name == 'runtime.json')
    records = json.loads(Path(manifest_source).read_bytes())['files']
    assert {record['name'] for record in records} == {'cublas64_12.dll', 'cublasLt64_12.dll'}
    for record in records:
        archive = Path(manifest_source).parent / (record['name'] + '.xz')
        decoded = lzma.decompress(archive.read_bytes())
        assert decoded == (tmp_path / 'bin' / 'cuda' / record['name']).read_bytes()
        assert record['size'] == len(decoded)
        assert record['sha256'] == hashlib.sha256(decoded).hexdigest()


def test_same_cuda_dll_is_not_collected_twice(tmp_path, monkeypatch):
    source = str(tmp_path / 'bin' / 'cuda' / 'cublasLt64_12.dll')
    canonical = (str(Path('bin') / 'cuda' / 'cublasLt64_12.dll'), source, 'BINARY')
    duplicate = ('cublasLt64_12.dll', source, 'BINARY')
    unrelated = ('another.dll', str(tmp_path / 'another.dll'), 'BINARY')
    different_source = ('cublas64_12.dll', str(tmp_path / 'other' / 'cublas64_12.dll'), 'BINARY')
    _, result = run_spec(tmp_path, monkeypatch, (canonical, duplicate, unrelated, different_source))
    assert result['a'].binaries == [unrelated]
