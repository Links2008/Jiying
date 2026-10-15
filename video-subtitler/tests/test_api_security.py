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
