import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Config, DEFAULTS
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.pipeline.runner import PipelineRunner


class FakeRouter:
    def download(self, url, resolution, dest_dir, on_progress=None, task_id=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        video = dest_dir / "clip.mp4"
        video.write_bytes(b"video")
        return "fake", [video]


def test_download_remains_complete_when_subtitles_fail(tmp_path):
    runner = PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    runner._router = FakeRouter()
    runner._stage_extract = lambda *_: (_ for _ in ()).throw(
        AppError(Codes.ASR_FAILED, "模型不可用", retryable=True))
    output = tmp_path / "downloads"
    task = runner.create_task("https://example.com/clip.mp4", "best", ["zh"],
                              "softmux", output_dir=str(output))

    result = runner.run(task)

    assert result["status"] == "completed"
    assert "视频已下载" in json.loads(result["error"])["message"]
    videos = [a for a in runner._store.list_artifacts(task["id"])
              if a["kind"] == "video"]
    assert len(videos) == 1
    video_path = Path(videos[0]["path"])
    assert video_path.parent == output
    assert video_path.read_bytes() == b"video"


def test_subtitle_sidecar_stays_in_task_work_directory(tmp_path):
    runner = PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    runner._data = tmp_path / "work"
    output = tmp_path / "downloads"
    task = runner.create_task("https://example.com/clip.mp4", "best", ["zh"],
                              "burn", output_dir=str(output))

    assert runner._srt_path(task, "clip", "zh") == runner._data / "tmp" / task["id"] / "clip.zh.srt"


def test_video_only_never_starts_subtitle_stages(tmp_path):
    runner = PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    runner._router = FakeRouter()
    runner._stage_extract = lambda *_: (_ for _ in ()).throw(
        AssertionError("video-only task must not extract audio"))
    task = runner.create_task("https://example.com/clip.mp4", "best", [], "none",
                              output_dir=str(tmp_path / "downloads"))

    result = runner.run(task)

    assert result["status"] == "completed"
    assert result["error"] == ""
    assert [a["kind"] for a in runner._store.list_artifacts(task["id"])] == ["video"]
    assert [s["stage"] for s in runner._store.list_stages(task["id"])] == ["download"]


@pytest.mark.parametrize("mode", ["none", "softmux", "burn"])
def test_same_named_tasks_never_overwrite_each_other(tmp_path, monkeypatch, mode):
    runner = PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    runner._data = tmp_path / "work"
    output = tmp_path / "downloads"

    class DistinctRouter:
        def download(self, url, resolution, dest_dir, on_progress=None, task_id=None):
            dest_dir.mkdir(parents=True, exist_ok=True)
            video = dest_dir / "clip.mp4"
            video.write_bytes(url.encode())
            return "fake", [video]

    runner._router = DistinctRouter()
    if mode == "softmux":
        runner._stage_extract = lambda *_: (_ for _ in ()).throw(
            AppError(Codes.ASR_FAILED, "ASR failed"))
    elif mode == "burn":
        runner._stage_extract = lambda *_: tmp_path / "audio.wav"

        def transcribe(task, *_):
            srt = runner._srt_path(task, "clip", "zh")
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
            return SimpleNamespace(language="zh"), srt

        runner._stage_transcribe = transcribe
        monkeypatch.setattr("app.pipeline.runner.burn",
                            lambda cfg, video, srt, out, **kw: out.write_bytes(video.read_bytes()))

    paths = []
    for name in ("first", "second"):
        task = runner.create_task(f"https://example.test/{name}", "best", [], mode,
                                  title="clip", output_dir=str(output))
        assert runner.run(task)["status"] == "completed"
        kind = "output" if mode == "burn" else "video"
        paths.append(Path(next(a["path"] for a in runner._store.list_artifacts(task["id"])
                               if a["kind"] == kind)))

    assert paths[0] != paths[1]
    assert [p.read_bytes() for p in paths] == [
        b"https://example.test/first", b"https://example.test/second"]


def test_retry_copies_a_legacy_video_shared_by_another_task(tmp_path):
    runner = PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    runner._data = tmp_path / "work"
    output = tmp_path / "downloads"
    output.mkdir()
    shared = output / "clip.mp4"
    shared.write_bytes(b"shared")
    first = runner.create_task("https://example.test/first", "best", [], "softmux",
                               output_dir=str(output))
    second = runner.create_task("https://example.test/second", "best", [], "none",
                                output_dir=str(output))
    for task in (first, second):
        runner._store.add_artifact(task["id"], "video", str(shared))
    runner._store.upsert_stage(first["id"], "download", "done")

    private = runner._stage_download(first)

    assert private.read_bytes() == b"shared"
    assert shared.read_bytes() == b"shared"
    assert runner._store.list_artifacts(second["id"])[0]["path"] == str(shared)


@pytest.mark.parametrize("bilingual", [False, True])
def test_burn_exposes_only_selected_video(tmp_path, monkeypatch, bilingual):
    runner = PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    runner._data = tmp_path / "work"
    runner._router = FakeRouter()
    runner._stage_extract = lambda *_: runner._data / "audio.wav"
    output = tmp_path / "downloads"
    task = runner.create_task("https://example.com/clip.mp4", "720p", ["zh"],
                              "burn", output_dir=str(output), bilingual=bilingual)

    def subtitle(lang):
        path = runner._srt_path(task, "clip", lang)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"1\n00:00:00,000 --> 00:00:01,000\n{lang}\n\n", encoding="utf-8")
        runner._store.add_artifact(task["id"], "subtitle", str(path), lang=lang)
        return path

    runner._stage_transcribe = lambda *_: (SimpleNamespace(language="fr"), subtitle("fr"))
    runner._stage_translate = lambda *_: {"zh": subtitle("zh")}
    monkeypatch.setattr("app.pipeline.runner.burn", lambda cfg, video, srt, out, **kw: out.write_bytes(b"burned"))

    assert runner.run(task)["status"] == "completed"
    files = list(output.iterdir())
    videos = [p for p in files if p.suffix == ".mp4"]
    assert len(videos) == 1 and videos[0].name.endswith(".burned.mp4")
    assert videos[0].read_bytes() == b"burned"
    merged = [p for p in files if p.suffix == ".srt"]
    assert len(merged) == int(bilingual)
    if bilingual:
        assert "zh" in merged[0].read_text(encoding="utf-8-sig")
        assert "fr" in merged[0].read_text(encoding="utf-8-sig")
    artifacts = runner._store.list_artifacts(task["id"])
    assert all(Path(a["path"]).exists() for a in artifacts if a["kind"] in ("video", "subtitle", "bilingual", "output"))
    assert all(Path(a["path"]).parent != output for a in artifacts if a["kind"] in ("video", "subtitle"))
    assert sum(a["kind"] == "bilingual" for a in artifacts) == int(bilingual)

    runner.rerender_subtitles(task["id"], "fr")
    assert sorted(output.iterdir()) == sorted(files), "editing a subtitle must replace the same selected output"
    assert not list((runner._data / "tmp" / task["id"]).glob("*+*.srt"))


@pytest.mark.parametrize("code", [Codes.NET_FAILED, Codes.TASK_CANCELLED])
def test_interrupted_download_keeps_resume_files(tmp_path, code):
    runner = PipelineRunner(Config(DEFAULTS), Store(tmp_path / "tasks.db"))
    task = runner.create_task("https://example.test/video", "best", [], "none",
                              output_dir=str(tmp_path / "downloads"))

    class InterruptedRouter:
        def download(self, url, resolution, dest_dir, on_progress=None, **kwargs):
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "video.mp4.part").write_bytes(b"already downloaded")
            (dest_dir / "video.mp4.ytdl").write_bytes(b"resume checkpoint")
            raise AppError(code, "interrupted", retryable=True)

    runner._router = InterruptedRouter()
    with pytest.raises(AppError):
        runner.run(task)
    work = runner._data / "tmp" / task["id"]
    assert (work / "video.mp4.part").read_bytes() == b"already downloaded"
    assert (work / "video.mp4.ytdl").read_bytes() == b"resume checkpoint"
