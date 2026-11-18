"""独立读取画面文字，根据位置、持续时间和名牌上下文识别英文姓名。"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from ..core import cancel
from ..core.errors import AppError, Codes
from ..core.paths import app_root
from .ocr_pipe import connected_worker, supports_worker, tracked_process
from .renderer import _run, _video_traits, find_ffmpeg

log = logging.getLogger(__name__)
_RATE = 2
_REFRESH_SECONDS = 3
_WINDOW_SECONDS = 45
_WORD = r"[A-ZÀ-ÖØ-ÞĀ-Ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž’'-]*"
_NAME = re.compile(rf"{_WORD}(?: (?:[A-Z]\. )?{_WORD}){{1,3}}")
_TITLE = re.compile(r"^(Dr\.?|Prof\.?|Director|Coach|President|Professor)\s+(.+)$", re.I)
_ROLE = re.compile(r"\b(?:host|guest|founder|chair|CEO|CTO|CFO|director|producer|editor|journalist|"
                   r"professor|lead|researcher|engineer|manager|analyst|reporter|president|correspondent|"
                   r"writer|author|interviewer|performer|speaker|coach|captain)\b", re.I)
_NOT_NAMES = {"news", "network", "copyright", "welcome", "chapter", "opening", "episode", "special", "view",
              "credits", "production", "official", "breaking", "updates", "university", "studio",
              "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "the", "your"}
# 只过滤无姓名或头衔证据的明确地点标识，保留其它无头衔的持续名牌。
_PLACE_NAMES = {"new york", "los angeles", "san francisco", "hong kong", "new delhi"}
_PLACE_SUFFIX = {"airport", "station", "district", "province"}


def _line_text(row: dict) -> str:
    return unicodedata.normalize("NFC", " ".join(str(row.get("text") or "").split()))


def _candidates(rows: list[dict]) -> dict[str, tuple[str, str, float, float]]:
    candidates = {}
    for row in rows:
        name = _line_text(row)
        prefix = ""
        if titled := _TITLE.fullmatch(name):
            prefix, name = titled.groups()
        x, y = float(row["x"]), float(row["y"])
        height = float(row["height"])
        eligible = _NAME.fullmatch(name) or prefix and re.fullmatch(_WORD, name)
        if not (eligible and .01 <= y <= .9 and .013 <= height <= .21 and
                not _ROLE.search(name) and not (_NOT_NAMES & set(name.casefold().split()))):
            continue
        labels = [(abs(float(item["y"]) - y), _line_text(item)) for item in rows if item is not row
                  and abs(float(item["x"]) - x) < .075 and abs(float(item["y"]) - y) < .13
                  and 0 < len(_line_text(item)) <= 92 and _ROLE.search(_line_text(item))]
        # 无头衔时仍接受持续存在的下沿或角落名牌。中央标题依赖明确的姓名/头衔布局。
        key = name.casefold()
        if not labels and not prefix and (y < .55 or .26 < x < .57
                                          or key in _PLACE_NAMES or key.split()[-1] in _PLACE_SUFFIX):
            continue
        title = min(labels)[1] if labels else prefix
        candidates[key] = (name, title, x, y)
    return candidates


def from_frames(frames: Iterable[tuple[float, list[dict]]], duration: float) -> list[dict]:
    spans = []
    latest = {}
    previous = {}

    def same_position(x, y, other_x, other_y):
        return abs(x - other_x) < .075 and abs(y - other_y) < .06

    for at, rows in frames:
        current = _candidates(rows)
        for key, (person, role, x, y) in current.items():
            if known := latest.get(key):
                note, last_x, last_y = known
                gap = at - note["end"]
                if gap > .26 and not (
                        gap <= 1 / _RATE + .01 and same_position(x, y, last_x, last_y)
                        and (not role or not note["sense"] or role.casefold() == note["sense"].casefold())
                        and not any(other != key and same_position(x, y, ox, oy)
                                    for other, (_, _, ox, oy) in previous.items())):
                    note = None
            else:
                note = None
            if note is None:
                note = {"source": person, "target": person, "sense": role, "kind": "person",
                        "origin": "screen", "start": at, "end": at, "seen": 0}
                spans.append(note)
            note["end"] = min(duration, at + 1 / _RATE)
            note["seen"] += 1
            if role:
                note["sense"] = role
            latest[key] = (note, x, y)
        previous = current
    return [{key: value for key, value in note.items() if key != "seen"}
            for note in spans if note["seen"] >= 2 or note["sense"]]


def detect(config, video: Path, task_id: str, work: Path, progress) -> list[dict]:
    if os.name != "nt":
        raise AppError(Codes.DEP_MISSING, "画面文字识别需使用 Windows 桌面版本")
    configured = config.get("paths.ocr_executable", "")
    tool = Path(configured) if configured else app_root().parent / "Jiying.Native.exe"
    if not tool.is_file():
        raise AppError(Codes.DEP_MISSING, "找不到画面文字识别组件，请使用配对的即影 Windows 完整包")
    traits = _video_traits(video, config, task_id)
    duration = traits.duration
    if duration <= 0:
        raise AppError(Codes.RENDER_FAILED, "无法读取画面识别所需的视频时长")
    if supports_worker(tool):
        try:
            return from_frames(_stream_frames(tool, config, video, traits, task_id, progress), duration)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            cancel.raise_if_cancelled(task_id)
            log.warning("OCR 内存管道暂不可用，使用批次识别：%s", type(error).__name__)
    return _detect_batches(tool, config, video, task_id, work, progress, duration)


def _different_frame(pixels, previous, width, height) -> bool:
    if pixels == previous:
        return False
    import numpy as np

    current = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)
    prior = np.frombuffer(previous, dtype=np.uint8).reshape(height, width, 4)
    # 同一灰度采样格上保留相邻变化，忽略不足以构成字形的孤立噪声点。
    difference = np.subtract(current[::2, ::2, 0], prior[::2, ::2, 0], dtype=np.int16)
    changed = np.abs(difference) >= 4
    return (np.count_nonzero(changed) >= 8
            or bool(np.any(changed[1:] & changed[:-1]))
            or bool(np.any(changed[:, 1:] & changed[:, :-1])))


def _stream_frames(tool, config, video, traits, task_id, progress):
    if not traits.width or not traits.height or traits.rotation % 90:
        raise ValueError("无法确定流式抽帧尺寸")
    with connected_worker(tool, task_id) as (client, ocr_limit):
        width, height = (traits.height, traits.width) if traits.rotation in (90, 270) else (traits.width, traits.height)
        scale = min(1., min(1280, ocr_limit) / max(width, height))
        width, height = (max(2, 2 * round(value * scale / 2)) for value in (width, height))
        args = [find_ffmpeg(config), "-v", "error", "-xerror", "-nostdin", "-threads", "2", "-filter_threads", "2",
                "-i", str(video), "-map", f"0:{traits.stream_index}", "-an", "-sn",
                "-vf", f"setpts=PTS-STARTPTS,fps={_RATE}:start_time=0,scale={width}:{height},format=gray,format=bgra",
                "-threads:v", "1", "-c:v", "rawvideo", "-f", "rawvideo", "pipe:1"]
        previous = None
        rows = []
        last_sent = calls = count = 0
        last_report = 0.
        with tempfile.TemporaryFile() as errors:
            with tracked_process(args, task_id, stdout=subprocess.PIPE, stderr=errors) as decoder:
                bytes_per_frame = width * height * 4
                while pixels := client.wait_io(decoder, decoder.stdout.read, bytes_per_frame):
                    if len(pixels) != bytes_per_frame:
                        raise OSError("抽帧像素数据不完整")
                    index, at = count, count / _RATE
                    count += 1
                    if at >= traits.duration:
                        continue
                    if previous is None or pixels != previous and (
                            index - last_sent >= _RATE * _REFRESH_SECONDS or _different_frame(pixels, previous, width, height)):
                        rows = client.recognize(index, round(at * 1000), width, height, pixels)
                        previous, last_sent = pixels, index
                        calls += 1
                    yield at, rows
                    if time.monotonic() - last_report >= .5:
                        progress(at / traits.duration)
                        last_report = time.monotonic()
                if decoder.wait(timeout=5) or count == 0:
                    errors.seek(0)
                    raise OSError("FFmpeg 抽帧失败：" + errors.read(2000).decode("utf-8", errors="replace"))
                log.info("OCR 内存抽帧完成，抽帧=%d，识别请求=%d，FFmpeg PID=%d，OCR PID=%d",
                         count, calls, decoder.pid, client.process.pid)
    progress(1.)


def _detect_batches(tool, config, video, task_id, work, progress, duration):
    frames = []
    ffmpeg = find_ffmpeg(config)
    work.mkdir(parents=True, exist_ok=True)
    for start in range(0, math.ceil(duration), _WINDOW_SECONDS):
        cancel.raise_if_cancelled(task_id)
        with tempfile.TemporaryDirectory(prefix="jiying-ocr-", dir=work) as temp:
            folder = Path(temp)
            result = folder / "lines.json"
            _run([ffmpeg, "-v", "error", "-nostdin", "-ss", str(start), "-i", str(video),
                  "-t", str(min(duration - start, _WINDOW_SECONDS)), "-an", "-sn", "-threads", "2",
                  "-vf", f"fps={_RATE},scale='min(1280,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease:"
                         "force_divisible_by=2,format=gray", "-start_number", "0",
                  str(folder / "frame-%05d.png")], task_id=task_id, timeout=180)
            try:
                _run([str(tool), "--ocr-frames", str(folder), str(result)], task_id=task_id, timeout=180)
                payload = json.loads(result.read_text(encoding="utf-8-sig"))
                for frame, lines in sorted(payload.items(), key=lambda item: int(item[0])):
                    at = start + int(frame) / _RATE
                    if 0 <= at < duration and isinstance(lines, list):
                        frames.append((at, lines))
            except (ValueError, OSError, AttributeError, AppError) as error:
                if isinstance(error, AppError) and error.code == Codes.TASK_CANCELLED:
                    raise
                raise AppError(Codes.RENDER_FAILED, "画面文字识别未完成，请检查 Windows 英语 OCR 语言组件",
                               retryable=True) from error
        progress(min(1., (start + _WINDOW_SECONDS) / duration))
    return from_frames(frames, duration)
