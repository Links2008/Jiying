"""进程内事件总线：任务/阶段/进度事件的发布订阅。

消费者：FastAPI SSE 端点（/api/v1/events）、未来的桌面壳与通知器。
设计取舍：慢消费者队列满时丢弃事件保活（进度事件天然可丢），不阻塞生产者。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass, field


@dataclass
class AppEvent:
    """统一事件：type=task 任务级 / stage 阶段级 / progress 流式进度。"""

    type: str
    task_id: str = ""
    stage: str = ""          # download/extract/transcribe/translate/render
    status: str = ""         # created/running/done/failed/completed
    message: str = ""        # 任务说明或阶段错误，供状态通知携带
    fraction: float = 0.0    # progress 专用：0~1
    speed_bps: float = 0.0
    eta: float = 0.0         # progress 专用：预计剩余秒数（0 = 未知）
    backend: str = ""        # native | aria2c
    note: str = ""           # 当前动作说明，如 "aria2c 双流并行"
    ts: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        return f"data: {json.dumps(asdict(self), ensure_ascii=False)}\n\n"


class EventBus:
    def __init__(self, max_queue: int = 500):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._max = max_queue

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._max)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: AppEvent) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # 慢消费者丢事件而非阻塞流水线


# 进程级单例：CLI 与 API 服务共用
bus = EventBus()
