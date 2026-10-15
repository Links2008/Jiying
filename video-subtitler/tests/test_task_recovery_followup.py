"""重渲染终态与重启恢复必须保留真实的处理结果。"""
from copy import deepcopy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import errno
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from app.core import cancel
from app.core.config import Config, DEFAULTS
from app.core.errors import AppError, Codes
from app.models import db
from app.models.db import Store
from app.pipeline import runner as pipeline
from app.services.transcriber import Segment, Transcript


@pytest.fixture
def ready(tmp_path):
    store = Store(tmp_path / "tasks.db")
    runner = pipeline.PipelineRunner(Config(deepcopy(DEFAULTS)), store)
    runner._data = tmp_path / "work"
    task = runner.create_task("https://example.test/video", "best", ["zh"], "burn",
                              output_dir=str(tmp_path / "output"))
    work = runner._data / "tmp" / task["id"]
    work.mkdir(parents=True)
    video, subtitle = work / "clip.mp4", work / "clip.zh.srt"
    video.write_bytes(b"original video")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n编辑后的字幕\n\n", encoding="utf-8-sig")
    output = tmp_path / "output" / "video.zh.burned.mp4"
    output.parent.mkdir()
    output.write_bytes(b"previous successful video")
    for kind, path, lang in (("video", video, ""), ("subtitle", subtitle, "zh"),
                             ("output", output, "")):
        store.add_artifact(task["id"], kind, str(path), lang=lang)
    store.upsert_stage(task["id"], "download", "done")
    store.upsert_stage(task["id"], "render", "done")
    store.update_task(task["id"], status="completed")
    yield SimpleNamespace(store=store, runner=runner, task=task, video=video,
                          subtitle=subtitle, output=output)
    cancel.clear(task["id"])


@pytest.mark.parametrize("code,task_status,stage_status", [
    (Codes.TASK_CANCELLED, "cancelled", "cancelled"),
    (Codes.RENDER_FAILED, "failed", "failed"),
])
def test_rerender_failure_has_its_own_terminal_state(ready, monkeypatch, code, task_status, stage_status):
    def fail(*args, **kwargs):
        if code == Codes.TASK_CANCELLED:
            cancel.cancel(ready.task["id"])
        raise AppError(code, "模拟重渲染中断")

    monkeypatch.setattr(pipeline, "burn", fail)
    with pytest.raises(AppError) as error:
        ready.runner.rerender_subtitles(ready.task["id"], "zh")
    assert error.value.code == code
    task = ready.store.get_task(ready.task["id"])
    assert task["status"] == task_status
    assert json.loads(task["error"])["code"] == code
    assert task["finished_at"]
    assert ready.store.stage_status(ready.task["id"], "render") == stage_status
    assert ready.output.read_bytes() == b"previous successful video"
    assert "编辑后的字幕" in ready.subtitle.read_text(encoding="utf-8-sig")
    assert not cancel.is_cancelled(ready.task["id"])
    assert ready.store.claim_retry(ready.task["id"])


