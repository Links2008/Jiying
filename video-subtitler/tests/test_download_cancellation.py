from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import cancel
from app.core.config import Config
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.pipeline import runner as pipeline
from app.services.downloader.aria2c import Aria2cBackend
from app.services.engines.base import DirectTarget
from app.services.engines import ytdlp_engine


@pytest.fixture
def task_id():
    tid = "isolated-cancel-test"
    cancel.clear(tid)
    yield tid
    cancel.clear(tid)


def test_cancel_before_process_registration_kills_late_process(task_id):
    killed = []
    cancel.cancel(task_id)
    cancel.register(task_id, SimpleNamespace(kill=lambda: killed.append(True)))
    assert killed == [True]


def test_cancelled_download_does_not_start_metadata_head(monkeypatch, tmp_path, task_id):
    engine = ytdlp_engine.YtDlpEngine(Config({}))
    requested = []
    monkeypatch.setattr(engine, "_head_direct_file", lambda *_: requested.append(True))
    cancel.cancel(task_id)
    with pytest.raises(AppError) as error:
        engine.download("https://example.test/clip", "best", tmp_path, task_id=task_id)
    assert error.value.code == Codes.TASK_CANCELLED
    assert requested == []


def test_aria_cancellation_before_submit_creates_no_remote_jobs(tmp_path):
    added = []
    client = SimpleNamespace(add_uri=lambda *args: added.append(args) or "gid",
                             remove=lambda *_: None)
    with pytest.raises(AppError) as error:
        Aria2cBackend(Config({}), client).download(
            DirectTarget(url="http://local.test/v", filename="v.mp4"),
            tmp_path, cancel_check=lambda: True)
    assert error.value.code == Codes.TASK_CANCELLED
    assert added == []


def test_cancel_between_split_submissions_stops_first_and_skips_second(tmp_path):
    added, removed = [], []
    client = SimpleNamespace(add_uri=lambda *args: added.append(args) or "first-gid",
                             remove=removed.append)
    with pytest.raises(AppError) as error:
        Aria2cBackend(Config({}), client).download_many(
            [DirectTarget(url="http://local.test/v", filename="v.mp4"),
             DirectTarget(url="http://local.test/a", filename="a.m4a")],
            tmp_path, cancel_check=lambda: bool(added))
    assert error.value.code == Codes.TASK_CANCELLED
    assert len(added) == 1
    assert removed == ["first-gid"]


def native_stub(monkeypatch, tmp_path, on_extract=None):
    captured = {}
    output = tmp_path / "video.mp4"
    output.write_bytes(b"video")
    class YoutubeDL:
        def __init__(self, opts):
            captured.update(opts)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def extract_info(self, *args, **kwargs):
            if on_extract:
                on_extract(captured)
            return {"requested_downloads": [{"filepath": str(output)}]}
    monkeypatch.setattr(ytdlp_engine, "_import_ytdlp", lambda: SimpleNamespace(
        YoutubeDL=YoutubeDL, utils=SimpleNamespace(DownloadError=RuntimeError)))
    return captured


def test_rpc_fallback_does_not_spawn_unregistered_external_aria(monkeypatch, tmp_path):
    from app.services import renderer
    monkeypatch.setattr(renderer, "validate_download", lambda _cfg, path, *_: path)
    captured = native_stub(monkeypatch, tmp_path)
    monkeypatch.setattr(ytdlp_engine.Aria2cBackend, "ping", lambda *_: False)
    engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": True}}}))
    monkeypatch.setattr(engine, "_head_direct_file", lambda *_: None)
    monkeypatch.setattr(engine, "_find_aria2c", lambda: Path("bundled-aria2c.exe"))
    engine.download("https://example.test/clip", "best", tmp_path)
    assert "external_downloader" not in captured


def test_native_postprocessing_checks_cancellation(monkeypatch, tmp_path, task_id):
    def postprocess(opts):
        assert opts.get("postprocessor_hooks"), "cancellation must be checked before native merge"
        cancel.cancel(task_id)
        for hook in opts.get("postprocessor_hooks", []):
            hook({"status": "started", "postprocessor": "Merger"})
    native_stub(monkeypatch, tmp_path, postprocess)
    engine = ytdlp_engine.YtDlpEngine(Config({}))
    monkeypatch.setattr(engine, "_head_direct_file", lambda *_: None)
    with pytest.raises(AppError) as error:
        engine.download("https://example.test/clip", "best", tmp_path, task_id=task_id)
    assert error.value.code == Codes.TASK_CANCELLED


def test_native_return_cannot_complete_cancelled_task(monkeypatch, tmp_path, task_id):
    native_stub(monkeypatch, tmp_path, lambda _: cancel.cancel(task_id))
    engine = ytdlp_engine.YtDlpEngine(Config({}))
    monkeypatch.setattr(engine, "_head_direct_file", lambda *_: None)
    with pytest.raises(AppError) as error:
        engine.download("https://example.test/clip", "best", tmp_path, task_id=task_id)
    assert error.value.code == Codes.TASK_CANCELLED


def test_cancel_when_router_returns_preserves_partial_and_marks_job_cancelled(tmp_path):
    store = Store(tmp_path / "tasks.db")
    runner = pipeline.PipelineRunner(Config({}), store)
    runner._data = tmp_path / "data"
    task = runner.create_task("https://example.test/clip", "best", [], "none")
    video = runner._data / "tmp" / task["id"] / "video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"downloaded bytes")
    def download(*_, **__):
        cancel.cancel(task["id"])
        return "fake", [video]
    runner._router = SimpleNamespace(download=download)
    with pytest.raises(AppError) as error:
        runner.run(task)
    assert error.value.code == Codes.TASK_CANCELLED
    assert store.get_task(task["id"])["status"] == "cancelled"
    assert store.list_artifacts(task["id"]) == []
    assert video.read_bytes() == b"downloaded bytes"
    jobs = store._exec("SELECT status FROM download_job WHERE task_id=?", (task["id"],)).fetchall()
    assert [row["status"] for row in jobs] == ["cancelled"]


def test_cancellation_during_io_queue_does_not_start_download(monkeypatch, tmp_path):
    store = Store(tmp_path / "tasks.db")
    runner = pipeline.PipelineRunner(Config({}), store)
    task = runner.create_task("https://example.test/clip", "best", [], "none")
    called = []
    class CancelOnAcquire:
        def acquire(self, timeout=None):
            cancel.cancel(task["id"])
            return True
        def release(self):
            pass
    monkeypatch.setitem(pipeline._POOLS, "io", CancelOnAcquire())
    monkeypatch.setattr(runner, "_download_inner", lambda *args: called.append(args))
    try:
        with pytest.raises(AppError) as error:
            runner._stage_download(task)
        assert error.value.code == Codes.TASK_CANCELLED
        assert called == []
    finally:
        cancel.clear(task["id"])


def test_final_task_boundary_checks_cancel_before_completed(monkeypatch, tmp_path):
    store = Store(tmp_path / "tasks.db")
    runner = pipeline.PipelineRunner(Config({}), store)
    task = runner.create_task("https://example.test/clip", "best", [], "none")
    def download(_):
        cancel.cancel(task["id"])
        return tmp_path / "video.mp4"
    monkeypatch.setattr(runner, "_stage_download", download)
    with pytest.raises(AppError) as error:
        runner.run(task)
    assert error.value.code == Codes.TASK_CANCELLED
    assert store.get_task(task["id"])["status"] == "cancelled"
