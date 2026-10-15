"""扫码只暴露图片和状态；登录凭据在后端原子保存并立即生效。"""
import base64
import io
from http.cookiejar import MozillaCookieJar
from types import SimpleNamespace

import pytest
import requests
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from app.api import server
from app.core import paths

HOST = {"Host": "127.0.0.1:8766"}
SECRET = "test-session-only-not-a-real-cookie"


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.proxies = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.closed = False
        self.calls = []
        self.states = [86101]
        self.error = None
        self.with_cookie = True
        self.generate_data = {"qrcode_key": SECRET,
                              "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?key=" + SECRET}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        if url.endswith("/generate"):
            body = {"code": 0, "data": self.generate_data}
        else:
            code = self.states.pop(0)
            if code == 0 and self.with_cookie:
                self.cookies.set("SESSDATA", SECRET, domain=".bilibili.com", path="/", secure=True)
                self.cookies.set("bili_jct", "test-csrf", domain=".bilibili.com", path="/")
                self.cookies.set("unrelated", "other", domain=".example.com", path="/")
            body = {"code": 0, "data": {"code": code, "url": SECRET, "refresh_token": SECRET}}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    def close(self):
        self.closed = True


@pytest.fixture
def login_client(monkeypatch):
    sessions = []

    def new_session():
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(requests, "Session", new_session)
    with TestClient(server.create_app()) as client:
        yield client, sessions


def start(client):
    response = client.post("/api/v1/bilibili/login/start", headers=HOST)
    assert response.status_code == 200
    return response.json()


def poll(client, session_id):
    return client.post("/api/v1/bilibili/login/poll", json={"session_id": session_id}, headers=HOST)


def test_start_returns_local_png_without_login_key_or_url(login_client):
    client, _ = login_client
    body = start(client)
    assert set(body) == {"session_id", "image_base64", "status", "message"}
    assert body["status"] == "pending"
    assert len(body["session_id"]) >= 32
    with Image.open(io.BytesIO(base64.b64decode(body["image_base64"]))) as image:
        assert image.format == "PNG"
        assert image.width >= 200
    assert SECRET not in str(body)


def test_poll_maps_wait_scan_and_expiry_without_credentials(login_client):
    client, sessions = login_client
    login = start(client)
    sessions[-1].states = [86101, 86090, 86038]
    for status in ("pending", "scanned", "expired"):
        response = poll(client, login["session_id"])
        assert response.status_code == 200
        assert response.json()["status"] == status
        assert set(response.json()) == {"status", "message"}
        assert SECRET not in response.text
    assert sessions[-1].closed


def test_start_replaces_old_session_and_rejects_stale_poll(login_client):
    client, sessions = login_client
    old = start(client)
    fresh = start(client)
    assert old["session_id"] != fresh["session_id"]
    assert sessions[0].closed
    assert poll(client, old["session_id"]).json()["status"] == "expired"
    assert len(sessions[1].calls) == 1


def test_poll_expires_locally_after_180_seconds(login_client, monkeypatch):
    from app.services import bilibili_login
    client, sessions = login_client
    clock = [1000.0]
    monkeypatch.setattr(bilibili_login.time, "monotonic", lambda: clock[0])
    login = start(client)
    clock[0] += 180
    assert poll(client, login["session_id"]).json()["status"] == "expired"
    assert len(sessions[0].calls) == 1
    assert sessions[0].closed


def test_connected_atomically_saves_netscape_config_and_is_idempotent(login_client):
    client, sessions = login_client
    server._config.raw.update(cookies="old-path", cookies_from_browser="edge", user_custom="keep")
    server._store.cache_put("old-probe", "probe", "test-probe.json", ttl_hours=1)
    login = start(client)
    sessions[-1].states = [0]
    first = poll(client, login["session_id"])
    assert first.status_code == 200
    assert first.json()["status"] == "connected"
    assert SECRET not in first.text
    cookiefile = paths.data_dir() / "bilibili-cookies.txt"
    jar = MozillaCookieJar(str(cookiefile))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert {c.name for c in jar} == {"SESSDATA", "bili_jct"}
    assert next(c.value for c in jar if c.name == "SESSDATA") == SECRET
    saved = yaml.safe_load(paths.default_config_path().read_text(encoding="utf-8"))
    assert saved["cookies"] == str(cookiefile)
    assert saved["cookies_from_browser"] == ""
    assert saved["user_custom"] == "keep"
    assert server._store.cache_get("old-probe") is None
    assert sessions[-1].closed
    assert poll(client, login["session_id"]).json() == first.json()
    assert len(sessions[-1].calls) == 2


@pytest.mark.parametrize("failure", ["network", "missing-cookie", "disk"])
def test_login_failure_does_not_claim_connected_or_replace_old_config(login_client, monkeypatch, failure):
    from app.services import bilibili_login
    client, sessions = login_client
    server._config.raw.update(cookies="old-path", cookies_from_browser="edge")
    cookiefile = paths.data_dir() / "bilibili-cookies.txt"
    cookiefile.write_text("old-cookie-file", encoding="utf-8")
    login = start(client)
    sessions[-1].states = [0]
    if failure == "network":
        sessions[-1].error = requests.ConnectionError("https://poll?key=" + SECRET)
    elif failure == "missing-cookie":
        sessions[-1].with_cookie = False
    else:
        monkeypatch.setattr(bilibili_login.os, "replace", lambda *_: (_ for _ in ()).throw(OSError(SECRET)))
    response = poll(client, login["session_id"])
    assert response.status_code == 503
    assert SECRET not in response.text
    assert server._config.get("cookies") == "old-path"
    assert server._config.get("cookies_from_browser") == "edge"
    assert cookiefile.read_text(encoding="utf-8") == "old-cookie-file"


def test_qr_routes_keep_existing_local_host_and_origin_guard(login_client):
    client, sessions = login_client
    assert client.post("/api/v1/bilibili/login/start", headers={"Host": "evil.example:8766"}).status_code == 403
    assert client.post("/api/v1/bilibili/login/start", headers=HOST | {"Origin": "http://evil.example"}).status_code == 403
    assert sessions == []


@pytest.mark.parametrize("field,value", [(field, value) for field in ("url", "qrcode_key")
                                         for value in (None, [], 123)])
def test_generate_rejects_non_string_url_and_key_with_safe_error(login_client, monkeypatch, field, value):
    client, _ = login_client
    session = FakeSession()
    session.generate_data[field] = value
    monkeypatch.setattr(requests, "Session", lambda: session)
    response = client.post("/api/v1/bilibili/login/start", headers=HOST)
    assert response.status_code == 503
    assert SECRET not in response.text
    assert session.closed


def test_confirmed_login_retries_config_save_without_another_scan_or_poll(login_client, monkeypatch):
    from app.core import config
    client, sessions = login_client
    server._config.raw.update(cookies="old-path", cookies_from_browser="edge")
    login = start(client)
    sessions[-1].states = [0]
    replace = config.os.replace
    failing = [True]

    def replace_config(source, target):
        if failing[0] and target == paths.default_config_path():
            raise OSError(SECRET)
        return replace(source, target)

    monkeypatch.setattr(config.os, "replace", replace_config)
    failed = poll(client, login["session_id"])
    assert failed.status_code == 503
    assert SECRET not in failed.text
    assert server._config.get("cookies_from_browser") == "edge"
    failing[0] = False
    assert poll(client, login["session_id"]).json()["status"] == "connected"
    assert len(sessions[-1].calls) == 2
