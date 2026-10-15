"""分离流合并必须检查实际视频时长；真实回归使用发布包 FFmpeg。"""
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.pipeline.runner import PipelineRunner
from app.services import renderer


@pytest.mark.parametrize("corrupt", [False, True])
def test_fast_ffmpeg_reports_its_final_time(monkeypatch, corrupt):
    def popen(*args, **kwargs):
        kwargs["stderr"].write(b"warning: deprecated pixel format\n")
        if corrupt:
            kwargs["stderr"].write(b"Invalid NAL unit size (34534444 > 2126).\n")
        kwargs["stderr"].write(b"frame=5400 time=00:01:30.00 speed=999x\n")
        return SimpleNamespace(poll=lambda: 0)

    monkeypatch.setattr(renderer.subprocess, "Popen", popen)
    reported = []
    if corrupt:
        with pytest.raises(AppError) as error:
            renderer._run(["ffmpeg"], on_time=reported.append)
        assert error.value.code == Codes.RENDER_FAILED
    else:
        renderer._run(["ffmpeg"], on_time=reported.append)
    assert reported == [90.0]


def test_duration_probe_reads_utf8_metadata(monkeypatch):
    def run(*args, **kwargs):
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"
        return SimpleNamespace(stderr="title: 中文\nDuration: 00:12:14.70\n")

    monkeypatch.setattr(renderer, "find_ffmpeg", lambda cfg: "ffmpeg")
    monkeypatch.setattr(renderer.subprocess, "run", run)
    assert renderer.probe_duration(Config({}), Path("video.mp4")) == 734.7


@pytest.mark.parametrize("duration,effective", [(2.02, 1.99), (100, 99.6), (1000, 999.6), (0, None)])
def test_mux_allows_codec_tolerance_and_unknown_duration(tmp_path, monkeypatch, duration, effective):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if kwargs.get("on_time"):
            kwargs["on_time"](effective)

    monkeypatch.setattr(renderer, "find_ffmpeg", lambda cfg: "ffmpeg")
    monkeypatch.setattr(renderer, "probe_duration", lambda cfg, path: duration)
    monkeypatch.setattr(renderer, "_run", run)
    monkeypatch.setattr(renderer, "_video_packet_duration", lambda *_: effective)
    output = tmp_path / "merged.mp4"
    assert renderer.mux_av(Config({}), Path("video"), Path("audio"), output) == output
    assert len(calls) == (1 if duration == 0 else 2)
    if duration:
        assert calls[-1][-6:] == ["0:v:0", "-c", "copy", "-f", "null", "-"]


@pytest.mark.parametrize("fallback", [False, True])
def test_incomplete_video_fails_outside_container_fallback(tmp_path, monkeypatch, fallback):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if fallback and len(calls) == 1:
            raise AppError(Codes.RENDER_FAILED, "MP4 incompatible")
        if kwargs.get("on_time"):
            kwargs["on_time"](90)

    monkeypatch.setattr(renderer, "find_ffmpeg", lambda cfg: "ffmpeg")
    monkeypatch.setattr(renderer, "probe_duration", lambda cfg, path: 734)
    monkeypatch.setattr(renderer, "_run", run)
    monkeypatch.setattr(renderer, "_video_packet_duration", lambda *_: 90)
    with pytest.raises(AppError) as error:
        renderer.mux_av(Config({}), Path("video"), Path("audio"), tmp_path / "merged.mp4")
    assert error.value.code == Codes.ENG_FAILED
    assert len(calls) == (3 if fallback else 2)


@pytest.fixture(scope="module")
def real_split_inputs(tmp_path_factory):
    executable = os.environ.get("JIYING_TEST_FFMPEG", "")
    if not executable or not Path(executable).is_file():
        pytest.skip("set JIYING_TEST_FFMPEG to the packaged FFmpeg executable")
    work = tmp_path_factory.mktemp("split-integrity")
    video, audio = work / "video.mp4", work / "audio.m4a"
    for args in (["-f", "lavfi", "-i", "color=s=16x16:r=1:d=90", "-c:v", "libx264", str(video)],
                 ["-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "734", "-c:a", "aac", str(audio)]):
        subprocess.run([executable, "-y", "-loglevel", "error", *args],
                       check=True, capture_output=True, timeout=30,
                       creationflags=renderer._NO_WINDOW)
    cfg = Config({"paths": {"ffmpeg": executable}})
    assert renderer.probe_duration(cfg, video) == pytest.approx(90, abs=.2)
    assert renderer.probe_duration(cfg, audio) == pytest.approx(734, abs=.2)
    return cfg, video, audio


def test_real_90_second_video_with_734_second_headers_is_rejected(real_split_inputs, tmp_path, monkeypatch):
    cfg, video, audio = real_split_inputs
    original = (video.read_bytes(), audio.read_bytes())
    # Fragmented MP4 can declare full duration despite containing only early packets.
    monkeypatch.setattr(renderer, "probe_duration", lambda cfg, path: 734)
    with pytest.raises(AppError) as error:
        renderer.mux_av(cfg, video, audio, tmp_path / "merged.mp4")
    assert error.value.code == Codes.ENG_FAILED
    assert (video.read_bytes(), audio.read_bytes()) == original
    assert not (tmp_path / "merged.webm").exists()


def test_video_only_pipeline_fails_and_preserves_split_inputs(real_split_inputs, tmp_path, monkeypatch):
    cfg, video, audio = real_split_inputs
    runner = PipelineRunner(cfg, Store(tmp_path / "tasks.db"))
    runner._data = tmp_path / "work"
    monkeypatch.setattr(renderer, "probe_duration", lambda cfg, path, task_id=None: 734)

    def download(url, resolution, dest_dir, on_progress=None, task_id=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        split_video, split_audio = dest_dir / video.name, dest_dir / audio.name
        shutil.copy2(video, split_video)
        shutil.copy2(audio, split_audio)
        merged = renderer.mux_av(cfg, split_video, split_audio, dest_dir / "merged.mp4", task_id=task_id)
        return "fake", [merged]

    runner._router = SimpleNamespace(download=download)
    output = tmp_path / "downloads"
    task = runner.create_task("https://example.test/video", "4k", [], "none", output_dir=str(output))
    with pytest.raises(AppError) as error:
        runner.run(task)
    assert error.value.code == Codes.ENG_FAILED
    assert runner._store.get_task(task["id"])["status"] == "failed"
    assert runner._store.stage_status(task["id"], "download") == "failed"
    assert runner._store.list_artifacts(task["id"]) == []
    work = runner._data / "tmp" / task["id"]
    assert (work / video.name).read_bytes() == video.read_bytes()
    assert (work / audio.name).read_bytes() == audio.read_bytes()
    assert not output.exists()
