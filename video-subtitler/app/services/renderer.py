"""渲染：ffmpeg 封装 —— 音频提取 / 合流 / 软字幕封装 / 硬字幕烧录 / 样式预览。

所有 ffmpeg 子进程支持任务级取消（app.core.cancel）：
注册进取消登记表，取消时被 kill，_run 在轮询中抛出 E-TASK-402。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
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


def probe_duration(config, video: Path) -> float:
    """视频总时长（秒）：解析 `ffmpeg -i` 的 Duration 行。

    刻意不依赖 ffprobe（单它就要 100MB 发布体积）；解析失败返回 0，
    进度显示退化为未知，不影响任务本身。
    """
    try:
        out = subprocess.run(
            [find_ffmpeg(config), "-hide_banner", "-i", str(video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=_NO_WINDOW)
        m = _DURATION_RE.search(out.stderr or "")
        if m:
            hh, mm, ss = m.groups()
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return 0.0


_AUDIO_RE = re.compile(r"Stream #\d+:\d+(?:\([^)]*\))?: Audio: ([A-Za-z0-9_]+)")

# mp4/mov 系列容器只接受下列音轨编码做流复制。其余（Vorbis、PCM、FLAC 等）
# 强行走 `-c:a copy` 时，ffmpeg 不会报错，而是写出一个「音轨标记与真实编码
# 不符」的容器（实测 webm/Vorbis 被标成 mp4a），播放器按 AAC 解码直接失败，
# 表现为有画面没声音 —— 比直接报错更隐蔽，故一律转 AAC 兜底。
_MP4_SAFE_AUDIO = {"aac", "mp3", "mp2", "ac3", "eac3", "alac", "opus"}


def audio_codec(config, video: Path) -> str:
    """源文件首条音轨的编码器名（解析 `ffmpeg -i`，不引入 ffprobe）。

    解析失败或文件本身无音轨时返回空串，调用方据此退化为流复制。
    """
    try:
        out = subprocess.run(
            [find_ffmpeg(config), "-hide_banner", "-i", str(video)],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW)
        m = _AUDIO_RE.search(out.stderr or "")
        return m.group(1).lower() if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def audio_args(config, video: Path, out_path: Path) -> list[str]:
    """音轨参数：目标为 mp4/mov 且源音轨不兼容时转 AAC，否则无损流复制。"""
    if out_path.suffix.lower() in (".mp4", ".m4v", ".mov"):
        codec = audio_codec(config, video)
        if codec and codec not in _MP4_SAFE_AUDIO:
            log.info("源音轨 %s 不适配 %s，改转 AAC：%s",
                     codec, out_path.suffix, video.name)
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


def extract_audio(config, video: Path, out_wav: Path,
                  task_id: str | None = None) -> Path:
    """提取 16kHz 单声道 wav —— Whisper 的最佳输入格式。"""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _run([find_ffmpeg(config), "-y", "-i", str(video),
          "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out_wav)],
         task_id=task_id)
    return out_wav


def _subtitle_filter(srt_path: Path, font_name: str = "", font_size: int = 18,
                     position: str = "bottom", outline: int = 1,
                     color: str = "#FFFFFF") -> str:
    """subtitles 滤镜参数：Windows 路径需转义盘符冒号、反斜杠与单引号。

    outline：黑色描边粗细（0 = 无描边；ASS 里越大越粗）。
    """
    p = str(srt_path).replace("\\", "/").replace(":", r"\:")
    p = p.replace("'", _QUOTE_ESC)
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
        font_path = str(font_dir).replace("\\", "/").replace(":", r"\:")
        font_path = font_path.replace("'", _QUOTE_ESC)
        font_option = f":fontsdir='{font_path}'"
    return f"subtitles='{p}'{font_option}:force_style='{style}'"


def _write_positioned_ass(srt: Path, target: Path, position: str,
                          vertical_offset: int, horizontal_offset: int = 0) -> Path:
    """Only custom offsets need ASS: SRT force_style ignores MarginV at middle alignment."""
    from .subtitler import parse_srt

    align, base_y = {"bottom": (2, 324), "middle": (5, 180),
                     "top": (8, 36)}.get(position, (2, 324))
    y = max(18, min(342, base_y + max(-120, min(120, int(vertical_offset)))))
    x = 320 + max(-270, min(270, int(horizontal_offset)))

    def ass_time(seconds: float) -> str:
        cs = max(0, round(seconds * 100))
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
    events = []
    for seg in parse_srt(srt.read_text(encoding="utf-8-sig", errors="replace")):
        safe_text = seg.text.replace("{", "(").replace("}", ")").replace("\n", r"\N")
        events.append(f"Dialogue: 0,{ass_time(seg.start)},{ass_time(seg.end)},"
                      f"Default,,0,0,0,,{{\\an{align}\\pos({x},{y})}}{safe_text}\n")
    target.write_text(header + "".join(events), encoding="utf-8")
    return target


def mux_av(config, video: Path, audio: Path, out_path: Path,
           task_id: str | None = None) -> Path:
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

    def _mux(target: Path, acodec: str) -> None:
        _run([ff, "-y", "-i", str(video), "-i", str(audio),
              "-map", "0:v:0", "-map", "1:a:0",
              "-c:v", "copy", "-c:a", acodec, "-b:a", "192k",
              str(target)], task_id=task_id)

    mp4 = out_path.with_suffix(".mp4")
    target = mp4
    try:
        _mux(mp4, "aac")
    except AppError as e:
        if e.code == Codes.TASK_CANCELLED:  # 取消不算失败，不得降级重试
            raise
        log.warning("合流 .mp4/AAC 失败，改用 .webm/Opus 重试：%s", e)
        target = out_path.with_suffix(".webm")
        _mux(target, "libopus")

    expected = max(probe_duration(config, video), probe_duration(config, audio))
    if expected > 0:
        effective_video = 0.0

        def _video_time(seconds: float) -> None:
            nonlocal effective_video
            effective_video = max(effective_video, seconds)

        # 分片容器的头部或长音轨可能掩盖截断视频；只扫描实际视频包，不解码。
        _run([ff, "-i", str(target), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
             task_id=task_id, on_time=_video_time)
        if abs(expected - effective_video) > max(5.0, expected * .02):
            raise AppError(
                Codes.ENG_FAILED, "音视频时长不一致，下载文件不完整",
                detail=f"预期 {expected:.2f} 秒，实际视频 {effective_video:.2f} 秒",
                retryable=True,
                suggestion="刷新画质后重试，并检查源站是否有试看限制或账号权限要求")
    return target


def softmux(config, video: Path, subs: list[tuple[Path, str]], out_path: Path,
            task_id: str | None = None) -> Path:
    """软字幕封装：流复制不重编码，秒级完成、画质无损。

    subs 为 [(字幕文件, 语言代码)] 列表 —— 全部语言各封装一条轨，
    播放器里可直接切换字幕语言。

    产物路径以返回值为准：MP4 装不下（VP8 视频无法复制、mov_text 仅 MP4/MOV
    支持、音轨不兼容）时自动回退 .mkv/SRT，保证「一定有可播放产物」。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _mux(target: Path, sub_codec: str) -> None:
        args = [find_ffmpeg(config), "-y", "-i", str(video)]
        for srt, _lang in subs:
            args += ["-i", str(srt)]
        args += ["-map", "0:v", "-map", "0:a?"]
        for i in range(len(subs)):
            args += ["-map", f"{i + 1}:s"]
        args += ["-c:v", "copy", *audio_args(config, video, target),
                 "-c:s", sub_codec]
        for i, (_srt, lang) in enumerate(subs):
            args += [f"-metadata:s:s:{i}",
                     f"language={LANG_CODES.get(lang.split('+')[0], lang.split('+')[0])}",
                     f"-metadata:s:s:{i}",
                     "title=" + " / ".join(LANG_NAMES.get(part, part) for part in lang.split("+")),
                     f"-disposition:s:{i}", "default" if i == 0 else "0"]
        args.append(str(target))
        _run(args, task_id=task_id)

    try:
        _mux(out_path, "mov_text")
        return out_path
    except AppError as e:
        if e.code == Codes.TASK_CANCELLED:  # 取消不算失败，不得降级重试
            raise
        log.warning("软封装 %s 失败，改用 .mkv/SRT 重试：%s",
                    out_path.suffix, e)
    mkv = out_path.with_suffix(".mkv")
    _mux(mkv, "srt")
    return mkv


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
    out_path.parent.mkdir(parents=True, exist_ok=True)
    size = int(font_size) or int(config.get("render.font_size", 18))
    name = font_name or str(config.get("render.font_name", "") or "Noto Sans CJK SC")
    positioned = out_path.with_suffix(".position.ass") if vertical_offset or horizontal_offset else None
    source = (_write_positioned_ass(srt, positioned, position, vertical_offset,
                                    horizontal_offset) if positioned else srt)
    vf = _subtitle_filter(source, font_name=name, font_size=size, position=position,
                          outline=outline, color=color)
    if height:
        vf += f",scale=-2:{height}"
    duration = probe_duration(config, video) if on_progress else 0.0

    def _args(encoder: str) -> list[str]:
        codec = (_HW_ENCODERS[encoder] if encoder in _HW_ENCODERS
                 else _CPU_ENCODER_ARGS)
        return [find_ffmpeg(config), "-y", "-i", str(video),
                "-vf", vf, *codec, *audio_args(config, video, out_path),
                str(out_path)]

    encoder = pick_encoder(config)
    try:
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
    finally:
        if positioned:
            positioned.unlink(missing_ok=True)
    return out_path


