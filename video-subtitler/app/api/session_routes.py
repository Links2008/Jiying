"""本机发现、认证、事件流与退出协商。"""
from __future__ import annotations

import asyncio
import queue
import secrets
import time
from collections.abc import Callable

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..core.paths import app_root
from .runtime import EngineRuntime


def session_routes(runtime: EngineRuntime, session_token: str | None,
                   request_exit: Callable[[], None] | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/instance")
    def instance():
        if session_token:
            return {"engine_root": str(app_root()), "requires_token": True}
        return {"engine_root": str(app_root())}

    @router.get("/api/v1/session/health", status_code=204)
    def session_health():
        return Response(status_code=204)

    if session_token and request_exit:
        @router.post("/api/v1/session/exit", status_code=202)
        def exit_session():
            """只在任务和运行组件空闲时接受切换请求。"""
            with runtime.exit_lock, runtime.submit_lock:
                idle_status = {"completed", "failed", "cancelled", "interrupted"}
                if (runtime.active_writes or runtime.terminal_operations
                        or any(t["status"] not in idle_status for t in runtime.store.list_tasks())
                        or runtime.components.busy() or runtime.jobs.busy or runtime.maintenance.busy):
                    raise HTTPException(409, "本地引擎仍有任务，请等待任务结束")
                runtime.exiting = True
                request_exit()
            return Response(status_code=202)

    @router.get("/api/v1/events")
    async def events():
        """SSE 任务事件流，空闲时每 15 秒发送带水位的心跳。"""
        q, cursor = runtime.events.subscribe_with_sequence()

        async def gen():
            loop = asyncio.get_running_loop()
            try:
                yield ": connected\n\n"
                heartbeat = time.monotonic() + 15
                while not runtime.exiting:
                    try:
                        ev = await loop.run_in_executor(None, q.get, True, 1)
                        yield ev.to_sse()
                    except queue.Empty:
                        if time.monotonic() >= heartbeat:
                            heartbeat = time.monotonic() + 15
                            yield f": keep-alive {runtime.events.sequence}\n\n"
            finally:
                runtime.events.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no",
                                          "X-Jiying-Event-Sequence": str(cursor)})

    return router


def install_local_guard(app: FastAPI, runtime: EngineRuntime, session_token: str | None) -> None:
    @app.middleware("http")
    async def local_guard(request, call_next):
        """已配对的桌面引擎同时验证回环地址、请求来源及会话凭据。"""
        from ipaddress import ip_address
        from urllib.parse import urlsplit

        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        try:
            authority = urlsplit("//" + host)
            is_local_host = (authority.hostname in {"127.0.0.1", "localhost", "::1"}
                             and not (authority.path or authority.query or authority.fragment or authority.username))
            if (host or session_token) and not is_local_host:
                return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
            if session_token and (not request.client or not ip_address(request.client.host).is_loopback):
                return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
            if origin and (session_token or request.method not in {"GET", "HEAD", "OPTIONS"}):
                parsed = urlsplit(origin)
                if parsed.scheme != request.url.scheme or parsed.netloc.lower() != host.lower():
                    return JSONResponse({"detail": "跨站请求已拒绝"}, status_code=403)
        except ValueError:
            return JSONResponse({"detail": "无效的本机请求地址"}, status_code=403)
        discovery = request.method == "GET" and request.scope["path"] == "/api/v1/instance"
        if session_token and not discovery:
            authorization = request.headers.get("authorization", "")
            if not secrets.compare_digest(authorization.encode("utf-8"), b"Bearer " + session_token.encode("ascii")):
                return JSONResponse({"detail": {"code": "E-LOCAL-AUTH", "message": "本地引擎会话已失效，请重新连接"}},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})
        writes = request.method not in {"GET", "HEAD", "OPTIONS"} and request.scope["path"] != "/api/v1/session/exit"
        if not writes:
            return await call_next(request)
        with runtime.exit_lock:
            if runtime.exiting:
                return JSONResponse({"detail": "本地引擎正在退出"}, status_code=503)
            runtime.active_writes += 1
        try:
            return await call_next(request)
        finally:
            with runtime.exit_lock:
                runtime.active_writes -= 1
