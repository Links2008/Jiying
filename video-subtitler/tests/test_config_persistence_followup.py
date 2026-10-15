"""配置保存失败、并发请求和重启一致性的隔离回归。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api import server
from app.core import config as config_module, paths


def client():
    return TestClient(server.create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)


def fail_config_write(monkeypatch, target, *, truncate=False):
    write_text = Path.write_text
    replace = config_module.os.replace if hasattr(config_module, "os") else __import__("os").replace

    def direct_write(path, text, *args, **kwargs):
        if path == target:
            if truncate:
                write_text(path, "download: [", encoding="utf-8")
            raise OSError("模拟保存失败")
        return write_text(path, text, *args, **kwargs)

    def atomic_replace(src, dst):
        if Path(dst) == target:
            raise OSError("模拟保存失败")
        return replace(src, dst)

    monkeypatch.setattr(Path, "write_text", direct_write)
    monkeypatch.setattr(__import__("os"), "replace", atomic_replace)


def test_failed_save_keeps_runtime_provider_capacity_and_probe_cache(tmp_path, monkeypatch):
    api = client()
    previous = deepcopy(server._config.raw)
    provider = server._runner._transcriber
    effects = []
    from app.pipeline import runner
    monkeypatch.setattr(runner, "set_max_concurrent", lambda value: effects.append(("concurrent", value)))
    monkeypatch.setattr(server._runner, "reload_transcriber", lambda **kw: effects.append(("reload", kw)))
    monkeypatch.setattr(server._store, "cache_purge_kind", lambda kind: effects.append(("cache", kind)))
    fail_config_write(monkeypatch, paths.default_config_path())
    response = api.post("/api/v1/config", json={"model": "medium", "transcriber_provider": "cloud",
                                               "max_concurrent": 4, "cookies": "模拟路径",
                                               "asr_api_key": "test-only-not-real"})
    assert response.status_code >= 500
    assert server._config.raw == previous
    assert server._runner._transcriber is provider
    assert effects == []
    assert response.status_code == 503
    assert "test-only-not-real" not in response.text


def test_interrupted_save_preserves_old_file_and_cleans_temporary_files(tmp_path, monkeypatch):
    target = paths.default_config_path()
    old = "transcriber:\n  model: small\nuser_custom:\n  untouched: true\n"
    target.write_text(old, encoding="utf-8")
    api = client()
    fail_config_write(monkeypatch, target, truncate=True)
    response = api.post("/api/v1/config", json={"model": "medium"})
    assert response.status_code >= 500
    assert target.read_text(encoding="utf-8") == old
    assert response.status_code == 503
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_save_creates_missing_config_parent_and_roundtrips_custom_settings(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "settings" / "config.yaml"
    monkeypatch.setattr(paths, "default_config_path", lambda: target)
    api = client()
    server._config.raw["user_custom"] = {"keep": "保留"}
    assert api.post("/api/v1/config", json={"asr_hotwords": "RAG", "model": "medium"}).status_code == 200
    loaded = config_module.load_config(target)
    assert loaded.get("user_custom.keep") == "保留"
    assert loaded.get("transcriber.hotwords") == "RAG"
    assert loaded.get("transcriber.model") == "medium"


@pytest.mark.parametrize("invalid", ["broken: [", "- unexpected-list\n"])
def test_invalid_existing_file_is_preserved_instead_of_silently_overwritten(tmp_path, invalid):
    api = client()
    target = paths.default_config_path()
    target.write_text(invalid, encoding="utf-8")
    response = api.post("/api/v1/config", json={"model": "medium"})
    assert response.status_code == 503
    assert target.read_text(encoding="utf-8") == invalid
    assert server._config.get("transcriber.model") == "small"


def test_explicit_empty_endpoint_and_models_restore_vendor_defaults_after_restart(tmp_path):
    api = client()
    assert api.post("/api/v1/config", json={"llm_base_url": "https://example.invalid/api",
                                               "llm_model": "custom-model", "asr_model": "custom-asr"}).status_code == 200
    assert api.post("/api/v1/config", json={"llm_base_url": "", "llm_model": "", "asr_model": ""}).status_code == 200
    loaded = config_module.load_config(paths.default_config_path())
    assert loaded.get("translator.llm.base_url") == ""
    assert loaded.get("translator.llm.model") == ""
    assert loaded.get("transcriber.cloud.model") == ""


def test_concurrent_save_serializes_disk_and_runtime_snapshots(tmp_path, monkeypatch):
    api = client()
    target = paths.default_config_path()
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    second_completed = threading.Event()
    write_text = Path.write_text
    import os
    replace = os.replace
    first_thread = []

    def before_commit():
        ident = threading.get_ident()
        if not first_thread:
            first_thread.append(ident)
            entered.set()
            assert release.wait(5)

    def direct_write(path, text, *args, **kwargs):
        if path == target:
            before_commit()
        return write_text(path, text, *args, **kwargs)

    def atomic_replace(src, dst):
        if Path(dst) == target:
            before_commit()
        return replace(src, dst)

    monkeypatch.setattr(Path, "write_text", direct_write)
    monkeypatch.setattr(os, "replace", atomic_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(api.post, "/api/v1/config", json={"asr_hotwords": "人名"})
        assert entered.wait(5)

        def save_second():
            second_started.set()
            try:
                return api.post("/api/v1/config", json={"llm_domain_context": "访谈"})
            finally:
                second_completed.set()

        second = pool.submit(save_second)
        assert second_started.wait(5)
        try:
            # 首次替换尚未完成，后续请求不能越过它写入另一个快照。
            assert not second_completed.wait(0.1)
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
        assert second.result(timeout=5).status_code == 200
    loaded = config_module.load_config(target)
    assert loaded.get("transcriber.hotwords") == "人名"
    assert loaded.get("translator.llm.domain_context") == "访谈"
    assert loaded.raw == server._config.raw


def test_failed_candidate_build_preserves_file_runtime_and_current_provider(tmp_path, monkeypatch):
    from app.services import transcriber
    target = paths.default_config_path()
    old = "transcriber:\n  provider: faster-whisper\n"
    target.write_text(old, encoding="utf-8")
    api = client()
    current = server._runner._transcriber
    previous = deepcopy(server._config.raw)

    def build(_):
        raise RuntimeError("test-only-sensitive-key")

    monkeypatch.setattr(transcriber, "build_transcriber", build)
    response = api.post("/api/v1/config", json={"transcriber_provider": "cloud"})
    assert response.status_code == 503
    assert "test-only-sensitive-key" not in response.text
    assert target.read_text(encoding="utf-8") == old
    assert server._config.raw == previous
    assert server._runner._transcriber is current


def test_prepared_provider_closed_when_save_fails(tmp_path, monkeypatch):
    from app.services import transcriber
    api = client()
    closed = []
    prepared = SimpleNamespace(name="cloud", close=lambda: closed.append(True))
    monkeypatch.setattr(transcriber, "build_transcriber", lambda cfg: prepared)
    fail_config_write(monkeypatch, paths.default_config_path())
    assert api.post("/api/v1/config", json={"transcriber_provider": "cloud"}).status_code == 503
    assert closed == [True]
    assert server._runner._transcriber is not prepared


def test_successful_save_swaps_prepared_provider_after_disk_commit(tmp_path, monkeypatch):
    from app.services import transcriber
    api = client()
    closed = []
    monkeypatch.setattr(server._runner._transcriber, "close", lambda: closed.append("old"), raising=False)
    prepared = SimpleNamespace(name="cloud", close=lambda: closed.append("new"))
    monkeypatch.setattr(transcriber, "build_transcriber", lambda cfg: prepared)
    assert api.post("/api/v1/config", json={"transcriber_provider": "cloud", "asr_model": "new-asr"}).status_code == 200
    assert server._runner._transcriber is prepared
    assert prepared._cfg is server._config
    assert prepared._cfg.get("transcriber.cloud.model") == "new-asr"
    assert config_module.load_config(paths.default_config_path()).get("transcriber.provider") == "cloud"
    assert closed == ["old"]


def test_fsync_failure_keeps_old_file_and_runtime(tmp_path, monkeypatch):
    target = paths.default_config_path()
    old = "transcriber:\n  model: small\n"
    target.write_text(old, encoding="utf-8")
    api = client()

    def fail(_):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(config_module.os, "fsync", fail)
    assert api.post("/api/v1/config", json={"model": "medium"}).status_code == 503
    assert target.read_text(encoding="utf-8") == old
    assert server._config.get("transcriber.model") == "small"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_whitespace_keys_keep_saved_mock_credentials(tmp_path):
    api = client()
    assert api.post("/api/v1/config", json={"asr_api_key": "test-asr-only", "llm_api_key": "test-llm-only"}).status_code == 200
    assert api.post("/api/v1/config", json={"asr_api_key": "  ", "llm_api_key": "  "}).status_code == 200
    loaded = config_module.load_config(paths.default_config_path())
    assert loaded.get("transcriber.cloud.api_key") == "test-asr-only"
    assert loaded.get("translator.llm.api_key") == "test-llm-only"


def test_config_get_uses_one_snapshot_during_concurrent_publication(monkeypatch):
    api = client()
    before = deepcopy(server._config.raw)
    before["cookies"] = "旧模拟路径"
    before["pipeline"] = {"max_concurrent": 1}
    before["transcriber"].update(model="small", hotwords="旧姓名")
    before["translator"]["llm"]["domain_context"] = "旧访谈"
    after = deepcopy(before)
    after["cookies"] = "新模拟路径"
    after["pipeline"]["max_concurrent"] = 4
    after["transcriber"].update(model="medium", hotwords="新姓名")
    after["translator"]["llm"]["domain_context"] = "新访谈"

    class SwitchingConfig(config_module.Config):
        reads = 0

        @property
        def raw(self):
            snapshot = super().raw
            self.reads += 1
            if self.reads == 1:
                # 精确模拟 GET 已捕获旧快照时，另一个请求发布完整新快照。
                self.replace_raw(after)
            return snapshot

    switching = SwitchingConfig(before)
    monkeypatch.setattr(server, "_config", switching)
    response = api.get("/api/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "small"
    assert body["asr_hotwords"] == "旧姓名"
    assert body["llm_domain_context"] == "旧访谈"
    assert body["download"]["cookies"] == "旧模拟路径"
    assert body["max_concurrent"] == 1
    assert switching.reads == 1
    assert switching.get("transcriber.model") == "medium"
