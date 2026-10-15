#!/usr/bin/env python3
"""VideoSubtitler 桌面入口 —— pywebview 窗口 + 内嵌 API 服务。

py webview 未安装时回退到默认浏览器（服务照常本机启动）。
"""
from __future__ import annotations

import socket
import threading
import time
import webbrowser

import uvicorn

from app.api.server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

    try:
        import webview  # pywebview：Windows 上走系统自带 WebView2
        webview.create_window("VideoSubtitler", url,
                              width=1100, height=780, min_size=(860, 600))
        print(f"VideoSubtitler 桌面窗口已启动：{url}")
        webview.start()
    except ImportError:
        print(f"未安装 pywebview，使用默认浏览器打开：{url}"
              "（桌面窗口体验：pip install pywebview）")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)  # 保持服务常驻，Ctrl+C 退出
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
