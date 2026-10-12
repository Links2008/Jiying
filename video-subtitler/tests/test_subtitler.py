"""subtitler 单元测试：时间轴格式与 SRT/VTT 输出。"""
from app.services.subtitler import (
    fmt_ts, layout_segments, parse_srt, save_srt, save_srt_bilingual, to_srt, to_vtt,
)
from app.services.transcriber import Segment


def test_fmt_ts_srt():
    assert fmt_ts(0) == "00:00:00,000"
    assert fmt_ts(3661.5) == "01:01:01,500"
    assert fmt_ts(59.999) == "00:00:59,999"
    assert fmt_ts(59.9999) == "00:01:00,000"  # 毫秒进位连跨秒位
    assert fmt_ts(3599.9999) == "01:00:00,000"  # 进位连跨分位


def test_fmt_ts_vtt():
    assert fmt_ts(75.4, ms_sep=".") == "00:01:15.400"


def test_to_srt_blocks():
    segs = [Segment(1.0, 2.0, "你好"), Segment(3.0, 4.5, "hello")]
    lines = to_srt(segs).strip().splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:01,000 --> 00:00:02,000"
    assert lines[2] == "你好"
    assert lines[4] == "2"  # 第二块序号
    assert lines[5].startswith("00:00:03,000 -->")


def test_to_vtt_header():
    segs = [Segment(0.0, 1.0, "hi")]
    assert to_vtt(segs).startswith("WEBVTT")


def test_short_sentences_are_not_forced_onto_two_lines():
    for text in ("这个字幕明明可以在一行里完整显示出来。", "你好。请继续。",
                 "Bonjour, écoutez, je voulais vous appeler personnellement."):
        laid_out = layout_segments([Segment(0, 3, text)])
        assert len(laid_out) == 1
        assert laid_out[0].text == text


def test_long_latin_text_wraps_between_words():
    text = "Bonjour, écoutez, je voulais vous appeler personnellement pour vous féliciter."
    laid_out = layout_segments([Segment(0, 4, text)])
    lines = "\n".join(seg.text for seg in laid_out).splitlines()
    assert len(lines) > 1
    assert " ".join(lines) == text
    assert all(line.split()[-1] in text.split() for line in lines)


def test_wrap_uses_whole_words_even_when_the_break_is_after_midpoint():
    text = "A" * 40 + " " + "B" * 30
    lines = layout_segments([Segment(0, 4, text)])[0].text.splitlines()
    assert lines == ["A" * 40, "B" * 30]


def test_short_translated_lines_are_combined():
    text = "Bonjour,\ncomment allez-vous ?"
    laid_out = layout_segments([Segment(0, 3, text)])
    assert [seg.text for seg in laid_out] == ["Bonjour, comment allez-vous ?"]


def test_bilingual_reflows_old_soft_breaks_and_matches_time(tmp_path):
    top = tmp_path / "top.srt"
    bottom = tmp_path / "bottom.srt"
    merged = tmp_path / "merged.srt"
    top.write_text(to_srt([Segment(0, 4, "你好，\n请继续。"),
                           Segment(4, 8, "第二句。")]), encoding="utf-8")
    bottom.write_text(to_srt([Segment(0, 2, "Hello,"),
                              Segment(2, 4, "please continue."),
                              Segment(4, 8, "Second translated line.")]), encoding="utf-8")
    save_srt_bilingual(top, bottom, merged)
    cues = parse_srt(merged.read_text(encoding="utf-8-sig"))
    assert cues[0].text.count("\n") == 1
    assert cues[0].text == "你好，请继续。\nHello, please continue."
    assert cues[1].text == "第二句。\nSecond translated line."
    assert r"\N" not in merged.read_text(encoding="utf-8-sig")


def test_long_speech_prefers_sentence_boundary_without_losing_text(tmp_path):
    text = ("当我们讨论数学研究时，问题的背景通常比结论更重要。"
            "接下来还要说明为什么这个假设成立，以及它如何影响最后的推理。")
    path = tmp_path / "speech.srt"
    save_srt([Segment(-2, 6, text)], path)
    cues = parse_srt(path.read_text(encoding="utf-8-sig"))
    assert len(cues) >= 2
    assert cues[0].text.endswith("。")
    assert "".join(cue.text.replace("\n", "") for cue in cues) == text
    assert all(0 <= cue.start <= cue.end <= 6 for cue in cues)
    assert all(len(cue.text.splitlines()) <= 2 for cue in cues)


def test_bilingual_cues_have_two_lines_and_no_repeated_translation(tmp_path):
    top = tmp_path / "top.srt"
    bottom = tmp_path / "bottom.srt"
    merged = tmp_path / "merged.srt"
    top_text = "这是一段关于数学发现如何发生的讨论，它说明了直觉和严谨证明都很重要。"
    translated = ("Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
                  "mike november oscar papa quebec romeo sierra tango uniform victor whiskey.")
    top.write_text(to_srt([Segment(0, 4, top_text), Segment(4, 8, "随后提出新的问题。")]),
                   encoding="utf-8")
    bottom.write_text(to_srt([Segment(0, 6, translated), Segment(6, 8, "Next question.")]),
                      encoding="utf-8")
    save_srt_bilingual(top, bottom, merged)
    cues = parse_srt(merged.read_text(encoding="utf-8-sig"))
    lines = [line for cue in cues for line in cue.text.splitlines()]
    source_lines = [line for line in lines if any("一" <= ch <= "龥" for ch in line)]
    translated_lines = [line for line in lines if line not in source_lines]
    assert all(len(cue.text.splitlines()) <= 2 for cue in cues)
    assert "".join(source_lines) == top_text + "随后提出新的问题。"
    assert " ".join(translated_lines) == translated + " Next question."
    assert all(cue.start >= 0 and cue.end >= cue.start for cue in cues)
    assert [cue.start for cue in cues] == sorted(cue.start for cue in cues)
    assert [cue.end for cue in cues] == sorted(cue.end for cue in cues)


def test_bilingual_long_translation_keeps_both_source_time_windows(tmp_path):
    top = tmp_path / "top.srt"
    bottom = tmp_path / "bottom.srt"
    merged = tmp_path / "merged.srt"
    text = "The first idea reaches a conclusion. The second idea starts from that result."
    top.write_text(to_srt([Segment(0, 4, "第一句。"), Segment(4, 8, "第二句。")]),
                   encoding="utf-8")
    bottom.write_text(to_srt([Segment(0, 8, text)]), encoding="utf-8")
    save_srt_bilingual(top, bottom, merged)
    cues = parse_srt(merged.read_text(encoding="utf-8-sig"))
    first = [cue for cue in cues if "第一句。" in cue.text]
    second = [cue for cue in cues if "第二句。" in cue.text]
    assert first and first[0].start < first[0].end <= 4
    assert second and 4 <= second[0].start < second[0].end <= 8
    assert any("The first idea" in cue.text for cue in cues if cue.end <= 4)
    assert any("The second idea" in cue.text for cue in cues if cue.start >= 4)
    assert all(len(cue.text.splitlines()) <= 2 for cue in cues)
