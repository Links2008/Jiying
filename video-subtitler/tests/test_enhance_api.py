"""移除超分后，旧客户端应收到明确错误，普通下载保持可用。"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import services
from app.api import server


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/enhance/status"), ("post", "/api/v1/enhance/download")])
def test_anime_component_routes_are_removed(method, path, monkeypatch):
    calls = []
    fake = SimpleNamespace(status=lambda: calls.append("status") or {
        "installed": True, "available": True}, download=lambda: calls.append("download"))
    monkeypatch.setattr(services, "enhancer", fake, raising=False)
    with TestClient(server.create_app()) as client:
        response = getattr(client, method)(path, headers={"Host": "127.0.0.1:8766"})
    assert response.status_code in (404, 405)  # 静态网页挂载会拒绝不存在路径的 POST
    assert calls == []


def test_legacy_anime_request_is_rejected_before_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "enhancer", SimpleNamespace(status=lambda: {
        "installed": True, "available": True}), raising=False)
    with TestClient(server.create_app()) as client:
        monkeypatch.setattr(server._router, "probe", lambda url: (
            calls.append(url), SimpleNamespace(title="clip")))
        monkeypatch.setattr(server, "_run", lambda _: None)
        response = client.post("/api/v1/tasks", json={
            "url": "https://example.test/video", "enhance": True,
            "enhance_scale": 4}, headers={"Host": "127.0.0.1:8766"})
        assert server._store.list_tasks() == []
    assert response.status_code == 422
    assert "已移除" in response.json()["detail"]
    assert calls == []


def test_disabled_legacy_anime_fields_do_not_block_download(monkeypatch):
    with TestClient(server.create_app()) as client:
        monkeypatch.setattr(server._router, "probe", lambda _: (
            "test", SimpleNamespace(title="clip")))
        monkeypatch.setattr(server, "_run", lambda _: None)
        response = client.post("/api/v1/tasks", json={
            "url": "https://example.test/video", "enhance": False,
            "enhance_scale": 3}, headers={"Host": "127.0.0.1:8766"})
    assert response.status_code == 200
    task = response.json()
    assert task["subtitle_mode"] == "none"
    assert task["enhance"] == 0
    assert task["enhance_scale"] == 2
