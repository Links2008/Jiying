"""领取任务后应使用当次快照和产物路径，共享路径别名不能绕过文件保护。"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import server

SRT = "1\n00:00:00,000 --> 00:00:01,000\n原始字幕\n"
EDITED = SRT.replace("原始字幕", "修改后的字幕")


def endpoint(context, path, method):
    return next(route.endpoint for route in context.app.routes if route.path == path and method in route.methods)


@pytest.fixture
def context(tmp_path):
    app = server.create_app()
    task = server._runner.create_task("https://example.test/isolated-first", "best", [], "burn",
                                      output_dir=str(tmp_path / "output"))
    server._store.update_task(task["id"], status="completed", error="历史提示", finished_at="历史完成时间")
    path = tmp_path / "output" / "captions.srt"
    path.parent.mkdir(parents=True)
    path.write_text(SRT, encoding="utf-8-sig")
    server._store.add_artifact(task["id"], "subtitle", str(path), lang="zh")
    artifact = server._store.list_artifacts(task["id"])[0]
    return SimpleNamespace(app=app, runner=server._runner, store=server._store,
                           task=task, artifact=artifact, path=path, root=tmp_path)


def after_first_task_read(context, monkeypatch, change):
    get_task = context.store.get_task
    changed = False

    def read(tid):
        nonlocal changed
        snapshot = get_task(tid)
        if tid == context.task["id"] and not changed:
            changed = True
            change()
        return snapshot

    monkeypatch.setattr(context.store, "get_task", read)


@pytest.mark.parametrize("operation", ["save", "partial_delete", "delete_fail"])
def test_file_operation_preserves_terminal_result_current_at_claim(context, monkeypatch, operation):
    after_first_task_read(context, monkeypatch, lambda: context.store.update_task(
        context.task["id"], status="cancelled", error="新的渲染结果", finished_at="新的完成时间"))
    if operation == "save":
        endpoint(context, "/api/v1/artifacts/{aid}/content", "PUT")(
            context.artifact["id"], server.SubtitleIn(text=EDITED))
    elif operation == "partial_delete":
        endpoint(context, "/api/v1/tasks/{tid}/artifacts", "DELETE")(
            context.task["id"], kind="subtitle")
    else:
        def fail(_):
            raise OSError("模拟工作文件清理失败")

        monkeypatch.setattr(context.runner, "cleanup_task_files", fail)
        with pytest.raises(OSError):
            endpoint(context, "/api/v1/tasks/{tid}", "DELETE")(context.task["id"])
    task = context.store.get_task(context.task["id"])
    assert (task["status"], task["error"], task["finished_at"]) == (
        "cancelled", "新的渲染结果", "新的完成时间")


def test_retry_launch_failure_restores_snapshot_current_at_claim(context, monkeypatch):
    context.store.update_task(context.task["id"], status="failed")
    after_first_task_read(context, monkeypatch, lambda: context.store.update_task(
        context.task["id"], status="interrupted", error="新一次尝试已中断", finished_at="新的完成时间"))

    class Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("模拟无法启动重试")

    monkeypatch.setattr(server, "threading", SimpleNamespace(Thread=Thread))
    with pytest.raises(HTTPException) as error:
        endpoint(context, "/api/v1/tasks/{tid}/retry", "POST")(context.task["id"])
    assert error.value.status_code == 503
    task = context.store.get_task(context.task["id"])
    assert (task["status"], task["error"], task["finished_at"]) == (
        "interrupted", "新一次尝试已中断", "新的完成时间")


@pytest.mark.parametrize("change", ["file_removed", "record_removed", "path_moved"])
def test_subtitle_save_rechecks_owned_artifact_after_claim(context, monkeypatch, change):
    moved = context.path.with_name("renamed.srt")

    def alter():
        if change == "file_removed":
            context.path.unlink()
        elif change == "record_removed":
            context.store.delete_artifacts(context.task["id"], [context.artifact["id"]])
        else:
            context.path.replace(moved)
            context.store.move_artifact(context.task["id"], str(context.path), str(moved))

    after_first_task_read(context, monkeypatch, alter)
    save = endpoint(context, "/api/v1/artifacts/{aid}/content", "PUT")
    if change == "path_moved":
        result = save(context.artifact["id"], server.SubtitleIn(text=EDITED))
        assert result["path"] == str(moved)
        assert moved.read_text(encoding="utf-8-sig") == EDITED
        assert not context.path.exists()
    else:
        with pytest.raises(HTTPException) as error:
            save(context.artifact["id"], server.SubtitleIn(text=EDITED))
        assert error.value.status_code == 404
        if change == "file_removed":
            assert not context.path.exists()
        else:
            assert context.path.read_text(encoding="utf-8-sig") == SRT
    assert context.store.get_task(context.task["id"])["status"] == "completed"


@pytest.mark.parametrize("alias", ["dotdot", "file_symlink", "directory_symlink"])
@pytest.mark.parametrize("operation", ["save", "partial_delete", "delete"])
def test_shared_historical_path_alias_preserves_other_task_file(context, alias, operation):
    other = context.runner.create_task("https://example.test/isolated-second", "best", [], "burn",
                                       output_dir=str(context.path.parent))
    context.store.update_task(other["id"], status="completed")
    if alias == "dotdot":
        directory = context.path.parent / "existing"
        directory.mkdir()
        other_path = directory / ".." / context.path.name
    elif alias == "file_symlink":
        other_path = context.path.with_name("alias.srt")
        other_path.symlink_to(context.path.name)
    else:
        directory = context.root / "aliased-output"
        directory.symlink_to(context.path.parent, target_is_directory=True)
        other_path = directory / context.path.name
    context.store.add_artifact(other["id"], "subtitle", str(other_path), lang="zh")
    if operation == "save":
        with pytest.raises(HTTPException) as error:
            endpoint(context, "/api/v1/artifacts/{aid}/content", "PUT")(
                context.artifact["id"], server.SubtitleIn(text=EDITED))
        assert error.value.status_code == 409
    else:
        if operation == "partial_delete":
            result = endpoint(context, "/api/v1/tasks/{tid}/artifacts", "DELETE")(
                context.task["id"], kind="subtitle")
        else:
            result = endpoint(context, "/api/v1/tasks/{tid}", "DELETE")(
                context.task["id"], with_files=True)
        assert result["deleted_files"] == [] and result["skipped"]
    assert context.path.read_text(encoding="utf-8-sig") == SRT
    assert other_path.read_text(encoding="utf-8-sig") == SRT
    assert context.store.get_task(other["id"])["status"] == "completed"


@pytest.mark.parametrize("with_files", [False, True])
def test_deleting_task_keeps_shared_legacy_file_in_its_work_directory(context, with_files):
    shared = context.runner._data / "tmp" / context.task["id"] / "nested" / "legacy.srt"
    shared.parent.mkdir(parents=True)
    shared.write_text(SRT, encoding="utf-8-sig")
    other = context.runner.create_task("https://example.test/isolated-second", "best", [], "burn")
    context.store.update_task(other["id"], status="completed")
    for task in (context.task, other):
        context.store.add_artifact(task["id"], "subtitle", str(shared), lang="zh")
    endpoint(context, "/api/v1/tasks/{tid}", "DELETE")(context.task["id"], with_files=with_files)
    assert context.store.get_task(context.task["id"]) is None
    assert shared.read_text(encoding="utf-8-sig") == SRT
    assert context.store.get_task(other["id"])["status"] == "completed"
    assert context.store.list_artifacts(other["id"])[0]["path"] == str(shared)


def test_shared_cleanup_paths_exclude_own_and_deduplicate_other_tasks(context):
    assert context.store.artifact_paths_owned_by_other(context.task["id"]) == []
    others = [context.runner.create_task(f"https://example.test/isolated-other-{number}", "best", [], "none")
              for number in range(2)]
    shared = str(context.root / "historical.srt")
    unique = str(context.root / "other.mp4")
    for task in others:
        context.store.add_artifact(task["id"], "subtitle", shared, lang="zh")
    context.store.add_artifact(others[0]["id"], "bilingual", shared, lang="zh+en")
    context.store.add_artifact(others[1]["id"], "video", unique)
    paths = context.store.artifact_paths_owned_by_other(context.task["id"])
    assert len(paths) == 2 and set(paths) == {shared, unique}
    assert str(context.path) not in paths
