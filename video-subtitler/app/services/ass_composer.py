"""输出保留原字幕排版与独立右上角人名注释的 ASS。"""
from __future__ import annotations

import bisect
import math
import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from ..core.paths import bin_dir
from .subtitle_filters import (ASS_DIALOGUE_MARGIN, ASS_DIALOGUE_SCALE, _ass_color,
                               _ass_text, _ass_time, _dialogue_anchor)
from .subtitler import _merge_overlapping_cues
from .transcriber import Segment

_CARD_EFFECT = "jiying-card"
_CARD_TAGS = re.compile(r"^\{\\an7\\q2\\pos\((\d+),(\d+)\)\\fad\((\d+),(\d+)\)\}(.*)$")
_CARD_LITERAL = re.compile(r"\\\{\}|\\[{}]")


@lru_cache(maxsize=64)
def _font_path(font: str) -> Path | None:
    if os.name != "nt":
        return None
    import winreg

    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as fonts:
                for index in range(winreg.QueryInfoKey(fonts)[1]):
                    name, file, _ = winreg.EnumValue(fonts, index)
                    if not isinstance(file, str):
                        continue
                    family = name.split(" (", 1)[0].casefold().split(" & ")
                    if font.casefold() in family:
                        path = font_dir / file
                        if path.is_file():
                            return path
        except OSError:
            continue
    return None


def _card_metrics(font, size):
    from PIL import ImageFont

    media = bin_dir()
    fallback = (media.parent if media.name == "bin" else media) / "fonts" / "NotoSansCJKsc-Regular.otf"
    source = _font_path(font) if font != "Noto Sans CJK SC" else fallback
    fonts = []
    for path in dict.fromkeys((source, fallback)):
        if path is not None and path.is_file():
            try:
                fonts.append(ImageFont.truetype(str(path), size))
            except OSError:
                continue

    def width(text):
        if not fonts:
            return sum(1 if unicodedata.east_asian_width(char) in {"W", "F"} else .65 for char in text) * size
        return max(max(face.getlength(text), (bounds := face.getbbox(text))[2] - min(0, bounds[0]))
                   for face in fonts)

    return width


def _wrap_card(text, width, measure):
    lines = []
    for source in text.split(r"\N"):
        if measure(source) <= width:
            lines.append(source)
            continue
        current = ""
        for token in re.findall(r"\\\{\}|\\[{}]|.", source):
            if current and measure(current + token) > width:
                lines.append(current)
                current = ""
            current += token
        lines.append(current)
    return lines


def _card_text_width(line, measure):
    return measure(_CARD_LITERAL.sub(lambda token: "\\" if token[0] == r"\{}" else token[0][-1], line))


def _card_lines(text, width, measure):
    return _wrap_card(text, width - 32, lambda line: _card_text_width(line, measure))


def _card_height(font_size, line_count):
    return math.ceil(font_size * 1.3 * line_count) + 8


def _card_shapes(text, width, top, font_size, measure, fade_in, fade_out):
    padding = 4
    lines = _card_lines(text, width, measure)
    box_width = min(width - 24, math.ceil(max(_card_text_width(line, measure) for line in lines)) + 2 * padding + 2)
    box_height = _card_height(font_size, len(lines))
    left = width - 12 - box_width
    y = max(0, min(top, 360 - 12 - box_height))
    fade = rf"\fad({fade_in},{fade_out})"
    bottom = (rf"{{\an7\pos({left},{y})\bord0\shad0\1c&H182028&\1a&H26&{fade}\p1}}"
              f"m 0 0 l {box_width} 0 {box_width} {box_height} 0 {box_height}")
    letters = rf"{{\an7\q2\pos({left + padding},{y + padding}){fade}}}" + r"\N".join(lines)
    return bottom, letters


