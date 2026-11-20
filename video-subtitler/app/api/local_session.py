"""本机 HTTP 会话的用户范围登记及单引擎数据目录锁。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..core import windows_data
from ..core.paths import replace_with_retry


def _state_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "VideoSubtitler" / "runtime"


def _session_file(engine_root: Path) -> Path:
    normalized = os.path.normcase(os.path.abspath(engine_root))
    identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return _state_dir() / (identity + ".json")


@contextmanager
def windows_session(engine_root: Path, port: int):
    """随机凭据仅通过当前用户 DPAPI 加密后的登记文件交给桌面端。"""
    token = secrets.token_hex(32)
    target = _session_file(engine_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = {"pid": os.getpid(), "port": port, "token_dpapi": windows_data.protect(token)}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=".session-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(descriptor, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            replace_with_retry(temporary, target)
        except OSError as error:
            if getattr(error, "winerror", None) != 17:
                raise
            # 某些 Windows 环境会拒绝同目录重命名。外层引擎锁保证单写者，
            # 桌面端会重试尚未写全的登记，并核对进程和加密凭据。
            payload = temporary.read_bytes()
            target.unlink(missing_ok=True)
            with target.open("xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        yield token
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        try:
            if json.loads(target.read_text(encoding="utf-8")) == descriptor:
                target.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


@contextmanager
def exclusive_data_dir(directory: Path):
    """在旧任务恢复前锁住该数据目录，避免两个新版引擎同时改库。"""
    import msvcrt

    with (directory / ".engine.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("该数据目录已有即影引擎在运行") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def source_session_token() -> str:
    """源码服务可由宿主通过环境传入预先协商的凭据。"""
    token = os.environ.pop("JIYING_LOCAL_API_TOKEN", "")
    if token and not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("本机引擎凭据格式无效")
    return token or secrets.token_hex(32)
