"""时长探测使用隔离脚本子进程，验证取消和超时没有遗留进程。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time

import pytest

from app.core import cancel
from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services import renderer


@pytest.fixture
def processes(tmp_path, monkeypatch):
    children = []
    original_popen = renderer.subprocess.Popen

    def use_script(text):
        script = tmp_path / "isolated_probe.py"
        script.write_text(text, encoding="utf-8")

        def popen(args, **kwargs):
            child = original_popen([sys.executable, "-u", str(script)], **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "isolated-ffmpeg")
        monkeypatch.setattr(renderer.subprocess, "Popen", popen)
        return children

    yield use_script
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)


@pytest.mark.parametrize("task_id", [None, "duration-probe-readable"])
def test_duration_probe_reads_metadata_even_with_ffmpeg_nonzero_exit(processes, task_id):
    children = processes("import sys\nsys.stderr.write('中文标题\\nDuration: 01:12:14.70\\n')\nsys.exit(1)\n")
    try:
        assert renderer.probe_duration(Config({}), Path("isolated-video.mp4"), task_id=task_id) == 4334.7
        assert children[0].poll() is not None
        assert not cancel._procs.get(task_id)
    finally:
        if task_id:
            cancel.clear(task_id)


def test_running_probe_is_cancelled_and_reaped_before_timeout(processes, monkeypatch):
    children = processes("import time\ntime.sleep(30)\n")
    task_id = "duration-probe-running"
    registered = threading.Event()
    original_register = cancel.register

    def register(tid, child):
        original_register(tid, child)
        registered.set()

    monkeypatch.setattr(cancel, "register", register)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            result = workers.submit(renderer.probe_duration, Config({}), Path("isolated-video.mp4"), task_id)
            assert registered.wait(2), "探测进程必须登记为可取消的任务进程"
            start = time.monotonic()
            cancel.cancel(task_id)
            with pytest.raises(AppError) as error:
                result.result(timeout=2)
            assert error.value.code == Codes.TASK_CANCELLED
            assert time.monotonic() - start < 1
        assert children[0].poll() is not None
        assert not cancel._procs.get(task_id)
    finally:
        cancel.clear(task_id)


def test_cancellation_between_spawn_and_registration_still_reaps_child(processes, monkeypatch):
    children = processes("import time\ntime.sleep(30)\n")
    task_id = "duration-probe-registration-race"
    original_register = cancel.register

    def register(tid, child):
        cancel.cancel(tid)
        original_register(tid, child)

    monkeypatch.setattr(cancel, "register", register)
    try:
        with pytest.raises(AppError) as error:
            renderer.probe_duration(Config({}), Path("isolated-video.mp4"), task_id)
        assert error.value.code == Codes.TASK_CANCELLED
        assert children[0].poll() is not None
        assert not cancel._procs.get(task_id)
    finally:
        cancel.clear(task_id)


def test_already_cancelled_probe_does_not_spawn(monkeypatch):
    task_id = "duration-probe-before-spawn"

    def forbidden(*args, **kwargs):
        raise AssertionError("取消后的探测不应创建子进程")

    monkeypatch.setattr(renderer.subprocess, "Popen", forbidden)
    cancel.cancel(task_id)
    try:
        with pytest.raises(AppError) as error:
            renderer.probe_duration(Config({}), Path("isolated-video.mp4"), task_id)
        assert error.value.code == Codes.TASK_CANCELLED
    finally:
        cancel.clear(task_id)


@pytest.mark.parametrize("task_id", [None, "duration-probe-timeout"])
def test_probe_timeout_returns_unknown_and_reaps_child(processes, monkeypatch, task_id):
    children = processes("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(renderer, "_DURATION_PROBE_TIMEOUT", .1, raising=False)
    try:
        assert renderer.probe_duration(Config({}), Path("isolated-video.mp4"), task_id=task_id) == 0
        assert children[0].poll() is not None
        assert not cancel._procs.get(task_id)
    finally:
        if task_id:
            cancel.clear(task_id)


@pytest.mark.parametrize("task_id", [None, "duration-probe-invalid"])
def test_failed_metadata_probe_keeps_unknown_duration_compatibility(processes, task_id):
    children = processes("import sys\nsys.stderr.write('Invalid input\\n')\nsys.exit(1)\n")
    try:
        assert renderer.probe_duration(Config({}), Path("isolated-video.mp4"), task_id=task_id) == 0
        assert children[0].poll() is not None
        assert not cancel._procs.get(task_id)
    finally:
        if task_id:
            cancel.clear(task_id)


def test_probe_launch_failure_still_returns_unknown(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("模拟工具无法启动")

    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "isolated-ffmpeg")
    monkeypatch.setattr(renderer.subprocess, "Popen", fail)
    assert renderer.probe_duration(Config({}), Path("isolated-video.mp4")) == 0
    assert renderer.probe_duration(Config({}), Path("isolated-video.mp4"), "duration-probe-unavailable") == 0


@pytest.mark.parametrize("entry", ["mux", "burn"])
def test_duration_cancellation_reaches_media_entry_before_processing(tmp_path, monkeypatch, entry):
    task_id = f"duration-probe-entry-{entry}"
    probed, processed = [], []

    def probe(config, media, task_id=None):
        probed.append(task_id)
        if task_id:
            raise AppError(Codes.TASK_CANCELLED, "模拟探测期间取消")
        return 0.0

    monkeypatch.setattr(renderer, "probe_duration", probe)
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "isolated-ffmpeg")
    monkeypatch.setattr(renderer, "_run", lambda args, **kwargs: processed.append(args))
    monkeypatch.setattr(renderer, "pick_encoder", lambda _: "h264_nvenc")
    monkeypatch.setattr(renderer, "audio_args", lambda *args: [])
    output = tmp_path / "output.mp4"
    with pytest.raises(AppError) as error:
        if entry == "mux":
            renderer.mux_av(Config({}), tmp_path / "isolated-video", tmp_path / "isolated-audio",
                            output, task_id=task_id)
        else:
            subtitle = tmp_path / "edited.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n编辑字幕\n", encoding="utf-8")
            renderer.burn(Config({}), tmp_path / "isolated-video", subtitle, output,
                          task_id=task_id, on_progress=lambda _: None, vertical_offset=24)
    assert error.value.code == Codes.TASK_CANCELLED
    assert probed == [task_id]
    assert processed == [], "探测取消后不得开始合流、容器回退或字幕烧录"
    assert not output.exists() and not output.with_suffix(".webm").exists()
    assert not output.with_suffix(".position.ass").exists()
