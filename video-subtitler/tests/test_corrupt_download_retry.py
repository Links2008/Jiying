from pathlib import Path

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.pipeline.runner import PipelineRunner
from app.services import renderer
from app.services.engines import ytdlp_engine
from app.services.engines.base import DirectTarget, DownloadProgress, has_download_bytes


@pytest.mark.parametrize("error", [
    AppError(Codes.RENDER_FAILED, "ffmpeg 视频数据损坏"),
    AppError(Codes.RENDER_FAILED, "ffmpeg 处理失败", detail="Invalid NAL unit size"),
    AppError(Codes.ENG_FAILED, "音视频时长不一致，下载文件不完整"),
])
def test_retry_redownloads_only_confirmed_corrupt_split(monkeypatch, tmp_path, error):
    dest = tmp_path / "failed-task"
    dest.mkdir()
    other = tmp_path / "other-task" / "finished.mp4"
    other.parent.mkdir()
    other.write_bytes(b"other task output")
    unrelated = dest / "captions.srt"
    unrelated.write_bytes(b"keep subtitle")
    output = tmp_path / "user-output.mp4"
    output.write_bytes(b"published video")
    targets = [DirectTarget("https://example.test/video", "clip.f30116.mp4"),
               DirectTarget("https://example.test/audio", "clip.f30280.m4a")]
    calls = []

    class Backend:
        def __init__(self, _): pass
        def ping(self): return True
        def download_many(self, streams, directory, on_progress, **kwargs):
            calls.append(1)
            files = [directory / t.filename for t in streams]
            for path in files:
                if not path.exists():
                    path.write_bytes(b"corrupt" if len(calls) == 1 else b"fresh")
                Path(str(path) + ".aria2").write_bytes(b"resume control")
            on_progress(DownloadProgress(fraction=1))
            return files

    def mux(config, video, audio, merged, **kwargs):
        merged.write_bytes(video.read_bytes())
        if video.read_bytes() == b"corrupt":
            merged.with_suffix(".webm").write_bytes(b"failed fallback")
            raise error
        return merged

    monkeypatch.setattr(ytdlp_engine, "Aria2cBackend", Backend)
    monkeypatch.setattr(renderer, "mux_av", mux)
    engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": True}}}))
    monkeypatch.setattr(engine, "_head_direct_file", lambda *_: None)
    monkeypatch.setattr(engine, "_resolve_split_streams", lambda *_: targets)
    with pytest.raises(AppError) as caught:
        engine.download("https://example.test/watch", "30116", dest)
    assert caught.value is error
    assert not (dest / targets[0].filename).exists(), "retry must not reuse corrupt bytes"
    saved = list(tmp_path.glob("failed-task.invalid-*"))
    assert len(saved) == 1
    assert {p.name for p in saved[0].iterdir()} == {
        t.filename for t in targets} | {t.filename + ".aria2" for t in targets} | {
        "clip.mp4", "clip.webm"}
    assert unrelated.read_bytes() == b"keep subtitle"
    assert other.read_bytes() == b"other task output" and output.read_bytes() == b"published video"
    unrelated.unlink()
    assert not has_download_bytes(dest), "quarantine must not look like resumable media"
    files = engine.download("https://example.test/watch", "30116", dest)
    assert len(calls) == 2 and files[0].read_bytes() == b"fresh"
    assert (saved[0] / targets[0].filename).read_bytes() == b"corrupt"


@pytest.mark.parametrize("error", [
    AppError(Codes.NET_FAILED, "传输中断"),
    AppError(Codes.TASK_CANCELLED, "任务已取消"),
    AppError(Codes.RENDER_FAILED, "ffmpeg 处理失败", detail="No space left on device"),
])
def test_retry_keeps_resumable_or_unconfirmed_split(monkeypatch, tmp_path, error):
    dest = tmp_path / "task"
    targets = [DirectTarget("https://example.test/video", "clip.f30116.mp4"),
               DirectTarget("https://example.test/audio", "clip.f30280.m4a")]

    class Backend:
        def __init__(self, _): pass
        def ping(self): return True
        def download_many(self, streams, directory, on_progress, **kwargs):
            directory.mkdir()
            files = [directory / t.filename for t in streams]
            for path in files:
                path.write_bytes(b"preserve transfer")
                Path(str(path) + ".aria2").write_bytes(b"resume control")
            if error.code != Codes.RENDER_FAILED:
                raise error
            return files

    monkeypatch.setattr(ytdlp_engine, "Aria2cBackend", Backend)
    monkeypatch.setattr(renderer, "mux_av", lambda *a, **kw: (_ for _ in ()).throw(error))
    engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": True}}}))
    monkeypatch.setattr(engine, "_head_direct_file", lambda *_: None)
    monkeypatch.setattr(engine, "_resolve_split_streams", lambda *_: targets)
    with pytest.raises(AppError) as caught:
        engine.download("https://example.test/watch", "30116", dest)
    assert caught.value is error
    for target in targets:
        assert (dest / target.filename).read_bytes() == b"preserve transfer"
        assert (dest / (target.filename + ".aria2")).read_bytes() == b"resume control"
    assert not list(tmp_path.glob("task.invalid-*"))


