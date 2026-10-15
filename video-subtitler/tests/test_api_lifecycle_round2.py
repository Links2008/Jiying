"""后台任务启动失败和短暂文件操作不得留下永久活跃任务或取消信号。"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import server
from app.core import cancel


def endpoint(app, path, method):
    return next(route.endpoint for route in app.routes if route.path == path and method in route.methods)


@pytest.fixture
def context(tmp_path, monkeypatch):
    app = server.create_app()
    monkeypatch.setattr(server._router, "probe", lambda _: ("fake", SimpleNamespace(title="隔离剪辑")))
    yield SimpleNamespace(app=app, store=server._store, runner=server._runner, root=tmp_path)
    for task in server._store.list_tasks():
        cancel.clear(task["id"])


def failing_threads(monkeypatch, where):
    class Thread:
        def __init__(self, **kwargs):
            if where == "constructor":
                raise RuntimeError("模拟线程构造失败")

        def start(self):
            raise RuntimeError("模拟系统无法启动线程")

    monkeypatch.setattr(server, "threading", SimpleNamespace(Thread=Thread))


@pytest.mark.parametrize("where", ["constructor", "start"])
def test_failed_submit_launch_remains_visible_and_retryable(context, monkeypatch, where):
    failing_threads(monkeypatch, where)
    submit = endpoint(context.app, "/api/v1/tasks", "POST")
    with pytest.raises(HTTPException) as error:
        submit(server.TaskIn(url="https://example.test/isolated-video"))
    assert error.value.status_code == 503
    tasks = context.store.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["status"] == "failed" and task["finished_at"]
    assert json.loads(task["error"])["retryable"] is True
    assert context.store.find_active_equivalent(task) is None
    assert context.store.claim_retry(task["id"])


@pytest.mark.parametrize("where", ["constructor", "start"])
@pytest.mark.parametrize("previous_status", ["failed", "cancelled", "interrupted", "completed"])
def test_failed_retry_launch_restores_previous_result(context, monkeypatch, where, previous_status):
    task = context.runner.create_task("https://example.test/isolated-video", "best", [], "none")
    context.store.update_task(task["id"], status=previous_status, error="原有字幕错误",
                              finished_at="此前的完成时间")
    before = context.store.get_task(task["id"])
    failing_threads(monkeypatch, where)
    retry = endpoint(context.app, "/api/v1/tasks/{tid}/retry", "POST")
    with pytest.raises(HTTPException) as error:
        retry(task["id"])
    assert error.value.status_code == 503
    assert context.store.get_task(task["id"]) == before
    assert not cancel.is_cancelled(task["id"])
    assert context.store.claim_retry(task["id"])


def terminal_task(context):
    task = context.runner.create_task("https://example.test/isolated-video", "best", [], "burn",
                                      output_dir=str(context.root / "output"))
    context.store.update_task(task["id"], status="completed", error="保留的提示",
                              finished_at="此前的完成时间")
    subtitle = context.root / "output" / "captions.srt"
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n原字幕\n", encoding="utf-8-sig")
    context.store.add_artifact(task["id"], "subtitle", str(subtitle), lang="zh")
    artifact = context.store.list_artifacts(task["id"])[0]
    return task, subtitle, artifact


@pytest.mark.parametrize("operation", ["save", "save_fail", "partial_delete", "delete", "delete_fail"])
def test_terminal_file_operation_rejects_cancel_without_poisoning_next_work(context, monkeypatch, operation):
    task, subtitle, artifact = terminal_task(context)
    before = context.store.get_task(task["id"])
    cancel_endpoint = endpoint(context.app, "/api/v1/tasks/{tid}/cancel", "POST")
    cancellation_results = []

    def attempt_cancel():
        try:
            cancel_endpoint(task["id"])
            cancellation_results.append(200)
        except HTTPException as error:
            cancellation_results.append(error.status_code)

    if operation.startswith("save"):
        replace = Path.replace

        def during_replace(path, target):
            if path.name.startswith(".subtitle-"):
                attempt_cancel()
                if operation == "save_fail":
                    raise PermissionError("模拟字幕文件被占用")
            return replace(path, target)

        monkeypatch.setattr(Path, "replace", during_replace)
        request = lambda: endpoint(context.app, "/api/v1/artifacts/{aid}/content", "PUT")(
            artifact["id"], server.SubtitleIn(text="1\n00:00:00,000 --> 00:00:01,000\n修改字幕\n"))
    elif operation == "delete_fail":
        def cleanup(tid):
            attempt_cancel()
            raise OSError("模拟任务文件清理失败")

        monkeypatch.setattr(context.runner, "cleanup_task_files", cleanup)
        request = lambda: endpoint(context.app, "/api/v1/tasks/{tid}", "DELETE")(task["id"])
    else:
        list_artifacts = context.store.list_artifacts

        def during_listing(tid):
            attempt_cancel()
            return list_artifacts(tid)

        monkeypatch.setattr(context.store, "list_artifacts", during_listing)
        if operation == "partial_delete":
            request = lambda: endpoint(context.app, "/api/v1/tasks/{tid}/artifacts", "DELETE")(
                task["id"], kind="subtitle")
        else:
            request = lambda: endpoint(context.app, "/api/v1/tasks/{tid}", "DELETE")(
                task["id"], with_files=True)

    if operation == "save_fail":
        with pytest.raises(HTTPException) as error:
            request()
        assert error.value.status_code == 500
    elif operation == "delete_fail":
        with pytest.raises(OSError):
            request()
    else:
        assert request()["ok"]
    assert cancellation_results == [409]
    assert not cancel.is_cancelled(task["id"])
    if operation == "delete":
        assert context.store.get_task(task["id"]) is None
    else:
        assert context.store.get_task(task["id"]) == before
        # 失败路径也要释放维护占用，真正运行中的任务仍可以取消。
        context.store.update_task(task["id"], status="running", finished_at="")
        assert cancel_endpoint(task["id"])["ok"]
        assert cancel.is_cancelled(task["id"])


def test_cancel_does_not_use_a_stale_running_snapshot(context, monkeypatch):
    task = context.runner.create_task("https://example.test/isolated-video", "best", [], "none")
    context.store.update_task(task["id"], status="running")
    stale = context.store.get_task(task["id"])
    context.store.update_task(task["id"], status="completed")
    monkeypatch.setattr(context.store, "get_task", lambda _: stale)
    with pytest.raises(HTTPException) as error:
        endpoint(context.app, "/api/v1/tasks/{tid}/cancel", "POST")(task["id"])
    assert error.value.status_code == 409
    assert not cancel.is_cancelled(task["id"])


def test_finish_task_blocks_retry_until_cancel_cleanup_finishes(context, monkeypatch):
    task = context.runner.create_task("https://example.test/isolated-video", "best", [], "none")
    tid = task["id"]
    context.store.update_task(tid, status="running")
    cancel.cancel(tid)
    clearing, release, retry_started = threading.Event(), threading.Event(), threading.Event()
    clear = cancel.clear

    def paused_clear(task_id):
        clearing.set()
        assert release.wait(3), "测试必须释放取消清理"
        clear(task_id)

    def retry():
        retry_started.set()
        claimed = context.store.claim_retry(tid)
        if claimed:
            cancel.cancel(tid)  # 下一次运行的取消信号应保留。
        return claimed

    monkeypatch.setattr(cancel, "clear", paused_clear)
    with ThreadPoolExecutor(max_workers=2) as workers:
        finishing = workers.submit(context.store.finish_task, tid, status="failed", error="模拟失败")
        try:
            assert clearing.wait(1)
            retrying = workers.submit(retry)
            assert retry_started.wait(1)
            with pytest.raises(TimeoutError):
                retrying.result(timeout=.05)
        finally:
            release.set()
        finishing.result(timeout=1)
        assert retrying.result(timeout=1)
    assert context.store.get_task(tid)["status"] == "pending"
    assert cancel.is_cancelled(tid)


def test_finish_task_write_failure_still_clears_cancel_state(context):
    task = context.runner.create_task("https://example.test/isolated-video", "best", [], "none")
    tid = task["id"]
    context.store.update_task(tid, status="running")
    context.store._exec("CREATE TRIGGER reject_finish BEFORE UPDATE OF status ON task "
                        "WHEN NEW.status='failed' BEGIN SELECT RAISE(ABORT, 'isolated failure'); END")
    cancel.cancel(tid)
    with pytest.raises(sqlite3.IntegrityError):
        context.store.finish_task(tid, status="failed")
    assert context.store.get_task(tid)["status"] == "running"
    assert not cancel.is_cancelled(tid)


@pytest.mark.parametrize("failure", ["false", "exception", "release"])
def test_maintenance_ownership_is_released_on_claim_or_finish_failure(context, monkeypatch, failure):
    task, _, artifact = terminal_task(context)
    original_claim, original_finish = context.store.claim_terminal_task_snapshot, context.store.finish_task

    def fail(*args, **kwargs):
        raise OSError("模拟维护登记失败")

    if failure == "false":
        monkeypatch.setattr(context.store, "claim_terminal_task_snapshot", lambda _: None)
    elif failure == "exception":
        monkeypatch.setattr(context.store, "claim_terminal_task_snapshot", fail)
    else:
        monkeypatch.setattr(context.store, "finish_task", fail)
    with pytest.raises(HTTPException if failure == "false" else OSError):
        endpoint(context.app, "/api/v1/artifacts/{aid}/content", "PUT")(
            artifact["id"], server.SubtitleIn(text="1\n00:00:00,000 --> 00:00:01,000\n修改字幕\n"))
    monkeypatch.setattr(context.store, "claim_terminal_task_snapshot", original_claim)
    monkeypatch.setattr(context.store, "finish_task", original_finish)
    context.store.update_task(task["id"], status="running", finished_at="")
    assert endpoint(context.app, "/api/v1/tasks/{tid}/cancel", "POST")(task["id"])["ok"]


def test_parallel_task_reads_never_return_missing_or_another_task(context):
    tasks = [context.runner.create_task(f"https://example.test/isolated-{number}", "best", [], "none")
             for number in range(3)]
    barrier = threading.Barrier(12)

    def query(number):
        task = tasks[number % len(tasks)]
        barrier.wait(timeout=3)
        invalid = []
        for iteration in range(500):
            try:
                value = context.store.get_task(task["id"])
                if value != task:
                    invalid.append((iteration, value))
            except Exception as error:
                invalid.append((iteration, type(error).__name__))
        return invalid

    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(query, range(12)))
    invalid = [item for group in results for item in group]
    assert not invalid, f"6000 次并发读取出现 {len(invalid)} 次错误，前几项：{invalid[:3]}"


def test_single_and_multiple_row_fetches_finish_before_unlock(context, monkeypatch):
    task = context.runner.create_task("https://example.test/isolated-video", "best", [], "none")
    connection = context.store._conn
    observations = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            observations.append("one")
            assert context.store._lock.locked(), "获取单行期间仍需占用共享连接锁"
            return self.cursor.fetchone()

        def fetchall(self):
            observations.append("all")
            assert context.store._lock.locked(), "获取全部行期间仍需占用共享连接锁"
            return self.cursor.fetchall()

    class Connection:
        def execute(self, *args):
            return Cursor(connection.execute(*args))

        def __getattr__(self, name):
            return getattr(connection, name)

    monkeypatch.setattr(context.store, "_conn", Connection())
    assert context.store.get_task(task["id"]) == task
    assert context.store.list_tasks() == [task]
    assert observations == ["one", "all"]
