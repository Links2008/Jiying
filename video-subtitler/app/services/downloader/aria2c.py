"""aria2c JSON-RPC 客户端 + 直达流下载后端。

适用边界（与架构 v3 一致）：aria2c 只处理 HTTP(S) 直达流——
- 单文件直链（generic mp4）：单 Job 多连接分段；
- DASH 拆流（video-only + audio-only）：两条 Job 并行下载后 ffmpeg 合并
  （音视频并行 + 各自多连接，这是对 YouTube 类站点最大的提速点）；
- HLS/DASH 分片清单仍由 yt-dlp -N 并发处理（aria2c 不理解播放列表）。
凭据透传：headers（UA/Referer/Cookie）由 yt-dlp 探测结果带入 --header。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ...core.errors import AppError, Codes
from ...core.paths import bin_dir
from ..engines.base import DirectTarget, DownloadProgress

log = logging.getLogger(__name__)
_default_rpc_url = "http://127.0.0.1:6800/jsonrpc"
_startup_lock = threading.Lock()


class _NoRpcRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_local_rpc_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRpcRedirect())


class Aria2cRpcClient:
    """aria2.addUri / tellStatus / remove 的最小 JSON-RPC 2.0 封装（标准库实现）。"""

    def __init__(self, rpc_url: str, secret: str = "", timeout: float = 10.0, *, managed: bool = False):
        self._url = rpc_url
        self._secret = secret
        self._timeout = timeout
        self._counter = 0
        self._open = _local_rpc_opener.open if managed else urllib.request.urlopen

    def _call(self, method: str, params: list) -> object:
        self._counter += 1
        payload = {"jsonrpc": "2.0", "id": f"vs-{self._counter}",
                   "method": f"aria2.{method}", "params": params}
        if self._secret:
            payload["params"] = [f"token:{self._secret}"] + params
        req = urllib.request.Request(
            self._url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise AppError(Codes.NET_FAILED, f"aria2c RPC 调用失败：{method}",
                           detail=str(e), retryable=True) from e
        if not isinstance(data, dict):
            raise AppError(Codes.NET_FAILED, "aria2c RPC 回复无效")
        if "error" in data:
            error = data["error"]
            message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
            if self._secret:
                message = message.replace(self._secret, "[凭据已隐藏]")
            raise AppError(Codes.NET_FAILED, f"aria2c RPC 错误：{message}")
        return data.get("result")

    # ---- JSON-RPC 方法 ----
    def get_version(self) -> dict:
        response = self._call("getVersion", [])
        if not isinstance(response, dict) or not response.get("version"):
            raise AppError(Codes.NET_FAILED, "aria2c RPC 版本回复无效")
        return response

    def add_uri(self, uri: str, options: dict) -> str:
        return str(self._call("addUri", [[uri], options]))

    def tell_status(self, gid: str) -> dict:
        return dict(self._call("tellStatus", [gid]) or {})

    def remove(self, gid: str) -> None:
        self._call("remove", [gid])


def _use_external(ac: dict) -> bool:
    mode = ac.get("mode", "auto")
    return mode == "external" or (mode == "auto" and
                                 (ac.get("secret") or ac.get("rpc_url", _default_rpc_url) != _default_rpc_url))


def ensure_managed_rpc(config) -> None:
    """每个引擎只启动一个内置子进程，握手成功后才发布给下载器。"""
    with _startup_lock:
        ac = config.get("download.aria2c", {})
        if not config.aria2_managed or not ac.get("enabled") or _use_external(ac):
            return
        current = config.aria2_runtime
        if current is not None and current[2].poll() is None:
            return
        config.aria2_runtime = None
        executable = bin_dir() / ("aria2c.exe" if os.name == "nt" else "aria2c")
        if not executable.is_file():
            return
        # 端口如在释放后被占用，只能通过携带本次随机密钥的 RPC 握手。
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        credential = secrets.token_hex(32)
        url = f"http://127.0.0.1:{port}/jsonrpc"
        try:
            process = subprocess.Popen(
                [str(executable), "--no-conf=true", "--enable-rpc", f"--rpc-listen-port={port}",
                 "--rpc-listen-all=false", f"--rpc-secret={credential}", "--quiet=true",
                 f"--stop-with-process={os.getpid()}"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            log.warning("内置 aria2c 未能启动，将使用原生下载")
            return
        client = Aria2cRpcClient(url, credential, timeout=.25, managed=True)
        deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < deadline:
            try:
                client.get_version()
                config.aria2_runtime = (url, credential, process)
                return
            except AppError:
                time.sleep(.05)
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        log.warning("内置 aria2c 未能就绪，将使用原生下载")


class Aria2cBackend:
    """提交直达流给 aria2c 并轮询进度；失败抛 AppError 由上层降级。"""

    def __init__(self, config, client: Aria2cRpcClient | None = None):
        self._cfg = config
        ac = config.get("download.aria2c", {})
        runtime = getattr(config, "aria2_runtime", None)
        if client is not None:
            self._client = client
        elif not getattr(config, "aria2_managed", False) or _use_external(ac):
            self._client = Aria2cRpcClient(str(ac.get("rpc_url", _default_rpc_url)), str(ac.get("secret") or ""))
        elif runtime is not None and runtime[2].poll() is None:
            self._client = Aria2cRpcClient(runtime[0], runtime[1], managed=True)
        else:
            self._client = None

    def ping(self) -> bool:
        """RPC 可达性预检（不可达时上层直接走 yt-dlp 原生路径）。"""
        if self._client is None:
            return False
        try:
            self._client.get_version()
            return True
        except AppError as e:
            log.info("aria2c RPC 预检失败：%s", e)
            return False

    # ---- 单流下载（直链）----
    def download(self, target: DirectTarget, dest_dir: Path, on_progress=None,
                 timeout: float = 7200.0, cancel_check=None) -> Path:
        outs = self.download_many([target], dest_dir, on_progress, timeout,
                                  cancel_check=cancel_check)
        return outs[0]

    # ---- 多流并行（音视频拆分双 Job）----
    def download_many(self, targets: list[DirectTarget], dest_dir: Path,
                      on_progress=None, timeout: float = 7200.0,
                      cancel_check=None) -> list[Path]:
        """并行提交多条流（aria2c 内部各自多连接），聚合进度，全部完成后返回。"""
        if self._client is None:
            raise AppError(Codes.NET_FAILED, "内置 aria2c 尚未就绪")
        ac = self._cfg.get("download.aria2c", {})
        dest_dir.mkdir(parents=True, exist_ok=True)
        jobs: list[tuple[DirectTarget, str, Path]] = []
        def stop_jobs():
            for _, gid, _ in jobs:
                try:
                    self._client.remove(gid)
                except AppError:
                    pass

        completed = False
        try:
            for t in targets:
                if cancel_check and cancel_check():
                    raise AppError(Codes.TASK_CANCELLED, "任务已取消")
                gid = self._client.add_uri(t.url, self._options(t, dest_dir, ac))
                jobs.append((t, gid, dest_dir / t.filename))
            log.info("aria2c 已接收 %d 条下载：%s", len(jobs),
                     ", ".join(t.filename for t, _, _ in jobs))

            poll = float(ac.get("poll_interval", 0.5))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cancel_check and cancel_check():
                    raise AppError(Codes.TASK_CANCELLED, "任务已取消")
                stats = [self._client.tell_status(gid) for _, gid, _ in jobs]
                done = sum(int(s.get("completedLength") or 0) for s in stats)
                total = sum(int(s.get("totalLength") or 0) for s in stats)
                speed = sum(float(s.get("downloadSpeed") or 0) for s in stats)
                eta = (total - done) / speed if speed > 0 and total > done else 0.0
                if on_progress:
                    on_progress(DownloadProgress(
                        fraction=(done / total) if total else 0.0,
                        speed_bps=speed, eta=eta, backend="aria2c",
                        note=f"aria2c {len(jobs)} 流并行"))
                for s, (t, _, _) in zip(stats, jobs):
                    if s.get("status") in ("error", "removed"):
                        raise AppError(Codes.ENG_FAILED,
                                       f"aria2c 下载失败（{t.kind}）："
                                       f"{s.get('errorMessage') or s.get('status')}",
                                       retryable=True)
                if all(s.get("status") == "complete" for s in stats):
                    if cancel_check and cancel_check():
                        raise AppError(Codes.TASK_CANCELLED, "任务已取消")
                    missing = [str(o) for _, _, o in jobs if not o.exists()]
                    if missing:
                        raise AppError(Codes.ENG_FAILED, "aria2c 报告完成但产物缺失",
                                       detail="; ".join(missing))
                    for s, (_, _, output) in zip(stats, jobs):
                        size = output.stat().st_size
                        expected = int(s.get("totalLength") or 0)
                        received = int(s.get("completedLength") or 0)
                        if not size or (expected and size != expected) or (received and size != received):
                            raise AppError(Codes.ENG_FAILED, "aria2c 报告完成但文件不完整",
                                           detail=f"{output.name}：预期 {expected} 字节，实际 {size} 字节",
                                           retryable=True)
                    completed = True
                    return [o for _, _, o in jobs]
                time.sleep(poll)
            raise AppError(Codes.NET_TIMEOUT, "aria2c 下载超时", retryable=True)
        finally:
            if not completed:
                stop_jobs()  # 包括进度回调抛出的取消，所有异常退出都撤回远端作业。

    # ---- helpers ----
    def _options(self, target: DirectTarget, dest_dir: Path, ac: dict) -> dict:
        options = {
            "dir": str(dest_dir),
            "out": target.filename,
            "max-connection-per-server": str(int(ac.get("max_conn_per_server", 16))),
            "split": str(int(ac.get("split", 16))),
            "min-split-size": str(ac.get("min_split_size", "1M")),
            "file-allocation": "none",
            "continue": "true",            # .aria2 控制文件断点续传
            "retry-wait": "2",
            "max-tries": "3",
            "auto-file-renaming": "false",
            "allow-overwrite": "true",
            # 按实时带宽动态调整分段数，尽量跑满带宽
            "optimize-concurrent-downloads": "true",
        }
        skip = {"accept-encoding", "content-length", "host", "connection"}
        headers = [f"{k}: {v}" for k, v in (target.headers or {}).items()
                   if k.lower() not in skip]
        if headers:
            options["header"] = headers
        mode = self._cfg.get("download.proxy_mode", "system")
        if mode == "custom" and self._cfg.get("download.custom_proxy", ""):
            options["all-proxy"] = self._cfg.get("download.custom_proxy")
        elif mode != "direct" and self._cfg.get("proxy", ""):
            options["all-proxy"] = self._cfg.get("proxy")
        # direct 模式不传 all-proxy：aria2c 默认不走系统代理，本身就是直连
        return options
