"""Whisper 下载/载入回归：只使用隔离临时文件和模拟网络。"""
import json
import threading
from types import SimpleNamespace

import pytest

from app.api import server
from app.core.config import Config
from app.services import transcriber as tc


def _snapshot(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "model.bin").write_bytes(b"mock model")
    for name in ("config.json", "tokenizer.json", "vocabulary.json"):
        (path / name).write_text(json.dumps({"ok": True}), encoding="utf-8")
    (path / "preprocessor_config.json").write_text(
        json.dumps({"feature_size": 128}), encoding="utf-8")
    return path


def _download_result(path, kwargs):
    if kwargs.get("dry_run"):
        return [SimpleNamespace(file_size=f.stat().st_size, will_download=True)
                for f in path.iterdir()]
    return str(path)


@pytest.fixture(autouse=True)
def _isolated_jobs(monkeypatch, tmp_path):
    import huggingface_hub
    from faster_whisper import utils
    def missing(*a, **k):
        raise OSError("no cached model")
    monkeypatch.setattr(utils, "download_model", missing)
    monkeypatch.setattr(tc, "_modelscope_model_path", lambda model: tmp_path / "domestic" / model)
    monkeypatch.setattr(tc, "_download_modelscope", missing)
    monkeypatch.setattr(huggingface_hub, "set_client_factory", lambda factory: None)
    monkeypatch.setattr(server, "_whisper_jobs", {})
    monkeypatch.setattr(server, "_whisper_probe", {})
    monkeypatch.setattr(server, "_config", Config({"download": {"proxy_mode": "direct"}}))


def test_progress_counts_bytes_without_console_output(capsys):
    seen = []
    bar = server._hf_progress_tqdm(seen.append)(total=100, unit="B")
    bar.update(50)
    assert bar.n == 50
    assert seen[-1] == 0.5
    bar.close()
    assert capsys.readouterr().err == ""


def test_progress_ignores_metadata_and_leaves_completion_to_worker():
    seen = []
    cls = server._hf_progress_tqdm(seen.append)
    files = cls(total=4, unit="file")
    files.update(4)
    files.close()
    assert seen == []
    weights = cls(total=100, unit="B")
    weights.update(10)
    assert seen == [0.1]
    weights.update(90)
    weights.close()
    assert 0.99 <= seen[-1] < 1.0


def test_progress_uses_fixed_total_when_hub_discovers_files_late():
    seen = []
    cls = server._hf_progress_tqdm(seen.append)
    cls.set_download_total(1000)
    transfer = cls(total=10, unit="B", desc="Downloading (incomplete total...)")
    bar = cls(total=10, unit="B", desc="Reconstructing (incomplete total...)")
    transfer.update(10)
    assert seen == []
    bar.update(10)
    assert seen == [0.01]
    bar.total = 1000  # 大权重文件线程晚于小 JSON 开始。
    bar.update(490)
    assert seen[-1] == 0.5
    transfer.close()
    bar.close()


@pytest.mark.parametrize("missing", ["model.bin", "config.json", "tokenizer.json", "vocabulary.json"])
def test_partial_snapshot_is_not_downloaded(monkeypatch, tmp_path, missing):
    from faster_whisper import utils
    path = _snapshot(tmp_path / "snapshot")
    (path / missing).unlink()
    monkeypatch.setattr(utils, "download_model", lambda *a, **k: str(path))
    assert not server._whisper_is_downloaded("tiny")


@pytest.mark.parametrize("contents", [None, "invalid json", '{"feature_size": 80}'])
def test_large_v3_requires_128_mel_preprocessor(monkeypatch, tmp_path, contents):
    from faster_whisper import utils
    path = _snapshot(tmp_path / "snapshot")
    prep = path / "preprocessor_config.json"
    if contents is None:
        prep.unlink()
    else:
        prep.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(utils, "download_model", lambda *a, **k: str(path))
    assert tc.local_whisper_model("large-v3") is None
    assert not server._whisper_is_downloaded("large-v3")


def test_download_success_preserved_when_loading_fails(monkeypatch, tmp_path):
    import huggingface_hub
    path = _snapshot(tmp_path / "snapshot")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: _download_result(path, k))
    def fail(model):
        raise RuntimeError("not enough memory")
    monkeypatch.setattr(server, "_runner", SimpleNamespace(
        _transcriber=SimpleNamespace(preload=fail), preload_whisper=fail))
    server._whisper_download_worker("tiny")
    job = server._whisper_job_get("tiny")
    assert job["status"] == "load_failed"
    assert job["downloaded"] and job["fraction"] == 1.0
    assert "not enough memory" in job["error"]


