"""yt-dlp 画质排序、下载选档及媒体大小估算。"""
from __future__ import annotations

import re

def _best_audio(formats: list[dict]) -> dict | None:
    """yt-dlp 已按偏好从低到高排序，与原生 ba 选择同一音轨。"""
    return next((f for f in reversed(formats) if f.get("vcodec") == "none"
                 and f.get("acodec") not in (None, "none")), None)


def _format_size(fmt: dict, audio: dict | None = None) -> tuple[int, bool]:
    streams = [fmt, audio] if fmt.get("acodec") == "none" and audio else [fmt]
    total, estimated = 0, False
    for stream in streams:
        exact = int(stream.get("filesize") or 0)
        size = exact if exact > 0 else int(stream.get("filesize_approx") or 0)
        if size <= 0:
            return 0, False  # 一个必需流未知时，不能把另一条流当成整片大小。
        total += size
        estimated |= exact <= 0
    return total, estimated


def _short_edge(fmt: dict) -> int:
    width = int(fmt.get("width") or 0)
    height = int(fmt.get("height") or 0)
    return min(width, height) if width and height else width or height


def _bilibili_uhd(fmt: dict) -> str:
    short = _short_edge(fmt)
    long = max(int(fmt.get("width") or 0), int(fmt.get("height") or 0))
    if short >= 3200 and long >= 7680:
        return "8K"
    if short >= 1600 and long >= 3840:
        return "4K"
    return ""


def _bilibili_choice(fmt: dict) -> str:
    return "bili-" + "-".join(str(value) for value in (
        fmt.get("quality") or 0, fmt.get("width") or 0, fmt.get("height") or 0,
        fmt.get("fps") or 0, fmt.get("vcodec") or "video", fmt.get("dynamic_range") or "SDR",
    ))


def _video_priority(fmt: dict, bilibili: bool = False) -> tuple:
    # 短边区分竖屏档位；分辨率相同时帧率优先，最后比较同档来源质量。
    long_edge = max(int(fmt.get("width") or 0), int(fmt.get("height") or 0))
    short = _short_edge(fmt)
    pixels = short * long_edge if bilibili and _bilibili_uhd(fmt) else 0
    return (pixels, short, long_edge, float(fmt.get("fps") or 0),
            (fmt.get("dynamic_range") or "SDR") == "SDR",
            float(fmt.get("quality") or 0), float(fmt.get("tbr") or 0),
            fmt.get("filesize") or fmt.get("filesize_approx") or 0,
            fmt.get("ext") == "mp4")


def _video_formats(formats: list[dict]) -> list[dict]:
    videos = [fmt for fmt in formats if fmt.get("format_id") is not None
              and fmt.get("vcodec") != "none" and fmt.get("has_drm") is not True]
    if _best_audio(formats) is None and any(fmt.get("acodec") != "none" for fmt in videos):
        # 有完整视频可用时，不把无法配音轨的分离流当成可交付档位。
        return [fmt for fmt in videos if fmt.get("acodec") != "none"
                or fmt.get('_jiying_complete_media')]
    return videos


def _select_video(formats: list[dict], resolution: str, bilibili: bool = False) -> dict | None:
    videos = _video_formats(formats)
    def priority(fmt):
        return _video_priority(fmt, bilibili)

    if resolution in ("best", "bestvideo", ""):
        return max(videos, key=priority, default=None)
    if bilibili and resolution.startswith("bili-"):
        return max((fmt for fmt in videos if _bilibili_uhd(fmt) and _bilibili_choice(fmt) == resolution),
                   key=priority, default=None)
    exact = next((fmt for fmt in videos if str(fmt["format_id"]) == resolution), None)
    if exact is not None:
        return exact
    if re.fullmatch(r"\d{3,4}p", resolution, re.I):
        maximum = _safe_height(resolution)
        return max((fmt for fmt in videos if 0 < _short_edge(fmt) <= maximum),
                   key=priority, default=None)
    return None


def _safe_height(resolution: str) -> int | None:
    """"1080p"/"1080" → 1080；无法解析（如 "sb2"、"hls-1080" 之外的怪值）返回 None。

    format_id 不一定是纯数字 —— 直接 int() 会让任务崩掉。
    """
    m = re.search(r"(\d{3,4})\s*p?$", str(resolution).strip(), re.I)
    return int(m.group(1)) if m else None
