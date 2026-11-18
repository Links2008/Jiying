"""硬件能力与失败分流：加速不可牺牲取消、原始错误和媒体完整性。"""
from types import SimpleNamespace
from dataclasses import replace
import os
import subprocess
import json
from pathlib import Path
import pytest
from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services import cuda_overlay, renderer
from app.services.media_info import VideoTraits


def test_auto_dual_zone_requires_verified_device_and_native_uhd_ass(monkeypatch, tmp_path):
    source = tmp_path / "app.ass"
    traits = VideoTraits(width=3840, height=2160, codec="h264", pixel="yuv420p",
        space="bt709", range="tv", single_video=True, duration=43)
    qualifies = cuda_overlay.auto_dual_zone_preferred
    monkeypatch.setattr(cuda_overlay, "_verified_dual_zone_device", lambda: False)
    assert not qualifies(source, "h264_nvenc", traits, None)
    monkeypatch.setattr(cuda_overlay, "_verified_dual_zone_device", lambda: True)
    assert qualifies(source, "h264_nvenc", traits, None)
    assert not qualifies(source, "h264_nvenc", traits, 1080)
    assert not qualifies(source.with_suffix(".srt"), "h264_nvenc", traits, None)
    assert not qualifies(source, "h264_qsv", traits, None)
    assert not qualifies(source, "h264_nvenc", replace(traits, rotation=90), None)
    assert not qualifies(source, "h264_nvenc", replace(traits, duration=15), None)


def test_auto_skips_compiled_but_unusable_gpu(monkeypatch, tmp_path):
    monkeypatch.setattr(renderer, 'find_ffmpeg', lambda _: str(tmp_path / 'ffmpeg.exe'))
    monkeypatch.setattr(renderer, 'has_encoder', lambda *_: True)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1 if 'h264_nvenc' in command else 0, stdout='', stderr='')
    monkeypatch.setattr(renderer.subprocess, 'run', run)
    assert renderer.pick_encoder(Config({})) == 'h264_qsv'
    count = len(calls)
    assert renderer.pick_encoder(Config({})) == 'h264_qsv'
    assert len(calls) == count


def prepare_burn(monkeypatch, tmp_path):
    captions = tmp_path / 'captions.srt'
    captions.write_text('1\n00:00:00,000 --> 00:00:01,000\n字幕测试\n', encoding='utf-8')
    monkeypatch.setattr(renderer, 'find_ffmpeg', lambda _: str(tmp_path / 'ffmpeg.exe'))
    monkeypatch.setattr(renderer, 'audio_args', lambda *_: ['-c:a', 'copy'])
    monkeypatch.setattr(renderer, 'pick_encoder', lambda _: 'h264_nvenc')
    return lambda: renderer.burn(Config({}), tmp_path / 'source.mp4', captions,
                                tmp_path / 'result.mp4', font_name='Arial')


def test_invalid_input_does_not_restart_on_cpu(monkeypatch, tmp_path):
    burn = prepare_burn(monkeypatch, tmp_path)
    calls = []
    error = AppError(Codes.RENDER_FAILED, 'ffmpeg 处理失败', detail='Invalid data found when processing input')
    def fail(command, **kwargs):
        calls.append(command)
        raise error
    monkeypatch.setattr(renderer, '_run', fail)
    with pytest.raises(AppError) as caught:
        burn()
    assert caught.value is error and len(calls) == 1


def test_runtime_gpu_failure_tries_available_second_gpu(monkeypatch, tmp_path):
    burn = prepare_burn(monkeypatch, tmp_path)
    monkeypatch.setattr(renderer, 'has_encoder', lambda _, name: name == 'h264_qsv')
    monkeypatch.setattr(renderer.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    encoders = []
    def run(command, **kwargs):
        encoder = command[command.index('-c:v') + 1]
        encoders.append(encoder)
        if encoder == 'h264_nvenc':
            raise AppError(Codes.RENDER_FAILED, 'ffmpeg 处理失败', detail='OpenEncodeSessionEx failed')
    monkeypatch.setattr(renderer, '_run', run)
    assert burn().exists()
    assert encoders == ['h264_nvenc', 'h264_qsv']


def test_real_nvenc_probe_recognizes_a_working_device():
    ffmpeg = os.environ.get('JIYING_TEST_FFMPEG')
    if not ffmpeg:
        pytest.skip('需要真实 FFmpeg 和可用 NVIDIA 显卡')
    control = subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=black:s=320x180:r=30', '-frames:v', '1', '-an', '-c:v', 'h264_nvenc',
        '-f', 'null', '-'], capture_output=True, timeout=10, creationflags=renderer._NO_WINDOW)
    if control.returncode:
        pytest.skip('此环境没有可用 NVENC')
    assert renderer.encoder_usable(Config({'paths': {'ffmpeg': ffmpeg}}), 'h264_nvenc')


