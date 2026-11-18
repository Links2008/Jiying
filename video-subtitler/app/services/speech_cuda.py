"""本地语音识别所需的 CUDA 检测及运行库加载。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ..core.paths import bin_dir, data_dir

log = logging.getLogger(__name__)
_cuda_dll_handles: dict[str, object] = {}


def _has_cuda() -> bool:
    try:
        if os.name == 'nt':
            import ctypes
            driver = ctypes.WinDLL('nvcuda.dll', winmode=0x800)
            driver.cuInit.argtypes, driver.cuInit.restype = [ctypes.c_uint], ctypes.c_int
            driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
            driver.cuDeviceGetCount.restype = ctypes.c_int
            count = ctypes.c_int()
            return driver.cuInit(0) == 0 and driver.cuDeviceGetCount(ctypes.byref(count)) == 0 and count.value > 0
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _add_nvidia_dll_dirs(check_cancel=None, on_status=None) -> None:
    """注册随包及 pip CUDA 运行库；原生 LoadLibrary 还需要进程 PATH。"""
    import os
    import sys
    if os.name != "nt":
        return
    from .cuda_runtime import ensure_cuda_runtime
    bundled = bin_dir() / "cuda"
    if (bundled / "runtime.json").is_file():
        bundled = ensure_cuda_runtime(bundled, data_dir() / "cache" / "cuda",
                                      check_cancel, on_status)
    candidates = [bundled] + [
        Path(base) / "nvidia" / pkg / "bin"
        for pkg in ("cublas", "cudnn") for base in list(sys.path)]
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        path = str(candidate.resolve())
        if path in _cuda_dll_handles:
            continue
        try:
            _cuda_dll_handles[path] = os.add_dll_directory(path)
            if path not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
            log.info("已注册 CUDA 运行库目录：%s", path)
        except OSError:
            pass
