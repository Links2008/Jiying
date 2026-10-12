"""真实 aria2 HTTP 503 回归；通过 JIYING_TEST_ARIA2C 指定发布包内的程序。"""
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.downloader.aria2c import Aria2cBackend, Aria2cRpcClient
from app.services.engines.base import DirectTarget


@pytest.fixture
def real_aria2(tmp_path, monkeypatch):
    executable = os.environ.get("JIYING_TEST_ARIA2C", "")
    if not executable or not Path(executable).is_file():
        pytest.skip("set JIYING_TEST_ARIA2C to the packaged aria2c executable")
    state = {"requests": 0, "always_fail": False, "payload": bytes(range(256)) * 16}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"] += 1
            failing = state["always_fail"] or state["requests"] == 1
            body = b"" if failing else state["payload"]
            self.send_response(503 if failing else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        rpc_port = port_socket.getsockname()[1]
    assert rpc_port not in (6800, 6801)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    process = subprocess.Popen(
        [executable, "--enable-rpc", "--rpc-listen-all=false",
         f"--rpc-listen-port={rpc_port}", "--no-conf=true", "--quiet=true"],
        cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    client = Aria2cRpcClient(f"http://127.0.0.1:{rpc_port}/jsonrpc", timeout=1)
    backend = Aria2cBackend(Config({"download": {
        "proxy_mode": "direct", "aria2c": {"poll_interval": 0.05,
                                             "max_conn_per_server": 1, "split": 1}},
        "proxy": ""}), client=client)
    target = DirectTarget(url=f"http://127.0.0.1:{server.server_port}/media",
                          filename="audio.m4a", kind="audio")
    try:
        deadline = time.monotonic() + 5
        while not backend.ping():
            assert process.poll() is None, "isolated aria2 exited before RPC was ready"
            assert time.monotonic() < deadline, "isolated aria2 RPC did not start"
            time.sleep(0.05)
        yield backend, client, target, state
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_503_once_retries_and_preserves_bytes(real_aria2, tmp_path):
    backend, _, target, state = real_aria2
    output = backend.download(target, tmp_path / "download", timeout=15)
    assert state["requests"] == 2
    assert output.read_bytes() == state["payload"]


def test_persistent_http_503_fails_with_bounded_attempts(real_aria2, tmp_path):
    backend, _, target, state = real_aria2
    state["always_fail"] = True
    started = time.monotonic()
    with pytest.raises(AppError) as error:
        backend.download(target, tmp_path / "download", timeout=15)
    assert error.value.code == Codes.ENG_FAILED
    assert error.value.retryable
    assert state["requests"] == 3
    assert time.monotonic() - started < 15


def test_cancel_during_http_503_retry_removes_job(real_aria2, tmp_path):
    backend, client, target, state = real_aria2
    state["always_fail"] = True
    with pytest.raises(AppError) as error:
        backend.download(target, tmp_path / "download", timeout=15,
                         cancel_check=lambda: state["requests"] > 0)
    assert error.value.code == Codes.TASK_CANCELLED
    deadline = time.monotonic() + 3
    while client._call("tellActive", []) or client._call("tellWaiting", [0, 100]):
        assert time.monotonic() < deadline, "cancelled RPC job remained active"
        time.sleep(0.05)
    assert state["requests"] == 1
