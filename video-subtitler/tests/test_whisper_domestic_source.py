"""国内 CT2 下载：仅模拟 HTTP 和临时缓存，不下载真实权重。"""
import hashlib
import json

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services import transcriber as tc


class Response:
    def __init__(self, data=b"", payload=None, status=200, headers=None):
        self.data, self.payload = data, payload
        self.status_code, self.headers = status, headers or {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")
    def json(self):
        return self.payload
    def iter_content(self, chunk_size):
        for offset in range(0, len(self.data), 4):
            yield self.data[offset:offset+4]


@pytest.fixture
def domestic(monkeypatch, tmp_path):
    import requests
    files = {"model.bin": b"model weights", "config.json": b"{}",
             "tokenizer.json": b"{}", "vocabulary.txt": b"word"}
    metadata = [{"Path": name, "Size": len(data), "Sha256": hashlib.sha256(data).hexdigest(),
                 "Revision": "abc123", "Type": "blob"} for name, data in files.items()]
    requests_seen = []
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kw):
            requests_seen.append((url, kw))
            if url.endswith("/repo/files"):
                return Response(payload={"Code": 200, "Data": {"Files": metadata}})
            data = files[kw["params"]["FilePath"]]
            start = int(kw.get("headers", {}).get("Range", "bytes=0-").split("=")[1].split("-")[0])
            return Response(data[start:], status=206 if start else 200,
                            headers={"Content-Range": f"bytes {start}-{len(data)-1}/{len(data)}"})
    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(tc, "_modelscope_model_path", lambda model: tmp_path / model, raising=False)
    return files, metadata, requests_seen, tmp_path / "tiny"


def test_domestic_download_validates_and_is_locally_reusable(domestic):
    files, _, calls, target = domestic
    seen = []
    result = tc._download_modelscope(Config({"download": {"proxy_mode": "direct"}}), "tiny",
        tqdm_class=tc.whisper_progress_tqdm(seen.append))
    assert result == target and tc.whisper_model_complete(result, "tiny")
    assert all((result / name).read_bytes() == data for name, data in files.items())
    assert calls and all(url.startswith("https://modelscope.cn/") for url, _ in calls)
    assert seen and 0 < seen[-1] < 1


def test_domestic_resume_requests_only_missing_bytes(domestic):
    files, metadata, calls, target = domestic
    target.mkdir()
    weight = next(f for f in metadata if f["Path"] == "model.bin")
    part = target / f"model.bin.{weight['Sha256'][:12]}.part"
    part.write_bytes(files["model.bin"][:4])
    tc._download_modelscope(Config({}), "tiny")
    weight_call = next(kw for _, kw in calls if kw.get("params", {}).get("FilePath") == "model.bin")
    assert weight_call["headers"]["Range"] == "bytes=4-"
    assert (target / "model.bin").read_bytes() == files["model.bin"]


def test_domestic_hash_mismatch_never_commits_weight(domestic):
    files, _, _, target = domestic
    files["model.bin"] = b"wrong weights"
    with pytest.raises(OSError, match="SHA256"):
        tc._download_modelscope(Config({}), "tiny")
    assert not (target / "model.bin").exists()
    assert not tc.whisper_model_complete(target, "tiny")


def test_domestic_cancel_leaves_resumable_partial_file(domestic):
    _, _, _, target = domestic
    def check():
        if any(p.stat().st_size >= 4 for p in target.glob("model.bin.*.part")):
            raise AppError(Codes.TASK_CANCELLED, "任务已取消")
    with pytest.raises(AppError) as err:
        tc._download_modelscope(Config({}), "tiny", check_cancel=check)
    assert err.value.code == Codes.TASK_CANCELLED
    assert not (target / "model.bin").exists()
    assert any(target.glob("model.bin.*.part"))


def test_domestic_cached_model_is_detected_without_network(monkeypatch, domestic):
    _, _, calls, target = domestic
    tc._download_modelscope(Config({}), "tiny")
    from faster_whisper import utils
    monkeypatch.setattr(utils, "download_model", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    calls.clear()
    assert tc.local_whisper_model("tiny") == target
    assert not calls


def test_existing_hub_model_is_reused_before_domestic_source(monkeypatch, domestic):
    import huggingface_hub
    from faster_whisper import utils
    _, _, calls, target = domestic
    tc._download_modelscope(Config({}), "tiny")
    calls.clear()
    monkeypatch.setattr(utils, "download_model", lambda *a, **k: str(target))
    monkeypatch.setattr(huggingface_hub, 'try_to_load_from_cache', lambda *a, **k: str(target / 'config.json'))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: pytest.fail("network"))
    assert tc.download_whisper_model(Config({}), "tiny") == target
    assert not calls


def test_domestic_nonempty_truncated_weight_is_not_reused(monkeypatch, domestic):
    _, _, _, target = domestic
    tc._download_modelscope(Config({}), "tiny")
    (target / "model.bin").write_bytes(b"m")
    from faster_whisper import utils
    monkeypatch.setattr(utils, "download_model", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert tc.local_whisper_model("tiny") is None


def test_completed_partial_weight_counts_toward_full_progress(domestic):
    files, metadata, _, target = domestic
    target.mkdir()
    weight = next(f for f in metadata if f["Path"] == "model.bin")
    (target / f"model.bin.{weight['Sha256'][:12]}.part").write_bytes(files["model.bin"])
    seen = []
    tc._download_modelscope(Config({}), "tiny", tqdm_class=tc.whisper_progress_tqdm(seen.append))
    assert seen[-1] == 0.99
