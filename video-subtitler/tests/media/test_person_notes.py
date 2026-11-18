"""带时间轴的人名卡与实际 libass 烧录回归。"""
import json
import os
from pathlib import Path
import subprocess

import pytest

from app.core.config import Config
from app.services.ass_composer import compose_person_notes, refresh_edited_cards
from app.services.ass_document import parse_ass
from app.services.cuda_overlay import _change_times, stream_plan
from app.services.media_info import VideoTraits
from app.services.renderer import burn, softmux
from app.services.transcriber import Segment
from app.services.subtitle_filters import _ass_filter, _subtitle_filter
from app.services.subtitler import save_srt
from app.services.visual_names import _different_frame, from_frames


def test_ass_keeps_dialogue_and_only_adds_first_supported_person(tmp_path):
    dialogue = [Segment(.4, 1.8, "露西说：路径 {video}"),
                Segment(2.1, 3.5, "露西再次发言"), Segment(4, 5, "没有证据")]
    record = {"source": "Lucy Smith", "target": "露西", "sense": "", "kind": "person"}
    notes = [{**record, "start": .4, "end": 1.8}, {**record, "start": 2.1, "end": 3.5},
             {**record, "source": "Jane", "target": "简", "start": 4, "end": 5}]
    output = tmp_path / "sub.ass"
    assert compose_person_notes(dialogue, notes, output, font_name="Arial")
    text = output.read_text(encoding="utf-8")
    assert "Style: MainDialogue,Arial," in text and "Style: PersonCard,Arial," in text
    assert text.count(",MainDialogue,") == 3
    assert text.count(",PersonCard,") == 1
    assert "Lucy Smith" in text and "Jane" not in text
    assert r"\{video\}" in text


