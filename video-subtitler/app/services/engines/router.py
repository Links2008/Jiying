"""引擎路由器：按配置顺序探测，失败自动降级到下一个引擎；探测结果缓存 24h。"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from ...core.errors import AppError, Codes
from ...core.paths import data_dir
from ...models.db import Store
from .base import EngineProvider, FormatInfo, ProbeResult, has_download_bytes
from .fallback import LuxEngine, YouGetEngine
from .ytdlp_engine import YtDlpEngine

log = logging.getLogger(__name__)

_FACTORY = {e.name: e for e in (YtDlpEngine, YouGetEngine, LuxEngine)}
PROBE_TTL_HOURS = 24
PROBE_CACHE_VER = 6  # 保留原始流，纠正竖屏画质，并刷新平台解析结果。


def _ytdlp_ver() -> str:
    """当前生效的 yt-dlp 版本（含热更新版本）；缓存随版本失效，
    引擎热更新后立即重探，不必等 24h。"""
    try:
        import yt_dlp
        return str(yt_dlp.version.__version__)
    except Exception:
        return "?"


class EngineRouter:
    def __init__(self, config, store: Store | None = None):
        self._cfg = config
        self._store = store
        order = config.get("engines.order", ["ytdlp"])
        self._engines: list[EngineProvider] = [
            _Factory(name, config) for name in order if name in _FACTORY
        ]

    # ---- 探测（带缓存）----
    def probe(self, url: str, use_cache: bool = True) -> tuple[EngineProvider, ProbeResult]:
        """返回 (命中的引擎, 探测结果)；全部失败抛 E-ENG-201。"""
        cache_file = self._probe_cache_path(url)
        if use_cache and self._store:
            cached = self._store.cache_get(self._probe_key(url))
            if cached and Path(cached).exists():
                try:
                    data = json.loads(Path(cached).read_text(encoding="utf-8"))
                    if (data.get("v") != PROBE_CACHE_VER
                            or data.get("ytdlp") != _ytdlp_ver()):
                        raise KeyError("缓存版本过期")  # 引擎已更新，重新探测
                    data["result"]["formats"] = [
                        FormatInfo(**f) for f in data["result"].get("formats", [])]
                    eng = next((e for e in self._engines
                                if e.name == data.get("engine")), self._engines[0])
                    return eng, ProbeResult(**data["result"])
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass  # 缓存损坏则重新探测

        errors: list[str] = []
        for eng in self._engines:
            if not eng.available():
                errors.append(f"{eng.name}: 引擎不可用（缺外部程序）")
                continue
            log.info("使用引擎 %s 探测 %s", eng.name, url)
            result = eng.probe(url)
            if result is not None:
                if self._store and cache_file:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        payload = {"v": PROBE_CACHE_VER, "ytdlp": _ytdlp_ver(),
                                   "engine": eng.name, "result": asdict(result)}
                        cache_file.write_text(json.dumps(payload, ensure_ascii=False),
                                              encoding="utf-8")
                        self._store.cache_put(self._probe_key(url), "probe",
                                              str(cache_file), PROBE_TTL_HOURS)
                    except OSError:
                        pass
                return eng, result
            errors.append(f"{eng.name}: 不支持该链接")

        raise AppError(
            Codes.ENG_UNSUPPORTED, "所有引擎均无法解析该链接",
            detail="；".join(errors),
            suggestion="确认链接可在浏览器访问，再刷新画质；登录内容请先连接登录态",
        )

    # ---- 下载（失败换引擎重试）----
    def download(self, url: str, resolution: str, dest_dir: Path,
                 on_progress=None, prefer_engine: str | None = None,
                 task_id: str | None = None) -> tuple[str, list[Path]]:
        """按序尝试下载，仅在尚未传输时换引擎。返回 (引擎名, 产物列表)。"""
        chain = list(self._engines)
        exact_selection = resolution not in ("best", "bestvideo", "")
        if exact_selection:
            # yt-dlp 的格式 ID 不能交给 you-get/lux；它们无法保证所选画质。
            chain = [eng for eng in chain if eng.name == "ytdlp"]
        if prefer_engine:
            chain.sort(key=lambda e: e.name != prefer_engine)
        errors: list[str] = []
        transferred = False
        def track_progress(progress):
            nonlocal transferred
            transferred |= progress.fraction > 0
            if on_progress:
                on_progress(progress)

        for eng in chain:
            if not eng.available():
                continue
            log.info("引擎 %s 开始下载（resolution=%s）", eng.name, resolution)
            try:
                files = eng.download(url, resolution, dest_dir, track_progress,
                                     task_id=task_id)
                return eng.name, files
            except AppError as e:
                if exact_selection:
                    raise  # 保留刷新画质建议，禁止悄悄替换选定格式。
                if e.code in (Codes.AUTH_NEED_COOKIES, Codes.TASK_CANCELLED):
                    raise  # 凭据/取消：换引擎无意义，直接抛给上层
                if transferred or has_download_bytes(dest_dir):
                    raise  # 已传输时切换引擎会重复消耗流量
                errors.append(f"{eng.name}: {e}")
                log.warning("引擎 %s 失败，尝试降级：%s", eng.name, e)
        raise AppError(
            Codes.ENG_FAILED, "所有引擎下载均失败",
            detail="；".join(errors),
            suggestion="查看日志定位原因；网络问题请检查代理设置",
        )

    # ---- helpers ----
    @staticmethod
    def _probe_key(url: str) -> str:
        return "probe:" + hashlib.sha1(url.encode()).hexdigest()

    def _probe_cache_path(self, url: str) -> Path:
        return data_dir() / "cache" / "probe" / f"{self._probe_key(url)}.json"


def _Factory(name: str, config) -> EngineProvider:  # noqa: N802（保持注册表写法直观）
    return _FACTORY[name](config)