@pytest.mark.parametrize("model", server._WHISPER_MODELS)
def test_all_models_retry_mirror_then_official(monkeypatch, tmp_path, model):
    import huggingface_hub
    from faster_whisper import utils
    path = _snapshot(tmp_path / model)
    calls = []
    def download(repo, **kwargs):
        calls.append((repo, kwargs))
        if kwargs.get("endpoint") == "https://mirror.invalid":
            raise OSError("temporary connection failure")
        return str(path)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    monkeypatch.setattr(tc.time, "sleep", lambda seconds: None)
    cfg = Config({"transcriber": {"hf_endpoint": "https://mirror.invalid"},
                  "download": {"proxy_mode": "direct"}})
    assert tc.download_whisper_model(cfg, model) == path
    assert [kw["endpoint"] for _, kw in calls] == [
        "https://mirror.invalid", "https://mirror.invalid", "https://huggingface.co"]
    assert all(repo == utils._MODELS[model] for repo, _ in calls)
    assert all(not kw.get("force_download") for _, kw in calls)


def test_custom_proxy_applies_without_disabling_tls(monkeypatch, tmp_path):
    import httpx
    import huggingface_hub
    path = _snapshot(tmp_path / "snapshot")
    options = []
    monkeypatch.setattr(httpx, "Client", lambda **kw: options.append(kw))
    monkeypatch.setattr(huggingface_hub, "set_client_factory", lambda factory: factory())
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: _download_result(path, k))
    cfg = Config({"download": {"proxy_mode": "custom",
                               "custom_proxy": "http://127.0.0.1:7890"}})
    assert tc.download_whisper_model(cfg, "tiny") == path
    assert options[-1]["proxy"] == "http://127.0.0.1:7890"
    assert options[-1].get("verify", True) is True
    assert options[-1]["trust_env"] is False


def test_failed_download_releases_lock_and_can_retry(monkeypatch, tmp_path):
    import huggingface_hub
    path = _snapshot(tmp_path / "snapshot")
    calls = []
    def fail(*a, **k):
        calls.append(k["endpoint"])
        raise OSError("connection timed out")
    monkeypatch.setattr(tc.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fail)
    monkeypatch.setattr(server, "_config", Config({"transcriber": {
        "hf_endpoint": "https://mirror.invalid"}, "download": {"proxy_mode": "direct"}}))
    monkeypatch.setattr(server, "_runner", None)
    server._whisper_download_worker("tiny")
    job = server._whisper_job_get("tiny")
    assert job["status"] == "failed" and not job["downloaded"]
    assert len(calls) == 4
    assert not tc._whisper_download_lock.locked()
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: _download_result(path, k))
    server._whisper_download_worker("tiny")
    assert server._whisper_job_get("tiny")["status"] == "downloaded"


def test_thread_start_failure_does_not_leave_model_busy(monkeypatch):
    from fastapi import HTTPException
    app = server.create_app()
    endpoint = next(r.endpoint for r in app.routes if r.path == "/api/v1/whisper/download")
    class Worker:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError("cannot start new thread")
    monkeypatch.setattr(server.threading, "Thread", Worker)
    with pytest.raises(HTTPException) as err:
        endpoint({"model": "tiny"})
    assert err.value.status_code == 503
    assert server._whisper_job_get("tiny")["status"] == "failed"


def test_same_model_download_starts_once(monkeypatch):
    app = server.create_app()
    endpoint = next(r.endpoint for r in app.routes if r.path == "/api/v1/whisper/download")
    read = server._whisper_job_get
    barrier = threading.Barrier(2)
    def overlapping_read(model):
        result = read(model)
        barrier.wait(timeout=2)
        return result
    monkeypatch.setattr(server, "_whisper_job_get", overlapping_read)
    started = []
    class Worker:
        def __init__(self, **kwargs):
            self.model = kwargs["args"][0]
        def start(self):
            started.append(self.model)
    results = []
    callers = [threading.Thread(target=lambda: results.append(endpoint({"model": "tiny"})))
               for _ in range(2)]
    monkeypatch.setattr(server.threading, "Thread", Worker)
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=3)
    assert sorted(r["started"] for r in results) == [False, True]
    assert started == ["tiny"]
