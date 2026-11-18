"""检查可编辑的 ASS 字幕与事件时间轴。"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AssEvent:
    start_ms: int
    end_ms: int
    style: str
    text: str


def _time(value: str) -> int:
    match = re.fullmatch(r"(\d{1,3}):([0-5]\d):([0-5]\d)\.(\d{2})", value.strip())
    if not match:
        raise ValueError("ASS 时间应为 时:分:秒.百分秒")
    hours, minutes, seconds, centis = map(int, match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + centis * 10


def parse_ass(text: str) -> list[AssEvent]:
    if len(text) > 8 * 1024 * 1024 or "\0" in text:
        raise ValueError("ASS 字幕过大或含无效字符")
    section = ""
    styles = set()
    events = []
    layout = []
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.casefold()
            continue
        label, separator, value = line.partition(":")
        label = label.casefold()
        if not separator:
            continue
        if section == "[v4+ styles]" and label == "style":
            name = value.split(",", 1)[0].strip()
            if name:
                styles.add(name)
        elif section == "[events]" and label == "format":
            layout = [name.strip().casefold() for name in value.split(",")]
        elif section == "[events]" and label == "dialogue":
            if (not layout or layout[-1] != "text" or
                    not {"start", "end", "style"}.issubset(layout) or len(set(layout)) != len(layout)):
                raise ValueError("ASS 事件栏缺少 Start、End、Style 或 Text")
            fields = value.split(",", len(layout) - 1)
            if len(fields) != len(layout):
                raise ValueError("ASS 对白行的字段数不符")
            cue = dict(zip(layout, fields))
            begin, end = _time(cue["start"]), _time(cue["end"])
            if end <= begin or not cue["text"].strip():
                raise ValueError("ASS 对白的时间或文字无效")
            events.append(AssEvent(begin, end, cue["style"].strip(), cue["text"].lstrip()))
    if not events or any(event.style not in styles for event in events):
        raise ValueError("ASS 缺少对白或引用了未定义的样式")
    return events
