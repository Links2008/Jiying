from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from fastapi import HTTPException

from app.core.config import Config, DEFAULTS
from app.models.db import Store
from app.pipeline import runner as pipeline


@pytest.mark.parametrize("batch", [False, True])
def test_delete_excludes_a_retry_during_file_cleanup(monkeypatch, batch):
    from app.api import server

    app = server.create_app()
    task = server._runner.create_task("https://example.test/video", "best", [], "none")
    server._store.update_task(task["id"], status="failed")
    claimed = []
    list_artifacts = server._store.list_artifacts

    def overlapping_retry(tid):
        claimed.append(server._store.claim_retry(tid))
        return list_artifacts(tid)

    monkeypatch.setattr(server._store, "list_artifacts", overlapping_retry)
    if batch:
        endpoint = next(r.endpoint for r in app.routes if r.path == "/api/v1/tasks/clear_done")
        endpoint(server.ClearDoneIn(with_files=True))
    else:
        endpoint = next(r.endpoint for r in app.routes if r.path == "/api/v1/tasks/{tid}" and "DELETE" in r.methods)
        endpoint(task["id"], with_files=True)
    assert claimed == [False], "retry must not acquire a task while its files are being deleted"
    assert server._store.get_task(task["id"]) is None


def test_delete_keeps_file_still_registered_to_another_task(tmp_path):
    from app.api import server

    app = server.create_app()
    output = tmp_path / "downloads"
    output.mkdir()
    video = output / "clip.mp4"
    video.write_bytes(b"shared old file")
    tasks = [server._runner.create_task(f"https://example.test/{n}", "best", [],
                                        "none", output_dir=str(output)) for n in (1, 2)]
    for task in tasks:
        server._store.update_task(task["id"], status="completed")
        server._store.add_artifact(task["id"], "video", str(video))

    endpoint = next(r.endpoint for r in app.routes
                    if r.path == "/api/v1/tasks/{tid}" and "DELETE" in r.methods)
    result = endpoint(tasks[0]["id"], with_files=True)

    assert video.read_bytes() == b"shared old file"
    assert result["deleted_files"] == []
    assert result["skipped"]
    assert server._store.get_task(tasks[1]["id"])


@pytest.mark.parametrize("category,removed", [
    ("subtitle", {"subtitle", "bilingual"}),
    ("video", {"video", "output"}),
])
@pytest.mark.parametrize("batch", [False, True])
def test_partial_delete_keeps_record_and_other_category(tmp_path, monkeypatch, category, removed, batch):
    from app.api import server

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(server, "data_dir", lambda: data)
    app = server.create_app()
    output = tmp_path / "downloads"
    output.mkdir()
    task = server._runner.create_task("https://example.test/clip", "best", [],
                                      "burn", output_dir=str(output))
    server._store.update_task(task["id"], status="completed")
    paths = {}
    for kind, suffix in (("video", ".source.mp4"), ("output", ".burned.mp4"),
                         ("subtitle", ".zh.srt"), ("bilingual", ".zh+en.srt")):
        path = output / ("clip" + suffix)
        path.write_text(kind, encoding="utf-8")
        server._store.add_artifact(task["id"], kind, str(path))
        paths[kind] = path

    if batch:
        endpoint = next(r.endpoint for r in app.routes if r.path == "/api/v1/tasks/clear_done")
        result = endpoint(server.ClearDoneIn(with_files=True, kind=category))
    else:
        endpoint = next(r.endpoint for r in app.routes
                        if r.path == "/api/v1/tasks/{tid}/artifacts" and "DELETE" in r.methods)
        result = endpoint(task["id"], kind=category)
    assert len(result["deleted_files"]) == 2
    assert server._store.get_task(task["id"])["status"] == "completed"
    assert {a["kind"] for a in server._store.list_artifacts(task["id"])} == set(paths) - removed
    assert all(path.exists() == (kind not in removed) for kind, path in paths.items())


