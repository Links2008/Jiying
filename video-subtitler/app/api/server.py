"""FastAPI 装配与本机引擎进程入口。"""
from __future__ import annotations

import logging
import os
import asyncio
from contextlib import asynccontextmanager
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core.config import load_config
from ..core.logging import setup_logging
from ..core.paths import app_root, data_dir
from .components import component_routes
from .media_routes import media_routes
from .runtime import EngineRuntime
from .schemas import ClearDoneIn, SubtitleIn, TaskIn
from .session_routes import install_local_guard, session_routes
from .settings_routes import settings_routes
from .task_routes import task_routes

__all__ = ["ClearDoneIn", "SubtitleIn", "TaskIn", "create_app", "main"]

log = logging.getLogger(__name__)


def _ensure_aria2c(config) -> None:
    """在 HTTP 服务进程中准备内置下载后端。"""
    from ..services.downloader.aria2c import ensure_managed_rpc
    ensure_managed_rpc(config)


def create_app(*, session_token: str | None = None, request_exit: Callable[[], None] | None = None) -> FastAPI:
    """构建 FastAPI 应用（供 uvicorn 或历史桌面壳复用）。"""
    setup_logging()
    config = load_config(migrate_credentials=session_token is not None)
    config.aria2_managed = True
    # HF 镜像端点（国内网络加速/解锁 Whisper 模型下载，如 https://hf-mirror.com）：
    # huggingface_hub 在首次导入时读取该环境变量，须赶在模型加载之前设置
    hf_ep = str(config.get("transcriber.hf_endpoint", "") or "").strip()
    if hf_ep:
        os.environ["HF_ENDPOINT"] = hf_ep
        if "huggingface.co" not in hf_ep:
            # 镜像站通过普通 HTTP 提供模型文件。
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        log.info("HF 模型仓库端点: %s", hf_ep)
    runtime = EngineRuntime(config, data_dir(), _ensure_aria2c)
    from ..pipeline import runner as runner_module
    runner_module.set_max_concurrent(config.get("pipeline.max_concurrent", 2))

    # 启动恢复：上次进程退出时遗留的 running/pending 是僵尸任务
    try:
        _recovered = runtime.store.recover_stale_running()
        if _recovered:
            print(f"[startup] 已将 {_recovered} 个僵尸任务标记为 interrupted（可在已完成列表重试）")
    except Exception as e:
        print(f"[startup] 僵尸任务恢复失败: {e}")

    from ..services import ytdlp_updater

    @asynccontextmanager
    async def lifespan(_app):
        try:
            ytdlp_updater.activate_vendored()
        except Exception as error:
            log.warning("挂载热更新 yt-dlp 失败（使用内置版本）：%s", error)
        runtime.refresh_aria2c()
        if config.get("update.yt_dlp_auto", True):
            runtime.maintenance.submit(ytdlp_updater.auto_update, config)
        try:
            yield
        finally:
            await asyncio.to_thread(runtime.close)

    app = FastAPI(title="VideoSubtitler", version=__version__, lifespan=lifespan)
    app.state.runtime = runtime
    app.include_router(session_routes(runtime, session_token, request_exit))
    app.include_router(task_routes(runtime))
    app.include_router(media_routes(runtime))
    app.include_router(settings_routes(runtime))
    app.include_router(component_routes(runtime))
    install_local_guard(app, runtime, session_token)

    web_dir = Path(__file__).resolve().parents[1] / "web"
    if (web_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


def _port_free(port: int) -> bool:
    """端口空闲判定：Windows 上绑定试探不可靠（SO_REUSEADDR 允许双绑），
    唯一可靠的探测是真实发起一次 TCP 连接 —— 能连上 = 被占用。"""
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _pick_port(preferred: int = 8765) -> int:
    """从首选端口起顺延找真正无冲突的端口。"""
    for port in range(preferred, preferred + 20):
        if _port_free(port):
            return port
    raise RuntimeError("8765~8784 均被占用，未找到可用端口")


def main() -> None:
    import uvicorn  # 可选依赖，延迟导入
    import sys
    from .local_session import exclusive_data_dir, source_session_token, windows_session

    if getattr(sys, "frozen", False) and os.name == "nt":
        import socket

        with exclusive_data_dir(data_dir()), socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            with windows_session(app_root(), port) as credential:
                def request_exit():
                    server.should_exit = True

                app = create_app(session_token=credential, request_exit=request_exit)
                server = uvicorn.Server(uvicorn.Config(app, access_log=False, proxy_headers=False,
                                                       timeout_graceful_shutdown=3))
                server.run(sockets=[listener])
        return

    # 源码服务由本机开发宿主提供凭据，保留历史调试端口。
    credential = source_session_token()
    port = _pick_port()
    print(f"VideoSubtitler 服务已启动：http://127.0.0.1:{port}")  # run 阻塞，须先打印
    # access_log 关闭：前端每 2.5s 轮询一次任务列表，访问日志纯属噪音与磁盘负担
    uvicorn.run(create_app(session_token=credential), host="127.0.0.1", port=port,
                access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
