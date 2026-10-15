"""渲染：ffmpeg 封装 —— 音频提取 / 合流 / 软字幕封装 / 硬字幕烧录 / 样式预览。

所有 ffmpeg 子进程支持任务级取消（app.core.cancel）：
注册进取消登记表，取消时被 kill，_run 在轮询中抛出 E-TASK-402。
"""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import tempfile
import subprocess
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..core import cancel
from ..core.errors import AppError, Codes, dep_missing
from ..core.paths import bin_dir
from .translator import LANG_CODES, LANG_NAMES

log = logging.getLogger(__name__)

# 打包版为 GUI 子系统（console=False），启动控制台子进程时若不带该标志，
# Windows 会给每个子进程分配一个新的控制台窗口（黑框弹窗）。预览/烧录会
# 反复调用 ffmpeg，故必须全局抑制。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 字幕位置 → Alignment。注意：ffmpeg+libass 对 SRT force_style 的编号与
# ASS 标准不一致，以下为逐值实测的经验映射（2=底部居中 6=顶部居中 10=中部居中）
POSITIONS = {"bottom": 2, "middle": 10, "top": 6}

# ffmpeg 路径转义：单引号需穿透两层解析（filtergraph 层 + subtitles 滤镜层）。
# 引号内 \ 不是转义符、' 必收尾，故用「收尾引号 + 转义反斜杠 + 转义引号
# + 重开引号」写法：第一层吐出 \' 、第二层还原为字面 '。否则标题含撇号
# （如 Don't Look Up）时滤镜解析失败 → 烧录零产物。
_QUOTE_ESC = "'" + r"\\" + r"\'" + "'"


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
    if out_path.suffix.lower() in (".mp4", ".m4v", ".mov"):
        # 软封装保留所有音轨；首条 AAC 不能代表后面的 PCM/Vorbis 也兼容。
        codecs = _AUDIO_RE.findall(_probe_media(config, video, task_id))
        unsupported = [codec for codec in codecs if codec.lower() not in _MP4_SAFE_AUDIO]
        if unsupported:
            log.info("源音轨 %s 不适配 %s，改转 AAC：%s",
                     ", ".join(unsupported), out_path.suffix, video.name)
            return ["-c:a", "aac", "-b:a", "192k"]
    return ["-c:a", "copy"]


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

# 硬件编码器 → ffmpeg 参数（VBR 质量优先；p4 是 NVENC 质量/速度平衡档）。
# NVENC 只依赖显卡驱动（不需要 CUDA 运行库），RTX 系列烧录 1080p 可达数倍实时速度，
# 且几乎不占 CPU —— 桌面常驻应用的关键减负点
_HW_ENCODERS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
                   "-cq", "19", "-b:v", "0"],
    "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", "19"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "quality"],
}
_CPU_ENCODER_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
_encoder_cache: dict[str, bool] = {}


