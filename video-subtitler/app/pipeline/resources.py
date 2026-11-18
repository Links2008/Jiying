"""跨任务共享的下载、编码和产物发布资源。"""
import re
import threading
from contextlib import contextmanager

from ..core import cancel as task_cancel

_PUBLISH_LOCK = threading.Lock()

_POOLS = {
    "io": threading.Semaphore(3),
    "gpu": threading.Semaphore(1),
    "cpu": threading.Semaphore(2),
}


@contextmanager
def resource_slot(kind: str, task_id: str):
    """资源繁忙时仍轮询取消；仅释放实际取得的名额。"""
    resource = _POOLS[kind]
    task_cancel.raise_if_cancelled(task_id)
    while not resource.acquire(timeout=.2):
        task_cancel.raise_if_cancelled(task_id)
    try:
        task_cancel.raise_if_cancelled(task_id)
        yield
    finally:
        resource.release()


@contextmanager
def publication_slot(task_id: str):
    """等待其他长视频发布时仍响应取消，仅释放自己取得的锁。"""
    task_cancel.raise_if_cancelled(task_id)
    while not _PUBLISH_LOCK.acquire(timeout=.2):
        task_cancel.raise_if_cancelled(task_id)
    try:
        task_cancel.raise_if_cancelled(task_id)
        yield
    finally:
        _PUBLISH_LOCK.release()


def safe_name(title: str, fallback: str = "video") -> str:
    """标题 → 文件名安全片段。"""
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (title or "").strip())
    return (name or fallback)[:80]