def test_rerender_wraps_unexpected_render_failure(ready, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("模拟编码器异常")

    monkeypatch.setattr(pipeline, "burn", fail)
    with pytest.raises(AppError) as error:
        ready.runner.rerender_subtitles(ready.task["id"], "zh")
    assert error.value.code == Codes.RENDER_FAILED
    assert ready.store.get_task(ready.task["id"])["status"] == "failed"
    assert ready.output.read_bytes() == b"previous successful video"


def test_rerender_retains_cancellation_through_cleanup(ready, monkeypatch):
    observed = []

    def render(task, *_):
        cancel.cancel(task["id"])
        raise AppError(Codes.TASK_CANCELLED, "模拟取消")

    def cleanup(tid):
        observed.append((ready.store.get_task(tid)["status"], cancel.is_cancelled(tid)))

    monkeypatch.setattr(ready.runner, "_stage_render", render)
    monkeypatch.setattr(ready.runner, "_cleanup_tmp", cleanup)
    with pytest.raises(AppError):
        ready.runner.rerender_subtitles(ready.task["id"], "zh")
    assert observed == [("running", True)]
    assert ready.store.get_task(ready.task["id"])["status"] == "cancelled"


def test_cancellation_during_rerender_cleanup_is_not_lost(ready, monkeypatch):
    monkeypatch.setattr(ready.runner, "_stage_render", lambda *_: [ready.output])
    monkeypatch.setattr(ready.runner, "_cleanup_tmp", cancel.cancel)
    ready.runner.rerender_subtitles(ready.task["id"], "zh")
    task = ready.store.get_task(ready.task["id"])
    assert task["status"] == "cancelled"
    assert json.loads(task["error"])["code"] == Codes.TASK_CANCELLED
    assert not cancel.is_cancelled(ready.task["id"])


def test_restart_interrupts_only_unfinished_stage_and_jobs(tmp_path):
    store = Store(tmp_path / "tasks.db")
    tasks = [store.create_task(f"https://example.test/{n}", "best", [], "none") for n in range(2)]
    active, finished = [task["id"] for task in tasks]
    store.update_task(active, status="running")
    store.upsert_stage(active, "download", "done")
    store.upsert_stage(active, "render", "running")
    store.add_job("unfinished", active, status="running", downloaded=7, total=10)
    store.add_job("done", active, status="completed", downloaded=10, total=10)
    store.update_task(finished, status="completed")
    store.upsert_stage(finished, "download", "done")
    store.add_job("other", finished, status="completed")
    before_finished = store.get_task(finished)

    assert store.recover_stale_running() == 1
    assert store.recover_stale_running() == 0
    assert store.get_task(active)["status"] == "interrupted"
    assert store.stage_status(active, "download") == "done"
    stage = next(row for row in store.list_stages(active) if row["stage"] == "render")
    assert stage["status"] == "interrupted"
    assert stage["finished_at"] and "重启" in stage["error"]
    jobs = {row["id"]: dict(row) for row in store._exec("SELECT * FROM download_job").fetchall()}
    assert jobs["unfinished"]["status"] == "interrupted"
    assert jobs["unfinished"]["downloaded"] == 7
    assert jobs["done"]["status"] == jobs["other"]["status"] == "completed"
    assert store.get_task(finished) == before_finished


@pytest.mark.parametrize("status", ["cancelled", "interrupted"])
def test_stopped_stage_records_finish_and_new_attempt_start(tmp_path, monkeypatch, status):
    store = Store(tmp_path / "tasks.db")
    task = store.create_task("https://example.test/video", "best", [], "none")
    moments = iter(["first start", "first finish", "second start"])
    monkeypatch.setattr(db, "_now", lambda: next(moments))
    store.upsert_stage(task["id"], "render", "running")
    store.upsert_stage(task["id"], "render", status, "中断原因")
    stage = store.list_stages(task["id"])[0]
    assert stage["finished_at"] == "first finish"
    store.upsert_stage(task["id"], "render", "running")
    stage = store.list_stages(task["id"])[0]
    assert stage["started_at"] == "second start"
    assert stage["finished_at"] == stage["error"] == ""


@pytest.mark.parametrize("stop", ["disk_full", "cancelled"])
def test_cross_device_publication_keeps_previous_output_on_interruption(ready, monkeypatch, stop):
    source = ready.video.parent / ready.output.name
    source.write_bytes(b"new complete encoded output")
    original_replace = pipeline.shutil.os.replace

    def cross_device(src, dst, *args, **kwargs):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "模拟跨盘输出")
        return original_replace(src, dst)

    def interrupted_flush(_):
        if stop == "disk_full":
            raise OSError(errno.ENOSPC, "模拟目标磁盘已满")
        cancel.cancel(ready.task["id"])

    monkeypatch.setattr(pipeline.shutil.os, "rename", cross_device)
    monkeypatch.setattr(pipeline.shutil.os, "replace", cross_device)
    monkeypatch.setattr(pipeline.os, "fsync", interrupted_flush)
    with pytest.raises(OSError if stop == "disk_full" else AppError):
        ready.runner._publish_file(ready.task["id"], source, ready.output.parent, "output")
    assert ready.output.read_bytes() == b"previous successful video"
    assert source.read_bytes() == b"new complete encoded output"
    assert not list(ready.output.parent.glob(".publish-*.tmp"))


def test_cross_device_copy_cancels_before_reading_rest_of_large_video(ready, monkeypatch):
    source = ready.video.parent / ready.output.name
    source.write_bytes(b"x" * (3 * 1024 * 1024))
    original_replace, original_open = pipeline.os.replace, Path.open
    reads = []

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "模拟跨盘输出")
        return original_replace(src, dst)

    class CancellableReader:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024, "长视频复制必须限制每次读取量"
            block = self.stream.read(size)
            reads.append(len(block))
            cancel.cancel(ready.task["id"])
            return block

    def open_source(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return CancellableReader(stream) if path == source and mode == "rb" else stream

    monkeypatch.setattr(pipeline.os, "replace", cross_device)
    monkeypatch.setattr(Path, "open", open_source)
    with pytest.raises(AppError) as error:
        ready.runner._publish_file(ready.task["id"], source, ready.output.parent, "output")
    assert error.value.code == Codes.TASK_CANCELLED
    assert len(reads) == 1 and sum(reads) < source.stat().st_size
    assert ready.output.read_bytes() == b"previous successful video"
    assert not list(ready.output.parent.glob(".publish-*.tmp"))


def test_unknown_registered_inputs_are_retained_when_cleanup_cannot_read_store(ready, monkeypatch):
    def unavailable(_):
        raise RuntimeError("模拟读取产物登记失败")

    monkeypatch.setattr(ready.store, "list_artifacts", unavailable)
    ready.runner._cleanup_tmp(ready.task["id"])
    assert ready.video.read_bytes() == b"original video"
    assert "编辑后的字幕" in ready.subtitle.read_text(encoding="utf-8-sig")


def test_cross_device_publication_registers_only_complete_output(ready, monkeypatch):
    source = ready.video.parent / ready.output.name
    source.write_bytes(b"new complete encoded output")
    original_replace = pipeline.os.replace

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "模拟跨盘输出")
        return original_replace(src, dst)

    monkeypatch.setattr(pipeline.os, "replace", cross_device)
    result = ready.runner._publish_file(ready.task["id"], source, ready.output.parent, "output")
    assert result == ready.output
    assert result.read_bytes() == b"new complete encoded output"
    assert not source.exists()
    assert not list(ready.output.parent.glob(".publish-*.tmp"))
    assert sum(row["path"] == str(result) for row in ready.store.list_artifacts(ready.task["id"])) == 1


