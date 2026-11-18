"""验证由引擎启动的 aria2c 使用隔离的运行时 RPC 配置。"""
from concurrent.futures import ThreadPoolExecutor
import pytest

from app.core.config import Config, save_config
from app.services.downloader import aria2c


def test_bundled_aria2_is_started_once_with_runtime_secret(tmp_path, monkeypatch):
    executable = tmp_path / ("aria2c.exe" if aria2c.os.name == "nt" else "aria2c")
    executable.touch()
    state = []
    config = Config({"download": {"aria2c": {"enabled": True}}})
    config.aria2_managed = True
    monkeypatch.setattr(aria2c, "bin_dir", lambda: tmp_path)

    class Process:
        def poll(self):
            return None
    def launch(args, **kwargs):
        state.append(args)
        return Process()
    monkeypatch.setattr(aria2c.subprocess, "Popen", launch)
    monkeypatch.setattr(aria2c.Aria2cRpcClient, "get_version", lambda self: {"version": "isolated"})
    start = aria2c.ensure_managed_rpc
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(start, [config] * 4))
    assert len(state) == 1
    secret = next(arg.split("=", 1)[1] for arg in state[0] if arg.startswith("--rpc-secret="))
    client = aria2c.Aria2cBackend(config)._client
    assert client._secret == secret and len(secret) == 64
    assert "--no-conf=true" in state[0]
    assert "--rpc-listen-all=false" in state[0]
    assert client._url.startswith("http://127.0.0.1:")
    assert client._url != "http://127.0.0.1:6800/jsonrpc"
    config.replace_raw({"download": {"aria2c": {"enabled": True, "split": 3}}})
    assert aria2c.Aria2cBackend(config)._client._secret == secret
    target = tmp_path / "config.yaml"
    save_config(config.raw, target)
    assert secret not in target.read_text(encoding="utf-8")


def test_explicit_external_configuration_is_not_spawned(tmp_path, monkeypatch):
    target = Config({"download": {"aria2c": {"enabled": True, "mode": "external",
        "rpc_url": "https://aria2.example.test/jsonrpc", "secret": "external-test-only"}}})
    target.aria2_managed = True
    def fail(*args, **kwargs):
        pytest.fail("外部 RPC 配置不能启动内置进程")
    monkeypatch.setattr(aria2c.subprocess, "Popen", fail)
    aria2c.ensure_managed_rpc(target)
    assert aria2c.Aria2cBackend(target)._client._secret == "external-test-only"
