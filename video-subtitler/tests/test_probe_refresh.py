"""重新读取登录态时绕过探测缓存，并向客户端提供画质提示。"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import server


@pytest.mark.parametrize("force_refresh", [False, True])
def test_probe_refresh_and_quality_notice(force_refresh, monkeypatch):
    calls = []
    result = SimpleNamespace(video_id="clip", title="clip", duration=12,
        webpage_url="https://example.test/video", thumbnail="", formats=[],
        quality_notice="当前账号可下载原始 4K", auth_status="loaded")
    def probe(url, use_cache=True):
        calls.append((url, use_cache))
        return SimpleNamespace(name="test"), result
    with TestClient(server.create_app()) as client:
        monkeypatch.setattr(server._router, "probe", probe)
        response = client.post("/api/v1/probe", json={
            "url": result.webpage_url, "force_refresh": force_refresh},
            headers={"Host": "127.0.0.1:8766"})
    assert response.status_code == 200
    assert calls == [(result.webpage_url, not force_refresh)]
    assert response.json()["quality_notice"] == result.quality_notice
    assert response.json()["auth_status"] == "loaded"
