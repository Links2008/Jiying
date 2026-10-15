"""转写结果归一化测试：中文繁→简（OpenCC 可选依赖）。"""
import pytest

from app.services.transcriber import Segment, Transcript


def _zh_transcript() -> Transcript:
    return Transcript(language="zh", segments=[
        Segment(0.0, 2.0, "今天天氣真好"),
        Segment(2.0, 4.0, "語音識別技術很有用"),
    ])


def test_zh_normalized_to_simplified():
    out = _zh_transcript().normalized()
    assert [s.text for s in out.segments] == ["今天天气真好", "语音识别技术很有用"]
    assert out.language == "zh"


def test_non_chinese_untouched():
    t = Transcript(language="en", segments=[Segment(0.0, 1.0, "The weather is nice")])
    assert t.normalized() is t  # 非中文原样返回，不做转换


def test_zh_without_opencc_falls_back():
    pytest.importorskip("opencc")  # 缺依赖时跳过本用例（功能本身可静默降级）
    assert _zh_transcript().normalized().segments[0].text == "今天天气真好"


@pytest.mark.parametrize("action", ["preload", "transcribe"])
def test_missing_cuda_library_falls_back_to_cpu(monkeypatch, tmp_path, action):
    from types import SimpleNamespace
    import faster_whisper
    from app.core.config import Config
    from app.services import transcriber as tc

    calls = []
    def model(path, device, compute_type):
        calls.append((device, compute_type))
        if device == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found")
        return object()

    monkeypatch.setattr(faster_whisper, "WhisperModel", model)
    monkeypatch.setattr(tc, "_has_cuda", lambda: True)
    monkeypatch.setattr(tc, "_add_nvidia_dll_dirs", lambda *a: None)
    monkeypatch.setattr(tc, "download_whisper_model", lambda *a, **k: tmp_path,
                        raising=False)
    t = tc.FasterWhisperTranscriber(Config({"transcriber": {
        "model": "tiny", "device": "auto", "compute_type": "int8_float16",
        "idle_unload_minutes": 0}}))
    monkeypatch.setattr(t, "_run_model", lambda *a, **k: (
        [Segment(0, 1, "ok")], SimpleNamespace(duration=1, language="en")))
    if action == "preload":
        assert t.preload("tiny") == "tiny"
    else:
        assert t.transcribe(tmp_path / "fake.wav").segments[0].text == "ok"
    assert calls == [("cuda", "int8_float16"), ("cpu", "int8")]
    assert t._cuda_broken
    t.preload("tiny")
    assert len(calls) == 2  # 同进程不能重新触发已失败的 CUDA 初始化


def test_model_loading_error_is_reported_without_cpu_retry(monkeypatch, tmp_path):
    import faster_whisper
    from app.core.config import Config
    from app.core.errors import AppError
    from app.services import transcriber as tc

    def fail(*a, **k):
        raise RuntimeError("invalid model binary")
    monkeypatch.setattr(faster_whisper, "WhisperModel", fail)
    monkeypatch.setattr(tc, "download_whisper_model", lambda *a, **k: tmp_path,
                        raising=False)
    t = tc.FasterWhisperTranscriber(Config({"transcriber": {
        "device": "cpu", "idle_unload_minutes": 0}}))
    with pytest.raises(AppError, match="invalid model binary"):
        t.transcribe(tmp_path / "fake.wav")
    assert not t._cuda_broken


def test_optional_runtime_missing_uses_cpu_and_can_enable_gpu_later(monkeypatch, tmp_path):
    import faster_whisper
    from app.core.config import Config
    from app.services import transcriber as tc
    calls = []
    monkeypatch.setattr(faster_whisper, 'WhisperModel', lambda path, device, compute_type:
                        calls.append((device, compute_type)) or object())
    monkeypatch.setattr(tc, '_has_cuda', lambda: True)
    def missing(*args):
        raise RuntimeError('GPU 组件尚未下载')
    monkeypatch.setattr(tc, '_add_nvidia_dll_dirs', missing)
    monkeypatch.setattr(tc, 'download_whisper_model', lambda *a, **k: tmp_path)
    transcriber = tc.FasterWhisperTranscriber(Config({'transcriber': {
        'device': 'auto', 'compute_type': 'int8_float16', 'idle_unload_minutes': 0}}))
    transcriber.preload('tiny')
    assert calls == [('cpu', 'int8')]
    assert not transcriber._cuda_broken
    monkeypatch.setattr(tc, '_add_nvidia_dll_dirs', lambda *a: None)
    transcriber.preload('tiny')
    assert calls[-1] == ('cuda', 'int8_float16')


def test_bundled_cuda_directory_keeps_handle_and_process_search_path(monkeypatch, tmp_path):
    import os
    import sys
    from app.services import transcriber as tc

    if os.name != "nt":
        pytest.skip("Windows DLL loader")
    runtime = tmp_path / "cuda"
    runtime.mkdir()
    handle = object()
    calls = []
    monkeypatch.setattr(tc, "bin_dir", lambda: tmp_path, raising=False)
    monkeypatch.setattr(tc, "_cuda_dll_handles", {}, raising=False)
    monkeypatch.setattr(sys, "path", [])
    monkeypatch.setenv("PATH", "existing-path")
    monkeypatch.setattr(os, "add_dll_directory", lambda path: calls.append(path) or handle)
    tc._add_nvidia_dll_dirs()
    tc._add_nvidia_dll_dirs()
    assert calls == [str(runtime)]
    assert tc._cuda_dll_handles[str(runtime)] is handle
    assert os.environ["PATH"].split(os.pathsep) == [str(runtime), "existing-path"]


def test_compressed_cuda_is_restored_before_native_registration(monkeypatch, tmp_path):
    import os
    import sys
    import json
    import lzma
    import hashlib
    from app.services import transcriber as tc

    if os.name != "nt":
        pytest.skip("Windows DLL loader")
    bundle = tmp_path / "bin" / "cuda"
    bundle.mkdir(parents=True)
    records = []
    for name in ("cublas64_12.dll", "cublasLt64_12.dll"):
        data = (name * 100).encode()
        packed = lzma.compress(data)
        (bundle / (name + ".xz")).write_bytes(packed)
        records.append({"name": name, "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "archive_sha256": hashlib.sha256(packed).hexdigest()})
    (bundle / "runtime.json").write_text(json.dumps({"files": records}), encoding="utf-8")
    monkeypatch.setattr(tc, "bin_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(tc, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(tc, "_cuda_dll_handles", {})
    monkeypatch.setattr(sys, "path", [])
    monkeypatch.setenv("PATH", "existing")
    registered = []

    def register(path):
        from pathlib import Path
        assert all((Path(path) / r["name"]).is_file() for r in records)
        registered.append(path)
        return object()

    monkeypatch.setattr(os, "add_dll_directory", register)
    statuses = []
    tc._add_nvidia_dll_dirs(on_status=statuses.append)
    tc._add_nvidia_dll_dirs()
    assert len(registered) == 1 and statuses
    assert registered[0].startswith(str(tmp_path / "data" / "cache" / "cuda"))