@pytest.mark.parametrize('encoder', ['auto', 'cpu'])
def test_ten_bit_hdr_uses_compatible_codec_and_preserves_color_tags(tmp_path, encoder):
    ffmpeg = os.environ.get('JIYING_TEST_FFMPEG')
    if not ffmpeg:
        pytest.skip('需要真实 FFmpeg')
    source = tmp_path / 'hdr.mp4'
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=30:d=1',
        '-vf', 'format=yuv420p10le', '-c:v', 'libx265', '-preset', 'ultrafast',
        '-x265-params', 'log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc',
        '-color_primaries', 'bt2020', '-color_trc', 'smpte2084',
        '-colorspace', 'bt2020nc', str(source)], check=True, capture_output=True,
        timeout=20, creationflags=renderer._NO_WINDOW)
    captions = tmp_path / 'hdr.srt'
    captions.write_text('1\n00:00:00,000 --> 00:00:01,000\nHDR 字幕\n', encoding='utf-8')
    output = renderer.burn(Config({'paths': {'ffmpeg': ffmpeg}, 'render': {'encoder': encoder}}), source, captions,
                           tmp_path / 'burn.mp4', font_name='Microsoft YaHei')
    raw = subprocess.check_output([str(Path(ffmpeg).with_name('ffprobe.exe')), '-v', 'error',
        '-select_streams', 'v:0', '-show_entries', 'stream=codec_name,pix_fmt,color_primaries,color_transfer,color_space',
        '-of', 'json', str(output)], timeout=15, creationflags=renderer._NO_WINDOW)
    info = json.loads(raw)['streams'][0]
    assert info['codec_name'] == 'hevc'
    assert info['pix_fmt'] == 'yuv420p10le'
    assert (info['color_primaries'], info['color_transfer'], info['color_space']) == ('bt2020', 'smpte2084', 'bt2020nc')


def test_nvdec_failure_keeps_nvenc_and_source_timestamps(monkeypatch, tmp_path):
    burn = prepare_burn(monkeypatch, tmp_path)
    monkeypatch.setattr(renderer, "_video_traits", lambda *_: renderer._VideoTraits(
        depth=10, time_base="1/90000", cuda_format="p010le", audio_codecs=("aac",)))
    monkeypatch.setattr(renderer, "_pick_10bit_encoder", lambda _: "hevc_nvenc")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "-hwaccel" in args:
            raise AppError(Codes.RENDER_FAILED, "Hardware device setup failed")

    monkeypatch.setattr(renderer, "_run", run)
    assert burn().is_file()
    assert len(calls) == 2 and "-hwaccel" not in calls[-1]
    for args in calls:
        assert "hevc_nvenc" in args and "passthrough" in args and "1/90000" in args


def test_av1_8bit_attempts_hardware_decode_and_falls_back_inside_nvenc(monkeypatch, tmp_path):
    burn = prepare_burn(monkeypatch, tmp_path)
    monkeypatch.setattr(renderer, "_video_traits", lambda *_: renderer._VideoTraits(
        cuda_format="nv12", audio_codecs=()))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "-hwaccel" in args:
            raise AppError(Codes.RENDER_FAILED, "Hardware does not support AV1")

    monkeypatch.setattr(renderer, "_run", run)
    assert burn().is_file()
    assert len(calls) == 2
    assert "hwdownload,format=nv12," in calls[0][calls[0].index("-vf") + 1]
    assert "-hwaccel" not in calls[1] and "h264_nvenc" in calls[1]


def test_real_av1_decoder_name_still_selects_cuda_format(tmp_path):
    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要真实 FFmpeg")
    source = tmp_path / "av1.mkv"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=1",
                    "-c:v", "libaom-av1", "-usage", "realtime", "-cpu-used", "8",
                    "-threads", "2", "-pix_fmt", "yuv420p", str(source)],
                   check=True, capture_output=True, timeout=20, creationflags=renderer._NO_WINDOW)
    traits = renderer._video_traits(source)
    assert traits.depth == 8 and traits.cuda_format == "nv12"


def test_real_cuda_subtitle_overlay_does_not_export_surface_padding(tmp_path, monkeypatch):
    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要真实 FFmpeg")
    config = Config({"paths": {"ffmpeg": ffmpeg}, "render": {"gpu_overlay": "on"}})
    if not renderer.encoder_usable(config, "h264_nvenc"):
        pytest.skip("需要可运行的 NVIDIA NVENC")
    source = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24:d=1",
                    "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p",
                    "-colorspace", "bt709", "-color_range", "tv", str(source)],
                   check=True, capture_output=True, timeout=20, creationflags=renderer._NO_WINDOW)
    subtitles = tmp_path / "captions.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:00,900\nGPU subtitle\n", encoding="utf-8")
    modes = []
    commands = []
    original_run = renderer._run

    def track(args, *positional, **kwargs):
        commands.append(args)
        return original_run(args, *positional, **kwargs)

    monkeypatch.setattr(renderer, "_run", track)
    rendered = renderer.burn(config, source, subtitles, tmp_path / "rendered.mp4",
                             height=360, font_name="Arial", on_encoding=modes.append)
    assert modes[-1].endswith("CUDA 解码与显存合成")
    assert "单进程" in modes[-1]
    assert sum(Path(command[0]).name.casefold().startswith("ffmpeg") for command in commands) == 1
    assert not any("layer.mkv" in str(command) for command in commands)
    assert renderer._video_traits(rendered, config).height == 360
