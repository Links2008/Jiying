"""字幕滤镜参数、路径转义及带位置偏移的 ASS 生成。"""
from __future__ import annotations

import re
from pathlib import Path

from ..core.errors import AppError, Codes
from ..core.paths import bin_dir

# 字幕位置 → Alignment。注意：ffmpeg+libass 对 SRT force_style 的编号与
# ASS 标准不一致，以下为逐值实测的经验映射（2=底部居中 6=顶部居中 10=中部居中）
POSITIONS = {"bottom": 2, "middle": 10, "top": 6}
# SRT 解码器的 PlayResY 为 288；自产 ASS 的画布高 360。
ASS_DIALOGUE_SCALE = 360 / 288
ASS_DIALOGUE_MARGIN = 45


def _dialogue_anchor(position: str, vertical_offset: int, horizontal_offset: int,
                     width: int = 640) -> tuple[int, int, int]:
    align, base_y = {"bottom": (2, 360 - ASS_DIALOGUE_MARGIN), "middle": (5, 180),
                     "top": (8, ASS_DIALOGUE_MARGIN)}.get(position, (2, 360 - ASS_DIALOGUE_MARGIN))
    y = max(18, min(342, base_y + max(-120, min(120, int(vertical_offset)))))
    x = round(width * (320 + max(-270, min(270, int(horizontal_offset)))) / 640)
    return align, x, y

# ffmpeg 路径转义：单引号需穿透两层解析（filtergraph 层 + subtitles 滤镜层）。
# 引号内 \ 不是转义符、' 必收尾，故用「收尾引号 + 转义反斜杠 + 转义引号
# + 重开引号」写法：第一层吐出 \' 、第二层还原为字面 '。否则标题含撇号
# （如 Don't Look Up）时滤镜解析失败 → 烧录零产物。
_QUOTE_ESC = "'" + r"\\" + r"\'" + "'"


def _filter_path(path: Path) -> str:
    """穿过两层滤镜语法转义路径，保留 POSIX 文件名里的字面反斜杠。"""
    return path.as_posix().replace("\\", r"\\").replace(":", r"\:").replace("'", _QUOTE_ESC)


def _font_option(font_name: str) -> str:
    if font_name != "Noto Sans CJK SC":
        return ""
    media = bin_dir()
    font_dir = (media.parent if media.name == "bin" else media) / "fonts"
    font_file = font_dir / "NotoSansCJKsc-Regular.otf"
    if not font_file.is_file():
        raise AppError(Codes.DEP_MISSING, "内置字幕字体 Noto Sans CJK SC 缺失",
                       detail=str(font_file))
    return f":fontsdir='{_filter_path(font_dir)}'"


def _ass_filter(path: Path, font_name: str) -> str:
    return f"ass='{_filter_path(path)}'{_font_option(font_name)}"


def _ass_color(color: str) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise AppError(Codes.CFG_INVALID, "字幕颜色必须是 #RRGGBB 格式")
    rgb = color[1:].upper()
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def _subtitle_filter(srt_path: Path, font_name: str = "", font_size: int = 18,
                     position: str = "bottom", outline: int = 1,
                     color: str = "#FFFFFF") -> str:
    """subtitles 滤镜参数：Windows 路径需转义盘符冒号、反斜杠与单引号。

    outline：黑色描边粗细（0 = 无描边；ASS 里越大越粗）。
    """
    p = _filter_path(srt_path)
    align = POSITIONS.get(position, 2)
    ass_color = _ass_color(color)
    try:
        width = max(0, min(8, int(outline)))
    except (TypeError, ValueError):
        width = 1
    style = (f"FontSize={int(font_size)},Outline={width},"
             f"OutlineColour=&H00000000,PrimaryColour={ass_color},"
             f"Alignment={align},MarginV=36")
    if font_name:
        # 字体名会拼进 force_style（, 和 : 是语法分隔符、' 是包裹符），
        # 一律剔除，防止通过字体名注入滤镜参数
        safe_font = re.sub(r"[',:;]", "", str(font_name))
        style = f"FontName={safe_font}," + style
    return f"subtitles='{p}'{_font_option(font_name)}:force_style='{style}'"


def _ass_time(centiseconds: int) -> str:
    hours, remaining = divmod(centiseconds, 360000)
    minutes, remaining = divmod(remaining, 6000)
    seconds, centis = divmod(remaining, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def _ass_text(text: str) -> str:
    # 禁止将对白或模型响应中的反斜杠、花括号当成排版指令。
    escaped = {"{": r"\{", "}": r"\}", "\\": r"\{}", "\n": r"\N"}
    return "".join(escaped.get(char, char) for char in text)


def _write_positioned_ass(srt: Path, target: Path, position: str,
                          vertical_offset: int, horizontal_offset: int = 0, *, segments=None) -> Path:
    """自定义偏移采用 ASS；中部对齐时 SRT 的 force_style 会忽略 MarginV。"""
    from .subtitler import _merge_overlapping_cues, read_srt
    from .transcriber import Segment

    align, x, y = _dialogue_anchor(position, vertical_offset, horizontal_offset)

    # 使用 SRT 解码器的画布和边距，使描边、换行与零偏移一致。
    header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 384\nPlayResY: 288\n"
              "ScaledBorderAndShadow: yes\nYCbCr Matrix: None\n"
              "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour,"
              " SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline,"
              " StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
              " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              "Style: Default,Arial,18,&H00FFFFFF,&H00FFFFFF,&H00000000,"
              "&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,36,1\n"
              "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL,"
              " MarginR, MarginV, Effect, Text\n")
    # ASS 文件只有百分之一秒精度。向外取整才能覆盖原字幕的毫秒时间窗，
    # 起止分别四舍五入会让极短的原文/译文变成零时长而消失。
    cues = []
    for seg in segments if segments is not None else read_srt(srt):
        start_cs = max(0, round(seg.start * 1000)) // 10
        end_cs = (round(seg.end * 1000) + 9) // 10
        cues.append(Segment(start_cs / 100, end_cs / 100, seg.text))
    events = []
    anchor = f"{{\\an{align}\\pos({x * 384 / 640:g},{y / ASS_DIALOGUE_SCALE:g})}}"
    # 精度转换后可能出现新的重叠；固定坐标会让多条文字画在同一位置，必须合并。
    for seg in _merge_overlapping_cues(cues):
        events.append(f"Dialogue: 0,{_ass_time(round(seg.start * 100))},{_ass_time(round(seg.end * 100))},"
                      f"Default,,0,0,0,,{anchor}{_ass_text(seg.text)}\n")
    target.write_text(header + "".join(events), encoding="utf-8")
    return target
