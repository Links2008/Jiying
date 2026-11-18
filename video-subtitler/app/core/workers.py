"""带队列上限和明确生命周期的后台执行资源。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)


class WorkerPool:
    def __init__(self, name: str, workers: int, queued: int):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._slots = threading.BoundedSemaphore(workers + queued)
        self._lock = threading.Lock()
        self._outstanding = 0

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._outstanding > 0

    def submit(self, operation, /, *args):
        if not self._slots.acquire(blocking=False):
            raise RuntimeError("后台队列已满，请等待现有操作完成")
        with self._lock:
            self._outstanding += 1
        try:
            future = self._pool.submit(operation, *args)
        except BaseException:
            self._release()
            raise
        future.add_done_callback(self._finished)
        return future

    def _release(self):
        with self._lock:
            self._outstanding -= 1
        self._slots.release()

    def _finished(self, future):
        try:
            if not future.cancelled() and (error := future.exception()) is not None:
                log.error("后台操作失败", exc_info=error)
        finally:
            self._release()

    def close(self, *, wait: bool = True):
        self._pool.shutdown(wait=wait, cancel_futures=True)
