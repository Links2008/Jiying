"""yt-dlp 引擎：主引擎，内置上千站点提取器。

外部下载器说明：yt-dlp 对 aria2c 是一等支持，启用 external_downloader 时
框架会自动向 aria2c 透传 UA/Referer/cookies/代理等凭据，无需手工拼 header。
"""
from __future__ import annotations

import logging
import copy
import hashlib
import json
import os
import re
import shutil
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ...core import cancel as task_cancel
from ...core.errors import AppError, Codes
from ...core.paths import bin_dir, data_dir
from ..downloader.aria2c import Aria2cBackend
from ..renderer import find_ffmpeg
from ..ytdlp_updater import runtime_session
from .base import DirectTarget, EngineProvider, FormatInfo, ProbeResult, has_download_bytes
from .platform_links import is_douyin_url, normalize_media_url
from .ytdlp_download_errors import (
    _is_bilibili_url, _is_corrupt_media_error, _quarantine_corrupt_split, _quarantine_failed_split, _ytdlp_error,
)
from .ytdlp_formats import (
    _best_audio, _bilibili_choice, _bilibili_uhd, _format_size, _safe_height,
    _select_video, _short_edge, _video_formats, _video_priority,
)

log = logging.getLogger(__name__)
_media_info_cache = OrderedDict()
_media_info_lock = threading.Lock()


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


