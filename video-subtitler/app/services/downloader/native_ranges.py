"""完整 HTTP 媒体的精确 Range 适配；传输、并行、重试和按序拼接交给 yt-dlp。"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from yt_dlp.downloader.fragment import FragmentFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, RequestError
from yt_dlp.utils import DownloadError, parse_http_range

from ...core import cancel
from ...core.errors import AppError, Codes

CHUNK_BYTES = 10 * 1024 * 1024


def _prepare_resume(attempt: Path, size: int) -> None:
    """只有分段索引与已写字节一致才续传；崩溃多写的尾部可安全回退。"""
    partial, checkpoint = Path(str(attempt) + ".part"), Path(str(attempt) + ".ytdl")
    if not partial.exists() and not checkpoint.exists():
        return
    try:
        index = json.loads(checkpoint.read_text(encoding="utf-8"))["downloader"]["current_fragment"]["index"]
        if type(index) is not int or not 0 <= index <= (size + CHUNK_BYTES - 1) // CHUNK_BYTES:
            raise ValueError("无效分段索引")
        expected = min(index * CHUNK_BYTES, size)
        if not expected <= partial.stat().st_size <= size:
            raise ValueError("分段与字节偏移不一致")
        with partial.open("r+b") as handle:
            handle.truncate(expected)
        return
    except (OSError, ValueError, KeyError, TypeError):
        # 仅清理本次来源标识下的临时检查点，不读取或覆盖其他任务文件。
        partial.unlink(missing_ok=True)
        checkpoint.unlink(missing_ok=True)


class CheckedRangeYDL:
    def __init__(self, ydl, size, task_id, stop_event=None):
        self.ydl, self.size, self.task_id = ydl, size, task_id
        self.stop_event = stop_event
        self.responses = []

    def __getattr__(self, name):
        return getattr(self.ydl, name)

    def urlopen(self, request):
        _check_stop(self.task_id, self.stop_event)
        if self.task_id:
            cancel.raise_if_cancelled(self.task_id)  # 排队片段取消后不得再发 HTTP。
        start, end, _ = parse_http_range(request.headers.get("Range"))
        if start is None or end is None:
            raise DownloadError("A precise Range request is required")
        response = self.ydl.urlopen(request)
        try:
            if self.task_id:
                cancel.raise_if_cancelled(self.task_id)
            _check_stop(self.task_id, self.stop_event)
            if (response.status != 206
                    or parse_http_range(response.headers.get("Content-Range")) != (start, end, self.size)
                    or response.headers.get("Content-Length") != str(end - start + 1)
                    or response.headers.get("Content-Encoding", "identity") != "identity"):
                raise DownloadError("HTTP Range response does not match requested fragment")
            self.responses.append(response)
            return response
        except BaseException:
            response.close()
            raise

    def close_responses(self):
        for response in self.responses:
            response.close()
        self.responses.clear()


class NativeRangeFD(FragmentFD):
    FD_NAME = "native-range"

    def real_download(self, filename, info_dict):
        ctx = {"filename": filename, "total_frags": len(info_dict["fragments"])}
        self._prepare_and_start_frag_download(ctx, info_dict)
        try:
            resume_index = ctx["fragment_index"]
            fragments = ({**item, "frag_index": index + 1, "index": index}
                         for index, item in enumerate(info_dict["fragments"])
                         if index + 1 > resume_index)
            return self.download_and_append_fragments(ctx, fragments, info_dict)
        finally:
            ctx["dest_stream"].close()
            self.ydl.close_responses()


def _check_stop(task_id, stop_event):
    if task_id:
        cancel.raise_if_cancelled(task_id)
    if stop_event is not None and stop_event.is_set():
        raise AppError(Codes.NET_FAILED, "同组下载已停止，已下载分段保留", retryable=True)


def _probe_media(ydl, url, headers, task_id, stop_event):
    try:
        response = ydl.urlopen(Request(url, headers=headers, method="HEAD"))
        from_range = False
    except HTTPError as error:
        error.close()
        if error.status not in (404, 405, 501):
            raise
        # 部分 CDN 只为 GET 提供媒体，使用一个字节读取 Content-Range 总长度。
        _check_stop(task_id, stop_event)
        response = ydl.urlopen(Request(url, headers=headers | {"Range": "bytes=0-0"}, method="GET"))
        from_range = True
    with response:
        _check_stop(task_id, stop_event)
        length = response.headers.get("Content-Length", "")
        if from_range:
            start, end, size = parse_http_range(response.headers.get("Content-Range"))
            valid = response.status == 206 and (start, end) == (0, 0) and length == "1"
        else:
            size = int(length) if length.isdecimal() else None
            valid = response.status == 200
        if (not valid or not size or size < 1
                or response.headers.get("Content-Encoding", "identity") != "identity"):
            raise AppError(Codes.ENG_FAILED, "此媒体未提供可确认的分段下载长度")
        etag = response.headers.get("ETag", "")
        validator = etag if etag and not etag.startswith("W/") else response.headers.get("Last-Modified", "")
        return size, validator


def download_ranges(ydl, target, dest_dir: Path, on_progress=None, task_id=None, concurrency=16,
                    stop_event=None) -> Path:
    from ..engines.ytdlp_engine import YtDlpEngine
    if Path(target.filename).name != target.filename or target.filename in {"", ".", ".."}:
        raise AppError(Codes.DISK_FAILED, "下载文件名必须位于任务目录内")
    _check_stop(task_id, stop_event)
    headers = {k: v for k, v in target.headers.items() if k.lower() not in {"range", "accept-encoding"}}
    headers["Accept-Encoding"] = "identity"
    try:
        size, validator = _probe_media(ydl, target.url, headers, task_id, stop_event)
    except RequestError as error:
        if isinstance(error, HTTPError):
            error.close()
        _check_stop(task_id, stop_event)
        raise AppError(Codes.NET_FAILED, "分段下载长度检查失败", retryable=True) from error
    params = ydl.params | {"retries": 3, "fragment_retries": 0, "skip_unavailable_fragments": False,
                          "concurrent_fragment_downloads": max(1, min(int(concurrency), 16)),
                          "continuedl": True, "http_chunk_size": 0, "noprogress": True}
    if params.get("ratelimit"):
        active_fragments = min(params["concurrent_fragment_downloads"], (size + CHUNK_BYTES - 1) // CHUNK_BYTES)
        params["ratelimit"] = max(1, int(params["ratelimit"]) // active_fragments)
    if validator:
        headers["If-Range"] = validator
    info = {"url": target.url, "http_headers": headers, "format_id": target.kind,
            "fragments": [{"url": target.url, "byte_range": {"start": start, "end": min(start + CHUNK_BYTES, size)}}
                          for start in range(0, size, CHUNK_BYTES)]}
    dest_dir.mkdir(parents=True, exist_ok=True)
    output = dest_dir / target.filename
    identity = hashlib.sha256(f"{target.url}\0{size}\0{validator}\0{CHUNK_BYTES}".encode()).hexdigest()[:24]
    # 缺少来源校验标识时不跨请求复用，避免同 URL 内容变化后拼接错片。
    attempt = output.with_name(output.name + ".native-" + (identity if validator else uuid4().hex))
    completed = output.with_name(output.name + ".native-complete.json")
    try:
        record = json.loads(completed.read_text(encoding="utf-8"))
        stat = output.stat()
        if validator and record == {"source": identity, "size": size, "mtime": stat.st_mtime_ns} and stat.st_size == size:
            return output
    except (OSError, ValueError):
        pass
    _prepare_resume(attempt, size)
    fd = NativeRangeFD(CheckedRangeYDL(ydl, size, task_id, stop_event), params)
    progress_hook = YtDlpEngine._make_hook(on_progress, task_id, {"done_before": 0})
    def progress(info):
        _check_stop(task_id, stop_event)
        progress_hook(info)
    fd.add_progress_hook(progress)
    if not fd.download(str(attempt), info)[0] or attempt.stat().st_size != size:
        raise AppError(Codes.ENG_FAILED, "分段下载文件长度不一致")
    _check_stop(task_id, stop_event)
    attempt.replace(output)
    try:
        completed.write_text(json.dumps({"source": identity, "size": size,
                                         "mtime": output.stat().st_mtime_ns}), encoding="utf-8")
    except OSError:
        pass  # 优化信息写入失败不影响已经完整下载的媒体。
    return output
