"""渲染：ffmpeg 封装 —— 音频提取 / 合流 / 软字幕封装 / 硬字幕烧录 / 样式预览。

所有 ffmpeg 子进程支持任务级取消（app.core.cancel）：
注册进取消登记表，取消时被 kill，_run 在轮询中抛出 E-TASK-402。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import tempfile
import subprocess
import time
import threading
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

from ..core import cancel
from ..core.errors import AppError, Codes, dep_missing
from ..core.paths import bin_dir
from ..pipeline.resources import resource_slot
from . import cuda_overlay
from .ass_document import parse_ass
from .media_info import STREAM_ENTRIES, VideoTraits as _VideoTraits, find_ffprobe, positive_ratio, traits_from_probe
from .subtitle_filters import _ass_filter, _subtitle_filter, _write_positioned_ass
from .translator import LANG_CODES, LANG_NAMES

log = logging.getLogger(__name__)

# 打包版为 GUI 子系统（console=False），启动控制台子进程时若不带该标志，
# Windows 会给每个子进程分配一个新的控制台窗口（黑框弹窗）。预览/烧录会
# 反复调用 ffmpeg，故必须全局抑制。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

def find_ffmpeg(config) -> str:
    """定位 ffmpeg：配置路径 → bin/ 内置 → PATH。"""
    cfg_path = config.get("paths.ffmpeg", "")
    if cfg_path and Path(cfg_path).exists():
        return cfg_path
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    bundled = bin_dir() / exe
    if bundled.exists():
        return str(bundled)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise dep_missing(
        "ffmpeg", "安装 ffmpeg 并加入 PATH（或放入 bin/ 目录）", "音视频处理")


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_DURATION_PROBE_TIMEOUT = 30


def _probe_media(config, video: Path, task_id: str | None = None) -> str:
    """读取 FFmpeg 媒体信息；时长、音轨探测共用可取消和超时回收逻辑。"""
    if task_id:
        cancel.raise_if_cancelled(task_id)
    try:
        args = [find_ffmpeg(config), "-hide_banner", "-i", str(video)]
        if not task_id:
            out = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_DURATION_PROBE_TIMEOUT, creationflags=_NO_WINDOW)
            stderr = out.stderr or ""
        else:
            import tempfile
            # 与渲染共用取消登记；临时文件避免大量媒体元数据填满 stderr 管道。
            with tempfile.TemporaryFile() as output:
                proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=output,
                                        creationflags=_NO_WINDOW)
                try:
                    cancel.register(task_id, proc)
                    deadline = time.monotonic() + _DURATION_PROBE_TIMEOUT
                    while proc.poll() is None:
                        cancel.raise_if_cancelled(task_id)
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(args, _DURATION_PROBE_TIMEOUT)
                        time.sleep(.05)
                    cancel.raise_if_cancelled(task_id)
                    output.seek(0)
                    stderr = output.read().decode("utf-8", errors="replace")
                finally:
                    if proc.poll() is None:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                    try:
                        proc.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    cancel.pop(task_id, proc)
        if task_id:
            cancel.raise_if_cancelled(task_id)
        return stderr
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if task_id:
        cancel.raise_if_cancelled(task_id)
    return ""


def probe_duration(config, video: Path, task_id: str | None = None) -> float:
    """读取媒体时长；探测失败返回 0，提供 task_id 时支持取消。"""
    match = _DURATION_RE.search(_probe_media(config, video, task_id))
    if match:
        hh, mm, ss = match.groups()
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    return 0.0


_AUDIO_RE = re.compile(r"Stream #\d+:\d+(?:\[[^]]*\])?(?:\([^)]*\))?: Audio: ([A-Za-z0-9_]+)")

# mp4/mov 系列容器只接受下列音轨编码做流复制。其余（Vorbis、PCM、FLAC 等）
# 强行走 `-c:a copy` 时，ffmpeg 不会报错，而是写出一个「音轨标记与真实编码
# 不符」的容器（实测 webm/Vorbis 被标成 mp4a），播放器按 AAC 解码直接失败，
# 表现为有画面没声音 —— 比直接报错更隐蔽，故一律转 AAC 兜底。
_MP4_SAFE_AUDIO = {"aac", "mp3", "mp2", "ac3", "eac3", "alac", "opus"}


def audio_codec(config, video: Path, task_id: str | None = None) -> str:
    """源文件首条音轨编码；支持带流 ID 的 MOV/MP4 信息及任务取消。"""
    match = _AUDIO_RE.search(_probe_media(config, video, task_id))
    return match.group(1).lower() if match else ""


def audio_args(config, video: Path, out_path: Path,
               task_id: str | None = None) -> list[str]:
    """音轨参数：目标为 mp4/mov 且源音轨不兼容时转 AAC，否则无损流复制。"""
    if task_id:
        cancel.raise_if_cancelled(task_id)
    if out_path.suffix.lower() not in (".mp4", ".m4v", ".mov"):
        return ["-c:a", "copy"]
    codecs = _AUDIO_RE.findall(_probe_media(config, video, task_id))
    return _audio_output_args(codecs, video, out_path)


def _audio_output_args(codecs, video: Path, out_path: Path) -> list[str]:
    if out_path.suffix.lower() in (".mp4", ".m4v", ".mov"):
        # 软封装保留所有音轨；首条 AAC 不能代表后面的 PCM/Vorbis 也兼容。
        unsupported = [codec for codec in codecs if codec.lower() not in _MP4_SAFE_AUDIO]
        if unsupported:
            log.info("源音轨 %s 不适配 %s，改转 AAC：%s",
                     ", ".join(unsupported), out_path.suffix, video.name)
            return ["-c:a", "aac", "-b:a", "192k"]
    return ["-c:a", "copy"]


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

# 硬件编码器 → ffmpeg 参数；NVENC 使用较快预设和较低量化值保留画质。
# NVENC 只依赖显卡驱动；字幕像素由 libass 生成，可按素材选择显卡内混合。
_HW_ENCODERS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p3", "-rc", "vbr",
                   "-cq", "18", "-b:v", "0"],
    "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", "19"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "quality"],
    "av1_nvenc": ["-c:v", "av1_nvenc", "-preset", "p3", "-rc", "vbr", "-cq", "18", "-b:v", "0"],
    "av1_qsv": ["-c:v", "av1_qsv", "-global_quality", "19"],
    "hevc_nvenc": ["-c:v", "hevc_nvenc", "-preset", "p3", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                   "-profile:v", "main10", "-pix_fmt", "p010le", "-tag:v", "hvc1"],
    "hevc_qsv": ["-c:v", "hevc_qsv", "-global_quality", "19", "-profile:v", "main10",
                 "-pix_fmt", "p010le", "-tag:v", "hvc1"],
    "hevc_amf": ["-c:v", "hevc_amf", "-quality", "quality", "-profile:v", "main10",
                 "-pix_fmt", "p010le", "-tag:v", "hvc1"],
}
_CPU_ENCODER_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
_CPU_10BIT_ARGS = ["-c:v", "libx265", "-preset", "fast", "-crf", "18",
                   "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1"]
_encoder_cache: dict[tuple, bool] = {}
_usable_encoder_cache: dict[tuple, tuple[float, bool]] = {}
_encoder_lock = threading.Lock()


def _encoder_key(config, name: str) -> tuple:
    executable = find_ffmpeg(config)
    try:
        stamp = Path(executable).stat().st_mtime_ns
    except OSError:
        stamp = 0
    return executable, stamp, name


def has_encoder(config, name: str) -> bool:
    """ffmpeg 是否编译了指定编码器（每进程只探测一次）。"""
    key = _encoder_key(config, name)
    if key not in _encoder_cache:
        try:
            out = subprocess.run([find_ffmpeg(config), "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=_NO_WINDOW)
            _encoder_cache[key] = name in (out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            _encoder_cache[key] = False
    return _encoder_cache[key]


def encoder_usable(config, name: str) -> bool:
    """以一帧真实编码验证驱动；能力缓存随 FFmpeg 变化失效。"""
    if not has_encoder(config, name):
        return False
    key = _encoder_key(config, name)
    with _encoder_lock:
        cached = _usable_encoder_cache.get(key)
        if cached and time.monotonic() - cached[0] < (600 if cached[1] else 30):
            return cached[1]
        try:
            result = subprocess.run([key[0], "-v", "error", "-f", "lavfi", "-i",
                "color=c=black:s=320x180:r=30", "-frames:v", "1", "-an",
                *_HW_ENCODERS[name], "-f", "null", "-"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=8, creationflags=_NO_WINDOW)
            available = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            available = False
        _usable_encoder_cache[key] = (time.monotonic(), available)
        return available


def pick_encoder(config) -> str:
    """render.encoder 配置：cpu 固定软编；auto 按实际可用的驱动顺序择优。

    一帧能力检查不能保证资源一直可用；运行期的硬件故障会尝试其他可用编码器。
    """
    pref = str(config.get("render.encoder", "auto") or "auto").lower()
    if pref == "cpu":
        return "libx264"
    if pref != "auto":
        return pref
    for name in _HW_ENCODERS:
        if name.startswith("h264_") and encoder_usable(config, name):
            return name
    return "libx264"


def _video_traits(video: Path, config=None, task_id: str | None = None) -> _VideoTraits:
    """读取实际视频位深和基础色彩标记，避免 10 位源进入 8 位编码路径。"""
    probe = find_ffprobe(config)
    if not probe or not video.is_file():
        return _VideoTraits()
    try:
        with tempfile.TemporaryFile() as output:
            _run([probe, "-v", "error", "-show_entries", STREAM_ENTRIES, "-of", "json", "-i", str(video)],
                 timeout=_DURATION_PROBE_TIMEOUT, stdout=output, task_id=task_id)
            output.seek(0)
            return traits_from_probe(json.load(output))
    except AppError as error:
        if error.code == Codes.TASK_CANCELLED:
            raise
    except (OSError, ValueError, TypeError, AttributeError):
        if task_id:
            cancel.raise_if_cancelled(task_id)
    return _VideoTraits()


def _pick_10bit_encoder(config) -> str:
    preference = str(config.get("render.encoder", "auto") or "auto").lower()
    if preference == "cpu":
        return "libx265"
    names = (name for name in _HW_ENCODERS if name.startswith("hevc_")) if preference == "auto" else (
        preference.replace("h264_", "hevc_"),)
    return next((name for name in names if name in _HW_ENCODERS and encoder_usable(config, name)), "libx265")


def _run(args: list[str], timeout: int = 7200, task_id: str | None = None,
         on_time=None, stdout=subprocess.DEVNULL) -> None:
    """执行 ffmpeg 子进程：轮询等待以支持任务取消与超时。

    stderr 写入临时文件而非 PIPE：轮询期间无人排空管道，ffmpeg 的日志
    写满 64KB 缓冲后会把自己堵死（长视频烧录必现的死锁）。
    on_time(已处理秒数)：从 stderr 的 time= 行解析编码进度（烧录重编码
    动辄数十分钟，是唯一必须给用户看百分比的阶段）。
    """
    if task_id:
        cancel.raise_if_cancelled(task_id)
    import tempfile
    with tempfile.TemporaryFile() as errf:
        log.info('ffmpeg 命令: %s', args)
        proc = subprocess.Popen(args, stdout=stdout, stderr=errf,
                                creationflags=_NO_WINDOW)
        if task_id:
            cancel.register(task_id, proc)
        deadline = time.monotonic() + timeout
        read_pos = 0
        last_report = 0.0
        try:
            while True:
                ret = proc.poll()
                if ret is not None:
                    # 先判取消再判退出码：取消指令 kill 进程后会走到这里
                    if task_id:
                        cancel.raise_if_cancelled(task_id)
                    if ret != 0:
                        errf.seek(0)
                        err = errf.read().decode("utf-8", errors="replace")
                        raise AppError(Codes.RENDER_FAILED, "ffmpeg 处理失败",
                                           detail=err[-4096:])
                    if on_time:
                        # 流复制可能在首次轮询前完成，也要读取最后的进度。
                        errf.seek(0)
                        stderr = errf.read()
                        for m in _TIME_RE.finditer(stderr[read_pos:].decode("utf-8", errors="replace")):
                            hh, mm, ss = m.groups()
                            on_time(int(hh) * 3600 + int(mm) * 60 + float(ss))
                        if b"Invalid NAL unit size" in stderr:
                            raise AppError(Codes.RENDER_FAILED, "ffmpeg 视频数据损坏",
                                           detail=stderr[-800:].decode("utf-8", errors="replace"))
                    return
                if task_id:
                    cancel.raise_if_cancelled(task_id)
                if on_time and time.monotonic() - last_report > 0.5:
                    # 增量读 stderr，从 time=HH:MM:SS.xx 提取已编码时长
                    errf.seek(read_pos)
                    fresh = errf.read().decode("utf-8", errors="replace")
                    read_pos = errf.tell()
                    for m in _TIME_RE.finditer(fresh):
                        hh, mm, ss = m.groups()
                        last_report = time.monotonic()
                        on_time(int(hh) * 3600 + int(mm) * 60 + float(ss))
                if time.monotonic() > deadline:
                    proc.kill()
                    raise AppError(Codes.RENDER_FAILED, "ffmpeg 执行超时",
                                   detail=" ".join(args)[:500], retryable=True)
                time.sleep(0.05)
        finally:
            running = proc.poll() is None
            if running:
                try:
                    proc.kill()
                except OSError:
                    pass
            # 登记时取消可能已 kill；退出码可见也需完成取消子进程的回收。
            if running or (task_id and cancel.is_cancelled(task_id)):
                try:
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if task_id:
                cancel.pop(task_id, proc)


def _check_output_target(target: Path, inputs: list[Path]) -> None:
    """输出不得指向输入本身、符号链接或同一文件的硬链接。"""
    for source in inputs:
        same = target.resolve() == source.resolve()
        if not same:
            try:
                same = target.samefile(source)
            except OSError:
                pass
        if same:
            raise AppError(Codes.CFG_INVALID, "输出路径与输入文件相同",
                           suggestion="请选择不同的输出文件名，保留原始媒体与字幕")


@contextmanager
def _temporary_media_output(target: Path, inputs: list[Path]):
    """同目录临时文件保留容器扩展名，失败和回退均不碰已有产物。"""
    _check_output_target(target, inputs)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".jiying-render-",
                                     suffix=target.suffix, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def _publish_media_output(temporary: Path, target: Path, inputs: list[Path], task_id=None):
    if task_id:
        cancel.raise_if_cancelled(task_id)
    # 处理可能耗时很长，发布前再次核对目标是否被改成输入的别名。
    _check_output_target(target, inputs)
    temporary.replace(target)


def extract_audio(config, video: Path, out_wav: Path,
                  task_id: str | None = None) -> Path:
    """提取 16kHz 单声道 WAV；成功后替换输出，失败保留原文件。"""
    with _temporary_media_output(out_wav, [video]) as temporary:
        _run([find_ffmpeg(config), "-y", "-i", str(video),
              "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(temporary)],
             task_id=task_id)
        _publish_media_output(temporary, out_wav, [video], task_id)
    return out_wav


def _video_packet_duration(video: Path, task_id: str | None = None, config=None) -> float:
    if task_id:
        cancel.raise_if_cancelled(task_id)
    probe = find_ffprobe(config)
    if not probe:
        raise dep_missing("ffprobe", "安装与 FFmpeg 同版本的 ffprobe", "媒体完整性检查")
    try:
        traits = _video_traits(video, config, task_id)
        interval = 1 / traits.fps if traits.fps > 0 else 0
        start, end = float("inf"), float("-inf")
        with tempfile.TemporaryFile() as output:
            _run([probe, "-v", "error", "-select_streams", "v:0", "-show_packets",
                  "-show_entries", "packet=pts_time,dts_time,duration_time,size,flags",
                  "-of", "compact=p=0:nk=0", "-i", str(video)], task_id=task_id, stdout=output)
            output.seek(0)
            for index, line in enumerate(output):
                if task_id and index % 512 == 0:
                    cancel.raise_if_cancelled(task_id)
                fields = dict(part.split(b"=", 1) for part in line.strip().split(b"|") if b"=" in part)
                if b"C" in fields.get(b"flags", b""):
                    raise ValueError("媒体包含损坏标记")
                if int(fields.get(b"size", b"0")) <= 0:
                    continue
                timestamp = fields.get(b"pts_time")
                if timestamp in (None, b"N/A"):
                    timestamp = fields.get(b"dts_time")
                if timestamp in (None, b"N/A"):
                    continue
                pts = float(timestamp)
                duration = fields.get(b"duration_time")
                packet_duration = float(duration) if duration not in (None, b"N/A") else 0
                if not math.isfinite(pts) or not math.isfinite(packet_duration):
                    raise ValueError("媒体包含非有限时间戳")
                start = min(start, pts)
                end = max(end, pts + (packet_duration if packet_duration > 0 else interval))
    except (OSError, ValueError, AppError) as error:
        if task_id:
            cancel.raise_if_cancelled(task_id)
        if isinstance(error, AppError) and error.code == Codes.TASK_CANCELLED:
            raise
        raise AppError(Codes.ENG_FAILED, "下载文件不完整，视频数据无法读取",
                       retryable=True) from error
    if task_id:
        cancel.raise_if_cancelled(task_id)
    return max(0.0, end - start)


def _packet_span(rows, task_id: str | None) -> float | None:
    """从 framecrc 读取视频包的 PTS 与时长。异常时间轴交给原探测路径。"""
    scale = None
    ambiguous = False
    start, end = float("inf"), float("-inf")
    for index, raw in enumerate(rows):
        if task_id and index % 512 == 0:
            cancel.raise_if_cancelled(task_id)
        if raw.startswith(b"#tb 0:"):
            try:
                num, den = raw.split()[-1].split(b"/")
                scale = int(num) / int(den)
            except (ValueError, ZeroDivisionError):
                return None
        if not raw.startswith(b"0,"):
            continue
        try:
            _, _, pts, duration, size, *details = raw.split(b",")
            pts, duration, size = int(pts), int(duration), int(size)
            flags = next((int(field.strip()[2:], 16) for field in details
                          if field.strip().startswith(b"F=")), 0)
        except ValueError:
            return None
        if flags & 2:  # AV_PKT_FLAG_CORRUPT 由解复用器登记。
            raise AppError(Codes.RENDER_FAILED, "ffmpeg 视频数据损坏", retryable=True)
        if not size:
            continue
        if not scale or duration <= 0 or abs(pts) >= 1 << 62:
            ambiguous = True
            continue
        start = min(start, pts * scale)
        end = max(end, (pts + duration) * scale)
    if task_id:
        cancel.raise_if_cancelled(task_id)
    return None if ambiguous else max(0.0, end - start)


def _scan_video_packets(config, video: Path, task_id: str | None = None) -> float:
    ffmpeg = find_ffmpeg(config)
    with tempfile.TemporaryFile(mode="w+b") as packets:
        # 以流复制扫描真实数据，一次保留每包 PTS、时长及损坏标记。
        _run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-copyts",
              "-i", str(video), "-map", "0:v:0", "-c:v", "copy",
              "-avoid_negative_ts", "disabled", "-f", "framecrc", "-"],
             task_id=task_id, stdout=packets, on_time=lambda _: None)
        packets.seek(0)
        duration = _packet_span(packets, task_id)
    return duration if duration is not None else _video_packet_duration(video, task_id, config)


def _check_video_duration(config, video: Path, expected: float,
                          task_id: str | None = None) -> None:
    if not math.isfinite(expected) or expected < 0:
        raise AppError(Codes.ENG_FAILED, "来源视频时长无效", suggestion="刷新链接后重新下载")

    # 只扫描视频包，音频尾部和容器声明的总时长不能证明画面完整。
    effective = _scan_video_packets(config, video, task_id)
    tolerance = min(1.0, max(.25, expected * .02))
    if effective <= 0 or (expected > 0 and abs(expected - effective) > tolerance):
        raise AppError(Codes.ENG_FAILED, "音视频时长不一致，下载文件不完整",
                       detail=f"预期 {expected:.2f} 秒，实际视频 {effective:.2f} 秒",
                       retryable=True,
                       suggestion="刷新画质后重试，并检查源站是否有试看限制或账号权限要求")


def mux_av(config, video: Path, audio: Path, out_path: Path,
           task_id: str | None = None, expected_duration: float = 0) -> Path:
    """合并分离音视频流；兼容音轨无损复制，只有容器不兼容时才转码。

    容器与音轨必须匹配：拆流下载的视频流常为 .webm（VP8/VP9/AV1），而 WebM
    只接受 Vorbis/Opus 音轨 —— 沿用原扩展名再写 AAC 会被 ffmpeg 直接拒绝
    （实测报错：Only VP8 or VP9 or AV1 video and Vorbis or Opus audio and
    WebVTT subtitles are supported for WebM.），使 aria2c 拆流加速整条路径
    失败降级。故先尝试 .mp4，并按实际音轨选择流复制或 AAC；视频流无法复制
    进 mp4 时退回 .webm，按音轨选择复制或 Opus。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ff = find_ffmpeg(config)
    if not math.isfinite(expected_duration) or expected_duration < 0:
        raise AppError(Codes.ENG_FAILED, "来源视频时长无效", suggestion="刷新链接后重新下载")
    if task_id:
        expected = max(probe_duration(config, video, task_id=task_id),
                       probe_duration(config, audio, task_id=task_id))
    else:
        expected = max(probe_duration(config, video), probe_duration(config, audio))
    expected = max(expected, expected_duration)
    source_audio = audio_codec(config, audio, task_id) if audio.is_file() else ""

    def _mux(target: Path, acodec: str) -> None:
        _run([ff, "-y", "-i", str(video), "-i", str(audio),
              "-map", "0:v:0", "-map", "1:a:0",
              "-c:v", "copy", "-c:a", acodec, *([] if acodec == "copy" else ["-b:a", "192k"]),
              str(target)], task_id=task_id)

    inputs = [video, audio]
    mp4_audio = "copy" if source_audio in _MP4_SAFE_AUDIO else "aac"
    webm_audio = "copy" if source_audio in {"opus", "vorbis"} else "libopus"
    for index, (target, codec) in enumerate(((out_path.with_suffix(".mp4"), mp4_audio),
                                            (out_path.with_suffix(".webm"), webm_audio))):
        with _temporary_media_output(target, inputs) as temporary:
            try:
                _mux(temporary, codec)
            except AppError as error:
                if index or error.code in (Codes.TASK_CANCELLED, Codes.CFG_INVALID):
                    raise
                log.warning("合流 .mp4/AAC 失败，改用 .webm/Opus 重试：%s", error)
                continue

            if expected > 0:
                # 完整性检查也在临时产物上完成，不能先覆盖旧文件再发现视频截断。
                _check_video_duration(config, temporary, expected, task_id)
            _publish_media_output(temporary, target, inputs, task_id)
            return target


