"""备用引擎：you-get / lux —— yt-dlp 解析失败时兜底国内站点。

两者都是外部命令行工具：开发态需自行安装（pip install you-get /
下载 lux 单文件），打包版可随 bin/ 分发。MVP 仅实现下载直通，
probe 返回 None（不参与分辨率列表），由 yt-dlp 独占探测职责。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ...core import cancel as task_cancel
from ...core.errors import AppError, Codes
from .base import DownloadProgress, EngineProvider, ProbeResult

log = logging.getLogger(__name__)


def _run_captured(cmd: list[str], task_id: str | None,
                  timeout: float) -> tuple[int, str]:
    """执行外部命令，返回 (退出码, 合并输出)。

    与 renderer._run 同源的轮询等待：真正可取消、可超时。旧实现用
    subprocess.run(timeout=7200) 一次性阻塞，期间无法响应取消请求。
    输出落临时文件而非 PIPE：轮询期间无人排空管道，外部工具写满 64KB
    缓冲后会把自己堵死。
    """
    with tempfile.TemporaryFile() as outf:
        proc = subprocess.Popen(
            cmd, stdout=outf, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if task_id:
            task_cancel.register(task_id, proc)
        deadline = time.monotonic() + timeout
        try:
            while True:
                ret = proc.poll()
                if ret is not None:
                    # 先判取消再判退出码：取消会 kill 进程并走到这里
                    if task_id:
                        task_cancel.raise_if_cancelled(task_id)
                    outf.seek(0)
                    return ret, outf.read().decode("utf-8", errors="replace")
                if task_id:
                    task_cancel.raise_if_cancelled(task_id)
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait(timeout=10)
                    raise AppError(
                        Codes.NET_TIMEOUT,
                        f"外部下载器超过 {int(timeout)} 秒仍未完成",
                        retryable=True,
                        suggestion="调大 download.subprocess_timeout，"
                                   "或改用主引擎 yt-dlp")
                time.sleep(0.2)
        finally:
            if task_id:
                task_cancel.pop(task_id, proc)


class _SubprocessEngine(EngineProvider):
    """you-get / lux 的公共骨架：探测不支持（None），下载直通外部命令。"""

    name = "subprocess"
    _cmd: str = ""
    _install_hint: str = ""

    def __init__(self, config):
        self._cfg = config

    def available(self) -> bool:
        return shutil.which(self._cmd) is not None

    def probe(self, url: str) -> ProbeResult | None:
        # 备用引擎不参与探测（无法稳定解析格式列表），只做下载兜底
        return None

    def download(self, url: str, resolution: str, dest_dir: Path,
                 on_progress=None, task_id: str | None = None) -> list[Path]:
        if not self.available():
            raise AppError(
                Codes.DEP_MISSING, f"备用引擎 {self._cmd} 不可用",
                suggestion=self._install_hint)
        dest_dir.mkdir(parents=True, exist_ok=True)
        cmd = [self._cmd, *self._extra_args(resolution), "-o", str(dest_dir), url]
        log.info("备用引擎执行：%s", " ".join(cmd))
        if on_progress:
            # 外部工具只吐文本日志，拿不到字节级进度：先推一条事件让前端
            # 从空白/0% 动起来，而不是一直显示「无动静」
            on_progress(DownloadProgress(
                fraction=0.0, speed_bps=0.0, backend=self.name,
                note=f"{self._cmd} 执行中（外部下载器不提供细粒度进度）"))
        timeout = float(self._cfg.get("download.subprocess_timeout", 1800) or 1800)
        code, output = _run_captured(cmd, task_id=task_id, timeout=timeout)
        if code != 0:
            raise AppError(Codes.ENG_FAILED, f"{self._cmd} 退出码 {code}",
                           detail=output[-800:], retryable=True)
        # 产物：目标目录下新出现的媒体文件
        media = sorted(dest_dir.glob("*"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        files = [p for p in media
                 if p.suffix.lower() in (".mp4", ".mkv", ".flv", ".webm", ".mp3", ".m4a")]
        if not files:
            raise AppError(Codes.ENG_FAILED, f"{self._cmd} 未产出可识别的媒体文件",
                           detail=output[-500:])
        return files

    def _extra_args(self, resolution: str) -> list[str]:
        return []


class YouGetEngine(_SubprocessEngine):
    name = "youget"
    _cmd = "you-get"
    _install_hint = "pip install you-get（国内站点备用引擎）"


class LuxEngine(_SubprocessEngine):
    name = "lux"
    _cmd = "lux"
    _install_hint = "从 https://github.com/iawia002/lux/releases 下载 lux 单文件并加入 PATH"

    def _extra_args(self, resolution: str) -> list[str]:
        if resolution and resolution not in ("best", ""):
            return ["-F", resolution.rstrip("pP")]  # lux -F 限流画质
        return []
