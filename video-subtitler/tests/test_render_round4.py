"""渲染第四轮回归：输出别名、失败后的旧产物、多音轨兼容。"""
import os
from pathlib import Path
import subprocess

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services import renderer


@pytest.fixture
def real_ffmpeg():
    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要显式指定真实 FFmpeg")
    return ffmpeg


@pytest.mark.parametrize("entry", ["extract", "burn", "softmux", "mux"])
@pytest.mark.parametrize("alias_kind", ["same", "symlink", "hardlink"])
def test_outputs_that_alias_an_input_are_rejected_before_processing(tmp_path, monkeypatch, entry, alias_kind):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original input")
    output = source if alias_kind == "same" else tmp_path / "alias.mp4"
    if alias_kind == "symlink":
        output.symlink_to(source)
    elif alias_kind == "hardlink":
        os.link(source, output)
    subtitle = tmp_path / "sub.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"original audio")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"overwritten")

    monkeypatch.setattr(renderer, "_run", run)
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "ffmpeg")
    monkeypatch.setattr(renderer, "audio_args", lambda *a: [])
    monkeypatch.setattr(renderer, "probe_duration", lambda *a, **kw: 0)
    with pytest.raises(AppError) as error:
        if entry == "extract":
            renderer.extract_audio(Config({}), source, output)
        elif entry == "burn":
            renderer.burn(Config({"render": {"encoder": "cpu"}}), source, subtitle,
                          output, font_name="Arial")
        elif entry == "softmux":
            renderer.softmux(Config({}), source, [(subtitle, "en")], output)
        else:
            renderer.mux_av(Config({}), source, audio, output)
    assert error.value.code == Codes.CFG_INVALID
    assert not calls
    assert source.read_bytes() == b"original input"


def test_real_extract_does_not_overwrite_original_wav_through_symlink(tmp_path, real_ffmpeg):
    source, output = tmp_path / "original.wav", tmp_path / "alias.wav"
    subprocess.run([real_ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", str(source)], check=True)
    output.symlink_to(source)
    before = source.read_bytes()
    with pytest.raises(AppError) as error:
        renderer.extract_audio(Config({"paths": {"ffmpeg": real_ffmpeg}}), source, output)
    assert error.value.code == Codes.CFG_INVALID
    assert source.read_bytes() == before and output.is_symlink()


@pytest.mark.parametrize("existing", [False, True])
def test_real_softmux_fallback_preserves_old_mp4_and_removes_failed_temporary(tmp_path, real_ffmpeg, existing):
    video, subtitle, output = tmp_path / "source.webm", tmp_path / "sub.srt", tmp_path / "output.mp4"
    subprocess.run([real_ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=black:s=32x32:d=1",
                    "-c:v", "libvpx", str(video)], check=True)
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    if existing:
        output.write_bytes(b"previous complete MP4")
    original = video.read_bytes()
    result = renderer.softmux(Config({"paths": {"ffmpeg": real_ffmpeg}}), video, [(subtitle, "en")], output)
    assert result == output.with_suffix(".mkv") and result.stat().st_size > 0
    if existing:
        assert output.read_bytes() == b"previous complete MP4"
    else:
        assert not output.exists()
    assert video.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ["source.webm", "sub.srt", "output.mkv"] + (["output.mp4"] if existing else []))


@pytest.mark.parametrize("entry", ["extract", "burn", "softmux", "mux"])
def test_failed_conversion_keeps_previous_outputs_and_removes_partial_files(tmp_path, monkeypatch, entry):
    source, subtitle, output = tmp_path / "source.mp4", tmp_path / "sub.srt", tmp_path / "output.mp4"
    source.write_bytes(b"original input")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    output.write_bytes(b"previous complete output")
    outputs = [output, output.with_suffix(".mkv"), output.with_suffix(".webm")]
    for path in outputs[1:]:
        path.write_bytes(b"previous fallback output")

    def run(args, **kwargs):
        Path(args[-1]).write_bytes(b"incomplete output")
        raise AppError(Codes.RENDER_FAILED, "模拟 FFmpeg 写出部分数据后失败")

    monkeypatch.setattr(renderer, "_run", run)
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "ffmpeg")
    monkeypatch.setattr(renderer, "audio_args", lambda *a: [])
    monkeypatch.setattr(renderer, "probe_duration", lambda *a, **kw: 0)
    with pytest.raises(AppError):
        if entry == "extract":
            renderer.extract_audio(Config({}), source, output)
        elif entry == "burn":
            renderer.burn(Config({"render": {"encoder": "cpu"}}), source, subtitle, output,
                          font_name="Arial", vertical_offset=20)
        elif entry == "softmux":
            renderer.softmux(Config({}), source, [(subtitle, "en")], output)
        else:
            renderer.mux_av(Config({}), source, tmp_path / "audio.wav", output)
    assert output.read_bytes() == b"previous complete output"
    assert all(path.read_bytes() == b"previous fallback output" for path in outputs[1:])
    assert source.read_bytes() == b"original input"
    assert len(list(tmp_path.iterdir())) == 5


