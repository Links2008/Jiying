"""API 实例的配置、存储、事件与写操作生命周期。"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path

from fastapi import HTTPException

from ..core.config import Config
from ..core.errors import AppError, Codes
from ..core.events import EventBus
from ..core.workers import WorkerPool
from ..models.db import Store
from ..pipeline.runner import PipelineRunner
from ..services.engines.router import EngineRouter
from .components import Components

log = logging.getLogger(__name__)


class EngineRuntime:
    def __init__(self, config: Config, data_root: Path, ensure_aria2c: Callable[[Config], None]):
        self.config = config
        self.data_root = data_root
        self.ensure_aria2c = ensure_aria2c
        self.events = EventBus()
        self.store = Store(data_root / "tasks.db")
        self.router = EngineRouter(config, self.store)
        self.jobs = WorkerPool("jiying-task", 4, 32)
        self.maintenance = WorkerPool("jiying-maintenance", 2, 8)
        self.runner = PipelineRunner(config, self.store, bus=self.events)
        self.runner.submit_render = self.jobs.submit
        self.components = Components(config, self.runner, self.maintenance)
        self.submit_lock = threading.Lock()
        self.deletion_lock = threading.Lock()
        self.config_lock = threading.RLock()
        self.exit_lock = threading.Lock()
        self.terminal_operations: set[str] = set()
        self.exiting = False
        self.active_writes = 0
        self.submit_job = self.jobs.submit
        self._aria_pending = threading.Lock()

    def refresh_aria2c(self):
        if not self._aria_pending.acquire(blocking=False):
            return

        def refresh():
            try:
                self.ensure_aria2c(self.config)
            finally:
                self._aria_pending.release()

        try:
            self.maintenance.submit(refresh)
        except RuntimeError:
            self._aria_pending.release()
            log.warning("aria2c 更新暂未入队")

    def close(self):
        self.exiting = True
        from ..core import cancel as cancellation
        active = [task["id"] for task in self.store.list_tasks()
                  if task["status"] in {"pending", "created", "running"}]
        for task_id in active:
            cancellation.cancel(task_id)
        self.jobs.close()
        self.maintenance.close()
        self.store.recover_stale_running()
        for task_id in active:
            cancellation.clear(task_id)
        self.runner.close()
        self.store.close()

    def start_task(self, task_id: str, previous: dict | None = None) -> None:
        try:
            self.submit_job(self.run_task, task_id)
        except Exception as exc:
            if previous is None:
                error = AppError(Codes.ENG_FAILED, "无法启动后台任务，请稍后重试", retryable=True)
                fields = dict(status="failed", error=json.dumps(error.to_dict(), ensure_ascii=False))
            else:
                fields = dict(status=previous["status"], error=previous.get("error", ""),
                              finished_at=previous.get("finished_at", ""))
            self.store.finish_task(task_id, **fields)
            self.runner._emit(type="task", task_id=task_id, status=fields["status"],
                          message=fields["error"])
            log.exception("后台任务线程启动失败：%s", task_id)
            raise HTTPException(503, "无法启动后台任务，请稍后重试") from exc

    @contextmanager
    def terminal_operation(self, task: dict, busy_message: str):
        tid = task["id"]
        with self.submit_lock:
            if self.exiting:
                raise HTTPException(503, "本地引擎正在退出")
            if tid in self.terminal_operations:
                raise HTTPException(409, busy_message)
            self.terminal_operations.add(tid)
            try:
                current = self.store.claim_terminal_task_snapshot(tid)
                if current is None:
                    raise HTTPException(409, busy_message)
                task = current
            except Exception:
                self.terminal_operations.discard(tid)
                raise
        try:
            yield task
        finally:
            with self.submit_lock:
                try:
                    self.store.finish_task(tid, status=task["status"], error=task.get("error", ""),
                                       finished_at=task.get("finished_at", ""))
                finally:
                    self.terminal_operations.discard(tid)

    def run_task(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task:
            try:
                self.runner.run(task)
            except Exception:
                pass  # 流水线负责写入终态。
