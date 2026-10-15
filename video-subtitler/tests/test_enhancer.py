"""删除超分执行能力后，历史任务、原片和成品仍可继续使用。"""
import sqlite3
from copy import deepcopy

import pytest

from app.core.config import Config, DEFAULTS
from app.models.db import Store
from app.pipeline.runner import PipelineRunner


def test_pre_anime_database_keeps_old_task(tmp_path):
    path = tmp_path / "tasks.db"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE task (id TEXT PRIMARY KEY, url TEXT NOT NULL, url_key TEXT NOT NULL, "
            "title TEXT DEFAULT '', status TEXT DEFAULT 'pending', resolution TEXT DEFAULT 'best', "
            "target_langs TEXT DEFAULT '', subtitle_mode TEXT DEFAULT 'none', error TEXT DEFAULT '', "
            "created_at TEXT NOT NULL, finished_at TEXT DEFAULT '')")
        old.execute("INSERT INTO task (id,url,url_key,created_at) VALUES ('old','x','x','today')")
    task = Store(path).get_task("old")
    assert task["id"] == "old" and task["created_at"] == "today"
    assert task["enhance"] == 0 and task["enhance_scale"] == 2


@pytest.mark.parametrize("rerender", [False, True])
def test_old_anime_task_never_runs_enhancement_and_keeps_files(tmp_path, monkeypatch, rerender):
    store = Store(tmp_path / "tasks.db")
    task = store.create_task("x", "best", [], "burn" if rerender else "none",
                             output_dir=str(tmp_path / "downloads"))
    tid = task["id"]
    store._exec("UPDATE task SET enhance=1,enhance_scale=4,status='failed' WHERE id=?", (tid,))
    data = tmp_path / "data"
    work = data / "tmp" / tid
    work.mkdir(parents=True)
    source, enhanced, subtitle = work / "source.mp4", work / "source.anime-x4.mp4", work / "zh.srt"
    subtitle_bytes = b"1\n00:00:00,000 --> 00:00:01,000\nsubtitle\n\n"
    for path, contents in ((source, b"original"), (enhanced, b"old-enhanced"), (subtitle, subtitle_bytes)):
        path.write_bytes(contents)
    store.add_artifact(tid, "video", str(enhanced), "anime-x4")
    store.add_artifact(tid, "video", str(source), "1080p")
    store.add_artifact(tid, "subtitle", str(subtitle), lang="zh")
    store.upsert_stage(tid, "download", "done")
    store.upsert_stage(tid, "enhance", "running")
    assert store.claim_retry(tid)
    store.update_task(tid, status="failed")
    config = Config(deepcopy(DEFAULTS))
    config.set("paths.keep_source_video", False)
    runner = PipelineRunner(config, store)
    runner._data = data
    enhancement_calls, rendered = [], []
    def forbidden(*args):
        enhancement_calls.append(args)
        raise AssertionError("不应运行已移除的超分")
    monkeypatch.setattr(runner, "_stage_enhance", forbidden, raising=False)
    monkeypatch.setattr(runner, "_stage_render", lambda task, video, subs: rendered.append(video) or [])
    if rerender:
        runner.rerender_subtitles(tid, "zh")
        assert rendered == [source]
    else:
        assert runner.run(store.get_task(tid))["error"] == ""
    assert enhancement_calls == []
    retained = Store(tmp_path / "tasks.db")
    assert retained.get_task(tid)["enhance_scale"] == 4
    assert {a["path"] for a in retained.list_artifacts(tid)} == {str(source), str(enhanced), str(subtitle)}
    assert source.read_bytes() == b"original"
    assert enhanced.read_bytes() == b"old-enhanced"
    assert subtitle.read_bytes() == subtitle_bytes
