"""真实本地 HTTP：重试复用已完成分段，同时验证来源和检查点。"""
from collections import Counter
import json
import time
import pytest
from app.core import cancel
from app.core.errors import AppError
from app.services.downloader import native_ranges
from test_native_ranges import range_server, ydl


def interrupt_after_two_fragments(target, state, ydl, tmp_path):
    task_id = "resume-isolated"
    def progress(_):
        if len(state["ends"]) >= 2:
            cancel.cancel(task_id)
    try:
        with pytest.raises(AppError):
            native_ranges.download_ranges(ydl, target, tmp_path, progress, task_id, 1)
    finally:
        cancel.clear(task_id)


def test_retry_reuses_completed_ranges_and_keeps_exact_bytes(range_server, ydl, tmp_path):
    target, state, payload = range_server
    interrupt_after_two_fragments(target, state, ydl, tmp_path)
    output = native_ranges.download_ranges(ydl, target, tmp_path, concurrency=1)
    assert output.read_bytes() == payload
    assert Counter(state["starts"])[0] == 1


def test_changed_etag_does_not_resume_old_content(range_server, ydl, tmp_path):
    target, state, payload = range_server
    interrupt_after_two_fragments(target, state, ydl, tmp_path)
    state["etag"] = '"new-source"'
    output = native_ranges.download_ranges(ydl, target, tmp_path, concurrency=1)
    assert output.read_bytes() == payload
    assert Counter(state["starts"])[0] == 2


def test_without_validator_restarts_safely(range_server, ydl, tmp_path):
    target, state, payload = range_server
    state["etag"] = ""
    interrupt_after_two_fragments(target, state, ydl, tmp_path)
    assert native_ranges.download_ranges(ydl, target, tmp_path, concurrency=1).read_bytes() == payload
    assert Counter(state["starts"])[0] == 2


def test_broken_checkpoint_cannot_append_misaligned_data(range_server, ydl, tmp_path):
    target, state, payload = range_server
    interrupt_after_two_fragments(target, state, ydl, tmp_path)
    for checkpoint in tmp_path.glob("*.ytdl"):
        data = json.loads(checkpoint.read_text())
        data["downloader"]["current_fragment"]["index"] = 7
        checkpoint.write_text(json.dumps(data), encoding="utf-8")
    assert native_ranges.download_ranges(ydl, target, tmp_path, concurrency=1).read_bytes() == payload


def test_parallel_fragments_share_the_configured_rate_limit(range_server, ydl, tmp_path):
    target, _, payload = range_server
    limit = 4 * 1024 * 1024
    ydl.params['ratelimit'] = limit
    started = time.monotonic()
    assert native_ranges.download_ranges(ydl, target, tmp_path, concurrency=4).read_bytes() == payload
    assert time.monotonic() - started >= len(payload) / limit * .7