def has_encoder(config, name: str) -> bool:
    """ffmpeg 是否编译了指定编码器（每进程只探测一次）。"""
    if name not in _encoder_cache:
        try:
            out = subprocess.run([find_ffmpeg(config), "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=_NO_WINDOW)
            _encoder_cache[name] = name in (out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            _encoder_cache[name] = False
    return _encoder_cache[name]


def pick_encoder(config) -> str:
    """render.encoder 配置：cpu 固定软编；auto 按构建/驱动顺序择优。

    注意 auto 只代表「优先尝试」，驱动层面不可用时 burn() 会在运行期
    自动回退 libx264（编码器列表里有 ≠ 当前机器真能用）。
    """
    pref = str(config.get("render.encoder", "auto") or "auto").lower()
    if pref == "cpu":
        return "libx264"
    if pref != "auto":
        return pref
    for name in _HW_ENCODERS:
        if has_encoder(config, name):
            return name
    return "libx264"


def _run(args: list[str], timeout: int = 7200, task_id: str | None = None,
         on_time=None) -> None:
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
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errf,
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
                                       detail=err[-800:])
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
                time.sleep(0.2)
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


def _filter_path(path: Path) -> str:
    """穿过两层滤镜语法转义路径，保留 POSIX 文件名里的字面反斜杠。"""
    return path.as_posix().replace("\\", r"\\").replace(":", r"\:").replace("'", _QUOTE_ESC)


def _subtitle_filter(srt_path: Path, font_name: str = "", font_size: int = 18,
                     position: str = "bottom", outline: int = 1,
                     color: str = "#FFFFFF") -> str:
    """subtitles 滤镜参数：Windows 路径需转义盘符冒号、反斜杠与单引号。

    outline：黑色描边粗细（0 = 无描边；ASS 里越大越粗）。
    """
    p = _filter_path(srt_path)
    align = POSITIONS.get(position, 2)
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise AppError(Codes.CFG_INVALID, "字幕颜色必须是 #RRGGBB 格式")
    rgb = color[1:].upper()
    ass_color = f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"
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
    font_option = ""
    if font_name == "Noto Sans CJK SC":
        font_dir = bin_dir().parent / "fonts"
        font_file = font_dir / "NotoSansCJKsc-Regular.otf"
        if not font_file.is_file():
            raise AppError(Codes.DEP_MISSING, "内置字幕字体 Noto Sans CJK SC 缺失",
                           detail=str(font_file))
        font_path = _filter_path(font_dir)
        font_option = f":fontsdir='{font_path}'"
    return f"subtitles='{p}'{font_option}:force_style='{style}'"


def _write_positioned_ass(srt: Path, target: Path, position: str,
                          vertical_offset: int, horizontal_offset: int = 0) -> Path:
    """自定义偏移采用 ASS；中部对齐时 SRT 的 force_style 会忽略 MarginV。"""
    from .subtitler import _merge_overlapping_cues, read_srt
    from .transcriber import Segment

    align, base_y = {"bottom": (2, 324), "middle": (5, 180),
                     "top": (8, 36)}.get(position, (2, 324))
    y = max(18, min(342, base_y + max(-120, min(120, int(vertical_offset)))))
    x = 320 + max(-270, min(270, int(horizontal_offset)))

    def ass_time(cs: int) -> str:
        hours, rem = divmod(cs, 360000)
        minutes, rem = divmod(rem, 6000)
        secs, centis = divmod(rem, 100)
        return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"

    header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n"
              "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour,"
              " SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline,"
              " StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
              " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              "Style: Default,Arial,18,&H00FFFFFF,&H00FFFFFF,&H00000000,"
              "&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,20,20,36,1\n"
              "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL,"
              " MarginR, MarginV, Effect, Text\n")
    # ASS 文件只有百分之一秒精度。向外取整才能覆盖原字幕的毫秒时间窗，
    # 起止分别四舍五入会让极短的原文/译文变成零时长而消失。
    cues = []
    for seg in read_srt(srt):
        start_cs = max(0, round(seg.start * 1000)) // 10
        end_cs = (round(seg.end * 1000) + 9) // 10
        cues.append(Segment(start_cs / 100, end_cs / 100, seg.text))
    events = []
    # 精度转换后可能出现新的重叠；固定坐标会让多条文字画在同一位置，必须合并。
    for seg in _merge_overlapping_cues(cues):
        # 花括号是字幕原文，不能改成圆括号；反斜杠后插入空样式块，防止
        # Windows 路径中的 \n 或字面量 \N / \h 被 libass 解释为排版命令。
        escaped = {"{": r"\{", "}": r"\}", "\\": r"\{}", "\n": r"\N"}
        safe_text = "".join(escaped.get(char, char) for char in seg.text)
        events.append(f"Dialogue: 0,{ass_time(round(seg.start * 100))},{ass_time(round(seg.end * 100))},"
                      f"Default,,0,0,0,,{{\\an{align}\\pos({x},{y})}}{safe_text}\n")
    target.write_text(header + "".join(events), encoding="utf-8")
    return target


def _video_packet_duration(video: Path, task_id: str | None = None) -> float:
    if task_id:
        cancel.raise_if_cancelled(task_id)
    import av
    start, end = float("inf"), float("-inf")
    try:
        with av.open(str(video)) as container:
            stream = container.streams.video[0]
            for packet in container.demux(stream):
                if task_id:
                    cancel.raise_if_cancelled(task_id)
                if not packet.size:
                    continue
                timestamp = packet.pts if packet.pts is not None else packet.dts
                time_base = packet.time_base or stream.time_base
                if timestamp is None or time_base is None:
                    continue
                # PTS 是画面播放时间；B 帧重排后的解码进度会把低帧率视频误判为截断。
                seconds = float(timestamp * time_base)
                duration = float((packet.duration or 0) * time_base)
                if not duration and stream.average_rate:
                    duration = 1 / float(stream.average_rate)
                start = min(start, seconds)
                end = max(end, seconds + duration)
    except (OSError, ValueError, IndexError, av.error.FFmpegError) as error:
        if task_id:
            cancel.raise_if_cancelled(task_id)
        raise AppError(Codes.ENG_FAILED, "下载文件不完整，视频数据无法读取",
                       retryable=True) from error
    if task_id:
        cancel.raise_if_cancelled(task_id)
    return max(0.0, end - start)


def _check_video_duration(config, video: Path, expected: float,
                          task_id: str | None = None) -> None:
    if not math.isfinite(expected) or expected < 0:
        raise AppError(Codes.ENG_FAILED, "来源视频时长无效", suggestion="刷新链接后重新下载")

    # 只扫描视频包，音频尾部和容器声明的总时长不能证明画面完整。
    _run([find_ffmpeg(config), "-i", str(video), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
         task_id=task_id, on_time=lambda _: None)
    effective = _video_packet_duration(video, task_id)
    tolerance = min(1.0, max(.25, expected * .02))
    if effective <= 0 or (expected > 0 and abs(expected - effective) > tolerance):
        raise AppError(Codes.ENG_FAILED, "音视频时长不一致，下载文件不完整",
                       detail=f"预期 {expected:.2f} 秒，实际视频 {effective:.2f} 秒",
                       retryable=True,
                       suggestion="刷新画质后重试，并检查源站是否有试看限制或账号权限要求")


def mux_av(config, video: Path, audio: Path, out_path: Path,
           task_id: str | None = None, expected_duration: float = 0) -> Path:
    """合并 aria2c 并行下载的分离音视频流（视频流复制，音频转 AAC 保证兼容）。

    容器与音轨必须匹配：拆流下载的视频流常为 .webm（VP8/VP9/AV1），而 WebM
    只接受 Vorbis/Opus 音轨 —— 沿用原扩展名再写 AAC 会被 ffmpeg 直接拒绝
    （实测报错：Only VP8 or VP9 or AV1 video and Vorbis or Opus audio and
    WebVTT subtitles are supported for WebM.），使 aria2c 拆流加速整条路径
    失败降级。故统一改写到兼容性最好的 .mp4/AAC；仅当视频流无法复制进 mp4
    时才退回与源容器匹配的 .webm/Opus。
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

    def _mux(target: Path, acodec: str) -> None:
        _run([ff, "-y", "-i", str(video), "-i", str(audio),
              "-map", "0:v:0", "-map", "1:a:0",
              "-c:v", "copy", "-c:a", acodec, "-b:a", "192k",
              str(target)], task_id=task_id)

    inputs = [video, audio]
    for index, (target, codec) in enumerate(((out_path.with_suffix(".mp4"), "aac"),
                                            (out_path.with_suffix(".webm"), "libopus"))):
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
        for i, (_srt, lang) in enumerate(tracks):
            args += [f"-metadata:s:s:{i}",
                     f"language={LANG_CODES.get(lang.split('+')[0], lang.split('+')[0])}",
                     f"-metadata:s:s:{i}",
                     "title=" + " / ".join(LANG_NAMES.get(part, part) for part in lang.split("+")),
                     f"-disposition:s:{i}", "default" if i == 0 else "0"]
        args.append(str(target))
        _run(args, task_id=task_id)

    inputs = [video, *(path for path, _ in subs)]
    with ExitStack() as stack:
        tracks = [(stack.enter_context(render_srt(path)), lang) for path, lang in subs]
        for index, (target, codec) in enumerate(((out_path, "mov_text"),
                                                (out_path.with_suffix(".mkv"), "srt"))):
            with _temporary_media_output(target, inputs) as temporary:
                try:
                    _mux(temporary, codec, tracks)
                except AppError as error:
                    if index or error.code in (Codes.TASK_CANCELLED, Codes.CFG_INVALID):
                        raise
                    log.warning("软封装 %s 失败，改用 .mkv/SRT 重试：%s", out_path.suffix, error)
                    continue
                _publish_media_output(temporary, target, inputs, task_id)
                return target


def burn(config, video: Path, srt: Path, out_path: Path,
          height: int | None = None, font_name: str = "", font_size: int = 0,
          position: str = "bottom", outline: int = 1, task_id: str | None = None,
          on_progress=None, color: str = "#FFFFFF",
          vertical_offset: int = 0, horizontal_offset: int = 0) -> Path:
    """硬字幕烧录：需重编码；height 顺带分辨率转码；字体/字号/位置覆盖样式。

    on_progress(fraction 0~1)：按 ffmpeg 已编码时长 / 视频总时长换算。
    编码器自动择优（NVENC/QSV/AMF → libx264）：硬件编码只在「编码器已编译」
    时尝试，驱动/会话层面失败（如独显被禁用、会话数占满）自动回退软编一次。
    音频：输出固定为 .mp4，源音轨不兼容时自动转 AAC（否则流复制），
    避免出现「有画面没声音」的坏产物。
    """
    from .subtitler import read_srt
    read_srt(srt)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = 0.0
    if on_progress:
        duration = (probe_duration(config, video, task_id=task_id)
                    if task_id else probe_duration(config, video))
    size = int(font_size) or int(config.get("render.font_size", 18))
    name = font_name or str(config.get("render.font_name", "") or "Noto Sans CJK SC")
    # 临时 ASS 独占目录，避免覆盖同名用户文件；参数校验失败也必须清理。
    with ExitStack() as resources:
        inputs = [video, srt]
        temporary = resources.enter_context(_temporary_media_output(out_path, inputs))
        source = srt
        if vertical_offset or horizontal_offset:
            directory = resources.enter_context(tempfile.TemporaryDirectory(
                prefix="jiying-burn-", dir=out_path.parent))
            source = _write_positioned_ass(srt, Path(directory) / "captions.ass",
                                           position, vertical_offset, horizontal_offset)
        vf = _subtitle_filter(source, font_name=name, font_size=size, position=position,
                              outline=outline, color=color)
        if height:
            vf += f",scale=-2:{height}"

        def _args(encoder: str) -> list[str]:
            codec = (_HW_ENCODERS[encoder] if encoder in _HW_ENCODERS
                     else _CPU_ENCODER_ARGS)
            audio = (audio_args(config, video, out_path, task_id)
                     if task_id else audio_args(config, video, out_path))
            return [find_ffmpeg(config), "-y", "-i", str(video),
                    "-vf", vf, *codec, *audio, str(temporary)]

        encoder = pick_encoder(config)
        try:
            _run(_args(encoder), task_id=task_id,
                 on_time=(lambda sec: on_progress(min(sec / duration, 1.0)))
                 if on_progress and duration > 0 else None)
        except AppError as e:
            # 硬件编码运行期失败（驱动/会话问题）→ 软编兜底；软编失败不回退
            if e.code == Codes.TASK_CANCELLED or (task_id and cancel.is_cancelled(task_id)) or encoder not in _HW_ENCODERS:
                raise
            log.warning("硬件编码 %s 失败，回退 libx264：%s", encoder, e.message)
            _run(_args("libx264"), task_id=task_id,
                 on_time=(lambda sec: on_progress(min(sec / duration, 1.0)))
                 if on_progress and duration > 0 else None)
        _publish_media_output(temporary, out_path, inputs, task_id)
    return out_path


def render_preview_png(config, text: str, font_name: str, font_size: int,
                        position: str, out_png: Path, outline: int = 1,
                        color: str = "#FFFFFF", vertical_offset: int = 0,
                        horizontal_offset: int = 0) -> Path:
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
        source = (_write_positioned_ass(srt, temporary / "captions.ass", position,
                                        vertical_offset, horizontal_offset)
                  if vertical_offset or horizontal_offset else srt)
        vf = _subtitle_filter(source, font_name=name, font_size=size, position=position,
                              outline=outline, color=color)
        image = temporary / "preview.png"
        _run([find_ffmpeg(config), "-y", "-loglevel", "error",
              "-f", "lavfi", "-i", "color=c=0x232a3a:s=640x360:d=10",
              "-vf", vf, "-frames:v", "1", str(image)])
        image.replace(out_png)
    return out_png