def validate_download(config, video: Path, expected_duration: float = 0,
                      task_id: str | None = None) -> Path:
    """完成前检查实际画面和容器，单文件也统一封装为真正的 MP4。"""
    if task_id:
        cancel.raise_if_cancelled(task_id)
    metadata = _probe_media(config, video, task_id) if video.is_file() and video.stat().st_size else ""
    container = re.search(r"Input #0, (.+?), from", metadata)
    if not container or not re.search(r"Stream #.*: Video:", metadata):
        raise AppError(Codes.ENG_FAILED, "下载文件不完整，媒体文件为空或无法读取",
                       retryable=True, suggestion="重新下载此视频")
    duration = float(expected_duration or 0)
    if not math.isfinite(duration) or duration < 0:
        raise AppError(Codes.ENG_FAILED, "来源视频时长无效", suggestion="刷新链接后重新下载")
    if duration <= 0 and (match := _DURATION_RE.search(metadata)):
        hh, mm, ss = match.groups()
        duration = int(hh) * 3600 + int(mm) * 60 + float(ss)
    _check_video_duration(config, video, duration, task_id)
    if video.suffix.lower() == ".mp4" and "mp4" in container.group(1).split(","):
        return video

    # 独立目录避免以错误扩展名下载的来源被原地覆盖；发布时保留正常文件名。
    target = video.parent / "mp4" / video.with_suffix(".mp4").name
    target.parent.mkdir(parents=True, exist_ok=True)
    with _temporary_media_output(target, [video]) as temporary:
        _run([find_ffmpeg(config), "-y", "-i", str(video), "-map", "0:v:0", "-map", "0:a:0?",
              "-c:v", "copy", *audio_args(config, video, target, task_id), str(temporary)],
             task_id=task_id)
        _check_video_duration(config, temporary, duration, task_id)
        _publish_media_output(temporary, target, [video], task_id)
    return target


