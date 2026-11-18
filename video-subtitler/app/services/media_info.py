"""读取本机媒体容器指标，集中管理 FFprobe 和各格式的位深。"""
from __future__ import annotations

import math
import os
import re
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..core.paths import bin_dir


def find_ffprobe(config=None) -> str | None:
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    ffmpeg = config.get("paths.ffmpeg", "") if config else ""
    ffprobe = config.get("paths.ffprobe", "") if config else ""
    candidates = [Path(ffprobe)] if ffprobe else []
    if ffmpeg:
        candidates.append(Path(ffmpeg).with_name(name))
    candidates.append(bin_dir() / name)
    return next((str(path) for path in candidates if path.is_file()), None) or shutil.which("ffprobe")


def nonnegative(value, default=0.0) -> float:
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else default
    except (ValueError, TypeError):
        return default


def positive_ratio(value) -> float:
    try:
        return nonnegative(Fraction(str(value).replace(":", "/")))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _pixel_depth(stream: dict) -> int:
    width = stream.get("bits_per_raw_sample")
    if str(width).isdigit() and int(width) > 0:
        return int(width)
    pixel = stream.get("pix_fmt") or ""
    depth = re.search(r"(?:p0?|gray|y)(9|10|12|14|16)(?:msb)?(?:le|be)$", pixel)
    if depth:
        return int(depth[1])
    if "f32" in pixel:
        return 32
    return 16 if re.search(r"(?:rgb48|bgr48|rgba64|bgra64|p016|xv48|y216)", pixel) else 8


@dataclass(frozen=True)
class VideoTraits:
    depth: int = 8
    colors: tuple[str, ...] = ()
    duration: float = 0
    time_base: str = ""
    audio_codecs: tuple[str, ...] | None = None
    cuda_format: str = ""
    aspect: float = 16 / 9
    width: int = 0
    height: int = 0
    codec: str = ""
    pixel: str = ""
    fps: float = 0
    space: str = ""
    range: str = ""
    rotation: int = 0
    sar: float = 1
    stream_index: int = 0
    single_video: bool = False


STREAM_ENTRIES = ("format=duration:stream=index,codec_name,codec_type,pix_fmt,bits_per_raw_sample,width,height,"
                  "time_base,r_frame_rate,avg_frame_rate,color_space,color_range,color_transfer,color_primaries,"
                  "sample_aspect_ratio,display_aspect_ratio,duration:stream_disposition=attached_pic:stream_side_data=rotation")


def traits_from_probe(info: dict) -> VideoTraits:
    streams = info.get("streams", ())
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        return VideoTraits()
    stream = max(videos, key=lambda s: nonnegative(s.get("width")) * nonnegative(s.get("height")))
    width, height = (int(nonnegative(stream.get(axis))) for axis in ("width", "height"))
    aspect = positive_ratio(stream.get("display_aspect_ratio"))
    sar = positive_ratio(stream.get("sample_aspect_ratio")) or 1.0
    if not aspect:
        aspect = width * sar / height if width and height else 16 / 9
    rotation = next((int(side["rotation"]) % 360 for side in stream.get("side_data_list", ())
                    if "rotation" in side), 0)
    if rotation in (90, 270):
        aspect = 1 / aspect
    duration = nonnegative(info.get("format", {}).get("duration")) or nonnegative(stream.get("duration"))
    audio = tuple(s["codec_name"] for s in streams if s.get("codec_type") == "audio" and s.get("codec_name"))
    tags = [("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"),
            ("-colorspace", "color_space"), ("-color_range", "color_range")]
    colors = tuple(item for flag, attr in tags if stream.get(attr) not in (None, "", "unknown", "reserved")
                   for item in (flag, stream[attr]))
    codec, pixel = stream.get("codec_name", ""), stream.get("pix_fmt", "")
    single = len(videos) == 1
    cuda = ""
    if single and not rotation:
        if codec == "hevc" and min(width, height) >= 2160 and pixel in {"yuv420p10le", "p010le"}:
            cuda = "p010le"
        elif codec == "av1" and pixel in {"yuv420p", "nv12"}:
            cuda = "nv12"
    return VideoTraits(
        depth=_pixel_depth(stream), colors=colors, duration=duration, audio_codecs=audio,
        time_base=stream.get("time_base", "") if single and positive_ratio(stream.get("time_base")) else "",
        cuda_format=cuda, aspect=aspect, width=width, height=height, codec=codec, pixel=pixel,
        fps=positive_ratio(stream.get("avg_frame_rate")) or positive_ratio(stream.get("r_frame_rate")),
        space=stream.get("color_space", ""), range=stream.get("color_range", ""),
        rotation=rotation, sar=sar, stream_index=int(stream.get("index", 0)), single_video=single,
    )