def refresh_edited_cards(source: Path, output: Path) -> Path:
    """只重算应用生成的文字与底板对，保留手写 ASS 和保存后的原稿。"""
    raw = source.read_text(encoding="utf-8-sig")
    width = re.search(r"(?m)^PlayResX: (\d+)\s*$", raw)
    style = re.search(r"(?m)^Style: PersonCard,([^,\n]+),(\d+),", raw)
    if not width or not style or not re.search(r"(?m)^PlayResY: 360\s*$", raw) or _CARD_EFFECT not in raw:
        return source
    w, size = int(width[1]), int(style[2])
    if not 120 <= w <= 1800 or not 1 <= size <= 42:
        return source
    lines = raw.splitlines()
    pairs = {}
    for index, line in enumerate(lines):
        if not line.startswith("Dialogue: "):
            continue
        fields = line[10:].split(",", 9)
        if len(fields) == 10 and fields[8] == _CARD_EFFECT and re.fullmatch(r"JiyingCard\d+", fields[4]):
            pairs.setdefault(fields[4], []).append((index, fields))
    updates = False
    measure = _card_metrics(style[1], size)
    for pair in pairs.values():
        if len(pair) != 2:
            continue
        background, text = sorted(pair, key=lambda item: item[1][3] != "PersonCardBackground")
        bg_index, bg = background
        text_index, fg = text
        old = _CARD_TAGS.fullmatch(fg[9])
        if bg[3] != "PersonCardBackground" or fg[3] != "PersonCard" or old is None:
            continue
        body = old[5]
        plain = _CARD_LITERAL.sub("", body.replace(r"\N", ""))
        if len(body) > 1024 or any(char in plain for char in "\\{}") or not body.strip():
            continue
        bg[9], fg[9] = _card_shapes(body, w, int(old[2]) - 4, size, measure, int(old[3]), int(old[4]))
        bg[1:3] = fg[1:3]
        lines[bg_index], lines[text_index] = "Dialogue: " + ",".join(bg), "Dialogue: " + ",".join(fg)
        updates = True
    if not updates:
        return source
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def _visible_text(text: str) -> str:
    return "".join(text.casefold().split())


def _fit(text: str, limit: int) -> str:
    """用东亚字符宽度限制每行文字，避免过长身份说明撑过对话区域。"""
    remaining = limit
    for index, char in enumerate(text):
        remaining -= 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if remaining < 0:
            return text[:max(1, index - 1)].rstrip() + "…"
    return text


def _cards(notes: list, dialogue: list[Segment], *, require_target: bool, line_limit: int,
           top: int, bottom: int, height_of, duration: float) -> list[tuple]:
    # 先按对白时间排序，再用当前字幕复核。改稿删去译名后，注释不会继续显示。
    ends = [segment.end for segment in dialogue]
    display = []
    for note in notes:
        if not isinstance(note, dict) or note.get("kind") != "person":
            continue
        start, end = note.get("start"), note.get("end")
        if not all(type(t) in (float, int) and math.isfinite(t) for t in (start, end)) or not 0 <= start < end:
            continue
        fields = [note.get(key, "") for key in ("source", "target", "sense")]
        if any(not isinstance(text, str) or len(text) > 160 for text in fields):
            continue
        source, target, sense = [" ".join(text.split()) for text in fields]
        if not source or not target:
            continue
        index = bisect.bisect_right(ends, start)
        snippets = []
        while index < len(dialogue) and dialogue[index].start < end:
            snippets.append(dialogue[index].text)
            index += 1
        evidence = " ".join(snippets)
        if target.isascii():
            pattern = r"(?<![A-Za-z0-9_])" + r"\s+".join(re.escape(part) for part in target.split()) + r"(?![A-Za-z0-9_])"
            matches = re.search(pattern, evidence, re.IGNORECASE) is not None
        else:
            matches = _visible_text(target) in _visible_text(evidence)
        if note.get("origin") != "screen" and (not snippets or (require_target and not matches)):
            continue
        lines = [target] if note.get("origin") == "screen" else [source]
        if note.get("origin") != "screen" and _visible_text(source) != _visible_text(target):
            lines.append(target)
        if sense:
            lines.append(sense)
        display.append((math.floor(start * 100), math.ceil(end * 100), _visible_text(source),
                        [_fit(line, line_limit) for line in lines]))
    if not display:
        return []
    # 相邻出现只显示一次，离开一段时间后的再次出场会重新提示。
    cooldown = {}
    cards = []
    occupied = []
    overflow = []
    latest_start = 0
    limit = math.ceil(duration * 100) if duration > 0 else max([math.ceil(t * 100) for t in ends] +
                                                                [entry[1] for entry in display])
    for start, end, key, lines in sorted(display):
        if start < cooldown.get(key, 0):
            continue
        cooldown[key] = start + 600
        text = "\n".join(lines)
        size = height_of(text)
        original_length = max(100, min(430, end - start))
        delayed = max(start, latest_start)
        while delayed < limit:
            occupied = [slot for slot in occupied if slot[0] > delayed]
            margin = top
            for _, y, height in sorted(occupied, key=lambda slot: slot[1]):
                if margin + size <= y:
                    break
                margin = max(margin, y + height + 4)
            if margin + size <= bottom:
                break
            delayed = min(until for until, _, _ in occupied) if occupied else limit
        if delayed >= limit:
            if start < limit:
                overflow.append((start, " · ".join(lines)))
            continue
        start = delayed
        latest_start = start
        end = min(limit, start + original_length)
        fade = max(1, min(210, (end - start) * 3))
        cards.append((start, end, margin, fade, text))
        occupied.append((end, margin, size))
    if overflow:
        # 尾端排不下独立卡片时，在剩余可见时间内合并仍在屏上的卡片和待显示的完整字段。
        begin = max(latest_start, max(start for start, _ in overflow))
        tail, earlier = [], []
        for start, end, margin, fade, text in cards:
            if end > begin:
                tail.append(" · ".join(text.splitlines()))
                end = begin
            if end > start:
                earlier.append((start, end, margin, min(fade, (end - start) * 3), text))
        tail.extend(text for _, text in overflow)
        earlier.append((begin, limit, top, min(210, (limit - begin) * 3), "\n".join(tail)))
        cards = earlier
    return cards


