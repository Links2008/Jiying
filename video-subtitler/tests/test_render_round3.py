"""渲染第三轮回归：真实路径、音轨兼容和任务取消。"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from app.core import cancel
from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services import renderer


@pytest.fixture
def real_ffmpeg():
    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要显式指定真实 FFmpeg")
    return ffmpeg


@pytest.mark.skipif(os.name == "nt", reason="Windows 不允许文件名包含反斜杠")
@pytest.mark.parametrize("name", [r"nested\captions.srt", "quote'caption\\:[v],;.srt"])
def test_real_filter_uses_literal_path_without_selecting_another_subtitle(tmp_path, real_ffmpeg, name):
    content = "1\n00:00:00,000 --> 00:00:01,000\nCorrect subtitle\n"
    intended = tmp_path / name
    intended.write_text(content, encoding="utf-8")
    # 同时放置错误转换后所指向的另一份字幕，以验证没有悄悄选错文件。
    wrong = tmp_path / name.replace("\\", "/")
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_text(content.replace("Correct subtitle", "WRONG FILE"), encoding="utf-8")
    reference = tmp_path / "plain.srt"
    reference.write_text(content, encoding="utf-8")

    def frame(path):
        return subprocess.check_output([real_ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                                        "color=black:s=640x360:d=1", "-vf",
                                        renderer._subtitle_filter(path, "DejaVu Sans"),
                                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])

    expected = frame(reference)
    assert len(expected) == 640 * 360 and sum(value > 100 for value in expected) > 100
    assert frame(intended) == expected


def test_real_mov_stream_id_keeps_pcm_audio_on_aac_compatibility_path(tmp_path, real_ffmpeg):
    source = tmp_path / "camera.mov"
    subprocess.run([real_ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=black:s=320x180:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264",
                    "-c:a", "pcm_s16le", "-shortest", str(source)], check=True)
    subtitle = tmp_path / "caption.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    config = Config({"paths": {"ffmpeg": real_ffmpeg}, "render": {"encoder": "cpu"}})
    assert renderer.audio_codec(config, source) == "pcm_s16le"
    output = renderer.burn(config, source, subtitle, tmp_path / "output.mp4", font_name="DejaVu Sans")
    assert renderer.audio_codec(config, output) == "aac"


def test_pre_cancelled_render_does_not_start_a_process(monkeypatch):
    task_id = "render-cancelled-before-spawn-round3"

    def forbidden(*args, **kwargs):
        raise AssertionError("已取消任务不能启动可能覆盖输出的 FFmpeg")

    monkeypatch.setattr(renderer.subprocess, "Popen", forbidden)
    cancel.cancel(task_id)
    try:
        with pytest.raises(AppError) as error:
            renderer._run(["ffmpeg"], task_id=task_id)
        assert error.value.code == Codes.TASK_CANCELLED
    finally:
        cancel.clear(task_id)


def test_cancelling_burn_terminates_audio_probe_before_starting_encoding(tmp_path, monkeypatch):
    script = tmp_path / "slow_probe.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    subtitle = tmp_path / "caption.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    original_popen = subprocess.Popen
    children = []
    started = threading.Event()

    def popen(args, **kwargs):
        child = original_popen([sys.executable, str(script)], **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(renderer.subprocess, "Popen", popen)
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "isolated-ffmpeg")
    monkeypatch.setattr(renderer, "pick_encoder", lambda _: "libx264")
    task_id = "render-audio-probe-cancel-round3"
    workers = ThreadPoolExecutor(max_workers=1)
    try:
        result = workers.submit(renderer.burn, Config({}), tmp_path / "source.mp4", subtitle,
                                tmp_path / "output.mp4", font_name="DejaVu Sans", task_id=task_id)
        assert started.wait(2)
        cancel.cancel(task_id)
        with pytest.raises(AppError) as error:
            result.result(timeout=1)
        assert error.value.code == Codes.TASK_CANCELLED
        assert len(children) == 1, "取消探测后不能开始编码或容器回退"
        assert children[0].poll() is not None
        assert not cancel._procs.get(task_id)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
        workers.shutdown(wait=True)
        cancel.clear(task_id)


@pytest.mark.parametrize("task_id", [None, "audio-probe-invalid-utf8-round3"])
def test_audio_probe_replaces_invalid_metadata_bytes(tmp_path, monkeypatch, task_id):
    script = tmp_path / "metadata.py"
    script.write_text("import sys\nsys.stderr.buffer.write(b'Stream #0:1[0x2](eng): Audio: vorbis\\n\\xff')\n",
                      encoding="utf-8")
    original_popen = subprocess.Popen
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "isolated-ffmpeg")
    monkeypatch.setattr(renderer.subprocess, "Popen", lambda args, **kwargs:
                        original_popen([sys.executable, str(script)], **kwargs))
    try:
        if task_id:
            assert renderer.audio_codec(Config({}), tmp_path / "video", task_id) == "vorbis"
        else:
            assert renderer.audio_codec(Config({}), tmp_path / "video") == "vorbis"
    finally:
        if task_id:
            cancel.clear(task_id)
