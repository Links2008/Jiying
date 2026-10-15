"""yt-dlp 引擎：主引擎，内置上千站点提取器。

外部下载器说明：yt-dlp 对 aria2c 是一等支持，启用 external_downloader 时
框架会自动向 aria2c 透传 UA/Referer/cookies/代理等凭据，无需手工拼 header。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ...core import cancel as task_cancel
from ...core.errors import AppError, Codes
from ...core.paths import bin_dir
from ..downloader.aria2c import Aria2cBackend
from ..renderer import find_ffmpeg
from ..ytdlp_updater import runtime_session
from .base import DirectTarget, EngineProvider, FormatInfo, ProbeResult, has_download_bytes
from .platform_links import is_douyin_url, normalize_media_url

log = logging.getLogger(__name__)


def _is_corrupt_media_error(error: AppError) -> bool:
    return (error.code == Codes.RENDER_FAILED and (
        error.message == "ffmpeg 视频数据损坏" or "Invalid NAL unit size" in error.detail
    )) or (error.code == Codes.ENG_FAILED and
           error.message == "音视频时长不一致，下载文件不完整")


def _quarantine_corrupt_split(dest_dir: Path, files: list[Path], merged: Path) -> None:
    # 已确认媒体损坏时续传会复用完整坏文件；仅隔离本次来源，保留诊断证据。
    # 放在任务目录旁，避免 has_download_bytes 把隔离文件当作可续传残片。
    candidates = set(files) | {Path(str(p) + ".aria2") for p in files}
    candidates |= {merged.with_suffix(".mp4"), merged.with_suffix(".webm")}
    sources = [p for p in candidates if p.parent == dest_dir and p.is_file()]
    if not sources:
        return
    invalid = dest_dir.parent / f"{dest_dir.name}.invalid-{time.time_ns()}"
    try:
        invalid.mkdir()
        for source in sources:
            source.replace(invalid / source.name)
    except OSError as e:
        raise AppError(Codes.DISK_FAILED, "损坏下载文件无法隔离",
                       suggestion="删除此失败任务后重新下载；检查临时目录写入权限") from e
    log.warning("已隔离损坏下载文件，重试将重新获取来源：%s", invalid.name)


def _quarantine_failed_split(dest_dir: Path, resolution: str, error: str) -> None:
    # 旧版失败记录仍在阶段 checkpoint 中；仅迁移与选定格式同标题的拆流来源。
    record = re.match(r"^\[([^]]+)\] (.*)", error, re.S)
    if not record or not _is_corrupt_media_error(AppError(
            record[1], record[2].partition("（")[0], detail=record[2])) or not dest_dir.exists():
        return
    matched = [(p, re.fullmatch(r"(.+)\.f(\d+)\.(mp4|m4a|webm)", p.name, re.I))
               for p in dest_dir.iterdir() if p.is_file()]
    selected = {m[1] for p, m in matched if m and p.suffix.lower() != ".m4a"
                and (not resolution.isdecimal() or m[2] == resolution)}
    for stem in selected:
        files = [p for p, m in matched if m and m[1] == stem]
        merged = dest_dir / re.sub(r"\.f\d+", "", files[0].name)
        _quarantine_corrupt_split(dest_dir, files, merged)


def _display_thumbnail(info: dict) -> str:
    """Prefer the highest quality thumbnail WPF can decode without a codec."""
    for item in reversed(info.get("thumbnails") or []):
        url = str(item.get("url") or "")
        if url.lower().split("?", 1)[0].endswith((".jpg", ".jpeg", ".png")):
            return url
    return str(info.get("thumbnail") or "")


def _import_ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError as e:
        raise AppError(
            code=Codes.DEP_MISSING,
            message="主引擎 yt-dlp 未安装",
            detail=str(e),
            suggestion="请在设置中更新下载引擎；仍不可用时重新安装完整即影安装包",
        ) from e


def _is_bilibili_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in ("bilibili.com", "b23.tv") or host.endswith(".bilibili.com")


def _ytdlp_error(message: str, stage: str, url: str = "") -> AppError:
    low = message.lower()
    if stage == '下载' and 'write to closed file' in low:
        return AppError(Codes.NET_FAILED, '视频分片下载中断，请重试', detail=message[:1000],
                        retryable=True, suggestion='已下载的分片保留；网络恢复后重新下载')
    from .xiaohongshu_public import is_xhs_url
    if is_xhs_url(url) and '图文笔记' in message:
        return AppError(Codes.ENG_UNSUPPORTED, '这是小红书图文笔记', suggestion='请复制视频笔记的完整分享链接')
    if is_xhs_url(url) and ('no video formats' in low or '网页未返回视频地址' in message):
        return AppError(Codes.AUTH_NEED_COOKIES, '小红书网页尚未提供视频地址',
                        suggestion='在解析页点击“连接小红书”，打开目标视频后刷新；链接失效时请重新复制完整分享链接')
    if is_douyin_url(url) and ("fresh cookies" in low or "分享页未返回" in message):
        return AppError(Codes.ENG_FAILED, "抖音未返回可下载的视频详情",
                        detail=message[:1000], retryable=True,
                        suggestion="确认目标视频仍可公开访问，再重新复制视频分享链接；稍后重试")
    if any(word in low for word in ("cookie", "login", "log in", "sign in", "decrypt", "dpapi")):
        return AppError(Codes.AUTH_NEED_COOKIES, "登录态未能读取或已失效，请重新连接登录态",
                        detail=message[:1000],
                        suggestion=("B 站请在解析页点击“连接 B 站”扫码登录，成功后自动刷新画质；浏览器或 Cookie 文件可作为备用连接方式"
                                    if _is_bilibili_url(url) else
                                    "此链接需要对应网站的登录信息；可在高级选项选择已登录该网站的浏览器或有效 Cookie 文件"))
    if "requested format" in low and "not available" in low:
        return AppError(Codes.ENG_FAILED, "所选画质已不可用，请刷新画质后重新选择",
                        detail=message[:1000], retryable=True,
                        suggestion="返回解析页刷新画质；高画质视频请先检查登录态和账号权限")
    return AppError(Codes.ENG_FAILED, f"{stage}失败：{message[:200]}",
                    detail=message[:1000], retryable=True,
                    suggestion="检查网络和代理后重试；持续失败可在设置中更新下载引擎")


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


class YtDlpEngine(EngineProvider):
    name = "ytdlp"

    def __init__(self, config):
        self._cfg = config

    def _common_opts(self) -> dict:
        """公共下载参数：重试 + cookies + 代理（按网络模式决定）。

        cookies 双通道：cookies_from_browser（直接复用浏览器登录态）优先，
        否则用 cookies（Netscape cookies.txt 路径）。登录信息是可选配置；
        公开内容先按网站实际返回的画质下载，权限限制由网站判断。
        """
        opts: dict = {"retries": 3, "socket_timeout": 30}
        browser = str(self._cfg.get("cookies_from_browser", "") or "").strip()
        if browser:
            opts["cookiesfrombrowser"] = _parse_browser(browser)
        else:
            cookies = str(self._cfg.get("cookies", "") or "").strip()
            if cookies:
                cookie_path = Path(os.path.expandvars(cookies)).expanduser()
                if cookie_path.is_file():
                    opts["cookiefile"] = str(cookie_path)
                else:
                    log.warning("已配置 Cookie 文件不可用，将尝试当前网站的公开内容")
        mode = self._cfg.get("download.proxy_mode", "system")
        if mode == "direct":
            opts["proxy"] = ""  # 显式置空 = 强制直连，绕过系统/环境代理
        elif mode == "custom" and self._cfg.get("download.custom_proxy", ""):
            opts["proxy"] = self._cfg.get("download.custom_proxy")
        elif self._cfg.get("proxy", ""):
            opts["proxy"] = self._cfg.get("proxy")  # 兼容旧配置字段
        return opts

    @contextmanager
    def _youtube_dl(self, opts: dict):
        """读取凭据失败时，在任何解析或传输之前仅尝试一次匿名初始化。"""
        yt_dlp = _import_ytdlp()
        if not (opts.get("cookiefile") or opts.get("cookiesfrombrowser")):
            with yt_dlp.YoutubeDL(opts) as ydl:
                yield ydl
            return
        credential_error = getattr(getattr(yt_dlp, "cookies", None),
                                   "CookieLoadError", yt_dlp.utils.DownloadError)
        try:
            ydl = yt_dlp.YoutubeDL(opts)
            if opts.get("cookiefile") or opts.get("cookiesfrombrowser"):
                getattr(ydl, "cookiejar", None)  # 强制加载；失败必须发生在媒体传输之前。
        except (yt_dlp.utils.DownloadError, credential_error):
            if not (opts.get("cookiefile") or opts.get("cookiesfrombrowser")):
                raise
            log.warning("无法读取已配置登录态，将尝试当前网站的公开内容")
            anonymous = dict(opts)
            anonymous.pop("cookiefile", None)
            anonymous.pop("cookiesfrombrowser", None)
            ydl = yt_dlp.YoutubeDL(anonymous)
        with ydl:
            yield ydl

    # ---- 探测：列出可选分辨率 ----
    @runtime_session()
    def probe(self, url: str) -> ProbeResult | None:
        url = normalize_media_url(url)
        yt_dlp = _import_ytdlp()
        opts = self._common_opts() | {
            "skip_download": True, "quiet": True, "no_warnings": True, "noprogress": True,
        }
        info = self._head_direct_file(url, "best")
        bilibili = _is_bilibili_url(url)
        auth_status = "unknown" if self._cfg.get("cookies_from_browser") or self._cfg.get("cookies") else "none"
        try:
            if info is None:
                with self._youtube_dl(opts) as ydl:
                    info = self._extract_info(ydl, url, download=False)
                    if bilibili and auth_status != "none":
                        jar = getattr(ydl, "cookiejar", None)
                        if jar and any(c.name == "SESSDATA" and c.value
                                       for c in jar.get_cookies_for_url("https://api.bilibili.com")):
                            auth_status = "loaded"
        except yt_dlp.utils.DownloadError as e:  # type: ignore[attr-defined]
            msg = str(e)
            low = msg.lower()
            if "unsupported url" in low or "no suitable" in low:
                return None  # 非本引擎职责 → 路由器换下一个引擎
            raise _ytdlp_error(msg, "链接解析", url) from e

        # 多视频帖子（X/微博等一条链接含多个视频）：extract_info 返回播放列表，
        # 顶层没有 formats —— 不取首个含流条目的话，分辨率列表为空、下载阶段
        # 还会因顶层无 requested_downloads 报“下载完成但未找到产物文件”
        entries = [e for e in (info or {}).get("entries") or [] if isinstance(e, dict)]
        if entries and not info.get("formats"):
            first = next((e for e in entries if e.get("formats")), None)
            if first is None:
                return None  # 全部条目无可用流 → 路由器换下一个引擎
            merged = dict(first)
            for k in ("thumbnail", "duration", "webpage_url"):
                if not merged.get(k) and info.get(k):
                    merged[k] = info[k]
            if len(entries) > 1:
                merged["title"] = (f'{merged.get("title") or info.get("title") or url}'
                                   f'（共 {len(entries)} 个视频）')
            info = merged

        # 同高度的不同帧率/HDR 保留独立选项；同一变体优先提取器画质和码率。
        # 与实际 ba / 拆流选择保持一致；只有所有必需流已知时才给出整片体积。
        best_audio = _best_audio(info.get("formats") or [])
        original_formats: list[FormatInfo] = []
        from .xiaohongshu_public import is_xhs_url
        xhs = is_xhs_url(url)
        variants: dict[tuple, FormatInfo] = {}
        scores: dict[tuple, tuple] = {}
        for f in info.get("formats") or []:
            h = f.get("height")
            if xhs and f.get("format_id") == "direct" and f.get("url"):
                raw, estimated = _format_size(f, best_audio)
                original_formats.append(FormatInfo(
                    format_id="direct", label="原始视频" if h else "原始视频 · 分辨率待确认",
                    height=h or 0, fps=f.get("fps") or 0.0, ext=f.get("ext", "mp4"),
                    filesize=raw, size_is_estimate=estimated,
                    vcodec=f.get("vcodec") or "", acodec=f.get("acodec") or "",
                    protocol=f.get("protocol", "https")))
                continue
            if not h or not f.get("vcodec") or f.get("vcodec") == "none":
                continue
            raw, estimated = _format_size(f, best_audio)
            fps = float(f.get("fps") or 0.0)
            dynamic_range = str(f.get("dynamic_range") or "SDR")
            key = (h, fps, dynamic_range)
            score = (float(f.get("quality") or 0), float(f.get("tbr") or 0),
                     raw, f.get("ext") == "mp4")
            if key not in variants or score > scores[key]:
                scores[key] = score
                width = int(f.get("width") or 0)
                quality = min(width, h) if width else h
                label = "4K / 2160p" if quality == 2160 else f"{quality}p"
                if width and width < h:
                    label += f" · {width}×{h}"
                variants[key] = FormatInfo(
                    format_id=str(f.get("format_id")), label=label,
                    height=h, fps=fps, ext=f.get("ext", ""),
                    filesize=raw, vcodec=f.get("vcodec", ""), acodec=f.get("acodec", ""),
                    protocol=f.get("protocol", "https"),
                    dynamic_range=dynamic_range,
                    size_is_estimate=estimated,
                )
        formats = original_formats + sorted(variants.values(), key=lambda x: (-x.height, -x.fps, x.dynamic_range != "SDR"))
        if not formats:
            # 直链等无高度信息的格式：保留为「源画质」条目，避免列表为空
            best = max(info.get("formats") or [],
                       key=lambda f: f.get("filesize") or f.get("filesize_approx") or 0,
                       default=None)
            if best:
                raw, estimated = _format_size(best, best_audio)
                formats.append(FormatInfo(
                    format_id=str(best.get("format_id")), label="source",
                    height=0, fps=best.get("fps") or 0.0, ext=best.get("ext", ""),
                    filesize=raw, size_is_estimate=estimated,
                    vcodec=best.get("vcodec", ""), acodec=best.get("acodec", ""),
                    protocol=best.get("protocol", "https")))
        quality_notice = ""
        if bilibili:
            highest = max((f.height for f in formats), default=0)
            if auth_status == "unknown":
                quality_notice = "未读取到哔哩哔哩登录态，仍可直接下载已列出的公开画质。如需账号画质，可点击“连接 B 站”扫码登录，成功后自动刷新画质；浏览器或 Cookie 文件可作为备用连接方式。"
            elif auth_status == "none" and highest < 2160:
                quality_notice = "当前未登录，可直接下载已列出的公开画质。如原视频支持 4K，可点击“连接 B 站”扫码登录，成功后自动刷新画质；浏览器或 Cookie 文件可作为备用连接方式。"
            if highest < 2160:
                quality_notice += f" 当前最高可下载画质为 {highest}p。4K 是否可下载还取决于原视频和账号权限。" if highest else " 当前未返回可确认的分辨率。"
            elif not quality_notice:
                quality_notice = "已获取 4K 画质，可直接选择下载。"
        return ProbeResult(
            video_id=str(info.get("id") or ""),
            title=info.get("title") or url,
            duration=float(info.get("duration") or 0),
            webpage_url=info.get("webpage_url") or url,
            thumbnail=_display_thumbnail(info),
            formats=formats,
            quality_notice=quality_notice.strip(), auth_status=auth_status,
        )

    # ---- 下载 ----
    @runtime_session()
    def download(self, url: str, resolution: str, dest_dir: Path,
                 on_progress=None, task_id: str | None = None) -> list[Path]:
        url = normalize_media_url(url)
        # 取消检查器：传入 aria2c 轮询循环与 yt-dlp 进度钩子
        cancel_check = (lambda: task_cancel.is_cancelled(task_id)) if task_id else None
        if task_id:
            task_cancel.raise_if_cancelled(task_id)
        direct_info = self._head_direct_file(url, resolution)

        bilibili = _is_bilibili_url(url)
        if bilibili:
            split = self._resolve_split_streams(url, resolution)
            if task_id:
                task_cancel.raise_if_cancelled(task_id)
            if split:
                from ..downloader.native_ranges import download_ranges
                from ..renderer import mux_av
                yt_dlp = _import_ytdlp()
                opts = self._common_opts() | {"quiet": True, "no_warnings": True}
                rate = self._cfg.get("download.rate_limit", "")
                if rate:
                    opts["ratelimit"] = _parse_rate(rate)
                last_fraction = 0.0
                def native_progress(progress):
                    nonlocal last_fraction
                    progress.fraction = max(last_fraction, min(progress.fraction, 0.99))
                    last_fraction = progress.fraction
                    if on_progress:
                        on_progress(progress)
                log.info("B站原生分段并行：%s", [t.filename for t in split])
                try:
                    with self._youtube_dl(opts) as ydl:
                        files = [download_ranges(ydl, target, dest_dir, native_progress,
                                 task_id=task_id, concurrency=int(self._cfg.get(
                                     "download.fragments_concurrency", 16))) for target in split]
                except yt_dlp.utils.DownloadError as error:
                    if task_id:
                        task_cancel.raise_if_cancelled(task_id)
                    raise _ytdlp_error(str(error), "分段下载", url) from error
                merged = dest_dir / re.sub(r"\.f\d+", "", split[0].filename)
                try:
                    return [mux_av(self._cfg, files[0], files[1], merged, task_id=task_id,
                                   expected_duration=split[0].duration)]
                except AppError as error:
                    if _is_corrupt_media_error(error):
                        _quarantine_corrupt_split(dest_dir, files, merged)
                    raise

        # 直链加速路径（架构 v3）：启用 aria2c 时按「拆流并行 → 单一直达流 → 原生」
        # 三级尝试；未开始传输时才降级，避免重复下载已传输的数据。
        transferred = False
        def track_progress(progress):
            nonlocal transferred
            transferred |= progress.fraction > 0
            if on_progress:
                on_progress(progress)

        if not bilibili and self._cfg.get("download.aria2c.enabled", False):
            backend = Aria2cBackend(self._cfg)
            if backend.ping():
                # 优先：DASH 拆流，视频+音频双 Job 并行（各 16 连接）
                split = None if direct_info else self._resolve_split_streams(url, resolution)
                if task_id:
                    task_cancel.raise_if_cancelled(task_id)
                if split:
                    log.info("aria2c 拆流并行：%s", [t.filename for t in split])
                    try:
                        files = backend.download_many(split, dest_dir, track_progress,
                                                      cancel_check=cancel_check)
                        transferred = True  # 已返回来源文件，合并失败不得改引擎自动重下。
                        from ..renderer import mux_av  # 局部导入避免环
                        merged = dest_dir / re.sub(r"\.f\d+", "", split[0].filename)
                        try:
                            return [mux_av(self._cfg, files[0], files[1], merged,
                                           task_id=task_id, expected_duration=split[0].duration)]
                        except AppError as error:
                            if _is_corrupt_media_error(error):
                                _quarantine_corrupt_split(dest_dir, files, merged)
                            raise
                    except AppError as e:
                        if e.code == Codes.TASK_CANCELLED:
                            raise
                        if task_id:
                            task_cancel.raise_if_cancelled(task_id)
                        if transferred or has_download_bytes(dest_dir):
                            raise  # 已传输时不再从头下载，保留原错误供手动重试
                        log.warning("aria2c 拆流路径失败，降级尝试：%s", e)
                # 次选：单一直达流（generic 直链）
                target = (self._target_from(direct_info["formats"][0], direct_info)
                          if direct_info else self._resolve_direct_stream(url, resolution))
                if task_id:
                    task_cancel.raise_if_cancelled(task_id)
                if target:
                    log.info("aria2c 直链加速：%s", target.filename)
                    try:
                        output = backend.download(target, dest_dir, track_progress,
                                                  cancel_check=cancel_check)
                        from ..renderer import validate_download
                        return [validate_download(self._cfg, output, target.duration, task_id)]
                    except AppError as e:
                        if e.code == Codes.TASK_CANCELLED:
                            raise
                        if task_id:
                            task_cancel.raise_if_cancelled(task_id)
                        if transferred or has_download_bytes(dest_dir):
                            raise
                        log.warning("aria2c 路径失败，降级 yt-dlp 原生下载：%s", e)
            else:
                log.warning("aria2c RPC 不可达，降级 yt-dlp 原生下载")

        if task_id:
            task_cancel.raise_if_cancelled(task_id)
        yt_dlp = _import_ytdlp()
        dest_dir.mkdir(parents=True, exist_ok=True)
        state = {"done_before": 0}  # 已完成流的累计体积（进度聚合用）
        opts = self._common_opts() | {
            # 原生 HTTP 下载器遇到不支持 Range 的服务器会清空残片并从头重试。
            # 分片重试和 aria2c 续传各自独立，此处只禁用整文件自动重下。
            "retries": 0,
            "skip_unavailable_fragments": False,
            "fragment_retries": 3,
            # playlist_index 条件后缀：多视频帖子的条目自动加 [N] 避免同名覆盖；
            # 单视频无该字段，输出名与之前完全一致
            "outtmpl": {"default": str(dest_dir / "%(title).100s%(playlist_index& [{}]|)s.%(ext)s")},
            "playlist_items": "1",  # 探测只展示首个视频的画质，下载也只取首个
            "merge_output_format": "mp4",
            "ffmpeg_location": find_ffmpeg(self._cfg),
            "noprogress": True,
            "progress_hooks": [self._wrap_aggregate(state)(
                self._make_hook(on_progress, task_id, state))],
            "postprocessor_hooks": [lambda _: task_cancel.raise_if_cancelled(task_id)]
            if task_id else [],
        }
        # 套接字超时：连接卡死时 yt-dlp 会抛错退出，取消才能在最近的异常点生效；
        # 否则进度钩子不再触发，任务将永远停在 running 状态无法取消
        opts.setdefault("socket_timeout", 30)
        if bilibili:
            opts["http_chunk_size"] = 10 * 1024 * 1024
        # 分片流并发（YouTube/B站主力加速手段，纯参数级、零风险）
        opts["concurrent_fragment_downloads"] = int(
            self._cfg.get("download.fragments_concurrency", 8))
        rate = self._cfg.get("download.rate_limit", "")
        if rate:
            opts["ratelimit"] = _parse_rate(rate)

        # 分辨率 → format 字符串（音视频流交给 ffmpeg 自动合并）
        if resolution in ("best", "bestvideo", ""):
            fmt = "bv+ba/b"
        elif re.fullmatch(r"\d{3,4}p", resolution, re.I):
            h = _safe_height(resolution)
            fmt = (f"bv[height<={h}]+ba/b[height<={h}]"
                   f"/bv[height<={h}][ext=mp4]+ba")
        else:
            fmt = f"{resolution}[acodec=none]+ba/{resolution}"
        opts["format"] = fmt

        try:
            with self._youtube_dl(opts) as ydl:
                info = (ydl.process_ie_result(direct_info, download=True)
                        if direct_info else self._extract_info(ydl, url, download=True))
        except (yt_dlp.utils.DownloadError, ValueError) as e:  # type: ignore[attr-defined]
            if task_id and task_cancel.is_cancelled(task_id):
                raise AppError(Codes.TASK_CANCELLED, "任务已取消") from e
            msg = str(e)
            raise _ytdlp_error(msg, "下载", url) from e

        entries = (info or {}).get("entries") or [info or {}]
        requested = [(entry, item) for entry in entries if isinstance(entry, dict)
                     for item in entry.get("requested_downloads") or [] if item.get("filepath")]
        files = [Path(item["filepath"]) for _, item in requested]
        if not files:
            raise AppError(Codes.ENG_FAILED, "下载完成但未找到产物文件",
                           suggestion="请携带日志反馈此问题")
        if task_id:
            task_cancel.raise_if_cancelled(task_id)
        from ..renderer import validate_download
        try:
            return [validate_download(self._cfg, Path(item["filepath"]),
                                      item.get("duration") or entry.get("duration") or 0, task_id)
                    for entry, item in requested]
        except AppError as error:
            if error.code == Codes.ENG_FAILED or _is_corrupt_media_error(error):
                _quarantine_corrupt_split(dest_dir, files, files[0])
            raise

    @staticmethod
    def _extract_info(ydl, url: str, download: bool):
        from ..platform_sessions import load_session
        load_session(ydl, url)
        if is_douyin_url(url):
            from .douyin_public import PublicDouyinIE
            ydl.add_info_extractor(PublicDouyinIE())
            return ydl.extract_info(url, download=download, ie_key=PublicDouyinIE.ie_key())
        from .xiaohongshu_public import ConnectedXiaoHongShuIE, is_xhs_url
        if is_xhs_url(url):
            ydl.add_info_extractor(ConnectedXiaoHongShuIE())
            return ydl.extract_info(url, download=download, ie_key=ConnectedXiaoHongShuIE.ie_key())
        return ydl.extract_info(url, download=download)

    def _head_direct_file(self, url: str, resolution: str) -> dict | None:
        """直连视频先查响应头，避免通用解析器 GET 媒体后再 GET 下载。"""
        name = unquote(urlsplit(url).path)
        ext = Path(name).suffix.lower().lstrip(".")
        if ext not in {"mp4", "m4v", "mov", "mkv", "webm", "flv"}:
            return None
        try:
            from yt_dlp.networking import HEADRequest
            with self._youtube_dl(self._common_opts() | {"quiet": True}) as ydl:
                with ydl.urlopen(HEADRequest(url)) as response:
                    mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if not mime.startswith("video/"):
                        return None
                    format_id = mime.split("/", 1)[1]
                    if resolution not in ("", "best", "bestvideo", "source", format_id):
                        return None
                    media_url = response.url
                    length = response.headers.get("Content-Length", "")
        except Exception as exc:
            log.debug("直连视频 HEAD 不可用，按常规方式解析：%s", exc)
            return None
        title = Path(name).stem or "video"
        return {"id": title, "title": title, "webpage_url": url,
                "formats": [{"format_id": format_id, "url": media_url, "ext": ext,
                             "filesize": int(length) if length.isdigit() else 0}]}

    def _resolve_direct_stream(self, url: str, resolution: str) -> DirectTarget | None:
        """解析「音视频合一的单一直达流」——只有这类才适合 aria2c 分段。

        命中场景：generic 直链（mp4 直链）、progressive 格式（B站部分清晰度）。
        DASH/HLS 分离流返回 None → 走 yt-dlp -N 原生路径。
        """
        yt_dlp = _import_ytdlp()
        opts = self._common_opts() | {
            "skip_download": True, "quiet": True, "no_warnings": True, "noprogress": True,
        }
        try:
            with self._youtube_dl(opts) as ydl:
                info = self._extract_info(ydl, url, download=False)
        except Exception as e:  # 探测失败不作定论，交给原生路径处理
            log.debug("直达流解析失败，忽略：%s", e)
            return None

        def is_av(f: dict) -> bool:
            """严格判定音视频合一：编解码未知（None）一律排除。

            注意：不能为兼容 generic 直链而放宽本判定 —— YouTube 部分格式的
            acodec=None 且实为 video-only，误判会下载出没有声音的"成品"。
            generic 直链由顶层 url 分支单独处理（无需编解码信息）。
            """
            return (f.get("vcodec") not in (None, "none")
                    and f.get("acodec") not in (None, "none"))

        def is_http(f: dict) -> bool:
            return f.get("protocol") in ("http", "https")

        # 情况 1：generic 直链，顶层即是媒体流（无需编解码信息即可认定）
        if (info.get("url") and is_http(info)
                and (not info.get("formats")
                     or str(info.get("format_id")) == resolution)):
            return self._target_from(info, info)

        # 情况 2：格式列表中存在音视频合一的 progressive 格式
        candidates = [f for f in info.get("formats") or []
                      if f.get("url") and is_av(f) and is_http(f)]
        if not candidates:
            return None

        chosen = next((f for f in candidates
                       if str(f.get("format_id")) == resolution), None)
        if chosen is None and re.fullmatch(r"\d{3,4}p", resolution, re.I):
            h = _safe_height(resolution)
            if h is not None:
                at_or_below = [f for f in candidates if (f.get("height") or 0) <= h]
                if at_or_below:
                    chosen = max(at_or_below, key=lambda f: f.get("height") or 0)
        if chosen is None and resolution in ("best", "bestvideo", ""):
            chosen = max(candidates,
                         key=lambda f: f.get("filesize") or f.get("filesize_approx") or 0)
        if chosen is None:
            return None
        return self._target_from(chosen, info)

    def _resolve_split_streams(self, url: str, resolution: str) -> list[DirectTarget] | None:
        """DASH 拆流：视频-only + 音频-only 双直达流（并行双 Job 的输入）。

        命中场景：YouTube/B站等分离流站点；generic 单文件直链返回 None。
        """
        yt_dlp = _import_ytdlp()
        opts = self._common_opts() | {
            "skip_download": True, "quiet": True, "no_warnings": True, "noprogress": True,
        }
        try:
            with self._youtube_dl(opts) as ydl:
                info = self._extract_info(ydl, url, download=False)
        except Exception as e:
            log.debug("拆流解析失败，忽略：%s", e)
            return None

        fmts = info.get("formats") or []
        videos = [f for f in fmts if f.get("vcodec") not in (None, "none")
                  and f.get("acodec") == "none" and f.get("url")
                  and f.get("protocol") in ("http", "https")]
        a = _best_audio(fmts)
        if not videos or not a or not a.get("url") or a.get("protocol") not in ("http", "https"):
            return None

        # 视频流按分辨率选；音频与探测及原生 ba 使用同一条。
        v = next((f for f in videos
                  if str(f.get("format_id")) == resolution), None)
        if v is None and re.fullmatch(r"\d{3,4}p", resolution, re.I):
            h = _safe_height(resolution)
            below = [f for f in videos if (f.get("height") or 0) <= h]
            v = max(below, key=lambda f: f.get("height") or 0) if below else None
        if v is None and resolution in ("best", "bestvideo", ""):
            v = max(videos, key=lambda f: f.get("height") or 0)
        if v is None:
            return None
        title = info.get("title") or "video"
        safe = re.sub(r'[\\/:*?"<>|]+', "_", title)[:80] or "video"
        common = info.get("http_headers") or {}
        return [
            DirectTarget(
                url=str(v["url"]),
                filename=f"{safe}.f{v.get('format_id')}.{v.get('ext') or 'mp4'}",
                headers=dict(v.get("http_headers") or common), kind="video",
                duration=float(info.get("duration") or 0)),
            DirectTarget(
                url=str(a["url"]),
                filename=f"{safe}.f{a.get('format_id')}.{a.get('ext') or 'm4a'}",
                headers=dict(a.get("http_headers") or common), kind="audio",
                duration=float(info.get("duration") or 0)),
        ]

    @staticmethod
    def _target_from(fmt: dict, info: dict) -> DirectTarget:
        title = info.get("title") or "video"
        safe = re.sub(r'[\\/:*?"<>|]+', "_", title)[:80] or "video"
        return DirectTarget(
            url=str(fmt["url"]),
            filename=f"{safe}.{fmt.get('ext') or 'mp4'}",
            headers=dict(fmt.get("http_headers") or info.get("http_headers") or {}),
            duration=float(info.get("duration") or 0),
        )

    def _find_aria2c(self) -> Path | None:
        """查找 aria2c：配置路径 → bin/ 内置 → PATH。"""
        cfg_path = self._cfg.get("paths.aria2c", "")
        if cfg_path and Path(cfg_path).exists():
            return Path(cfg_path)
        exe = "aria2c.exe" if os.name == "nt" else "aria2c"
        candidate = bin_dir() / exe
        if candidate.exists():
            return candidate
        found = shutil.which("aria2c")
        return Path(found) if found else None

    @staticmethod
    def _wrap_aggregate(state):
        """多流进度聚合：视频/音频先后下载时进度单调递增，不回跳。

        yt-dlp 对分离流是先下完视频再下音频，各自从 0 报进度 —— 直接透传
        会让前端进度条回跳（用户看到的"进度不正常"）。这里记录已完成流的
        体积，把后续流的进度叠加在其上，形成单调曲线。
        """
        def wrap(hook):
            def inner(d: dict):
                if d.get("status") == "finished":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    state["done_before"] += total
                hook(d)
            return inner
        return wrap

    @staticmethod
    def _make_hook(on_progress, task_id: str | None = None, state=None):
        """yt-dlp progress hook → 统一 DownloadProgress 事件。

        取消：钩子里抛出 AppError(E-TASK-402) 可立即中止 yt-dlp 下载。
        注意：音视频双流在单次 extract_info 内先后下载，进度会在两条流间
        各自从 0 开始 —— MVP 接受这一点；直连 aria2c 拆双 Job 后消除。
        """
        def hook(d: dict):
            if task_id:
                task_cancel.raise_if_cancelled(task_id)
            if on_progress is None:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                base = state["done_before"] if state else 0
                denom = base + total
                frac = (base + done) / denom if denom else 0.0
                # 下一条音频流出现后分母会增大；保留上一帧进度，且下载阶段
                # 真正结束前不显示 100%（总任务完成由上层确认）。
                if state is not None:
                    frac = max(state.get("last_fraction", 0.0), min(frac, 0.99))
                    state["last_fraction"] = frac
                from .base import DownloadProgress
                fmt = (d.get("info_dict") or {}).get("format_id", "")
                on_progress(DownloadProgress(
                    fraction=frac, speed_bps=d.get("speed") or 0.0,
                    eta=d.get("eta"), note=f"下载流 {fmt}" if fmt else ""))
            # finished 不发事件：硬编码 1.0 会在还有音频流时造成"假 100%"回跳，
            # 冻结在最后已知值，由下一条流的聚合值无缝接续
        return hook


def _parse_rate(rate: str) -> int:
    """"5M"/"500K"/"5MB"/"1048576" → 字节/秒；无法解析返回 0（不限速）。"""
    rate = str(rate).strip().upper().rstrip("B")
    mult = 1
    if rate.endswith("M"):
        mult, rate = 1024 * 1024, rate[:-1]
    elif rate.endswith("K"):
        mult, rate = 1024, rate[:-1]
    try:
        return int(float(rate) * mult)
    except ValueError:
        return 0


def _safe_height(resolution: str) -> int | None:
    """"1080p"/"1080" → 1080；无法解析（如 "sb2"、"hls-1080" 之外的怪值）返回 None。

    format_id 不一定是纯数字 —— 直接 int() 会让任务崩掉。
    """
    m = re.search(r"(\d{3,4})\s*p?$", str(resolution).strip(), re.I)
    return int(m.group(1)) if m else None


def _parse_browser(spec: str) -> tuple:
    """浏览器标识 → yt-dlp cookiesfrombrowser 元组。

    支持 "chrome"/"edge"/"firefox"/"brave"/"chromium"/"opera"/"vivaldi"/"safari"，
    以及 "edge:Default" 形式（冒号后为 profile）。yt-dlp 接受
    (browser, profile, keyring, container) 四元组，缺省填 None。
    """
    parts = [p.strip() for p in str(spec).split(":", 1)]
    browser = (parts[0] or "chrome").lower()
    profile = parts[1] if len(parts) > 1 and parts[1] else None
    return (browser, profile, None, None)