def compose_person_notes(dialogue: list[Segment], notes: list, target: Path, *,
                         font_name: str, font_size: int = 18, color: str = "#FFFFFF",
                         position: str = "bottom", outline: int = 1, vertical_offset: int = 0,
                         horizontal_offset: int = 0, aspect: float = 16 / 9,
                         require_target: bool = True, duration: float = 0.) -> Path | None:
    dialogue = _merge_overlapping_cues(dialogue)
    width = max(120, min(1800, round(360 * aspect)))
    font = "".join(char for char in font_name if char not in ",\r\n").strip() or "Arial"
    font_size = max(1, font_size)
    card_size = max(6, min(42, round((13 if width >= 500 else 11) * font_size / 18)))
    align, x, y = _dialogue_anchor(position, vertical_offset, horizontal_offset, width)
    # 与普通 SRT 使用相同的字号、边距和描边比例。
    size = font_size * ASS_DIALOGUE_SCALE
    border = max(0, min(8, int(outline))) * ASS_DIALOGUE_SCALE
    top = 82 + max(0, vertical_offset) if align == 8 and dialogue else 12
    bottom = 174 if align == 2 and dialogue else min(342, max(175, top + 106))
    if align == 5 and dialogue and vertical_offset < 0:
        measure_dialogue = _card_metrics(font, math.ceil(size))
        rows = max(sum(max(1, math.ceil(measure_dialogue(line) / (width - 26)))
                       for line in cue.text.split("\n")) for cue in dialogue)
        extent = math.ceil(size * .7 * rows + border + 4)
        if y - extent - top >= _card_height(card_size, 3):
            bottom = min(bottom, y - extent)
        else:
            top = min(342, y + extent)
            bottom = min(342, max(175, top + 106))
    line_limit = max(12, min(46, width // 10))
    while True:
        measure = _card_metrics(font, card_size)

        @lru_cache(maxsize=128)
        def height_of(text):
            return _card_height(card_size, len(_card_lines(_ass_text(text), width, measure)))

        cards = _cards(notes, dialogue, require_target=require_target, line_limit=line_limit,
                       top=top, bottom=bottom, height_of=height_of, duration=duration)
        excess = max((margin + height_of(text) - bottom for _, _, margin, _, text in cards), default=0)
        if excess <= 0 or card_size <= 6:
            break
        usable = max(1, bottom - top - 8)
        card_size = max(6, min(card_size - 1, card_size * usable // (usable + excess)))
    if not cards:
        return None
    primary = _ass_color(color)
    positioned = bool(vertical_offset or horizontal_offset)
    placement = "{\\an%d\\pos(%d,%d)}" % (align, x, y) if positioned else ""
    top = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: 360\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\nYCbCr Matrix: None\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY,"
        " Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: MainDialogue,{font},{size:g},{primary},{primary},&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,{border:g},0,{align},13,13,{ASS_DIALOGUE_MARGIN},1\n"
        f"Style: PersonCard,{font},{card_size},{primary},{primary},&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,12,12,12,1\n"
        f"Style: PersonCardBackground,{font},{card_size},&H26182028,&H26182028,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events = []
    for segment in dialogue:
        start = max(0, round(segment.start * 1000)) // 10
        end = (round(segment.end * 1000) + 9) // 10
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(max(start + 1, end))},"
                      f"MainDialogue,,0,0,0,,{placement}{_ass_text(segment.text)}\n")
    for index, (start, end, margin, fade, text) in enumerate(cards, 1):
        background, letters = _card_shapes(_ass_text(text), width, margin, card_size, measure, fade, fade)
        time = f"{_ass_time(start)},{_ass_time(end)}"
        suffix = f",JiyingCard{index},0,0,0,{_CARD_EFFECT},"
        events.extend((f"Dialogue: 1,{time},PersonCardBackground{suffix}{background}\n",
                       f"Dialogue: 2,{time},PersonCard{suffix}{letters}\n"))
    target.write_text(top + "".join(events), encoding="utf-8")
    return target