@pytest.mark.parametrize("prepared", [False, True])
def test_provider_switch_closes_previous_after_new_instance_is_ready(ready, monkeypatch, prepared):
    closed = []
    replacement = SimpleNamespace(name="cloud")
    old = SimpleNamespace(name="faster-whisper", close=lambda: closed.append(ready.runner._transcriber))
    ready.runner._transcriber = old
    ready.runner._cfg.set("transcriber.provider", "cloud")

    def build(_):
        assert not prepared, "预构建实例不应再次创建"
        return replacement

    monkeypatch.setattr(pipeline, "build_transcriber", build)
    ready.runner.reload_transcriber(prepared=replacement if prepared else None)
    assert ready.runner._transcriber is replacement
    assert closed == [replacement]


def test_failed_provider_construction_keeps_previous_instance(ready, monkeypatch):
    closed = []
    old = SimpleNamespace(name="faster-whisper", close=lambda: closed.append(True))
    ready.runner._transcriber = old
    ready.runner._cfg.set("transcriber.provider", "cloud")

    def fail(_):
        raise AppError(Codes.CFG_INVALID, "模拟后端构造失败")

    monkeypatch.setattr(pipeline, "build_transcriber", fail)
    with pytest.raises(AppError):
        ready.runner.reload_transcriber()
    assert ready.runner._transcriber is old
    assert not closed


def test_same_provider_keeps_model_and_disposes_unused_prepared_instance(ready, monkeypatch):
    closed = []
    old = SimpleNamespace(name="faster-whisper", close=lambda: closed.append("old"))
    unused = SimpleNamespace(name="faster-whisper", close=lambda: closed.append("unused"))
    ready.runner._transcriber = old

    def forbidden(_):
        raise AssertionError("同后端不应丢弃已加载的模型")

    monkeypatch.setattr(pipeline, "build_transcriber", forbidden)
    ready.runner.reload_transcriber()
    ready.runner.reload_transcriber(prepared=unused)
    assert ready.runner._transcriber is old
    assert closed == ["unused"]


def test_close_failure_does_not_undo_successful_switch(ready):
    def fail():
        raise RuntimeError("模拟旧模型释放失败")

    ready.runner._transcriber = SimpleNamespace(name="faster-whisper", close=fail)
    ready.runner._cfg.set("transcriber.provider", "cloud")
    replacement = SimpleNamespace(name="cloud")
    ready.runner.reload_transcriber(prepared=replacement)
    assert ready.runner._transcriber is replacement


def test_long_transcription_holds_old_provider_lease_through_switch(ready, monkeypatch):
    class LeasedProvider:
        name = "faster-whisper"

        def __init__(self):
            self.active = 0
            self.retired = self.released = False

        @contextmanager
        def lease(self):
            assert not self.retired
            self.active += 1
            try:
                yield
            finally:
                self.active -= 1
                self.released = self.retired and self.active == 0

        def close(self):
            self.retired = True
            self.released = self.active == 0

    previous, replacement = LeasedProvider(), SimpleNamespace(name="cloud")
    ready.runner._transcriber = previous

    def transcribe(provider, *args, **kwargs):
        assert provider is previous and provider.active == 1
        ready.runner._cfg.set("transcriber.provider", "cloud")
        ready.runner.reload_transcriber(prepared=replacement)
        assert provider.retired and not provider.released
        assert ready.runner._transcriber is replacement
        return Transcript("zh", [Segment(0, 1, "完整的识别结果")])

    monkeypatch.setattr(pipeline, "transcribe_long_audio", transcribe)
    result, _ = ready.runner._stage_transcribe(ready.task, ready.video, Path("unused.wav"))
    assert result.segments[0].text == "完整的识别结果"
    assert previous.released and previous.active == 0


def test_retry_and_terminal_claim_share_one_atomic_owner(tmp_path):
    store = Store(tmp_path / "tasks.db")
    task = store.create_task("https://example.test/video", "best", [], "none")
    store.update_task(task["id"], status="failed")
    barrier = threading.Barrier(8)

    def claim(index):
        barrier.wait(timeout=3)
        return (store.claim_retry if index % 2 else store.claim_terminal_task)(task["id"])

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(claim, range(8)))
    assert sum(results) == 1
    assert store.get_task(task["id"])["status"] in ("pending", "running")
