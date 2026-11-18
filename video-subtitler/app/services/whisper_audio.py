"""将识别文件读成 Whisper 所需的 16 kHz 单声道 PCM。"""
from __future__ import annotations

import subprocess
import time
import wave
from pathlib import Path


def decode_audio(path: Path, config, check_cancel=None):
    import numpy as np

    raw = None
    if check_cancel:
        check_cancel()
    try:
        with wave.open(str(path), "rb") as stream:
            if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (16000, 1, 2):
                raw = stream.readframes(stream.getnframes())
    except (wave.Error, EOFError):
        pass
    if raw is None:
        raw = _resample(path, config, check_cancel)
    if check_cancel:
        check_cancel()
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    audio *= 1.0 / 32768.0
    return audio


def _resample(path: Path, config, check_cancel) -> bytes:
    from .renderer import find_ffmpeg, _NO_WINDOW

    args = [find_ffmpeg(config), "-v", "error", "-nostdin", "-i", str(path),
            "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"]
    deadline = time.monotonic() + 900
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=_NO_WINDOW) as process:
        try:
            while True:
                try:
                    raw, stderr = process.communicate(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    if check_cancel:
                        check_cancel()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("FFmpeg 读取识别音轨超时")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"FFmpeg 无法读取语音识别音轨：{detail[-900:]}")
    return raw