@pytest.mark.parametrize("fail", [False, True])
def test_terminal_status_is_published_after_cleanup(tmp_path, fail):
    from app.core.errors import AppError, Codes

    runner = pipeline.PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    task = runner.create_task("https://example.test/video", "best", [], "none")
    states_during_cleanup = []
    runner._cleanup_tmp = lambda tid: states_during_cleanup.append(runner._store.get_task(tid)["status"])

    def download(_):
        if fail:
            raise AppError(Codes.NET_FAILED, "offline")
        return Path("unused.mp4")

    runner._stage_download = download
    if fail:
        with pytest.raises(AppError):
            runner.run(task)
    else:
        runner.run(task)
    assert states_during_cleanup == ["running"], "retry/delete must not overlap the previous worker cleanup"
    assert runner._store.get_task(task["id"])["status"] == ("failed" if fail else "completed")


def test_changing_concurrency_during_task_still_cleans_up(tmp_path):
    runner = pipeline.PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    task = runner.create_task("https://example.test/video", "best", [], "none")
    cleaned = []
    runner._cleanup_tmp = lambda tid: cleaned.append(tid)
    runner._set_live(task["id"], fraction=.5)

    def download(_):
        pipeline.set_max_concurrent(1)
        return Path("unused.mp4")

    runner._stage_download = download
    try:
        assert runner.run(task)["status"] == "completed"
        assert cleaned == [task["id"]]
        assert runner.live_progress(task["id"]) == {}
    finally:
        pipeline.set_max_concurrent(2)


def test_simultaneous_retry_schedules_only_one_worker(monkeypatch):
    from app.api import server

    app = server.create_app()
    task = server._runner.create_task("https://example.test/video", "best", [], "none")
    server._store.update_task(task["id"], status="failed")
    get_task = server._store.get_task
    both_read = threading.Barrier(2)

    def overlapping_read(tid):
        snapshot = get_task(tid)
        both_read.wait(timeout=3)
        return snapshot

    started = []

    class DeferredThread:
        def __init__(self, **kwargs):
            self.args = kwargs["args"]

        def start(self):
            started.append(self.args)

    monkeypatch.setattr(server._store, "get_task", overlapping_read)
    monkeypatch.setattr(server, "threading", SimpleNamespace(Thread=DeferredThread))
    retry = next(r.endpoint for r in app.routes if r.path == "/api/v1/tasks/{tid}/retry")

    def request(_):
        try:
            retry(task["id"])
            return 200
        except HTTPException as ex:
            return ex.status_code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(request, range(2)))
    assert sorted(results) == [200, 409]
    assert len(started) == 1


def test_waiting_task_can_cancel_before_running_task_finishes(tmp_path):
    from app.core import cancel
    from app.core.errors import AppError, Codes

    runner = pipeline.PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    first = runner.create_task("https://example.test/first", "best", [], "none")
    second = runner.create_task("https://example.test/second", "best", [], "none")
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def download(task):
        assert task["id"] == first["id"], "cancelled task must not download"
        entered.set()
        assert release.wait(3)
        return Path("unused.mp4")

    def run(task):
        try:
            runner.run(task)
        except AppError as ex:
            if ex.code == Codes.TASK_CANCELLED:
                cancelled.set()
            else:
                errors.append(ex)
        except Exception as ex:
            errors.append(ex)

    runner._stage_download = download
    pipeline.set_max_concurrent(1)
    workers = [threading.Thread(target=run, args=(task,)) for task in (first, second)]
    try:
        workers[0].start()
        assert entered.wait(1)
        workers[1].start()
        cancel.cancel(second["id"])
        assert cancelled.wait(1), "queued cancellation must not wait for the active download"
        assert runner._store.get_task(second["id"])["status"] == "cancelled"
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(3)
        pipeline.set_max_concurrent(2)
    assert not errors
