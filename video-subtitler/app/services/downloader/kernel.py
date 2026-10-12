"""下载内核（MVP 版）：Task/Job 模型 + 协议识别 + 速度看门狗。

与架构 v3 的对应关系：
- Job 记录随下载创建（MVP 阶段 yt-dlp 内部完成音视频双流与合并，
  故一个任务先记一条 merged Job；接入 aria2c RPC 直连后拆分为
  video/audio 两条 Job 并行，接口已预留 kind 字段）。
- 速度看门狗 MVP 只做采样与「停滞」判定并写日志；自动切换后端在
  adaptive 策略成熟后接入（切换点在 EngineRouter.download 内）。
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass

from ...models.db import Store

log = logging.getLogger(__name__)

# 视为分片流的协议前缀（这类流走 yt-dlp -N 并发，不适合 aria2c 分段）
FRAGMENT_PROTOCOLS = ("m3u8", "dash")


@dataclass
class DownloadJob:
    """一次下载单元（v3：业务 Task 与内核 Job 分离）。"""

    job_id: str
    task_id: str
    kind: str = "merged"          # merged | video | audio
    protocol: str = "direct"      # direct | fragments
    backend: str = "native"       # native | aria2c
    status: str = "pending"       # pending/downloading/completed/failed/paused
    downloaded: int = 0
    total: int = 0
    speed_bps: float = 0.0


def classify_protocol(fmt: dict) -> str:
    """yt-dlp 格式 dict → 协议归类。"""
    proto = str(fmt.get("protocol", "https"))
    return "fragments" if proto.startswith(FRAGMENT_PROTOCOLS) else "direct"


class JobRecorder:
    """把下载过程记录进 download_job 表（进度/速度/后端）。"""

    def __init__(self, store: Store, task_id: str, protocol: str = "direct",
                 backend: str = "native"):
        self.job = DownloadJob(
            job_id=uuid.uuid4().hex[:12], task_id=task_id,
            protocol=protocol, backend=backend,
        )
        store.add_job(self.job.job_id, task_id, kind=self.job.kind,
                      protocol=protocol, backend=backend, url="", status="downloading")

    def update(self, store: Store, *, fraction: float = None, speed_bps: float = None,
               total: int = None, status: str = None, backend: str = None) -> None:
        fields: dict = {}
        if status:
            self.job.status = status
            fields["status"] = status
        if backend and backend != self.job.backend:
            self.job.backend = backend
            fields["backend"] = backend
        if speed_bps is not None:
            self.job.speed_bps = float(speed_bps)
            fields["speed_bps"] = float(speed_bps)
        if total is not None and total > 0:
            self.job.total = int(total)
            fields["total"] = int(total)
        if fraction is not None and self.job.total:
            self.job.downloaded = int(self.job.total * fraction)
            fields["downloaded"] = self.job.downloaded
        if fields:
            store.update_job(self.job.job_id, **fields)


class SpeedMonitor:
    """速度看门狗（MVP：滑动窗口采样 + 停滞判定）。"""

    WINDOW = 5  # 采样窗口大小

    def __init__(self, floor_kbps: float = 500):
        self._floor = floor_kbps * 1024
        self._samples: deque[float] = deque(maxlen=self.WINDOW)
        self._last = time.monotonic()

    def sample(self, speed_bps: float) -> None:
        now = time.monotonic()
        if now - self._last >= 1.0:  # 每秒最多采一次
            self._samples.append(speed_bps or 0.0)
            self._last = now

    def is_stalled(self) -> bool:
        """连续窗口内速度都低于阈值 → 判定停滞（触发自适应切换的信号）。"""
        return (len(self._samples) == self.WINDOW
                and all(s < self._floor for s in self._samples))
