"""解析与下载失败归类、损坏分轨隔离。"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from ...core.errors import AppError, Codes
from .platform_links import is_douyin_url

log = logging.getLogger(__name__)
def _is_bilibili_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in ("bilibili.com", "b23.tv") or host.endswith(".bilibili.com")


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


def _ytdlp_error(message: str, stage: str, url: str = "") -> AppError:
    low = message.lower()
    if any(word in low for word in ("tls fingerprint", "impersonation", "certificate verify failed",
                                    "connection timed out", "unable to download webpage: http error 5")):
        return AppError(Codes.NET_FAILED, "暂时无法连接视频网站", detail=message[:1000],
                        retryable=True, suggestion="请检查网络后重试")
    if stage == '下载' and 'write to closed file' in low:
        return AppError(Codes.NET_FAILED, '视频分片下载中断，请重试', detail=message[:1000],
                        retryable=True, suggestion='已下载的分片保留；网络恢复后重新下载')
    from .xiaohongshu_public import is_xhs_url
    if is_xhs_url(url) and '图文笔记' in message:
        return AppError(Codes.ENG_UNSUPPORTED, '这是小红书图文笔记', suggestion='请复制视频笔记的完整分享链接')
    if is_xhs_url(url) and ('no video formats' in low or '网页未返回视频地址' in message):
        return AppError(Codes.AUTH_NEED_COOKIES, '小红书网页尚未提供视频地址',
                        suggestion='扫码连接小红书后自动继续解析；链接失效时请重新复制分享链接')
    if is_douyin_url(url) and ("fresh cookies" in low or "分享页未返回" in message):
        return AppError(Codes.ENG_FAILED, "抖音未返回可下载的视频详情",
                        detail=message[:1000], retryable=True,
                        suggestion="确认目标视频仍可公开访问，再重新复制视频分享链接；稍后重试")
    if any(word in low for word in ("cookie", "login", "log in", "sign in", "decrypt", "dpapi")):
        return AppError(Codes.AUTH_NEED_COOKIES, "此视频需要连接账号后解析",
                        detail=message[:1000],
                        suggestion=("扫码连接 B 站后自动继续解析"
                                    if _is_bilibili_url(url) else
                                    "连接对应网站的账号后重试"))
    if "requested format" in low and "not available" in low:
        return AppError(Codes.ENG_FAILED, "所选画质已不可用，请刷新画质后重新选择",
                        detail=message[:1000], retryable=True,
                        suggestion="返回解析页刷新画质；高画质视频请先检查登录态和账号权限")
    return AppError(Codes.ENG_FAILED, f"{stage}失败",
                    detail=message[:1000], retryable=True,
                    suggestion="请确认视频链接有效后重试；详细原因已保留在日志中")
