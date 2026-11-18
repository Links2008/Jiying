"""进程内只运行一个共享模型维护循环，闲置或退休的实例不驻留。"""
import logging
import threading
import weakref

log = logging.getLogger(__name__)
_condition = threading.Condition()
_tracked = weakref.WeakSet()
_thread = None


def track(provider):
    global _thread
    with _condition:
        _tracked.add(provider)
        _condition.notify_all()
        if _thread is None:
            _thread = threading.Thread(target=_run, name="jiying-model-maintenance", daemon=True)
            try:
                _thread.start()
            except RuntimeError:
                _thread = None


def forget(provider):
    with _condition:
        _tracked.discard(provider)
        if not _tracked:
            _condition.notify_all()


def wait_if_idle():
    """最后一个所有者退出时回收维护线程，其他运行时仍可使用共享循环。"""
    with _condition:
        thread = _thread
        if thread is None or thread is threading.current_thread() or _tracked:
            return
        _condition.notify_all()
        _condition.wait_for(lambda: _thread is not thread or bool(_tracked))
        if _thread is thread:
            return
    thread.join()


def _run():
    global _thread
    while True:
        with _condition:
            if _tracked:
                _condition.wait(15)
            if not _tracked:
                _thread = None
                _condition.notify_all()
                return
            members = tuple(weakref.ref(member) for member in _tracked)
        for reference in members:
            provider = reference()
            if provider is not None:
                try:
                    provider.release_idle_model()
                except Exception:
                    log.exception("检查闲置语音模型失败")
            del provider