def test_real_burn_keeps_person_card_separate_from_dialogue(tmp_path):
    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要显式指定真实 FFmpeg")
    video = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=black:s=640x360:d=2",
                    "-c:v", "libx264", str(video)], check=True)
    subtitle = tmp_path / "a'b, [c].ass"
    assert compose_person_notes([Segment(.3, 1.7, "Bottom dialogue")],
        [{"source": "Lucy Smith", "target": "Lucy Smith", "kind": "person", "sense": "",
          "start": .3, "end": 1.7}], subtitle, font_name="Arial", require_target=False)
    cfg = Config({"paths": {"ffmpeg": ffmpeg}, "render": {"encoder": "cpu"}})
    out = burn(cfg, video, subtitle, tmp_path / "out.mp4", font_name="Arial")

    def luma(at):
        pixels = subprocess.check_output([ffmpeg, "-v", "error", "-ss", at, "-i", str(out),
                                         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
        return (sum(pixels[y * 640 + x] > 170 for y in range(100) for x in range(320, 640)),
                sum(pixels[y * 640 + x] > 170 for y in range(240, 360) for x in range(640)))

    assert luma("0.1") == (0, 0)
    top, bottom = luma("1")
    assert top > 20 and bottom > 20


def test_screen_only_names_keep_real_intervals_and_repeat_after_leaving():
    title = {"text": "Lucy Morgan", "x": .03, "y": .77, "height": .075}
    role = {"text": "Research Director", "x": .03, "y": .855, "height": .045}
    background = {"text": "Welcome Back", "x": .32, "y": .15, "height": .04}
    spans = from_frames([(0.5, [title, role, background]), (1, [title, role]), (9, [title, role])], 12)
    assert [(note["source"], note["sense"], note["start"], note["end"]) for note in spans] == [
        ("Lucy Morgan", "Research Director", .5, 1.5), ("Lucy Morgan", "Research Director", 9, 9.5)]


def test_screen_names_bridge_one_miss_only_when_the_nameplate_agrees():
    title = {"text": "Lucy Morgan", "x": .03, "y": .77, "height": .075}
    role = {"text": "Research Director", "x": .03, "y": .855, "height": .045}
    other = {**title, "text": "Jane Miller"}
    spans = from_frames([(0, [title, role]), (.5, []), (1, [title, role]),
                         (1.5, [other, role]), (2, [title, role])], 2.5)
    assert [(n["source"], n["start"], n["end"]) for n in spans] == [
        ("Lucy Morgan", 0, 1.5), ("Jane Miller", 1.5, 2), ("Lucy Morgan", 2, 2.5)]


@pytest.mark.parametrize("text", ["New York", "Tokyo Station"])
def test_untitled_scene_label_is_not_a_local_person_card(text):
    title = {"text": text, "x": .03, "y": .77, "height": .075}
    actual = {**title, "text": "Jane Miller"}
    assert [n["source"] for n in from_frames([
        (0, [title]), (.5, [title]), (1, [actual]), (1.5, [actual])], 2)] == ["Jane Miller"]


def test_crowded_last_second_keeps_original_translated_names_and_titles(tmp_path):
    notes = [{"source": f"Name {i}", "target": f"译名 {i}", "sense": f"头衔 {i}",
              "kind": "person", "start": .7, "end": 1.1} for i in range(6)]
    dialogue = [Segment(.6, 1.2, "，".join(note["target"] for note in notes))]
    target = compose_person_notes(dialogue, notes, tmp_path / "crowded.ass",
                                  font_name="Arial", duration=1.2)
    body = "\n".join(line for line in target.read_text(encoding="utf-8").splitlines()
                     if ",PersonCard," in line and line.startswith("Dialogue: "))
    for note in notes:
        assert all(note[k] in body for k in ("source", "target", "sense"))


def test_real_middle_dialogue_moved_up_keeps_card_pixels_clear(tmp_path):
    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要显式指定真实 FFmpeg")
    notes = [{"source": name, "target": name, "sense": "National Research Director and Assistant Host",
              "kind": "person", "origin": "screen", "start": 0., "end": 2.}
             for name in ("Lucy Morgan", "Jane Miller")]
    target = compose_person_notes(
        [Segment(0, 2, "A long subtitle line covers the right side too\nSecond long dialogue line there")],
        notes, tmp_path / "middle.ass", font_name="Arial", font_size=24,
        position="middle", vertical_offset=-120, duration=2)
    header, _, events = target.read_text(encoding="utf-8").partition("[Events]\n")
    layout, *rows = events.splitlines()

    def ink(dialogue):
        layer = tmp_path / f"{dialogue}.ass"
        layer.write_text(header + "[Events]\n" + layout + "\n" + "\n".join(
            row for row in rows if (",MainDialogue," in row) == dialogue) + "\n", encoding="utf-8")
        pixels = subprocess.check_output([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
            "color=black:s=640x360:d=2", "-ss", "0.8", "-vf", _ass_filter(layer, "Arial"),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"], timeout=15)
        return {i for i, value in enumerate(pixels) if value > 10}

    words, cards = ink(True), ink(False)
    assert words and cards and not words & cards


def test_frame_difference_filters_single_noise_pixel_but_keeps_small_letter_change():
    import numpy as np

    width, height = 16, 16
    plain = np.zeros((height, width, 4), dtype=np.uint8)
    noisy = plain.copy()
    noisy[2, 2, 0] = 255
    assert not _different_frame(noisy.tobytes(), plain.tobytes(), width, height)
    short_text = plain.copy()
    short_text[12:16, 8, 0] = 5
    assert _different_frame(short_text.tobytes(), plain.tobytes(), width, height)


def test_screen_names_with_initials_accents_and_a_title_prefixed_single_name():
    def row(text, x=.03, y=.77):
        return {"text": text, "x": x, "y": y, "height": .045}

    spans = from_frames([(0, [row("José F. García"), row("Research Director", y=.85)]),
                         (.5, [row("Dr. René")])], 1)
    assert [(note["source"], note["sense"]) for note in spans] == [
        ("José F. García", "Research Director"), ("René", "Dr.")]


def test_edited_card_reflows_shape_while_preserving_original_ass(tmp_path):
    note = {"source": "Lucy Morgan", "target": "露西", "kind": "person", "sense": "嘉宾",
            "origin": "screen", "start": 0, "end": 3}
    source = compose_person_notes([], [note], tmp_path / "notes.ass", font_name="Arial",
                                  font_size=22, color="#43ACCC")
    original = source.read_text(encoding="utf-8")
    first = parse_ass(original)[0].text
    assert r"\p1" in first and r"\fad(210,210)" in first
    assert "Style: PersonCard,Arial,16,&H00CCAC43" in original
    edited = original.replace("嘉宾", r"研究团队的国际项目统筹与科学传播总负责人\{项目\}")
    source.write_text(edited, encoding="utf-8")
    new = refresh_edited_cards(source, tmp_path / "render.ass")
    events = parse_ass(new.read_text(encoding="utf-8"))
    assert events[0].text != first and "科学传播" in events[1].text and r"\{项目\}" in events[1].text
    assert source.read_text(encoding="utf-8") == edited


def test_gpu_layer_hits_the_end_of_the_fade_and_leaves_karaoke_to_libass(tmp_path):
    card = {"source": "Lucy Morgan", "target": "Lucy Morgan", "sense": "", "kind": "person",
            "origin": "screen", "start": .6, "end": 2.6}
    ass = compose_person_notes([], [card], tmp_path / "visual.ass", font_name="Arial")
    raw = ass.read_text(encoding="utf-8")
    first = parse_ass(raw)[0].start_ms
    assert first + 210 in _change_times(ass, None, 24)
    ass.write_text(raw.replace("Lucy Morgan", r"{\k10}Lucy Morgan"), encoding="utf-8")
    assert _change_times(ass, None, 24) == []


def test_bottom_srt_local_canvas_preserves_libass_alpha(tmp_path):
    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要真实 FFmpeg")
    cues = [Segment(.25, 1.5, "字幕渲染测试 · subtitle sample")]
    source = tmp_path / "bottom.srt"
    save_srt(cues, source)
    font = "Noto Sans CJK SC"
    full_filter = _subtitle_filter(source, font, 18, "bottom", 1)
    traits = VideoTraits(width=1920, height=1080, fps=25)
    plan = stream_plan(source, cues, full_filter, traits, None, tmp_path, "bottom", font)
    graph = plan.graph_file.read_text(encoding="utf-8")
    assert not plan.checks and "scale=1920:540:flags=neighbor" in graph
    assert "PlayResY=144'" in graph

    def render(height, subtitle):
        return subprocess.check_output([ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-f", "lavfi",
            "-i", f"color=black@0:s=1920x{height}:r=25:d=0.08,format=rgba",
            "-vf", f"setpts=0.8/TB,{subtitle}:alpha=1,format=rgba,alphaextract",
            "-frames:v", "1", "-threads:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], timeout=15)

    full = render(1080, full_filter)
    generated = graph.split("setsar=1,", 1)[1].split(":alpha=1", 1)[0]
    assert any(full) and full[:1920 * 540].count(0) == 1920 * 540
    assert render(540, generated) == full[-1920 * 540:]


@pytest.mark.parametrize("height", [720, 1080, 2160])
def test_generated_bottom_ass_has_two_local_canvases(tmp_path, height):
    font = "Noto Sans CJK SC"
    card = {"source": "Lucy Morgan", "target": "露西·摩根", "sense": "研究总监", "kind": "person",
            "origin": "screen", "start": .6, "end": 2.6}
    ass = compose_person_notes([Segment(.6, 2.6, "Dialogue and person")], [card],
        tmp_path / "generated.ass", font_name=font, duration=3)
    width = height * 16 // 9
    traits = VideoTraits(width=width, height=height, fps=24)
    plan = stream_plan(ass, None, _ass_filter(ass, font), traits, None, tmp_path, "bottom", font)
    graph = plan.graph_file.read_text(encoding="utf-8")
    assert graph.count("overlay_cuda=") == 2 and not plan.checks and plan.dual_local
    assert f"scale={width}:{170 * height // 360}:flags=neighbor" in graph
    assert r"\fad(" in (tmp_path / "card.ass").read_text(encoding="utf-8")


def test_notes_are_a_selectable_default_mkv_track_without_video_transcode(tmp_path):
    ffmpeg = os.environ.get("JIYING_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("需要真实 FFmpeg 和 FFprobe")
    source = tmp_path / "video.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=2",
                    "-c:v", "libx264", str(source)], check=True)
    notes = [{"source": "Lucy Morgan", "target": "露西", "kind": "person", "sense": "研究员",
              "origin": "screen", "start": .1, "end": 1.5}]
    ass = compose_person_notes([], notes, tmp_path / "notes.ass", font_name="Arial")
    assert ass is not None
    srt = tmp_path / "dialogue.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nText\n", encoding="utf-8")
    output = softmux(Config({"paths": {"ffmpeg": ffmpeg}}), source, [(ass, "zh"), (srt, "en")],
                     tmp_path / "video.en.subbed.mp4")
    assert output.suffix == ".mkv"
    probe = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    payload = json.loads(subprocess.check_output([str(probe), "-v", "error",
        "-show_entries", "stream=codec_type,codec_name:stream_disposition=default", "-of", "json", str(output)]))
    tracks = payload["streams"]
    assert tracks[0]["codec_name"] == "h264"
    assert [(item["codec_name"], item["disposition"]["default"]) for item in tracks
            if item["codec_type"] == "subtitle"] == [("ass", 1), ("subrip", 0)]
