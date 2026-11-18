"""即影桌面 WinRT OCR 的本地帧管道及子进程清理。"""
from __future__ import annotations

import io
import json
import struct
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from ..core import cancel
from ..core.errors import AppError, Codes
from .renderer import _NO_WINDOW

_HEADER = struct.Struct("<IqIII")


def supports_worker(exe: Path) -> bool:
    """旧桌面程序会把未知参数当正常启动，先读取版本资源。"""
    import ctypes

    version = ctypes.WinDLL("version")
    name = ctypes.c_wchar_p(str(exe))
    size = version.GetFileVersionInfoSizeW(name, None)
    if size <= 0:
        return False
    metadata = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(name, 0, size, metadata):
        return False
    address, length = ctypes.c_void_p(), ctypes.c_uint()
    if not version.VerQueryValueW(metadata, ctypes.c_wchar_p("\\"), ctypes.byref(address), ctypes.byref(length)):
        return False
    if length.value < 16:
        return False
    signature, _, major_minor, build_revision = (ctypes.c_uint32 * 4).from_address(address.value)
    return signature == 0xFEEF04BD and (major_minor, build_revision) >= (0x10001, 45 << 16)


@contextmanager
def tracked_process(args, task_id, **options):
    cancel.raise_if_cancelled(task_id)
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, **options) as process:
        cancel.register(task_id, process)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            cancel.pop(task_id, process)


def _open_pipe(name, process, task_id):
    deadline = time.monotonic() + 25
    while process.poll() is None:
        cancel.raise_if_cancelled(task_id)
        try:
            return open(rf"\\.\pipe\{name}", "r+b", buffering=0)
        except OSError as error:
            if not isinstance(error, FileNotFoundError) and getattr(error, "winerror", None) not in (2, 231):
                raise
            if time.monotonic() >= deadline:
                break
            time.sleep(.04)
    cancel.raise_if_cancelled(task_id)
    raise OSError("OCR 管道未能建立")


class OcrPipe:
    def __init__(self, stream, process, executor, task_id):
        self.stream = stream
        self.process = process
        self.executor = executor
        self.task_id = task_id

    def wait_io(self, process, operation, *args, timeout=20):
        cancel.raise_if_cancelled(self.task_id)
        pending = self.executor.submit(operation, *args)
        try:
            value = pending.result(timeout=timeout)
        except TimeoutError as error:
            if process.poll() is None:
                process.kill()
            raise OSError("OCR 帧管道等待超时") from error
        finally:
            cancel.raise_if_cancelled(self.task_id)
        return value

    def _response(self):
        line = self.stream.readline(1024 * 1024)
        if not line.endswith(b"\n"):
            raise OSError("OCR 管道返回不完整")
        result = json.loads(line)
        if not isinstance(result, dict):
            raise OSError("OCR 管道应答格式错误")
        if result.get("ok") is not True:
            message = str(result.get("error") or "OCR 识别未完成")
            if result.get("code") == "InvalidOperationException" and "语言设置" in message:
                raise AppError(Codes.DEP_MISSING, message)
            raise OSError(message)
        return result

    def recognize(self, index, pts, width, height, pixels):
        def exchange():
            self.stream.write(_HEADER.pack(index, pts, width, height, len(pixels)))
            self.stream.write(pixels)
            self.stream.flush()
            reply = self._response()
            if reply.get("frame_id") != index or reply.get("pts_ms") != pts or not isinstance(reply.get("lines"), list):
                raise OSError("OCR 帧应答未匹配")
            return reply["lines"]

        return self.wait_io(self.process, exchange)

    def finish(self):
        def send_end():
            self.stream.write(_HEADER.pack(0, 0, 0, 0, 0))
            self.stream.flush()

        self.wait_io(self.process, send_end)
        if self.process.wait(timeout=5) != 0:
            raise OSError("OCR 子进程退出异常")


@contextmanager
def connected_worker(tool: Path, task_id: str):
    name = "jiying-ocr-" + uuid.uuid4().hex
    with tracked_process([str(tool), "--ocr-worker", name], task_id,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        raw = _open_pipe(name, process, task_id)
        with io.BufferedRWPair(raw, raw) as stream, ThreadPoolExecutor(1, "jiying-ocr-io") as executor:
            client = OcrPipe(stream, process, executor, task_id)
            ready = client.wait_io(process, client._response)
            limit = ready.get("max_dimension")
            if ready.get("protocol") != 1 or type(limit) is not int or limit < 2:
                raise OSError("OCR 协议版本或尺寸协商失败")
            yield client, limit
            client.finish()
