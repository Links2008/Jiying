"""下载完成后，修正字幕必须保存并用于重生成，不能重新下载或识别。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest
from fastapi import HTTPException

from app.api import server
from app.core.errors import AppError

SRT = "1\n00:00:00,000 --> 00:00:01,000\n识别错字\n\n"
FIXED = SRT.replace("识别错字", "识别正确")


@pytest.fixture
def context(tmp_path):
    app = server.create_app()
    server._runner._cfg.set("paths.keep_source_video", True)
    return SimpleNamespace(app=app, runner=server._runner, store=server._store, root=tmp_path)


def endpoint(context, path, method):
    return next(r.endpoint for r in context.app.routes if r.path == path and method in r.methods)


def artifact(context, task, name, kind, lang, text=SRT):
    directory = context.root / "output" if kind == "bilingual" else context.runner._data / "tmp" / task["id"]
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")
    context.store.add_artifact(task["id"], kind, str(path), lang=lang)
    return next(a for a in context.store.list_artifacts(task["id"]) if a["path"] == str(path))


def ready_task(context, mode="burn", bilingual=False):
    task = context.runner.create_task("https://example.test/video", "best", ["zh"], mode,
                                      output_dir=str(context.root / "output"), bilingual=bilingual)
    video = artifact(context, task, "source.mp4", "video", "", "原始视频")
    context.store.upsert_stage(task["id"], "download", "done")
    context.store.update_task(task["id"], status="completed")
    return task, video


@pytest.mark.parametrize("kind,lang", [("subtitle", "zh"), ("bilingual", "zh+en")])
def test_read_and_save_all_editable_subtitles(context, kind, lang):
    task, _ = ready_task(context)
    item = artifact(context, task, "captions.srt", kind, lang)
    read = endpoint(context, "/api/v1/artifacts/{aid}/content", "GET")
    save = endpoint(context, "/api/v1/artifacts/{aid}/content", "PUT")
    assert read(item["id"])["text"] == SRT
    save(item["id"], server.SubtitleIn(text=FIXED))
    assert read(item["id"])["text"] == FIXED
    assert Path(item["path"]).read_bytes().startswith(b"\xef\xbb\xbf")
    assert context.store.get_task(task["id"])["status"] == "completed"


@pytest.mark.parametrize("text", ["", "只有文字", FIXED + "2\n错误时间轴\n其他句子",
                                   SRT.replace("00:00:01,000", "00:00:00,000"),
                                   SRT.replace("00:00:01,000", "00:90:01,000")])
def test_bad_edit_keeps_existing_subtitle(context, text):
    task, _ = ready_task(context)
    item = artifact(context, task, "captions.srt", "subtitle", "zh")
    before = Path(item["path"]).read_bytes()
    with pytest.raises(HTTPException) as error:
        endpoint(context, "/api/v1/artifacts/{aid}/content", "PUT")(
            item["id"], server.SubtitleIn(text=text))
    assert error.value.status_code == 422
    assert Path(item["path"]).read_bytes() == before


def test_save_during_render_cannot_change_its_input(context):
    task, _ = ready_task(context)
    item = artifact(context, task, "captions.srt", "subtitle", "zh")
    context.store.update_task(task["id"], status="running")
    with pytest.raises(HTTPException) as error:
        endpoint(context, "/api/v1/artifacts/{aid}/content", "PUT")(
            item["id"], server.SubtitleIn(text=FIXED))
    assert error.value.status_code == 409
    assert Path(item["path"]).read_text(encoding="utf-8-sig") == SRT


def test_missing_subtitle_is_not_silently_recreated(context):
    task, _ = ready_task(context)
    item = artifact(context, task, "captions.srt", "subtitle", "zh")
    Path(item["path"]).unlink()
    with pytest.raises(HTTPException) as error:
        endpoint(context, "/api/v1/artifacts/{aid}/content", "PUT")(
            item["id"], server.SubtitleIn(text=FIXED))
    assert error.value.status_code == 404
    assert not Path(item["path"]).exists()


def test_render_uses_selected_language_instead_of_default(context, monkeypatch):
    task, video = ready_task(context)
    artifact(context, task, "zh.srt", "subtitle", "zh")
    chosen = artifact(context, task, "en.srt", "subtitle", "en", FIXED)
    used = []

    def burn(config, source, subtitle, output, **kwargs):
        used.append(Path(subtitle).read_text(encoding="utf-8-sig"))
        output.write_bytes(b"corrected video")

    monkeypatch.setattr("app.pipeline.runner.burn", burn)
    outputs = context.runner.rerender_subtitles(task["id"], "en", artifact_id=chosen["id"])
    assert used == [FIXED]
    assert outputs[0].read_bytes() == b"corrected video"
    assert Path(video["path"]).read_text(encoding="utf-8-sig") == "原始视频"


def test_failed_file_replace_keeps_text_and_releases_task(context, monkeypatch):
    task, _ = ready_task(context)
    item = artifact(context, task, "captions.srt", "subtitle", "zh")
    before = Path(item["path"]).read_bytes()
    original_replace = Path.replace

    def fail_replace(path, target):
        if path.name.startswith(".subtitle-"):
            raise PermissionError("测试文件占用")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(HTTPException) as error:
        endpoint(context, "/api/v1/artifacts/{aid}/content", "PUT")(
            item["id"], server.SubtitleIn(text=FIXED))
    assert error.value.status_code == 500
    assert Path(item["path"]).read_bytes() == before
    assert context.store.get_task(task["id"])["status"] == "completed"
    assert not list(Path(item["path"]).parent.glob(".subtitle-*.tmp"))


def test_shared_legacy_subtitle_cannot_change_another_task(context):
    task, _ = ready_task(context)
    other, _ = ready_task(context)
    item = artifact(context, task, "captions.srt", "subtitle", "zh")
    context.store.add_artifact(other["id"], "subtitle", item["path"], lang="zh")
    with pytest.raises(HTTPException) as error:
        endpoint(context, "/api/v1/artifacts/{aid}/content", "PUT")(
            item["id"], server.SubtitleIn(text=FIXED))
    assert error.value.status_code == 409
    assert Path(item["path"]).read_text(encoding="utf-8-sig") == SRT


@pytest.mark.parametrize("mode", ["burn", "softmux"])
def test_bilingual_edit_survives_render_and_future_edits(context, monkeypatch, mode):
    task, video = ready_task(context, mode, bilingual=True)
    artifact(context, task, "zh.srt", "subtitle", "zh")
    artifact(context, task, "en.srt", "subtitle", "en", SRT.replace("识别错字", "Original"))
    corrected = FIXED.replace("识别正确", "识别正确\nCorrected English")
    chosen = artifact(context, task, "video.zh+en.srt", "bilingual", "zh+en", corrected)
    used = []

    def render(config, source, subtitles, output, **kwargs):
        path = subtitles if mode == "burn" else subtitles[0][0]
        used.append(Path(path).read_text(encoding="utf-8-sig"))
        output.write_bytes(b"corrected video")
        return output

    monkeypatch.setattr(f"app.pipeline.runner.{'burn' if mode == 'burn' else 'softmux'}", render)
    for _ in range(2):
        context.runner.rerender_subtitles(task["id"], "zh+en", artifact_id=chosen["id"])
        assert Path(chosen["path"]).read_text(encoding="utf-8-sig") == corrected
    assert used == [corrected, corrected]
    assert Path(video["path"]).read_text(encoding="utf-8-sig") == "原始视频"


def test_subtitle_from_another_task_cannot_be_rendered(context):
    task, _ = ready_task(context)
    other, _ = ready_task(context)
    chosen = artifact(context, other, "captions.srt", "subtitle", "zh")
    with pytest.raises(AppError):
        context.runner.rerender_subtitles(task["id"], "zh", artifact_id=chosen["id"])
    assert context.store.get_task(task["id"])["status"] == "completed"


def test_rerender_returns_promptly_and_claims_before_background_work(context, monkeypatch):
    task, _ = ready_task(context)
    chosen = artifact(context, task, "captions.srt", "subtitle", "zh", FIXED)
    started, release = threading.Event(), threading.Event()
    used = []

    def burn(config, source, subtitle, output, **kwargs):
        used.append(Path(subtitle).read_text(encoding="utf-8-sig"))
        started.set()
        assert release.wait(5), "测试必须释放渲染线程"
        output.write_bytes(b"corrected video")

    monkeypatch.setattr("app.pipeline.runner.burn", burn)
    rerender = endpoint(context, "/api/v1/tasks/{tid}/rerender", "POST")
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(rerender, task["id"], {"lang": "zh", "artifact_id": chosen["id"], "background": True})
        try:
            assert started.wait(2)
            assert request.result(timeout=.5)["status"] == "running"
            assert context.store.get_task(task["id"])["status"] == "running"
            with pytest.raises(HTTPException) as error:
                rerender(task["id"], {"lang": "zh", "artifact_id": chosen["id"], "background": True})
            assert error.value.status_code == 422
        finally:
            release.set()
            request.result(timeout=3)
    deadline = time.monotonic() + 3
    while context.store.get_task(task["id"])["status"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    assert context.store.get_task(task["id"])["status"] == "completed"
    assert used == [FIXED]
