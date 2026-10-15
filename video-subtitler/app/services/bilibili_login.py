"""B 站网页扫码登录：后端保管临时密钥，前端只接收 PNG 与状态。"""
from __future__ import annotations

import base64
import io
import os
import tempfile
import threading
import time
import uuid
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import getproxies

from ..core.errors import AppError, Codes

_BASE = "https://passport.bilibili.com/x/passport-login/web/qrcode/"
_MESSAGES = {"pending": "请用哔哩哔哩 App 扫码", "scanned": "已扫码，请在手机上确认登录",
             "expired": "二维码已过期，请重新获取", "connected": "B 站登录成功，正在刷新可用画质"}


class BilibiliLogin:
    def __init__(self, config, cookie_path: Path, on_connected):
        self._config = config
        self._cookie_path = cookie_path
        self._on_connected = on_connected
        self._lock = threading.Lock()
        self._http = None
        self._session_id = ""
        self._key = ""
        self._deadline = 0.0
        self._status = "expired"
        self._confirmed = False

    def _close(self):
        if self._http is not None:
            self._http.close()
            self._http = None
        self._key = ""

    def _request(self, endpoint, **kwargs):
        import requests

        try:
            response = self._http.get(_BASE + endpoint, timeout=15, **kwargs)
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError):
            # 异常 URL 可能带扫码密钥，禁止向 API 或日志转发原异常文本。
            raise AppError(Codes.NET_FAILED, "无法连接 B 站登录服务，请检查网络和代理后重试",
                           retryable=True) from None
        if not isinstance(body, dict) or body.get("code") != 0 or not isinstance(body.get("data"), dict):
            raise AppError(Codes.AUTH_NEED_COOKIES, "B 站登录服务暂时不可用，请重试", retryable=True)
        return body["data"]

    def _result(self, status):
        return {"status": status, "message": _MESSAGES[status]}

    def start(self):
        import requests
        import qrcode
        from qrcode.image.pil import PilImage

        with self._lock:
            self._close()
            self._status = "expired"
            self._session_id = ""
            self._confirmed = False
            self._http = requests.Session()
            self._http.trust_env = False
            self._http.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com/"})
            mode = self._config.get("download.proxy_mode", "system")
            if mode != "direct":
                proxy = self._config.get("download.custom_proxy", "") if mode == "custom" else ""
                proxy = proxy or self._config.get("proxy", "")
                if proxy:
                    self._http.proxies.update({"http": proxy, "https": proxy})
                else:
                    self._http.proxies.update(getproxies())
            self._deadline = time.monotonic() + 180
            try:
                data = self._request("generate")
                url, key = data.get("url", ""), data.get("qrcode_key", "")
                if not isinstance(url, str) or not isinstance(key, str) or not url or not key:
                    raise AppError(Codes.AUTH_NEED_COOKIES, "B 站返回的二维码无效，请重试", retryable=True)
                try:
                    parsed = urlsplit(url)
                    host = parsed.hostname or ""
                except ValueError:
                    raise AppError(Codes.AUTH_NEED_COOKIES, "B 站返回的二维码无效，请重试", retryable=True) from None
                if parsed.scheme != "https" or not host.endswith(".bilibili.com"):
                    raise AppError(Codes.AUTH_NEED_COOKIES, "B 站返回的二维码无效，请重试", retryable=True)
                qr = qrcode.QRCode(box_size=6, border=4)
                qr.add_data(url)
                image = io.BytesIO()
                qr.make_image(image_factory=PilImage).save(image, format="PNG")
            except Exception:
                self._close()
                raise
            self._key = key
            self._session_id = uuid.uuid4().hex
            self._status = "pending"
            return self._result("pending") | {
                "session_id": self._session_id,
                "image_base64": base64.b64encode(image.getvalue()).decode("ascii")}

    def poll(self, session_id: str):
        with self._lock:
            if not session_id or session_id != self._session_id:
                return self._result("expired")
            if self._status == "connected":
                return self._result("connected")
            if self._confirmed:
                # 手机已确认；仅重试本地持久化，避免再次扫码或消费已完成的远端会话。
                self._save_cookies()
                self._status = "connected"
                self._close()
                return self._result("connected")
            if self._status == "expired" or time.monotonic() >= self._deadline:
                self._status = "expired"
                self._close()
                return self._result("expired")
            data = self._request("poll", params={"qrcode_key": self._key})
            code = data.get("code")
            if code == 0:
                self._save_cookies()
                self._status = "connected"
                self._close()
            elif code in (86101, 86090, 86038):
                self._status = {86101: "pending", 86090: "scanned", 86038: "expired"}[code]
                if self._status == "expired":
                    self._close()
            else:
                raise AppError(Codes.AUTH_NEED_COOKIES, "B 站登录状态异常，请重新获取二维码", retryable=True)
            return self._result(self._status)

    def _save_cookies(self):
        cookies = [c for c in self._http.cookies if c.domain.lstrip(".") == "bilibili.com"
                   or c.domain.endswith(".bilibili.com")]
        if not any(c.name == "SESSDATA" and c.value for c in cookies):
            raise AppError(Codes.AUTH_NEED_COOKIES, "未取得 B 站登录凭据，请重新扫码", retryable=True)
        self._confirmed = True
        temporary = None
        try:
            self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self._cookie_path.parent, suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
            jar = MozillaCookieJar(str(temporary))
            for cookie in cookies:
                jar.set_cookie(cookie)
            jar.save(ignore_discard=True, ignore_expires=True)
            os.replace(temporary, self._cookie_path)
            self._on_connected(self._cookie_path)
        except Exception:
            raise AppError(Codes.DISK_FAILED, "登录凭据保存失败，请重试", retryable=True) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
