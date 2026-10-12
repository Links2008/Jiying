"""完整 HTTP 媒体的精确 Range 适配；传输、并行、重试和按序拼接交给 yt-dlp。"""
from pathlib import Path
from uuid import uuid4

from yt_dlp.downloader.fragment import FragmentFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.utils import DownloadError, parse_http_range

from ...core import cancel
from ...core.errors import AppError, Codes

CHUNK_BYTES = 10 * 1024 * 1024


class CheckedRangeYDL:
    def __init__(self, ydl, size, task_id):
        self.ydl, self.size, self.task_id = ydl, size, task_id
        self.responses = []

    def __getattr__(self, name):
        return getattr(self.ydl, name)

    def urlopen(self, request):
        if self.task_id:
            cancel.raise_if_cancelled(self.task_id)  # 排队片段取消后不得再发 HTTP。
        start, end, _ = parse_http_range(request.headers.get("Range"))
        if start is None or end is None:
            raise DownloadError("A precise Range request is required")
        response = self.ydl.urlopen(request)
        try:
            if self.task_id:
                cancel.raise_if_cancelled(self.task_id)
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


def download_ranges(ydl, target, dest_dir: Path, on_progress=None, task_id=None, concurrency=16) -> Path:
    from ..engines.ytdlp_engine import YtDlpEngine
    if Path(target.filename).name != target.filename or target.filename in {"", ".", ".."}:
        raise AppError(Codes.DISK_FAILED, "下载文件名必须位于任务目录内")
    if task_id:
        cancel.raise_if_cancelled(task_id)
    headers = {k: v for k, v in target.headers.items() if k.lower() not in {"range", "accept-encoding"}}
    headers["Accept-Encoding"] = "identity"
    try:
        with ydl.urlopen(Request(target.url, headers=headers, method="HEAD")) as response:
            size = response.headers.get("Content-Length", "")
            if response.status != 200 or not size.isdecimal() or int(size) <= 0:
                raise AppError(Codes.ENG_FAILED, "此媒体未提供可确认的分段下载长度")
            size = int(size)
    except RequestError as error:
        if task_id:
            cancel.raise_if_cancelled(task_id)
        raise AppError(Codes.NET_FAILED, "分段下载长度检查失败", retryable=True) from error
    params = ydl.params | {"retries": 3, "fragment_retries": 0, "skip_unavailable_fragments": False,
                          "concurrent_fragment_downloads": max(1, min(int(concurrency), 16)),
                          "continuedl": False, "http_chunk_size": 0, "noprogress": True}
    info = {"url": target.url, "http_headers": headers, "format_id": target.kind,
            "fragments": [{"url": target.url, "byte_range": {"start": start, "end": min(start + CHUNK_BYTES, size)}}
                          for start in range(0, size, CHUNK_BYTES)]}
    dest_dir.mkdir(parents=True, exist_ok=True)
    output = dest_dir / target.filename
    attempt = output.with_name(output.name + ".native-" + uuid4().hex)
    fd = NativeRangeFD(CheckedRangeYDL(ydl, size, task_id), params)
    fd.add_progress_hook(YtDlpEngine._make_hook(on_progress, task_id, {"done_before": 0}))
    if not fd.download(str(attempt), info)[0] or attempt.stat().st_size != size:
        raise AppError(Codes.ENG_FAILED, "分段下载文件长度不一致")
    if task_id:
        cancel.raise_if_cancelled(task_id)
    attempt.replace(output)
    return output