def render_preview_png(config, text: str, font_name: str, font_size: int,
                        position: str, out_png: Path, outline: int = 1,
                        color: str = "#FFFFFF", vertical_offset: int = 0,
                        horizontal_offset: int = 0) -> Path:
    """渲染字幕样式预览帧：深色 16:9 背景 + 样例文本按所选样式烧录。

    不依赖任何视频文件，样式参数实时可见（所见即所得的烧录效果）。
    """
    out_png.parent.mkdir(parents=True, exist_ok=True)
    srt = out_png.with_suffix(".srt")
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:09,000\n" + text.replace("\n", "\\N") + "\n",
        encoding="utf-8")
    size = int(font_size) or int(config.get("render.font_size", 18))
    name = font_name or str(config.get("render.font_name", "") or "Noto Sans CJK SC")
    source = (_write_positioned_ass(srt, out_png.with_suffix(".ass"), position,
                                    vertical_offset, horizontal_offset)
              if vertical_offset or horizontal_offset else srt)
    vf = _subtitle_filter(source, font_name=name, font_size=size, position=position,
                          outline=outline, color=color)
    _run([find_ffmpeg(config), "-y", "-loglevel", "error",
          "-f", "lavfi", "-i", "color=c=0x232a3a:s=640x360:d=10",
          "-vf", vf, "-frames:v", "1", str(out_png)])
    return out_png
