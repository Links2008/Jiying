"""安全中间件测试：Host 校验（防 DNS rebinding）+ Origin 校验（防跨站 CSRF）。

使用 FastAPI TestClient；不触发真实网络（探测端点在中间件之后才执行）。
"""
import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import app.api.server as server  # noqa: E402
from app.core.paths import app_root  # noqa: E402

LOCAL_HOST = "127.0.0.1:8766"


@pytest.fixture()
def client():
    return TestClient(server.create_app())


def test_get_tasks_ok_on_localhost(client):
    r = client.get("/api/v1/tasks", headers={"Host": LOCAL_HOST})
    assert r.status_code == 200


def test_local_directory_navigation_is_bounded_to_the_managed_library(client, tmp_path):
    data = tmp_path / "isolated"
    root = data / "library"
    root.mkdir(parents=True)
    for index in range(503):
        (root / f"job-{index:03}").mkdir()
    outside = tmp_path / "unrelated"
    outside.mkdir()
    client.app.state.runtime.data_root = data
    headers = {"Host": LOCAL_HOST}
    drives = client.get("/api/v1/fs/drives", headers=headers).json()
    assert drives == {"home": str(root), "drives": [str(root)]}
    response = client.get("/api/v1/fs/list", headers=headers).json()
    assert response["parent"] == "" and response["truncated"] and len(response["dirs"]) == 500
    assert response["dirs"][0]["name"] == "job-000"
    nested = client.get("/api/v1/fs/list", headers=headers, params={"path": str(root / "job-000")}).json()
    assert nested["parent"] == str(root) and nested["dirs"] == [] and not nested["truncated"]
    assert client.get("/api/v1/fs/list", headers=headers, params={"path": str(outside)}).status_code == 403


def test_instance_identifies_bundled_engine(client):
    r = client.get("/api/v1/instance", headers={"Host": LOCAL_HOST})
    assert r.status_code == 200
    assert r.json() == {"engine_root": str(app_root())}


def test_post_with_evil_origin_blocked(client):
    """恶意网页发起的跨站写请求必须 403。"""
    r = client.post("/api/v1/tasks/does-not-exist/cancel",
                    headers={"Host": LOCAL_HOST,
                             "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_post_with_same_origin_allowed(client):
    r = client.post("/api/v1/tasks/does-not-exist/cancel",
                    headers={"Host": LOCAL_HOST,
                             "Origin": f"http://{LOCAL_HOST}"})
    assert r.status_code == 404  # 通过了 CSRF 校验，走到业务层（任务不存在）


def test_post_without_origin_allowed(client):
    """curl/CLI 等非浏览器客户端不带 Origin → 放行。"""
    r = client.post("/api/v1/tasks/does-not-exist/cancel",
                    headers={"Host": LOCAL_HOST})
    assert r.status_code == 404


def test_evil_host_blocked():
    """DNS rebinding：恶意域名解析到 127.0.0.1 时凭 Host 拦截。"""
    client = TestClient(server.create_app())
    r = client.get("/api/v1/tasks", headers={"Host": "evil.example:8766"})
    assert r.status_code == 403


def test_llm_key_masked_in_config(client):
    """配置接口不得回显 API Key 明文。"""
    r = client.get("/api/v1/config", headers={"Host": LOCAL_HOST})
    assert r.status_code == 200
    body = r.json()
    assert "llm_api_key" not in body
    assert "llm_api_key_masked" in body


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/config"), ("get", "/api/v1/fs/drives"),
    ("get", "/api/v1/tasks"), ("get", "/api/v1/events"),
    ("post", "/api/v1/tasks/nonexistent/cancel"),
])
def test_paired_engine_requires_bearer(method, path):
    client = TestClient(server.create_app(session_token="test-paired-engine-token"),
                        base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers={"Authorization": "Bearer expired-token"}).status_code == 401
    assert client.request(method, path, params={"token": "test-paired-engine-token"}).status_code == 401


def test_paired_discovery_does_not_disclose_token_or_task_state():
    client = TestClient(server.create_app(session_token="test-paired-engine-token"),
                        base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    result = client.get("/api/v1/instance")
    assert result.status_code == 200
    assert result.json() == {"engine_root": str(app_root()), "requires_token": True}
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer test-paired-engine-token"}).status_code == 200
    assert client.post("/api/v1/tasks/missing/cancel", headers={"Authorization": "Bearer test-paired-engine-token",
                                                                "Origin": "https://unrelated.example"}).status_code == 403


def test_paired_engine_rejects_non_loopback_peer_even_with_token():
    client = TestClient(server.create_app(session_token="test-paired-engine-token"),
                        base_url="http://127.0.0.1", client=("192.0.2.12", 50000))
    assert client.get("/api/v1/config", headers={"Authorization": "Bearer test-paired-engine-token"}).status_code == 403

def test_paired_exit_requires_idle_task_state(monkeypatch):
    exits = []
    client = TestClient(server.create_app(session_token="isolated-session", request_exit=lambda: exits.append(True)),
                        base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    headers = {"Authorization": "Bearer isolated-session"}
    monkeypatch.setattr(client.app.state.runtime.store, "list_tasks", lambda: [{"status": "running"}])
    assert client.post("/api/v1/session/exit").status_code == 401
    assert client.post("/api/v1/session/exit", headers=headers).status_code == 409
    assert not exits
    monkeypatch.setattr(client.app.state.runtime.store, "list_tasks", lambda: [{"status": "completed"}])
    assert client.get("/api/v1/session/health", headers=headers).status_code == 204
    assert client.post("/api/v1/session/exit", headers=headers).status_code == 202
    assert exits == [True]
    assert client.post("/api/v1/config", headers=headers, json={}).status_code == 503