def softmux(config, video: Path, subs: list[tuple[Path, str]], out_path: Path,
            task_id: str | None = None) -> Path:
    """软字幕封装：流复制不重编码，秒级完成、画质无损。

    subs 为 [(字幕文件, 语言代码)] 列表 —— 全部语言各封装一条轨，
    播放器里可直接切换字幕语言。

    产物路径以返回值为准：MP4 装不下（VP8 视频无法复制、mov_text 仅 MP4/MOV
    支持、音轨不兼容）时自动回退 .mkv/SRT，保证「一定有可播放产物」。
    """
    from .subtitler import render_srt
    out_path.parent.mkdir(parents=True, exist_ok=True)
    styled = any(path.suffix.casefold() == ".ass" for path, _ in subs)

    def _mux(target: Path, sub_codec: str, tracks: list[tuple[Path, str]]) -> None:
        args = [find_ffmpeg(config), "-y", "-i", str(video)]
        for srt, _lang in tracks:
            args += ["-i", str(srt)]
        args += ["-map", "0:v", "-map", "0:a?"]
        for i in range(len(tracks)):
            args += ["-map", f"{i + 1}:s"]
        audio = (audio_args(config, video, target, task_id)
                 if task_id else audio_args(config, video, target))
        args += ["-c:v", "copy", *audio,
                 "-c:s", sub_codec]
        for i, (subtitle, lang) in enumerate(tracks):
            ass = subtitle.suffix.casefold() == ".ass"
            if ass:
                args += [f"-c:s:{i}", "copy"]
            args += [f"-metadata:s:s:{i}",
                     f"language={LANG_CODES.get(lang.split('+')[0], lang.split('+')[0])}",
                     f"-metadata:s:s:{i}",
                     "title=" + ("人名注释 · " if ass else "")
                     + " / ".join(LANG_NAMES.get(part, part) for part in lang.split("+")),
                     f"-disposition:s:{i}", "default" if i == 0 else "0"]
        if styled:
            media = bin_dir()
            font = (media.parent if media.name == "bin" else media) / "fonts" / "NotoSansCJKsc-Regular.otf"
            if font.is_file():
                args += ["-attach", str(font), "-metadata:s:t:0", "mimetype=application/vnd.ms-opentype"]
        args.append(str(target))
        _run(args, task_id=task_id)

    inputs = [video, *(path for path, _ in subs)]
    with ExitStack() as stack:
        tracks = []
        for path, lang in subs:
            if path.suffix.casefold() == ".ass":
                try:
                    parse_ass(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as error:
                    raise AppError(Codes.CFG_INVALID, "人名注释 ASS 无法封装", detail=str(error)) from error
            else:
                path = stack.enter_context(render_srt(path))
            tracks.append((path, lang))
        formats = ([(out_path.with_suffix(".mkv"), "srt")] if styled else
                   [(out_path, "mov_text"), (out_path.with_suffix(".mkv"), "srt")])
        for index, (target, codec) in enumerate(formats):
            with _temporary_media_output(target, inputs) as temporary:
                try:
                    _mux(temporary, codec, tracks)
                except AppError as error:
                    if styled or index or error.code in (Codes.TASK_CANCELLED, Codes.CFG_INVALID):
                        raise
                    log.warning("软封装 %s 失败，改用 .mkv/SRT 重试：%s", out_path.suffix, error)
                    continue
                _publish_media_output(temporary, target, inputs, task_id)
                return target


def burn(config, video: Path, srt: Path, out_path: Path,
          height: int | None = None, font_name: str = "", font_size: int = 0,
          position: str = "bottom", outline: int = 1, task_id: str | None = None,
          on_progress=None, color: str = "#FFFFFF",
          vertical_offset: int = 0, horizontal_offset: int = 0, on_encoding=None,
          video_traits: _VideoTraits | None = None) -> Path:
    """硬字幕烧录：需重编码；height 顺带分辨率转码；字体/字号/位置覆盖样式。

    on_progress(fraction 0~1)：按 ffmpeg 已编码时长 / 视频总时长换算。
    编码器自动择优（NVENC/QSV/AMF → libx264），验证实际可用性。
    驱动或会话故障尝试下一种可用编码器；输入错误和取消不触发重复烧录。
    音频：输出固定为 .mp4，源音轨不兼容时自动转 AAC（否则流复制），
    避免出现「有画面没声音」的坏产物。
    """
    from .subtitler import read_srt
    if task_id:
        cancel.raise_if_cancelled(task_id)
    styled_ass = srt.suffix.casefold() == ".ass"
    _check_output_target(out_path, [video, srt])
    segments = None if styled_ass else read_srt(srt)
    traits = video_traits or _video_traits(video, config, task_id)
    if traits.depth > 10:
        raise AppError(Codes.RENDER_FAILED, "此视频位深高于当前硬烧录支持范围",
                       suggestion="可使用内嵌软字幕保留原始画质")
    ten_bit = traits.depth > 8
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = traits.duration
    if on_progress and duration <= 0:
        duration = (probe_duration(config, video, task_id=task_id)
                    if task_id else probe_duration(config, video))
    size = int(font_size) or int(config.get("render.font_size", 18))
    name = font_name or str(config.get("render.font_name", "") or "Noto Sans CJK SC")
    # 临时 ASS 独占目录，避免覆盖同名用户文件；参数校验失败也必须清理。
    with ExitStack() as resources:
        inputs = [video, srt]
        temporary = resources.enter_context(_temporary_media_output(out_path, inputs))
        source = srt
        if not styled_ass and (vertical_offset or horizontal_offset):
            directory = resources.enter_context(tempfile.TemporaryDirectory(
                prefix="jiying-burn-", dir=out_path.parent))
            source = _write_positioned_ass(srt, Path(directory) / "captions.ass",
                                           position, vertical_offset, horizontal_offset, segments=segments)
        vf = (_ass_filter(source, name) if styled_ass else
              _subtitle_filter(source, font_name=name, font_size=size, position=position,
                               outline=outline, color=color))
        layer_filter = vf
        if height:
            vf += f",scale=-2:{height}"

        audio = (_audio_output_args(traits.audio_codecs, video, out_path)
                 if traits.audio_codecs is not None else audio_args(config, video, out_path, task_id))
        ffmpeg = find_ffmpeg(config)
        timing = ["-fps_mode:v", "passthrough"]
        if traits.time_base:
            timing += ["-enc_time_base:v", traits.time_base]

        layer = stream = None

        def _args(encoder: str, cuda: bool) -> list[str]:
            codec = (_HW_ENCODERS[encoder] if encoder in _HW_ENCODERS
                     else _CPU_10BIT_ARGS if ten_bit else _CPU_ENCODER_ARGS)
            if not ten_bit and encoder.startswith("hevc_") and encoder in _HW_ENCODERS:
                codec = [arg for arg in codec if arg not in {"-pix_fmt", "p010le"}]
                codec[codec.index("main10")] = "main"
            if stream:
                return [ffmpeg, "-y", "-xerror", *stream.input_args(), "-i", str(video),
                        "-filter_complex_threads", "2", "-/filter_complex", str(stream.graph_file),
                        "-map", "[v]", "-map", "0:a?", *timing, *codec, *traits.colors, *audio, str(temporary)]
            if layer:
                return [ffmpeg, "-y", "-xerror", "-init_hw_device", "cuda=compose:0", "-filter_hw_device", "compose",
                        "-hwaccel", "cuda", "-hwaccel_device", "compose", "-hwaccel_output_format", "cuda",
                        "-i", str(video), "-i", str(layer.mask), "-filter_complex", layer.graph,
                        "-map", "[v]", "-map", "0:a?", *timing, *codec, *traits.colors, *audio, str(temporary)]
            decode = ["-xerror", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] if cuda else []
            filtergraph = f"hwdownload,format={traits.cuda_format},{vf}" if cuda else vf
            return [ffmpeg, "-y", *decode, "-i", str(video),
                    "-vf", filtergraph, *timing, *codec, *traits.colors, *audio, str(temporary)]

        encoder, attempted = _pick_10bit_encoder(config) if ten_bit else pick_encoder(config), set()
        cuda = bool(traits.cuda_format and encoder in {"h264_nvenc", "hevc_nvenc"})
        mode = config.get("render.gpu_overlay", "auto")

        def legacy_layer():
            if encoder != "h264_nvenc" or not cuda_overlay.preferred(traits, mode):
                return None
            try:
                if on_encoding:
                    on_encoding("准备字幕透明图层")
                overlay_dir = resources.enter_context(tempfile.TemporaryDirectory(prefix="jiying-layer-", dir=out_path.parent))
                with (resource_slot("cpu", task_id) if task_id else nullcontext()):
                    return cuda_overlay.prepare(ffmpeg, source, segments, layer_filter,
                                                traits, height, Path(overlay_dir), _run, task_id)
            except (AppError, OSError, ValueError) as error:
                if isinstance(error, AppError) and error.code == Codes.TASK_CANCELLED:
                    raise
                log.info("透明图层不可用，保留传统字幕滤镜：%s", type(error).__name__)
                return None

        auto_dual_zone = str(mode).casefold() == "auto" and cuda_overlay.auto_dual_zone_preferred(
            source, encoder, traits, height)
        if cuda_overlay.stream_preferred(encoder, traits, mode, auto_dual_zone=auto_dual_zone) and (
                not encoder.startswith("av1_") or encoder_usable(config, encoder)):
            try:
                graph_dir = resources.enter_context(tempfile.TemporaryDirectory(
                    prefix="jiying-stream-", dir=out_path.parent))
                stream = cuda_overlay.stream_plan(source, segments, layer_filter, traits, height,
                                                  Path(graph_dir), position, name)
                if auto_dual_zone and stream and not stream.dual_local:
                    stream = None
            except (OSError, ValueError) as error:
                log.info("流式显存合成图不可用：%s", type(error).__name__)
        if stream is None:
            layer = legacy_layer()
        last_progress = 0.0

        def report(seconds: float) -> None:
            nonlocal last_progress
            last_progress = max(last_progress, min(seconds / duration, 1.0))
            on_progress(last_progress)

        while True:
            attempted.add(encoder)
            hardware = encoder in _HW_ENCODERS
            mode = ({"nvenc": "NVIDIA NVENC", "qsv": "Intel QSV", "amf": "AMD AMF"}[encoder.split("_")[-1]]
                    if hardware else "CPU 软件编码")
            decode_mode = ("单进程 CUDA 解码与显存合成" if stream else
                           "CUDA 解码与显存合成" if layer else "CUDA 解码" if cuda else "软件解码")
            detail = f"{mode} · {decode_mode}" if hardware else mode
            if on_encoding:
                on_encoding("等待 " + detail)
            try:
                slot = resource_slot("gpu" if hardware else "cpu", task_id) if task_id else nullcontext()
                with slot:
                    if on_encoding:
                        on_encoding(detail)
                    _run(_args(encoder, cuda), task_id=task_id,
                         on_time=report if on_progress and duration > 0 else None)
                    if output_layer := stream or layer:
                        result = _video_traits(temporary, config, task_id)
                        if (result.width, result.height, result.depth) != (
                                output_layer.width, output_layer.height, traits.depth):
                            raise AppError(Codes.RENDER_FAILED, "显卡输出尺寸或位深与目标不符")
                        if stream and (result.rotation or (
                                traits.audio_codecs is not None and result.audio_codecs is not None
                                and len(result.audio_codecs) != len(traits.audio_codecs)) or not stream.bounds_valid()):
                            raise AppError(Codes.RENDER_FAILED, "流式烧录的方向、音轨或字幕边界与目标不符")
                break
            except AppError as error:
                if error.code == Codes.TASK_CANCELLED or (task_id and cancel.is_cancelled(task_id)):
                    raise
                if stream:
                    detail = (error.detail or error.message).strip().splitlines()
                    log.info("流式显存合成失败，回退兼容路径：%s", " | ".join(detail[:4])[:700])
                    stream = None
                    layer = legacy_layer()
                    continue
                if layer:
                    layer = None
                    log.info("显存合成失败，改用传统字幕混合")
                    continue
                if cuda and _hardware_decode_failure(error):
                    cuda = False
                    log.info("硬件解码不可用，改用软件解码并继续显卡编码")
                    continue
                if encoder not in _HW_ENCODERS or not _hardware_failure(error):
                    raise
                _usable_encoder_cache[_encoder_key(config, encoder)] = (time.monotonic(), False)
                previous = encoder
                encoder = next((name for name in _HW_ENCODERS if name not in attempted
                    and name.startswith("hevc_" if ten_bit else "h264_")
                    and str(config.get("render.encoder", "auto") or "auto").lower() == "auto"
                    and encoder_usable(config, name)), "libx265" if ten_bit else "libx264")
                cuda = False
                log.warning("硬件编码 %s 不可用，改用 %s：%s", previous, encoder, error.message)
        _publish_media_output(temporary, out_path, inputs, task_id)
    return out_path


def _hardware_failure(error: AppError) -> bool:
    detail = (error.message + " " + error.detail).lower()
    return any(word in detail for word in (
        "openencodesession", "no nvenc capable", "cannot load nvcuda", "cannot load libcuda",
        "driver does not support", "unsupported device", "error while opening encoder",
        "error initializing output stream", "device creation failed", "mfx session", "amf failed"))


def _hardware_decode_failure(error: AppError) -> bool:
    detail = (error.message + " " + error.detail).lower()
    return any(word in detail for word in (
        "no device available for decoder", "hardware device setup failed", "failed setup for format cuda",
        "device creation failed", "cannot load nvcuda", "cannot load libcuda",
        "hwdownload", "cuvid", "nvdec", "error transferring the data",
        "hardware does not support", "failed to get hw surface"))


def render_preview_png(config, text: str, font_name: str, font_size: int,
                        position: str, out_png: Path, outline: int = 1,
                        color: str = "#FFFFFF", vertical_offset: int = 0,
                        horizontal_offset: int = 0, *, person_notes: bool = False) -> Path:
    """渲染字幕样式预览帧：深色 16:9 背景 + 样例文本按所选样式烧录。

    不依赖任何视频文件，样式参数实时可见（所见即所得的烧录效果）。
    """
    out_png.parent.mkdir(parents=True, exist_ok=True)
    # 预览接口会直接读取缓存 PNG。先完成临时图再替换，避免失败或并发时读到半张图。
    with tempfile.TemporaryDirectory(prefix="jiying-preview-", dir=out_png.parent) as directory:
        temporary = Path(directory)
        srt = temporary / "captions.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:09,000\n" + text + "\n",
            encoding="utf-8")
        size = int(font_size) or int(config.get("render.font_size", 18))
        name = font_name or str(config.get("render.font_name", "") or "Noto Sans CJK SC")
        if person_notes:
            from .ass_composer import compose_person_notes
            from .subtitler import read_srt
            sample = {"source": "Lucy Morgan", "target": "露西·摩根", "sense": "节目嘉宾",
                      "kind": "person", "origin": "screen", "start": 0., "end": 4.}
            source = compose_person_notes(read_srt(srt), [sample], temporary / "preview.ass",
                font_name=name, font_size=size, position=position, color=color, outline=outline,
                vertical_offset=vertical_offset, horizontal_offset=horizontal_offset)
            vf = _ass_filter(source, name)
        else:
            source = (_write_positioned_ass(srt, temporary / "captions.ass", position,
                                            vertical_offset, horizontal_offset)
                      if vertical_offset or horizontal_offset else srt)
            vf = _subtitle_filter(source, font_name=name, font_size=size, position=position,
                                  outline=outline, color=color)
        image = temporary / "preview.png"
        _run([find_ffmpeg(config), "-y", "-loglevel", "error",
              "-f", "lavfi", "-i", "color=c=0x232a3a:s=640x360:d=10",
              "-vf", vf, *(["-ss", "0.8"] if person_notes else []), "-frames:v", "1", str(image)])
        image.replace(out_png)
    return out_png
