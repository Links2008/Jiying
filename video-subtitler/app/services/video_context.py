"""自动采样视频画面，供已选定的视觉模型理解语境；不新增模型或服务。"""
from __future__ import annotations

import base64
import math
from pathlib import Path
import tempfile

from ..core import cancel
from ..core.errors import AppError, Codes


def supports_images(model: str) -> bool:
    """只启用明确具有视觉能力的模型系列，未知模型仍使用字幕语境。"""
    name = model.lower().rsplit('/', 1)[-1]
    return (name.startswith(('gpt-4o', 'gpt-4.1', 'gpt-5', 'gpt-6', 'glm-4v', 'glm-4.1v', 'glm-4.5v'))
            and not any(part in name for part in ('audio', 'transcribe', 'tts'))
            or name.startswith(('qwen2.5-vl', 'qwen3-vl', 'gemini-')))


def sample_frames(video: Path, config, task_id: str | None = None) -> list[dict]:
    """最多四张 640×360 范围的 JPEG，只存内存和临时目录，支持取消。"""
    # renderer 也使用翻译语言表；延迟导入避免模块初始化的循环依赖。
    from .renderer import find_ffmpeg, probe_duration, _run
    if task_id:
        cancel.raise_if_cancelled(task_id)
    try:
        duration = (probe_duration(config, video, task_id=task_id) if task_id
                    else probe_duration(config, video))
    except (OSError, AppError) as error:
        if isinstance(error, AppError) and error.code == Codes.TASK_CANCELLED:
            raise
        return []  # 可选画面采样不可用时，字幕仍足以进入文本翻译。
    if task_id:
        cancel.raise_if_cancelled(task_id)
    if not math.isfinite(duration) or duration <= 0:
        return []
    times = sorted({round(min(duration * fraction, max(0, duration - 1)), 3)
                    for fraction in (.02, .33, .66, .95)})
    frames = []
    with tempfile.TemporaryDirectory(prefix='jiying-context-') as directory:
        for timestamp in times:
            if task_id:
                cancel.raise_if_cancelled(task_id)
            path = Path(directory) / f'{len(frames)}.jpg'
            try:
                _run([find_ffmpeg(config), '-hide_banner', '-loglevel', 'error',
                      '-ss', str(timestamp), '-i', str(video), '-an', '-frames:v', '1',
                      '-vf', 'scale=640:360:force_original_aspect_ratio=decrease',
                      '-q:v', '5', '-y', str(path)], timeout=15, task_id=task_id)
                raw = path.read_bytes()
            except (OSError, AppError) as error:
                if isinstance(error, AppError) and error.code == Codes.TASK_CANCELLED:
                    raise
                continue  # 缺失画面不阻止根据字幕理解语境。
            if raw.startswith(b'\xff\xd8') and len(raw) <= 160_000:
                frames.append({'id': len(frames) + 1, 'seconds': timestamp,
                               'url': 'data:image/jpeg;base64,' + base64.b64encode(raw).decode('ascii')})
    return frames
