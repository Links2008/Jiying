"""真实慢流取消：只访问自建 HTTP 与自建 aria2，不访问用户会话。"""
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import server
from app.core import cancel
from app.services.downloader.aria2c import Aria2cRpcClient


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.025)
    raise AssertionError("isolated cancellation check timed out")


@pytest.mark.parametrize("rpc_available", [True, False], ids=["rpc", "native-fallback"])
def test_cancel_api_stops_actual_slow_transfer(tmp_path, monkeypatch, rpc_available):
    binary = os.environ.get("JIYING_TEST_ARIA2C", "")
    if not binary or not Path(binary).is_file():
        pytest.skip("set JIYING_TEST_ARIA2C for isolated real cancellation acceptance")
    state = {"sent": 0, "requests": 0, "active": 0}
    state_lock, stop = threading.Lock(), threading.Event()
    size = 8 * 1024 * 1024
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.end_headers()
        def do_GET(self):
            with state_lock:
                state["requests"] += 1
                state["active"] += 1
            try:
                self.do_HEAD()
                for offset in range(0, size, 32768):
                    if stop.wait(.08):
                        break
                    chunk = b"x" * min(32768, size - offset)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    with state_lock:
                        state["sent"] += len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                with state_lock:
                    state["active"] -= 1
        def log_message(self, *_):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http.daemon_threads = True
    http_thread = threading.Thread(target=http.serve_forever, daemon=True)
    http_thread.start()
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        rpc_port = port.getsockname()[1]
    assert rpc_port not in (6800, 6801, 6802)
    rpc = Aria2cRpcClient(f"http://127.0.0.1:{rpc_port}/jsonrpc", timeout=1)
    process, task = None, None
    app = server.create_app()
    cfg = server._config
    cfg.set("download.aria2c.rpc_url", rpc._url)
    cfg.set("download.aria2c.enabled", True)
    cfg.set("download.aria2c.poll_interval", .05)
    cfg.set("download.aria2c.max_conn_per_server", 1)
    cfg.set("download.aria2c.split", 1)
    cfg.set("download.proxy_mode", "direct")
    cfg.set("cookies", "")
    cfg.set("cookies_from_browser", "")
    monkeypatch.setattr(server._router, "probe", lambda *_: ("local", SimpleNamespace(title="slow clip")))
    try:
        if rpc_available:
            env = {key: value for key, value in os.environ.items()
                   if key.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
            process = subprocess.Popen(
                [binary, "--enable-rpc", "--rpc-listen-all=false", f"--rpc-listen-port={rpc_port}",
                 "--no-conf=true", "--quiet=true"], cwd=tmp_path, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            def ready():
                assert process.poll() is None
                try:
                    return bool(rpc.get_version())
                except Exception:
                    return False
            wait_for(ready)
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as api:
            body = {"url": f"http://127.0.0.1:{http.server_port}/slow.mp4"}
            response = api.post("/api/v1/tasks", json=body)
            assert response.status_code == 200, response.text
            task = response.json()
            duplicate = api.post("/api/v1/tasks", json=body)
            assert duplicate.status_code == 200
            assert duplicate.json()["id"] == task["id"]
            assert len(server._store.list_tasks()) == 1
            wait_for(lambda: state["sent"] >= 65536)
            started = time.monotonic()
            cancellation = api.post(f"/api/v1/tasks/{task['id']}/cancel")
            assert cancellation.status_code == 200, cancellation.text
            wait_for(lambda: server._store.get_task(task["id"])["status"] == "cancelled")
            terminal_seconds = time.monotonic() - started
            final = api.get(f"/api/v1/tasks/{task['id']}")
            assert final.status_code == 200
            assert final.json()["status"] == "cancelled"
            wait_for(lambda: state["active"] == 0)
            if rpc_available:
                assert rpc._call("tellActive", []) == []
                assert rpc._call("tellWaiting", [0, 10]) == []
                stopped = rpc._call("tellStopped", [0, 10])
                assert stopped and all(job["status"] == "removed" for job in stopped)
            partial_dir = server._runner._data / "tmp" / task["id"]
            def sizes():
                return {p.name: p.stat().st_size for p in partial_dir.rglob("*") if p.is_file()}
            first_sizes, sent, requests = sizes(), state["sent"], state["requests"]
            time.sleep(1)
            assert sizes() == first_sizes, "cancelled partial files must stop growing"
            assert state["sent"] == sent and state["requests"] == requests
            assert requests == 1, "duplicate submit must not start a second transfer"
            assert 0 < sent < size, "cancellation must stop before the full payload is sent"
            assert first_sizes, "cancel must preserve partial files for explicit retry"
            jobs = server._store._exec("SELECT status,backend FROM download_job WHERE task_id=?",
                                      (task["id"],)).fetchall()
            assert [job["status"] for job in jobs] == ["cancelled"]
            report = os.environ.get("JIYING_CANCEL_REPORT", "")
            if report:
                path = Path(report)
                results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
                results.append(dict(mode="rpc" if rpc_available else "native-fallback",
                                    api_cancel_ack=cancellation.json(), final_status=final.json()["status"],
                                    terminal_seconds=round(terminal_seconds, 3), sent_bytes=sent,
                                    full_payload_bytes=size, get_requests=requests,
                                    partial_files=first_sizes, stable_seconds=1,
                                    job_status=jobs[0]["status"], isolated_rpc_port=rpc_port,
                                    owned_aria2_pid=process.pid if process else None,
                                    rpc_active=0 if rpc_available else None,
                                    rpc_waiting=0 if rpc_available else None, passed=True))
                path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    finally:
        stop.set()
        if task and server._store.get_task(task["id"])["status"] in ("pending", "running"):
            cancel.cancel(task["id"])
            wait_for(lambda: server._store.get_task(task["id"])["status"] not in ("pending", "running"))
        if process:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        http.shutdown()
        http.server_close()
        http_thread.join(timeout=3)