class YtDlpEngine(EngineProvider):
    name = "ytdlp"

    def __init__(self, config):
        self._cfg = config

    def _info_key(self, url):
        options = self._common_opts()
        if options.get("cookiesfrombrowser"):
            return None  # 浏览器登录可能随时改变，不额外读取浏览器状态来缓存。
        from ..platform_sessions import platform_for_url, session_path
        paths = [Path(options["cookiefile"])] if options.get("cookiefile") else []
        platform = platform_for_url(url)
        if platform:
            paths.append(session_path(platform))
        stamps = []
        for path in paths:
            try:
                stat = path.stat()
                stamps.append((str(path), stat.st_size, stat.st_mtime_ns))
            except OSError:
                stamps.append((str(path), 0, 0))
        version = getattr(getattr(_import_ytdlp(), "version", None), "__version__", "")
        scope = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()
        return str(data_dir()), normalize_media_url(url), version, scope, tuple(stamps)

    def _remember_info(self, url, info, key):
        if key is None or not isinstance(info, dict) or not info.get("formats") or info.get("entries"):
            return
        if key != self._info_key(url):
            return  # 请求期间账号或代理变化，旧响应不能登记到新账号范围。
        try:
            snapshot = copy.deepcopy(info)
        except (TypeError, ValueError, RecursionError):
            return  # 缓存是优化；特殊提取器的运行状态不能使原本可用的解析失败。
        with _media_info_lock:
            _media_info_cache[key] = (time.monotonic() + 60, snapshot)
            _media_info_cache.move_to_end(key)
            while len(_media_info_cache) > 32:
                _media_info_cache.popitem(last=False)

    def _source_info(self, url):
        key = self._info_key(url)
        with _media_info_lock:
            cached = _media_info_cache.get(key)
            if cached and time.monotonic() < cached[0]:
                return copy.deepcopy(cached[1])
            _media_info_cache.pop(key, None)
        with self._youtube_dl(self._common_opts() | {"quiet": True, "no_warnings": True,
                                                    "skip_download": True, "noprogress": True}) as ydl:
            info = self._extract_info(ydl, url, download=False)
        self._remember_info(url, info, key)
        return info

    def _common_opts(self) -> dict:
        """公共下载参数：重试 + cookies + 代理（按网络模式决定）。

        cookies 双通道：cookies_from_browser（直接复用浏览器登录态）优先，
        否则用 cookies（Netscape cookies.txt 路径）。登录信息是可选配置；
        公开内容先按网站实际返回的画质下载，权限限制由网站判断。
        """
        opts: dict = {"retries": 3, "socket_timeout": 30}
        # 安装包自带运行时；源码环境可复用系统安装，无需用户配置 JavaScript。
        runtimes = {}
        for runtime in ("deno", "node"):
            bundled = bin_dir() / (runtime + (".exe" if os.name == "nt" else ""))
            path = str(bundled) if bundled.is_file() else shutil.which(runtime)
            if path:
                runtimes[runtime] = {"path": path}
        if runtimes:
            opts["js_runtimes"] = runtimes
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
        cache_scope = self._info_key(url)
        yt_dlp = _import_ytdlp()
        opts = self._common_opts() | {
            "skip_download": True, "quiet": True, "no_warnings": True, "noprogress": True,
        }
        info = self._head_direct_file(url, "best")
        bilibili = _is_bilibili_url(url)
        from .xiaohongshu_public import is_xhs_url
        xhs = is_xhs_url(url)
        auth_status = "unknown" if self._cfg.get("cookies_from_browser") or self._cfg.get("cookies") else "none"
        try:
            if info is None:
                with self._youtube_dl(opts) as ydl:
                    info = self._extract_info(ydl, url, download=False)
                    if bilibili or xhs:
                        jar = getattr(ydl, "cookiejar", None)
                        cookie_name = 'SESSDATA' if bilibili else 'web_session'
                        origin = 'https://api.bilibili.com' if bilibili else 'https://www.xiaohongshu.com'
                        if jar and any(c.name == cookie_name and c.value
                                       for c in jar.get_cookies_for_url(origin)):
                            auth_status = "loaded"
        except yt_dlp.utils.DownloadError as e:  # type: ignore[attr-defined]
            msg = str(e)
            low = msg.lower()
            if "unsupported url" in low or "no suitable" in low:
                return None  # 非本引擎职责 → 路由器换下一个引擎
            raise _ytdlp_error(msg, "链接解析", url) from e

        self._remember_info(url, info, cache_scope)
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
        source_formats = info.get("formats") or []
        best_audio = _best_audio(source_formats)
        formats: list[FormatInfo] = []
        seen: set[tuple] = set()
        highest = 0
        highest_uhd = ""
        for f in sorted(_video_formats(source_formats),
                        key=lambda fmt: _video_priority(fmt, bilibili), reverse=True):
            width, height = int(f.get("width") or 0), int(f.get("height") or 0)
            fps = float(f.get("fps") or 0)
            dynamic_range = str(f.get("dynamic_range") or "SDR")
            quality = _short_edge(f)
            original = xhs and f["format_id"] == "direct"
            variant = (width, height, fps, dynamic_range)
            if not original and variant in seen:
                continue
            if not original:
                seen.add(variant)
            highest = max(highest, quality)
            label = "4K / 2160p" if quality == 2160 else f"{quality}p" if quality else "源画质"
            if width and height and (width < height or abs(width * 9 - height * 16) > 16):
                label += f" · {width}×{height}"
            if original:
                label = "原始视频 · " + (label if quality else "分辨率待确认")
            uhd = _bilibili_uhd(f) if bilibili else ""
            if uhd:
                highest_uhd = highest_uhd or uhd
                label = f"{uhd} / 2160p" if uhd == "4K" and quality == 2160 else f"{uhd} · {width}×{height}"
            raw, estimated = _format_size(f, best_audio)
            formats.append(FormatInfo(
                format_id=_bilibili_choice(f) if uhd else str(f["format_id"]),
                label=label, height=height, fps=fps,
                ext=f.get("ext") or "", filesize=raw, size_is_estimate=estimated,
                vcodec=f.get("vcodec") or "", acodec=f.get("acodec") or "",
                protocol=f.get("protocol") or "https", dynamic_range=dynamic_range))
        quality_notice = ""
        if xhs and highest and min(info.get('_jiying_source_size') or (0, 0)) > highest:
            quality_notice = f'当前最高 {highest}p。可连接小红书尝试更多画质。'
        if bilibili:
            if auth_status == "unknown":
                quality_notice = "账号暂不可用，当前画质可直接下载。"
            elif auth_status == "none" and highest < 2160:
                quality_notice = "当前画质可直接下载。"
            if highest_uhd and not quality_notice:
                quality_notice = f"已获取 B站 {highest_uhd} 画质，可直接选择下载。"
            elif 0 < highest < 2160 and not highest_uhd:
                quality_notice = f"当前最高 {highest}p。" + quality_notice
                if info.get("_jiying_bilibili_advertises_uhd"):
                    quality_notice += "B站本次未返回可下载的 4K。"
            elif highest >= 2160 and not quality_notice:
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
        try:
            return self._download(url, resolution, dest_dir, on_progress, task_id)
        except BaseException:
            # 失效媒体地址只影响当前尝试，下次重试会重新向平台获取。
            try:
                key = self._info_key(url)
                with _media_info_lock:
                    _media_info_cache.pop(key, None)
            except Exception:
                pass
            raise

    def _download(self, url, resolution, dest_dir, on_progress, task_id):
        url = normalize_media_url(url)
        if resolution.startswith("bili-") and not _is_bilibili_url(url):
            raise AppError(Codes.ENG_FAILED, "此链接与所选 B站画质不匹配",
                           suggestion="重新解析目标视频后选择画质")
        # 取消检查器：传入 aria2c 轮询循环与 yt-dlp 进度钩子
        cancel_check = (lambda: task_cancel.is_cancelled(task_id)) if task_id else None
        if task_id:
            task_cancel.raise_if_cancelled(task_id)
        if resolution == "mixkit-40913-4k":  # 仅识别历史任务的档位标识。
            raise AppError(Codes.ENG_FAILED, "历史任务的 4K 原素材档已停用",
                           suggestion="重新解析原链接，选择当前可用的画质")
        direct_info = self._head_direct_file(url, resolution)

        bilibili = _is_bilibili_url(url)
        if bilibili:
            split = self._resolve_split_streams(url, resolution)
            if task_id:
                task_cancel.raise_if_cancelled(task_id)
            if split:
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
                    files = self._download_native_streams(split, dest_dir, opts, native_progress, task_id)
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

        if direct_info and resolution == "source":
            resolution = direct_info["formats"][0]["format_id"]
        opts["format"] = self._format_selector(resolution, bilibili)

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
    def _format_selector(resolution: str, bilibili: bool = False):
        """用实际格式 ID 选择视频，分离流交给 yt-dlp 原生合并。"""
        def select(context):
            formats = context["formats"]
            video = _select_video(formats, resolution, bilibili)
            if video is None:
                return
            if video.get("acodec") != "none" or video.get('_jiying_complete_media'):
                yield video
                return
            audio = _best_audio(formats)
            if audio is None:
                if context["incomplete_formats"]:
                    yield video
                return
            # 使用上游的合并选择器，以保留容器及请求字段的标准生成规则。
            yt_dlp = _import_ytdlp()
            with yt_dlp.YoutubeDL({"quiet": True, "merge_output_format": "mp4"}, auto_init=False) as selector:
                scoped = {**context, "formats": [copy.copy(video), copy.copy(audio)]}
                yield from selector.build_format_selector("bv+ba")(scoped)
        return select

    def _download_native_streams(self, targets, dest_dir, options, on_progress, task_id):
        """独立会话并行下载双流，共享连接预算与失败信号。"""
        from ..downloader.native_ranges import download_ranges, _check_stop
        from .base import DownloadProgress
        budget = max(1, min(int(self._cfg.get("download.fragments_concurrency", 16)), 16))
        workers = min(len(targets), budget)
        shares = [budget - workers + 1] + [1] * (len(targets) - 1)
        if workers == 1:
            shares = [budget] * len(targets)
        stop, lock = threading.Event(), threading.Lock()
        fractions, speeds = [0.0] * len(targets), [0.0] * len(targets)
        weights = [target.filesize for target in targets]
        if not all(weight > 0 for weight in weights):
            weights = [1] * len(targets)
        def report(index, progress):
            with lock:
                fractions[index] = max(fractions[index], min(progress.fraction, 1.0))
                speeds[index] = progress.speed_bps if fractions[index] < 1 else 0
                fraction = sum(f * w for f, w in zip(fractions, weights)) / sum(weights)
                on_progress(DownloadProgress(fraction=fraction, speed_bps=sum(speeds),
                            backend="native-range", note="音视频并行下载"))
        def download(index):
            _check_stop(task_id, stop)
            opts = dict(options)
            # 限速是本任务总量，不因并行流数翻倍。
            if opts.get("ratelimit"):
                opts["ratelimit"] = max(1, int(opts["ratelimit"]) // workers)
            with self._youtube_dl(opts) as ydl:
                output = download_ranges(ydl, targets[index], dest_dir,
                    lambda progress: report(index, progress), task_id=task_id,
                    concurrency=shares[index], stop_event=stop)
            report(index, DownloadProgress(fraction=1.0))
            return output
        results = [None] * len(targets)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jiying-stream") as pool:
            futures = {pool.submit(download, index): index for index in range(len(targets))}
            try:
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
            except BaseException:
                stop.set()
                for future in futures:
                    future.cancel()
                raise
        return results

    @staticmethod
    def _extract_info(ydl, url: str, download: bool):
        from ..platform_sessions import load_session
        load_session(ydl, url)
        if _is_bilibili_url(url):
            from .bilibili_quality import BiliBiliIE
            ydl.add_info_extractor(BiliBiliIE())
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
        try:
            info = self._source_info(url)
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
                    and (f.get("acodec") not in (None, "none")
                         or f.get('_jiying_complete_media')))

        def is_http(f: dict) -> bool:
            return f.get("protocol") in ("http", "https")

        # 情况 1：generic 直链，顶层即是媒体流（无需编解码信息即可认定）
        if (info.get("url") and is_http(info)
                and (not info.get("formats")
                     or str(info.get("format_id")) == resolution)):
            return self._target_from(info, info)

        chosen = _select_video(info.get("formats") or [], resolution)
        if chosen is None or not chosen.get("url") or not is_av(chosen) or not is_http(chosen):
            return None
        return self._target_from(chosen, info)

    def _resolve_split_streams(self, url: str, resolution: str) -> list[DirectTarget] | None:
        """DASH 拆流：视频-only + 音频-only 双直达流（并行双 Job 的输入）。

        命中场景：YouTube/B站等分离流站点；generic 单文件直链返回 None。
        """
        try:
            info = self._source_info(url)
        except Exception as e:
            log.debug("拆流解析失败，忽略：%s", e)
            return None

        fmts = info.get("formats") or []
        v = _select_video(fmts, resolution, _is_bilibili_url(url))
        a = _best_audio(fmts)
        if (v is None or v.get("vcodec") is None or v.get("acodec") != "none"
                or not v.get("url") or v.get("protocol") not in ("http", "https")
                or not a or not a.get("url") or a.get("protocol") not in ("http", "https")):
            return None
        title = info.get("title") or "video"
        safe = re.sub(r'[\\/:*?"<>|]+', "_", title)[:80] or "video"
        common = info.get("http_headers") or {}
        return [
            DirectTarget(
                url=str(v["url"]),
                filename=f"{safe}.f{v.get('format_id')}.{v.get('ext') or 'mp4'}",
                headers=dict(v.get("http_headers") or common), kind="video",
                filesize=int(v.get("filesize") or v.get("filesize_approx") or 0),
                duration=float(info.get("duration") or 0)),
            DirectTarget(
                url=str(a["url"]),
                filename=f"{safe}.f{a.get('format_id')}.{a.get('ext') or 'm4a'}",
                headers=dict(a.get("http_headers") or common), kind="audio",
                filesize=int(a.get("filesize") or a.get("filesize_approx") or 0),
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