def test_mux_publishes_only_after_duration_integrity_check(tmp_path, monkeypatch):
    output = tmp_path / "output.mp4"
    output.write_bytes(b"previous complete output")

    def run(args, **kwargs):
        if kwargs.get("on_time"):
            kwargs["on_time"](1)
        else:
            Path(args[-1]).write_bytes(b"truncated media")

    monkeypatch.setattr(renderer, "_run", run)
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "ffmpeg")
    monkeypatch.setattr(renderer, "probe_duration", lambda *a, **kw: 100)
    with pytest.raises(AppError) as error:
        renderer.mux_av(Config({}), tmp_path / "source.mp4", tmp_path / "audio.wav", output)
    assert error.value.code == Codes.ENG_FAILED
    assert output.read_bytes() == b"previous complete output"
    assert list(tmp_path.iterdir()) == [output]


def test_real_multitrack_pcm_is_converted_even_when_first_track_is_aac(tmp_path, real_ffmpeg):
    video, subtitle, output = tmp_path / "source.mkv", tmp_path / "sub.srt", tmp_path / "output.mp4"
    subprocess.run([real_ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=black:s=32x32:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-map", "0:v", "-map", "1:a",
                    "-map", "1:a", "-c:v", "libx264", "-c:a:0", "aac", "-c:a:1", "pcm_s16le", str(video)], check=True)
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    renderer.softmux(Config({"paths": {"ffmpeg": real_ffmpeg}}), video, [(subtitle, "en")], output)
    info = subprocess.run([real_ffmpeg, "-hide_banner", "-i", str(output)], capture_output=True, text=True)
    assert renderer._AUDIO_RE.findall(info.stderr) == ["aac", "aac"]


@pytest.mark.parametrize("entry", ["extract", "burn", "softmux", "mux"])
def test_cancellation_after_processing_does_not_publish_or_replace_old_output(tmp_path, monkeypatch, entry):
    from app.core import cancel

    source, subtitle, output = tmp_path / "source.mp4", tmp_path / "sub.srt", tmp_path / "output.mp4"
    source.write_bytes(b"original input")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    output.write_bytes(b"previous complete output")
    task_id = f"render-final-cancellation-round4-{entry}"
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[-1] != "-":
            Path(args[-1]).write_bytes(b"new complete output")
        if kwargs.get("on_time"):
            kwargs["on_time"](1)
        if entry != "mux" or args[-1] == "-":
            cancel.cancel(task_id)

    monkeypatch.setattr(renderer, "_run", run)
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "ffmpeg")
    monkeypatch.setattr(renderer, "audio_args", lambda *a: [])
    monkeypatch.setattr(renderer, "probe_duration", lambda *a, **kw: 1)
    try:
        with pytest.raises(AppError) as error:
            if entry == "extract":
                renderer.extract_audio(Config({}), source, output, task_id)
            elif entry == "burn":
                renderer.burn(Config({"render": {"encoder": "cpu"}}), source, subtitle, output,
                              font_name="Arial", task_id=task_id,
                              on_progress=lambda _: cancel.cancel(task_id))
            elif entry == "softmux":
                renderer.softmux(Config({}), source, [(subtitle, "en")], output, task_id)
            else:
                renderer.mux_av(Config({}), source, tmp_path / "audio.wav", output, task_id)
        assert error.value.code == Codes.TASK_CANCELLED
        assert output.read_bytes() == b"previous complete output"
        assert len(calls) == (2 if entry == "mux" else 1)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["output.mp4", "source.mp4", "sub.srt"]
    finally:
        cancel.clear(task_id)


@pytest.mark.parametrize("entry,suffix", [("mux", ".webm"), ("softmux", ".mkv")])
def test_container_fallback_cannot_replace_the_source_file(tmp_path, monkeypatch, entry, suffix):
    source, subtitle, output = tmp_path / ("output" + suffix), tmp_path / "sub.srt", tmp_path / "output.mp4"
    source.write_bytes(b"original input")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    output.write_bytes(b"previous complete output")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"partially written output")
        raise AppError(Codes.RENDER_FAILED, "模拟 MP4 不支持此视频编码")

    monkeypatch.setattr(renderer, "_run", run)
    monkeypatch.setattr(renderer, "find_ffmpeg", lambda _: "ffmpeg")
    monkeypatch.setattr(renderer, "audio_args", lambda *a: [])
    monkeypatch.setattr(renderer, "probe_duration", lambda *a, **kw: 0)
    with pytest.raises(AppError) as error:
        if entry == "mux":
            renderer.mux_av(Config({}), source, tmp_path / "audio.wav", output)
        else:
            renderer.softmux(Config({}), source, [(subtitle, "en")], output)
    assert error.value.code == Codes.CFG_INVALID
    assert len(calls) == 1
    assert source.read_bytes() == b"original input" and output.read_bytes() == b"previous complete output"
    assert len(list(tmp_path.iterdir())) == 3
