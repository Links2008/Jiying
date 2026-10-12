"""yt-dlp 热更新：跟随上游 PyPI 发布，站点解析规则过期时自动换新。

背景：引擎以 PyInstaller 冻结包分发，yt-dlp 固化在 exe 内；平台改接口后
旧解析器失效（用户感知为"解析失败/下不了"）。本模块在后台检查 PyPI 新版本，
下载官方 wheel（纯 Python、无编译依赖），解压到数据目录 vendor/，并通过
sys.path 置顶实现热替换 —— CPython 的 PathFinder 在 meta_path 中先于
PyInstaller 的 FrozenImporter，因此 vendor/ 里的包优先于 exe 内置版本。

热替换：安装成功后把 yt_dlp* 从 sys.modules 驱逐，下次 _import_ytdlp()
即载入新版，无需重启引擎；正在进行的下载持有旧模块引用，不受影响。

安全性：任何一步失败都只记日志、回滚到内置版本，绝不影响启动。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from ..core.paths import data_dir

log = logging.getLogger(__name__)

PYPI_API = "https://pypi.org/pypi/yt-dlp/json"
CHECK_INTERVAL = 12 * 3600          # 检查节流：12h 一次（状态文件持久化）
HTTP_TIMEOUT = 10
_PKG = "yt_dlp"


def _vendor_dir() -> Path:
    return data_dir() / "vendor" / "ytdlp"


def _state_path() -> Path:
    return _vendor_dir() / "state.json"


def _parse_ver(v: str) -> tuple:
    """"2026.08.19"/"2026.8.19" → (2026, 8, 19)，可比较。"""
    parts = []
    for p in str(v).strip().split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def bundled_version() -> str:
    """当前进程里已生效（或内置）的 yt-dlp 版本。"""
    try:
        import yt_dlp
        return str(yt_dlp.version.__version__)
    except Exception:
        return "0"


def vendored_version() -> str:
    """vendor 目录里热更新包的版本；不存在返回空串。"""
    try:
        txt = (_vendor_dir() / _PKG / "version.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"__version__\s*=\s*'([^']+)'", txt)
    return m.group(1) if m else ""


def activate_vendored() -> str:
    """启动时同步调用：vendor 内若有比内置更新的 yt_dlp，挂到 sys.path 最前。

    纯文件系统操作、无网络，毫秒级完成；返回生效版本（未生效返回空串）。
    注意：bundled_version() 的 import 会把内置 yt_dlp 缓存进 sys.modules，
    挂载后必须驱逐，否则后续 import 仍命中缓存的旧模块，热替换不生效。
    挂载后立即冒烟自检（import + 版本核对）：vendor 包可能在上次写入时损坏
    （断电/磁盘错误），损坏则废弃整个 vendor 目录、驱逐回内置版本 ——
    保证引擎永远起得来，热更新目录出问题绝不拖垮启动。
    """
    vv = vendored_version()
    if not vv:
        return ""
    bv = bundled_version()
    if _parse_ver(vv) <= _parse_ver(bv):
        return ""
    root = str(_vendor_dir())
    if root not in sys.path:
        sys.path.insert(0, root)
    _evict_ytdlp()  # 驱逐内置模块缓存，下次 import 从 vendor 载入
    try:
        import yt_dlp
        got = str(yt_dlp.version.__version__)
        if _parse_ver(got) != _parse_ver(vv):
            raise RuntimeError(f"vendor 版本异常：{got} != {vv}")
    except Exception as e:
        log.warning("vendor yt-dlp 冒烟自检失败，回退内置版本：%s", e)
        shutil.rmtree(_vendor_dir() / _PKG, ignore_errors=True)
        _evict_ytdlp()
        return ""
    log.info("yt-dlp 热更新版本生效：%s（内置 %s）", vv, bv)
    return vv


def _evict_ytdlp() -> None:
    """把 yt_dlp* 从模块缓存驱逐，下次 import 从 sys.path 重新载入。

    已持有旧模块引用的执行中代码不受影响（旧对象继续存活到引用释放）。
    """
    for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        sys.modules.pop(name, None)


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(st: dict) -> None:
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _latest_release() -> tuple[str, str, str]:
    """查询 PyPI 最新版本、wheel 下载地址与其 sha256；失败抛异常（调用方兜底）。"""
    import requests
    r = requests.get(PYPI_API, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    ver = str(data["info"]["version"])
    entry = next((u for u in data.get("urls", [])
                  if u.get("packagetype") == "bdist_wheel"), None)
    if not entry:
        raise RuntimeError("PyPI 响应中未找到 wheel")
    sha = str((entry.get("digests") or {}).get("sha256") or "")
    return ver, entry["url"], sha


def _install_wheel(url: str, sha256: str = "") -> None:
    """下载 wheel 并把 yt_dlp 包原子换入 vendor 目录。

    完整性双保险：sha256（PyPI 官方摘要，防下载损坏/篡改）+ zipfile 内建
    CRC；任一不符立即抛异常弃装，现有 vendor 版本不受影响。
    """
    import hashlib
    import requests
    vendor = _vendor_dir()
    vendor.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ytdlp_up") as td:
        whl = Path(td) / "pkg.whl"
        h = hashlib.sha256()
        with requests.get(url, timeout=180, stream=True) as r:
            r.raise_for_status()
            with open(whl, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    h.update(chunk)
        if sha256 and h.hexdigest().lower() != sha256.lower():
            raise RuntimeError(f"wheel sha256 不符（本地 {h.hexdigest()[:12]}…），已丢弃")
        with zipfile.ZipFile(whl) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError(f"wheel 文件损坏: {bad}")
            z.extractall(td, members=[n for n in z.namelist()
                                      if n.startswith("yt_dlp/") and not n.endswith("/")])
        src, dst = Path(td) / "yt_dlp", vendor / "yt_dlp"
        tmp, old = vendor / "yt_dlp.new", vendor / "yt_dlp.old"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        shutil.copytree(src, tmp)
        if dst.exists():
            dst.replace(old)          # Windows 不能覆盖非空目录，先改名挪走
        tmp.replace(dst)
        shutil.rmtree(old, ignore_errors=True)


def _smoke_test(expect_ver: str) -> bool:
    """热替换自检：驱逐旧模块→重载→校验版本→实例化 YoutubeDL。"""
    try:
        root = str(_vendor_dir())
        if root not in sys.path:
            sys.path.insert(0, root)
        _evict_ytdlp()
        import yt_dlp
        if _parse_ver(yt_dlp.version.__version__) != _parse_ver(expect_ver):
            raise RuntimeError(f"版本不符：{yt_dlp.version.__version__} != {expect_ver}")
        yt_dlp.YoutubeDL({"quiet": True}).close()
        return True
    except Exception as e:
        log.warning("yt-dlp 热更新自检失败：%s", e)
        return False


def _rollback() -> None:
    """自检失败回滚：删除 vendor 包并驱逐模块，回落到内置版本。"""
    shutil.rmtree(_vendor_dir() / _PKG, ignore_errors=True)
    _evict_ytdlp()


def _check_once(force: bool = False) -> None:
    st = _read_state()
    now = time.time()
    if not force and now - float(st.get("last_check", 0)) < CHECK_INTERVAL:
        return
    st["last_check"] = now
    _write_state(st)
    latest, wheel, sha = _latest_release()
    current = vendored_version() or bundled_version()
    st["latest_seen"] = latest
    _write_state(st)
    if _parse_ver(latest) <= _parse_ver(current):
        log.info("yt-dlp 已是最新（%s）", current)
        return
    log.info("发现 yt-dlp 新版本：%s（当前 %s），开始热更新…", latest, current)
    _install_wheel(wheel, sha)
    if _smoke_test(latest):
        st["installed"] = latest
        _write_state(st)
        log.info("yt-dlp 已热更新到 %s，下次解析即生效，无需重启", latest)
    else:
        _rollback()


def auto_update(config=None) -> None:
    """后台线程入口（引擎启动时拉起）：立即检查一次，之后按间隔复查。

    config.get("update.yt_dlp_auto", False) 可关闭自动检查；
    已下载的 vendor 版本不受影响（activate_vendored 在 create_app 里同步执行）。
    """
    if config is not None and not bool(config.get("update.yt_dlp_auto", True)):
        log.info("yt-dlp 自动更新已关闭（update.yt_dlp_auto=false）")
        return
    while True:
        try:
            _check_once()
        except Exception as e:
            log.info("yt-dlp 更新检查未完成（不影响使用）：%s", e)
        time.sleep(CHECK_INTERVAL)


def check_now() -> dict:
    """手动触发一次更新检查（绕过 12h 节流）；同步执行，返回结果摘要。"""
    try:
        _check_once(force=True)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    st = _read_state()
    return {"ok": True, "installed": st.get("installed", ""),
            "latest_seen": st.get("latest_seen", "")}


def status() -> dict:
    """当前 yt-dlp 版本状态（供设置页展示）。"""
    st = _read_state()
    return {
        "active": bundled_version(),      # 当前进程实际生效版本
        "vendored": vendored_version(),   # vendor 目录已就绪版本
        "bundled": "",                    # 兼容字段（引擎内置版本随 exe）
        "latest_seen": st.get("latest_seen", ""),
        "last_check": st.get("last_check", 0),
    }


__all__ = ["auto_update", "activate_vendored", "bundled_version",
           "vendored_version", "check_now", "status"]
