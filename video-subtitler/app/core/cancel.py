"""任务取消协调：跨线程的取消事件 + 运行中子进程注册表。

- cancel(task_id)：置位取消事件，并立即终止该任务登记的 ffmpeg/aria2c 子进程；
- is_cancelled(task_id)：流水线在阶段边界、下载进度回调、转写回调里轮询，
  一旦置位就在最近的安全点抛出 AppError(E-TASK-402) 终止任务。
"""
from __future__ import annotations

import threading

from .errors import AppError, Codes

_lock = threading.Lock()
_events: dict[str, threading.Event] = {}
_procs: dict[str, list] = {}


def _event(task_id: str) -> threading.Event:
    with _lock:
        return _events.setdefault(task_id, threading.Event())


def is_cancelled(task_id: str) -> bool:
    with _lock:
        event = _events.get(task_id)
    return event is not None and event.is_set()


def register(task_id: str, proc) -> None:
    """登记一个正在运行的子进程（Popen），供取消时终止。"""
    with _lock:
        _procs.setdefault(task_id, []).append(proc)
        event = _events.get(task_id)
        cancelled = event is not None and event.is_set()
    if cancelled:
        try:
            proc.kill()
        except OSError:
            pass


def pop(task_id: str, proc=None) -> None:
    """子进程结束后移除登记。"""
    with _lock:
        if proc is None:
            _procs.pop(task_id, None)
        elif task_id in _procs:
            try:
                _procs[task_id].remove(proc)
            except ValueError:
                pass


def cancel(task_id: str) -> None:
    """请求取消：置位事件 + 终止所有已登记子进程。"""
    _event(task_id).set()
    with _lock:
        procs = list(_procs.get(task_id, []))
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


def clear(task_id: str) -> None:
    """任务结束（完成/失败/取消）后清理状态，防内存泄漏。"""
    with _lock:
        _events.pop(task_id, None)
        _procs.pop(task_id, None)


def raise_if_cancelled(task_id: str) -> None:
    """便捷检查：已取消则抛出统一取消异常。"""
    if is_cancelled(task_id):
        raise AppError(code=Codes.TASK_CANCELLED, message="任务已取消",
                       suggestion="如需重新下载请重新提交任务")
