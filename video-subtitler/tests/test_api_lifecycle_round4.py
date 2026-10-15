"""任务删除的数据库事务和共享文件最后一个所有者必须保持一致。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading

import pytest

from app.api import server


def delete_endpoint(app):
    return next(route.endpoint for route in app.routes
                if route.path == "/api/v1/tasks/{tid}" and "DELETE" in route.methods)


def create_finished_task(output, number):
    task = server._runner.create_task(f"https://example.test/isolated-{number}", "best", [], "none",
                                      output_dir=str(output))
    server._store.update_task(task["id"], status="completed")
    return task


def test_failed_task_delete_rolls_back_related_records(tmp_path):
    app = server.create_app()
    output = tmp_path / "output"
    output.mkdir()
    task = create_finished_task(output, 1)
    subtitle = output / "kept.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n隔离字幕\n", encoding="utf-8")
    server._store.add_artifact(task["id"], "subtitle", str(subtitle), lang="zh")
    server._store.upsert_stage(task["id"], "download", "done")
    server._store.add_job("isolated-job", task["id"], status="completed")
    before = (server._store.get_task(task["id"]), server._store.list_artifacts(task["id"]),
              server._store.list_stages(task["id"]),
              [dict(row) for row in server._store._fetchall("SELECT * FROM download_job")])
    server._store._exec("CREATE TRIGGER reject_task_delete BEFORE DELETE ON task "
                        "BEGIN SELECT RAISE(ABORT, 'isolated delete failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        delete_endpoint(app)(task["id"], with_files=False)
    after = (server._store.get_task(task["id"]), server._store.list_artifacts(task["id"]),
             server._store.list_stages(task["id"]),
             [dict(row) for row in server._store._fetchall("SELECT * FROM download_job")])
    assert after == before
    assert subtitle.exists()


def test_concurrent_final_owners_delete_shared_file_once(tmp_path, monkeypatch):
    app = server.create_app()
    output = tmp_path / "output"
    output.mkdir()
    shared = output / "historical.mp4"
    shared.write_bytes(b"isolated legacy video")
    tasks = [create_finished_task(output, number) for number in range(2)]
    for task in tasks:
        server._store.add_artifact(task["id"], "video", str(shared))
    paused, release = threading.Event(), threading.Event()
    owned_by_other = server._store.artifact_owned_by_other
    paused_once = False

    def delayed_owner_check(tid, path):
        nonlocal paused_once
        result = owned_by_other(tid, path)
        if tid == tasks[0]["id"] and Path(path) == shared and not paused_once:
            paused_once = True
            paused.set()
            assert release.wait(3), "测试必须释放共享判断"
        return result

    monkeypatch.setattr(server._store, "artifact_owned_by_other", delayed_owner_check)
    delete = delete_endpoint(app)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(delete, tasks[0]["id"], with_files=True)
        try:
            assert paused.wait(1)
            second = workers.submit(delete, tasks[1]["id"], with_files=True)
            try:
                second.result(timeout=.15)
            except TimeoutError:
                pass
        finally:
            release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]
    assert all(result["ok"] for result in results)
    assert server._store.list_tasks() == []
    assert not shared.exists(), "所有任务均请求删除文件，最后一个登记所有者应回收共享产物"
    assert sum(str(shared) in result["deleted_files"] for result in results) == 1


def test_record_only_deletion_preserves_unregistered_shared_output(tmp_path):
    app = server.create_app()
    output = tmp_path / "output"
    output.mkdir()
    shared = output / "historical.mp4"
    shared.write_bytes(b"explicitly retained output")
    tasks = [create_finished_task(output, number) for number in range(2)]
    for task in tasks:
        server._store.add_artifact(task["id"], "video", str(shared))
    delete = delete_endpoint(app)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda task: delete(task["id"], with_files=False), tasks))
    assert all(result["ok"] and not result["deleted_files"] for result in results)
    assert server._store.list_tasks() == []
    assert shared.read_bytes() == b"explicitly retained output"


def clear_endpoint(app):
    return next(route.endpoint for route in app.routes if route.path == "/api/v1/tasks/clear_done")


@pytest.mark.parametrize("kind", ["", "subtitle", "video"])
def test_bulk_delete_is_limited_to_confirmed_task_snapshot(tmp_path, kind):
    app = server.create_app()
    output = tmp_path / "output"
    output.mkdir()
    confirmed = create_finished_task(output, "confirmed")
    confirmed_ids = [confirmed["id"]]
    # 弹窗确认快照后才完成的任务，不能被这次批量删除覆盖。
    later = create_finished_task(output, "completed-later")
    paths = {}
    for task in (confirmed, later):
        for category, extension in (("subtitle", ".srt"), ("video", ".mp4")):
            path = output / f"{task['id']}{extension}"
            path.write_bytes(b"isolated artifact")
            server._store.add_artifact(task["id"], category, str(path))
            paths[task["id"], category] = path
    result = clear_endpoint(app)(server.ClearDoneIn(with_files=True, kind=kind, task_ids=confirmed_ids))
    assert result["cleared"] == 1
    assert server._store.get_task(later["id"]) is not None
    assert all(paths[later["id"], category].exists() for category in ("subtitle", "video"))
    if not kind:
        assert server._store.get_task(confirmed["id"]) is None
    else:
        assert not paths[confirmed["id"], kind].exists()


def test_explicit_empty_bulk_snapshot_does_not_delete_all(tmp_path):
    app = server.create_app()
    task = create_finished_task(tmp_path, 1)
    result = clear_endpoint(app)(server.ClearDoneIn(task_ids=[]))
    assert result["cleared"] == 0
    assert server._store.get_task(task["id"]) is not None


def test_bulk_snapshot_deduplicates_and_ignores_missing_or_running_ids(tmp_path):
    app = server.create_app()
    selected, untouched = [create_finished_task(tmp_path, number) for number in range(2)]
    active = server._runner.create_task("https://example.test/active", "best", [], "none")
    server._store.update_task(active["id"], status="running")
    result = clear_endpoint(app)(server.ClearDoneIn(
        task_ids=[selected["id"], selected["id"], "isolated-missing", active["id"]]))
    assert result["cleared"] == 1
    assert server._store.get_task(selected["id"]) is None
    assert server._store.get_task(untouched["id"]) is not None
    assert server._store.get_task(active["id"])["status"] == "running"


@pytest.mark.parametrize("fields", [{}, {"task_ids": None}])
def test_legacy_bulk_clear_without_snapshot_keeps_all_terminal_behavior(tmp_path, fields):
    app = server.create_app()
    for number in range(2):
        create_finished_task(tmp_path, number)
    result = clear_endpoint(app)(server.ClearDoneIn(**fields))
    assert result["cleared"] == 2
    assert server._store.list_tasks() == []