@pytest.mark.parametrize("error,invalid", [
    ("[E-RENDER-701] ffmpeg 视频数据损坏", True),
    ("[E-RENDER-701] ffmpeg 处理失败（Invalid NAL unit size）", True),
    ("[E-ENG-202] 音视频时长不一致，下载文件不完整", True),
    ("[E-RENDER-701] ffmpeg 处理失败（No space left on device）", False),
    ("[E-NET-002] 传输中断", False),
    ("[E-TASK-402] 任务已取消", False),
])
def test_upgrade_retry_uses_failed_stage_error_before_reset(tmp_path, error, invalid):
    runner = PipelineRunner(Config({}), Store(tmp_path / "tasks.db"))
    runner._data = tmp_path / "data"
    task = runner.create_task("https://example.test/watch", "30116", [], "none")
    dest = runner._data / "tmp" / task["id"]
    dest.mkdir(parents=True)
    legacy = [dest / name for name in (
        "clip.f30116.mp4", "clip.f30280.m4a", "clip.f30116.mp4.aria2",
        "clip.f30280.m4a.aria2", "clip.mp4", "clip.webm")]
    for path in legacy:
        path.write_bytes(b"legacy source")
    unrelated = [dest / "captions.srt", dest / "unrelated.mp4",
                 dest / "different.f999.mp4", dest / "different.mp4"]
    for path in unrelated:
        path.write_bytes(b"preserve")
    published = tmp_path / "finished.mp4"
    published.write_bytes(b"finished")
    runner._store.upsert_stage(task["id"], "download", "failed", error)
    runner._store.update_task(task["id"], status="failed", error=error)
    assert runner._store.claim_retry(task["id"])
    task = runner._store.get_task(task["id"])
    assert task["error"] == "", "claim_retry clears task.error; stage.error must be used"

    def download(current, tid, directory):
        assert all(p.exists() == (not invalid) for p in legacy), "old corrupt sources must be isolated before download"
        assert all(p.read_bytes() == b"preserve" for p in unrelated)
        assert published.read_bytes() == b"finished"
        assert runner._store.stage_status(tid, "download") == "running"
        return published

    runner._download_inner = download
    assert runner._stage_download(task) == published
    saved = list(dest.parent.glob(task["id"] + ".invalid-*"))
    assert len(saved) == int(invalid)
    if invalid:
        assert {p.name for p in saved[0].iterdir()} == {p.name for p in legacy}


@pytest.mark.parametrize("status,error_stage", [
    ("done", "download"), ("cancelled", "download"), ("failed", "render"),
])
def test_upgrade_retry_preserves_finished_and_unrelated_stage_sources(tmp_path, status, error_stage):
    runner = PipelineRunner(Config({}), Store(tmp_path / "tasks.db"))
    runner._data = tmp_path / "data"
    task = runner.create_task("https://example.test/watch", "30116", [], "none")
    dest = runner._data / "tmp" / task["id"]
    dest.mkdir(parents=True)
    source = dest / "clip.f30116.mp4"
    source.write_bytes(b"preserve source")
    merged = dest / "clip.mp4"
    merged.write_bytes(b"completed video")
    runner._store.upsert_stage(task["id"], "download", status)
    runner._store.upsert_stage(task["id"], error_stage, status,
                              "[E-RENDER-701] ffmpeg 视频数据损坏")
    if status == "done":
        runner._store.add_artifact(task["id"], "video", str(merged))
    runner._download_inner = lambda *args: merged

    assert runner._stage_download(task) == merged
    assert source.read_bytes() == b"preserve source"
    assert merged.read_bytes() == b"completed video"
    assert not list(dest.parent.glob(task["id"] + ".invalid-*"))
