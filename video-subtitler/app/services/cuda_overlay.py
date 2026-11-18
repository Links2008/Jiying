"""为 CUDA 合成生成仅在字幕变化时更新的透明图层。"""
from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .ass_document import parse_ass
from .media_info import VideoTraits
from .subtitle_filters import _ass_filter, _filter_path

_NV_DECODERS = {"h264", "hevc", "av1", "vp9", "vp8", "mpeg1video", "mpeg2video", "vc1", "mjpeg"}
_SUPPORTED_RANGE = {"", "tv", "unknown"}
_SPACES = {"", "unknown", "bt709", "bt470bg", "smpte170m", "bt2020nc"}


@dataclass(frozen=True)
class SubtitleLayer:
    mask: Path
    graph: str
    width: int
    height: int


@dataclass(frozen=True)
class StreamLayer:
    graph_file: Path
    width: int
    height: int
    checks: tuple
    rotation: int
    dual_local: bool = False

    def input_args(self) -> list[str]:
        rotate = ["-noautorotate", "-display_rotation:v:0", "0"] if self.rotation else []
        return ["-init_hw_device", "cuda=compose:0", "-filter_hw_device", "compose",
                "-hwaccel", "cuda", "-hwaccel_device", "compose", "-hwaccel_output_format", "cuda", *rotate]

    def bounds_valid(self) -> bool:
        for file, (left, top, width, height) in self.checks:
            try:
                actual = _bounds(file, self.width, self.height)
            except OSError:
                return False
            if actual is None:
                return False
            x, y, w, h = actual
            if x < left or y < top or x + w > left + width or y + h > top + height:
                return False
        return True


def supported(traits: VideoTraits) -> bool:
    return (traits.single_video and traits.depth == 8 and traits.codec in _NV_DECODERS
            and traits.pixel in {"yuv420p", "nv12"} and traits.sar == 1 and traits.rotation == 0
            and traits.range in _SUPPORTED_RANGE and traits.space in _SPACES
            and traits.width >= 128 and traits.height >= 72)


def preferred(traits: VideoTraits, setting) -> bool:
    value = str(setting).casefold()
    if value in {"true", "on", "always"}:
        return supported(traits)
    # 短片应避开第一次着色器初始化与生成透明层的固定开销。
    return value == "auto" and supported(traits) and traits.duration >= 30 and traits.width * traits.height >= 1920 * 1080


