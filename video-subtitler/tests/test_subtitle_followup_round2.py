"""编辑字幕和预览文件的完整性回归，所有媒体均为现场生成。"""
from pathlib import Path

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services import renderer
from app.services.subtitler import read_srt, to_srt, validate_srt
from app.services.transcriber import Segment


@pytest.mark.parametrize("prefix", ["\n", "\r\n", " \n", "\ufeff\n"])
def test_accepted_leading_blank_line_keeps_the_first_caption(tmp_path, prefix):
    expected = [Segment(0, 2, "首条不能遗漏。"), Segment(3, 4, "Second caption.")]
    body = prefix + to_srt(expected)
    validate_srt(body)
    subtitle = tmp_path / "edited.srt"
    subtitle.write_text(body, encoding="utf-8")
    assert read_srt(subtitle) == expected


def test_positioned_burn_preflight_failure_removes_temporary_caption(tmp_path):
    subtitle = tmp_path / "edited.srt"
    subtitle.write_text(to_srt([Segment(0, 2, "Caption.")]), encoding="utf-8")
    with pytest.raises(AppError) as error:
        renderer.burn(Config({}), tmp_path / "source.mp4", subtitle,
                      tmp_path / "output.mp4", font_name="Arial",
                      vertical_offset=20, color="invalid")
    assert error.value.code == Codes.CFG_INVALID
    assert sorted(path.name for path in tmp_path.iterdir()) == ["edited.srt"]


def test_positioned_burn_does_not_overwrite_an_existing_ass_file(tmp_path, monkeypatch):
    subtitle = tmp_path / "edited.srt"
    subtitle.write_text(to_srt([Segment(0, 2, "Caption.")]), encoding="utf-8")
    existing = tmp_path / "output.position.ass"
    existing.write_bytes(b"previous user document")
    monkeypatch.setattr(renderer, "pick_encoder", lambda _: "libx264")
    monkeypatch.setattr(renderer, "audio_args", lambda *_: [])
    monkeypatch.setattr(renderer, "_run", lambda *args, **kwargs: None)
    renderer.burn(Config({}), tmp_path / "source.mp4", subtitle,
                  tmp_path / "output.mp4", font_name="Arial", vertical_offset=20)
    assert existing.read_bytes() == b"previous user document"


@pytest.mark.parametrize("failure", [False, True])
def test_preview_publishes_only_complete_image_and_cleans_attachments(tmp_path, monkeypatch, failure):
    image = tmp_path / "preview.png"
    image.write_bytes(b"previous complete image")

    def render(args, **kwargs):
        Path(args[-1]).write_bytes(b"half rendered image")
        if failure:
            raise AppError(Codes.RENDER_FAILED, "模拟预览写入失败")
        Path(args[-1]).write_bytes(b"new complete image")

    monkeypatch.setattr(renderer, "_run", render)
    if failure:
        with pytest.raises(AppError):
            renderer.render_preview_png(Config({}), "原文\nTranslation.", "Arial", 18,
                                        "bottom", image, vertical_offset=20)
        assert image.read_bytes() == b"previous complete image"
    else:
        renderer.render_preview_png(Config({}), "原文\nTranslation.", "Arial", 18,
                                    "bottom", image, vertical_offset=20)
        assert image.read_bytes() == b"new complete image"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["preview.png"]


@pytest.mark.parametrize("offset", [0, 20])
def test_real_preview_is_complete_and_has_no_leftover_subtitles(tmp_path, offset):
    import os
    import struct

    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要显式指定真实 FFmpeg")
    image = tmp_path / "preview.png"
    renderer.render_preview_png(Config({"paths": {"ffmpeg": ffmpeg}}),
                                "First line.\nSecond line.", "DejaVu Sans", 18,
                                "bottom", image, vertical_offset=offset)
    body = image.read_bytes()
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", body[16:24]) == (640, 360)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["preview.png"]


def test_real_positioned_burn_preserves_literal_braces_and_backslash_commands(tmp_path):
    import os
    import subprocess

    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要显式指定真实 FFmpeg")
    subtitle = tmp_path / "literal.srt"
    subtitle.write_text(to_srt([Segment(0, 2, r"Literal {name}, C:\new \N \n \h.")]),
                        encoding="utf-8")
    actual = renderer._write_positioned_ass(subtitle, tmp_path / "actual.ass", "middle", 20)
    reference = tmp_path / "reference.ass"
    header = actual.read_text(encoding="utf-8").split("Dialogue: ")[0]
    # 明确写出 libass 的字面量规则，再让真实渲染器比较可见像素。
    reference.write_text(header + r"Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,"
                         r"{\an5\pos(320,200)}Literal \{name\}, C:\{}new \{}N \{}n \{}h." + "\n",
                         encoding="utf-8")

    def pixels(path):
        return subprocess.check_output([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                                        "color=black:s=640x360:d=1", "-vf",
                                        renderer._subtitle_filter(path, "DejaVu Sans"),
                                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])

    expected = pixels(reference)
    assert len(expected) == 640 * 360 and sum(value > 100 for value in expected) > 100
    assert pixels(actual) == expected