@lru_cache(maxsize=1)
def _verified_dual_zone_device() -> bool:
    # 自动策略限定为同机五轮过线的单卡和驱动，其他设备仍可显式选 on。
    if os.name != "nt" or os.environ.get("CUDA_VISIBLE_DEVICES") not in (None, "0"):
        return False
    tool = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
    if not tool.is_file():
        return False
    try:
        probe = subprocess.run(
            [str(tool), "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=4, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "NVIDIA GeForce RTX 3060 Laptop GPU, 610.88"


def auto_dual_zone_preferred(path: Path, encoder: str, traits: VideoTraits, output_height: int | None) -> bool:
    return (path.suffix.casefold() == ".ass" and encoder == "h264_nvenc"
            and (traits.width, traits.height) == (3840, 2160) and output_height in (None, 2160)
            and traits.codec == "h264" and traits.pixel == "yuv420p" and traits.space == "bt709"
            and traits.range == "tv" and preferred(traits, "auto") and _verified_dual_zone_device())


def stream_preferred(encoder: str, traits: VideoTraits, setting, *, auto_dual_zone: bool = False) -> bool:
    value = str(setting).casefold()
    if value not in {"on", "true", "always"} and not (value == "auto" and auto_dual_zone):
        return False
    # QSV 编码可用性与像素级透明图层保真独立判断。后者未通过本机回归，继续用传统混合。
    if encoder not in {"h264_nvenc", "hevc_nvenc", "av1_nvenc"}:
        return False
    return (traits.single_video and traits.depth == 8 and traits.codec in _NV_DECODERS
            and traits.pixel in {"yuv420p", "nv12"} and traits.sar == 1
            and traits.range in _SUPPORTED_RANGE and traits.space in _SPACES
            and traits.width >= 128 and traits.height >= 72 and traits.rotation in {0, 90, 180, 270})


def _change_times(path: Path, segments, fps: float) -> list[int]:
    moments = {0}
    if path.suffix.casefold() == ".ass":
        for cue in parse_ass(path.read_text(encoding="utf-8-sig")):
            moments.update((cue.start_ms, cue.end_ms))
            # ASS 动画仅在过渡区间重新绘制。复杂自定义效果沿用连续 libass 路径。
            tags = "".join(re.findall(r"\{([^{}]*)\}", cue.text))
            if re.search(r"\\(?:move|t|fade)\s*\(|\\k[fo]?\s*\d", tags, re.IGNORECASE):
                return []
            for match in re.finditer(r"\\fad\(\s*(\d+)\s*,\s*(\d+)\s*\)", tags):
                step = max(8, round(1000 / max(24, fps or 24)))
                fade_in, fade_out = map(int, match.groups())
                length = cue.end_ms - cue.start_ms
                if any(fade > 2000 for fade in (fade_in, fade_out)):
                    return []
                fully_visible = cue.start_ms + min(fade_in, length)
                fading_out = cue.end_ms - min(fade_out, length)
                moments.update(range(cue.start_ms, fully_visible, step))
                moments.update(range(fading_out, cue.end_ms, step))
                moments.update((fully_visible, fading_out))
    else:
        for cue in segments:
            moments.update((max(0, round(cue.start * 1000)), max(0, round(cue.end * 1000))))
    return sorted(moments)


def _timestamp_expr(times: list[int], begin: int, end: int) -> str:
    if end - begin == 1:
        return str(times[begin])
    center = (begin + end) // 2
    return f"if(lt(N,{center}),{_timestamp_expr(times, begin, center)},{_timestamp_expr(times, center, end)})"


def _bounds(metadata: Path, width: int, height: int) -> tuple[int, int, int, int] | None:
    sides: dict[str, list[int]] = {}
    with metadata.open(encoding="utf-8") as rows:
        for row in rows:
            if match := re.fullmatch(r"lavfi\.bbox\.(x1|x2|y1|y2)=(\d+)", row.strip()):
                sides.setdefault(match[1], []).append(int(match[2]))
    if set(sides) != {"x1", "x2", "y1", "y2"}:
        return None
    # 4:2:0 的色度和 CUDA 覆盖起点必须对齐偶数。外扩一对像素避免切掉抗锯齿。
    left, top = (max(0, (min(sides[axis]) - 2) // 2 * 2) for axis in ("x1", "y1"))
    right = min(width, (max(sides["x2"]) + 5) // 2 * 2)
    bottom = min(height, (max(sides["y2"]) + 5) // 2 * 2)
    return left, top, right - left, bottom - top


def _outward_box(left, top, right, bottom, width, height, *, step=2):
    x, y = max(0, step * math.floor(left / step)), max(0, step * math.floor(top / step))
    return x, y, min(width, step * math.ceil(right / step)) - x, min(height, step * math.ceil(bottom / step)) - y


def _dialogue_box(width, height, position):
    top, bottom = {"top": (0, .48), "middle": (.22, .78)}.get(position, (.52, 1))
    return _outward_box(0, top * height, width, bottom * height, width, height)


_POSITION = re.compile(r"\\pos\((\d+),(\d+)\)")
_RECTANGLE = re.compile(r"m 0 0 l (\d+) 0 \1 (\d+) 0 \2$")


def _plain_dialogue(texts, font, size, script_width):
    """只对可确定行数、字宽和默认排版的对白裁掉上半画布。"""
    from .ass_composer import _card_metrics

    if font not in {"Noto Sans CJK SC", "Arial"} or not 0 < size <= 24:
        return False
    unique = set(texts)
    if not unique or len(unique) > 512:
        return False
    measure = _card_metrics(font, math.ceil(size))
    for text in unique:
        rows = text.splitlines()
        if (not 1 <= len(rows) <= 2 or re.search(r"[\\{}<>]", text)
                or any(len(row) > 120 or measure(row) * 1.25 > script_width - 32 for row in rows)):
            return False
    return True


def _local_srt_filter(filter_, segments, width, height, position, font):
    if (position != "bottom" or width * 9 != height * 16 or height % 4
            or not filter_.startswith("subtitles=") or not filter_.endswith("'")):
        return None
    style = re.search(r"FontSize=(\d+),Outline=(\d+),.*Alignment=2,MarginV=36'$", filter_)
    if not style or int(style[2]) > 4 or not _plain_dialogue(
            (cue.text for cue in segments or ()), font, int(style[1]), 384):
        return None
    # FFmpeg 字幕解码器的默认 SRT 画布是 384×288。上下坐标一起减半，
    # 保持字形缩放、底部边距和换行宽度。
    return filter_[:-1] + ",PlayResY=144'"


def _local_ass_dialogue(header, dialogue, width, height, script_width, position, font):
    if position != "bottom" or height % 360 or width * 360 != height * script_width:
        return None
    style = re.search(r"(?m)^Style: MainDialogue,([^\n]+)$", header)
    if not style:
        return None
    fields = style[1].split(",")
    if (len(fields) != 22 or fields[0] != font
            or fields[6:15] != ["0", "0", "0", "0", "100", "100", "0", "0", "1"]
            or fields[16:21] != ["0", "2", "13", "13", "45"]):
        return None
    try:
        size, outline = float(fields[1]), float(fields[15])
    except ValueError:
        return None
    if not 0 <= outline <= 4:
        return None
    events = [line[10:].split(",", 9) for line in dialogue]
    if any(event[0] != "0" or event[4:9] != ["", "0", "0", "0", ""] for event in events):
        return None
    if not _plain_dialogue((event[9].replace(r"\N", "\n") for event in events),
                           font, size, script_width):
        return None
    scale = height // 360
    return header.replace("PlayResY: 360", "PlayResY: 170"), (0, 190 * scale, width, 170 * scale)

def _card_tracks(path, folder, width, height, position, font):
    """分离应用生成的两条 ASS 轨，保持脚本坐标和图层。"""
    from .ass_composer import _CARD_TAGS

    raw = path.read_text(encoding="utf-8-sig")
    header, section, body = raw.partition("[Events]\n")
    res_x = re.search(r"(?m)^PlayResX: (\d+)$", header)
    if not section or not res_x or not re.search(r"(?m)^PlayResY: 360$", header):
        return ()
    layout, _, events = body.partition("\n")
    if not layout.startswith("Format: Layer, Start, End, Style, Name,"):
        return ()
    dialogue, cards = [], []
    boxes = {}
    anchors = {}
    for line in events.splitlines():
        if not line.strip():
            continue
        if not line.startswith("Dialogue: "):
            return ()
        fields = line[10:].split(",", 9)
        if len(fields) != 10:
            return ()
        if fields[3] == "MainDialogue":
            dialogue.append(line)
            continue
        if fields[3] not in {"PersonCard", "PersonCardBackground"} or fields[8] != "jiying-card":
            return ()
        cards.append(line)
        if fields[3] == "PersonCardBackground":
            pos, shape = _POSITION.search(fields[9]), _RECTANGLE.search(fields[9])
            if not pos or not shape:
                return ()
            x, y = map(int, pos.groups())
            w, h = map(int, shape.groups())
            boxes[fields[4]] = (x, y, x + w, y + h)
        else:
            tags = _CARD_TAGS.fullmatch(fields[9])
            if not tags or any(char in tags[5].replace(r"\N", "") for char in "\\{}"):
                return ()
            anchors[fields[4]] = (int(tags[1]), int(tags[2]))
    if (not boxes or len(cards) != 2 * len(boxes) or boxes.keys() != anchors.keys()
            or any(anchors[key] != (box[0] + 4, box[1] + 4) for key, box in boxes.items())):
        return ()
    script_width = int(res_x[1])
    if not 120 <= script_width <= 1800:
        return ()
    left = max(0, min(box[0] for box in boxes.values()) - 8)
    top = max(0, min(box[1] for box in boxes.values()) - 8)
    right = min(script_width, max(box[2] for box in boxes.values()) + 8)
    bottom = min(360, max(box[3] for box in boxes.values()) + 8)
    if right <= left or bottom <= top:
        return ()
    sx, sy = width / script_width, height / 360
    integral_scale = sx == sy and sx.is_integer()
    # CUDA 4:2:0 需要偶数边界，局部 ASS 同时需要整脚本坐标。1080p 的 3 倍缩放须按 6 对齐。
    step = math.lcm(2, int(sx)) if integral_scale else 2
    region = _outward_box(left * sx, top * sy, right * sx, bottom * sy, width, height, step=step)
    main = _dialogue_box(width, height, position)
    local_main = _local_ass_dialogue(header, dialogue, width, height, script_width, position, font) if dialogue else None
    dialogue_header = header
    if local_main:
        dialogue_header, main = local_main
    if dialogue and region[1] < main[1] + main[3] and main[1] < region[1] + region[3]:
        return ()
    card_header = header
    local = integral_scale and all(value % sx == 0 for value in region)
    if local:
        x, y, w, h = [int(value / sx) for value in region]
        card_header = re.sub(r"(?m)^PlayResX: \d+$", f"PlayResX: {w}", header)
        card_header = card_header.replace("PlayResY: 360", f"PlayResY: {h}")
        cards = [_POSITION.sub(lambda match: rf"\pos({int(match[1])-x},{int(match[2])-y})", line, count=1)
                 for line in cards]
    prefix = section + layout + "\n"
    card_path = folder / "card.ass"
    card_path.write_text(card_header + prefix + "\n".join(cards) + "\n", encoding="utf-8")
    tracks = []
    if dialogue:
        main_path = folder / "dialogue.ass"
        main_path.write_text(dialogue_header + prefix + "\n".join(dialogue) + "\n", encoding="utf-8")
        tracks.append((main_path, _ass_filter(main_path, font), main, bool(local_main)))
    tracks.append((card_path, _ass_filter(card_path, font), region, local))
    return tracks


def _active_gate(path, segments):
    if path.suffix.casefold() == ".ass":
        cues = [(cue.start_ms / 1000, cue.end_ms / 1000)
                for cue in parse_ass(path.read_text(encoding="utf-8-sig"))]
    else:
        cues = [(cue.start, cue.end) for cue in segments]
    intervals = []
    for begin, end in sorted(cues):
        if intervals and begin <= intervals[-1][1]:
            intervals[-1] = (intervals[-1][0], max(end, intervals[-1][1]))
        else:
            intervals.append((begin, end))
    if not intervals or len(intervals) > 24:
        return ""
    return ":enable='" + "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in intervals) + "'"


def stream_plan(path: Path, segments, subtitle_filter: str, traits: VideoTraits,
                output_height: int | None, folder: Path, position: str, font: str) -> StreamLayer | None:
    w, h = (traits.height, traits.width) if traits.rotation in (90, 270) else (traits.width, traits.height)
    if output_height:
        w, h = round(w * output_height / h / 2) * 2, output_height
    if min(w, h) < 2 or w % 2 or h % 2:
        return None
    tracks = _card_tracks(path, folder, w, h, position, font) if path.suffix.casefold() == ".ass" else ()
    if not tracks:
        local = (_local_srt_filter(subtitle_filter, segments, w, h, position, font)
                 if path.suffix.casefold() == ".srt" else None)
        box = (0, h // 2, w, h // 2) if local else (0, 0, w, h)
        tracks = [(path, local or subtitle_filter, box, bool(local))]
    rotation = {0: "", 90: "transpose_cuda=cclock,", 180: "transpose_cuda=reversal,",
                270: "transpose_cuda=clock,"}[traits.rotation]
    scaled = f"{rotation}scale_cuda={w}:{h}:format=yuv420p:interp_algo=bicubic:use_filters=0"
    graph = [f"[0:{traits.stream_index}]{scaled}[base]"]
    matrix = {"bt709": "bt709", "bt470bg": "bt601", "smpte170m": "bt601", "bt2020nc": "bt2020"}.get(traits.space)
    colors = f"colorspace={traits.space}:" if matrix else ""
    checks = []
    master = "base"
    for index, (subtitle, filter_, box, local) in enumerate(tracks):
        moments = _change_times(subtitle, segments, traits.fps)
        if not 1 < len(moments) <= 24_000:
            return None
        x, y, bw, bh = box
        canvas_w, canvas_h = (bw, bh) if local else (w, h)
        timeline = _timestamp_expr(moments, 0, len(moments))
        layer = f"paint{index}"
        graph.append(f"color=c=black@0:s=2x2:r=1000,format=rgba,trim=end_frame={len(moments)},"
                     f"setpts='{timeline}',scale={canvas_w}:{canvas_h}:flags=neighbor,setsar=1,{filter_}:alpha=1,"
                     f"format=rgba[{layer}]")
        if not local and box != (0, 0, w, h):
            check = folder / f"bounds-{index}.txt"
            graph.append(f"[{layer}]split=2[crop{index}][check{index}]")
            graph.append(f"[check{index}]alphaextract,format=gray,bbox=min_val=0,"
                         f"metadata=mode=print:file='{_filter_path(check)}',nullsink")
            graph.append(f"[crop{index}]crop={bw}:{bh}:{x}:{y}[region{index}]")
            layer = f"region{index}"
            checks.append((check, box))
        convert = f"scale=out_color_matrix={matrix}:out_range=limited," if matrix else ""
        upload = f"{convert}format=yuva420p,setparams={colors}range=limited:alpha_mode=straight,hwupload"
        gate = _active_gate(subtitle, segments)
        graph.append(f"[{layer}]{upload}[mask{index}]")
        graph.append(f"[{master}][mask{index}]overlay_cuda=x={x}:y={y}:repeatlast=1{gate}[mix{index}]")
        master = f"mix{index}"
    if w % 32 or h % 32:
        graph.append(f"[{master}]crop={w}:{h}:0:0,scale_cuda={w}:{h}:format=nv12:interp_algo=nearest:passthrough=0[v]")
    else:
        graph[-1] = graph[-1].removesuffix(f"[{master}]") + "[v]"
    filter_file = folder / "stream.txt"
    filter_file.write_text(";\n".join(graph), encoding="utf-8")
    return StreamLayer(filter_file, w, h, tuple(checks), traits.rotation,
                       dual_local=len(tracks) == 2 and all(track[3] for track in tracks))


def prepare(ffmpeg: str, path: Path, segments, subtitle_filter: str, traits: VideoTraits,
            output_height: int | None, directory: Path, run, task_id: str | None) -> SubtitleLayer | None:
    moments = _change_times(path, segments, traits.fps)
    if not 1 < len(moments) <= 24_000:
        return None
    w, h = traits.width, traits.height
    if output_height:
        w = round(w * output_height / h / 2) * 2
        h = output_height
    if min(w, h) < 2 or w % 2 or h % 2:
        return None
    color_matrix = traits.space if traits.space in {"bt709", "bt470bg", "smpte170m", "bt2020nc"} else ""
    convert = {"bt470bg": "bt601", "smpte170m": "bt601", "bt2020nc": "bt2020"}.get(color_matrix, color_matrix)
    scale_yuv = f"scale=out_color_matrix={convert}:out_range=limited," if convert else ""
    timeline = _timestamp_expr(moments, 0, len(moments))
    mask, bounds, graph = (directory / name for name in ("layer.mkv", "bbox.txt", "layer.txt"))
    graph.write_text(
        f"[0:v]setpts='{timeline}',scale={w}:{h}:flags=neighbor,setsar=1,"
        f"{subtitle_filter}:alpha=1,format=rgba,split=2[paint][alpha];"
        f"[paint]{scale_yuv}format=yuva420p[track];"
        f"[alpha]format=rgba,alphaextract,format=gray,bbox=min_val=0,metadata=mode=print:file='{_filter_path(bounds)}'[bounds]",
        encoding="utf-8",
    )
    frames = str(len(moments))
    run([ffmpeg, "-nostdin", "-y", "-f", "lavfi", "-i", "color=black@0:s=2x2:r=1000,format=rgba",
         "-/filter_complex", str(graph), "-map", "[track]", "-fps_mode", "passthrough",
         "-enc_time_base", "1/1000", "-frames:v", frames, "-c:v", "ffv1", "-threads", "3",
         "-pix_fmt", "yuva420p", str(mask), "-map", "[bounds]", "-frames:v", frames, "-f", "null", "-"],
        task_id=task_id)
    box = _bounds(bounds, w, h)
    if not box:
        return None
    left, top, width, height = box
    color = f"colorspace={color_matrix}:" if color_matrix else ""
    # overlay_cuda 在复制可写帧时可把帧的可见尺寸扩大到 32 像素的分配边界。
    # crop 标记填充区域，随后在显存内恢复编码尺寸，同时转换成 NVENC 的 NV12。
    normalize = (f",crop={w}:{h}:0:0,scale_cuda={w}:{h}:format=nv12:interp_algo=nearest:passthrough=0"
                 if w % 32 or h % 32 else "")
    graph = (f"[0:{traits.stream_index}]scale_cuda={w}:{h}:format=yuv420p:interp_algo=bicubic:use_filters=0[main];"
             f"[1:v]crop={width}:{height}:{left}:{top},setparams={color}range=limited:alpha_mode=straight,"
             f"hwupload[mask];[main][mask]overlay_cuda=x={left}:y={top}:repeatlast=1{normalize}[v]")
    return SubtitleLayer(mask, graph, w, h)
