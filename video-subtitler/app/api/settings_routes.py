"""下载与字幕配置、平台登录、目录选择和解析器版本。"""
from __future__ import annotations

import copy
import heapq
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from ..core.config import Config, save_config
from ..core.errors import AppError
from .runtime import EngineRuntime
from .schemas import BilibiliLoginPollIn, DownloadConfigIn

log = logging.getLogger(__name__)


def _mask_key(key: str) -> str:
    """API Key 掩码：保留前 3 位供辨认，其余隐藏。"""
    return (key[:3] + "***") if key else ""


def settings_routes(runtime: EngineRuntime) -> APIRouter:
    router = APIRouter()

    def _clear_probe_cache():
        """登录态变更后清空探测缓存：否则旧的「只有 1080p」列表会继续命中
        24h 缓存，用户补了 cookies 也看不到 4K。"""
        try:
            import shutil
            p = runtime.data_root / "cache" / "probe"
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
        try:
            if runtime.store:
                runtime.store.cache_purge_kind("probe")
        except Exception:
            pass

    def _save_config(candidate):
        """先原子落盘，成功后一次发布，保存失败不能修改运行配置。"""
        saved = save_config(candidate)
        runtime.config.replace_raw(saved)

    def _connect_bilibili(cookie_path: Path):
        with runtime.config_lock:
            candidate = copy.deepcopy(runtime.config.raw)
            candidate.update(cookies=str(cookie_path), cookies_from_browser="")
            _save_config(candidate)
            _clear_probe_cache()

    from ..services.bilibili_login import BilibiliLogin
    bilibili_login = BilibiliLogin(runtime.config, runtime.data_root / "bilibili-cookies.txt", _connect_bilibili)

    @router.post("/api/v1/bilibili/login/start")
    def start_bilibili_login():
        try:
            return bilibili_login.start()
        except AppError as e:
            raise HTTPException(503, e.to_dict()) from None

    @router.post("/api/v1/bilibili/login/poll")
    def poll_bilibili_login(body: BilibiliLoginPollIn):
        try:
            return bilibili_login.poll(body.session_id)
        except AppError as e:
            raise HTTPException(503, e.to_dict()) from None

    @router.post("/api/v1/platform/session")
    async def connect_platform_session(request: Request):
        # Parse explicitly so validation errors never echo cookie values.
        from ..services.platform_sessions import save_session
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(422, '官网会话格式无效') from None
        try:
            save_session(body.get('platform'), body.get('cookies'))
        except AppError as e:
            raise HTTPException(422, e.to_dict()) from None
        _clear_probe_cache()
        return {'ok': True}

    @router.get("/api/v1/config")
    def get_download_config():
        """返回前端可调的下载设置。"""
        snapshot = runtime.config.raw
        d = snapshot.get("download", {})
        t = snapshot.get("translator", {})
        tc = snapshot.get("transcriber", {})
        cloud = tc.get("cloud") or {}
        return {"download": {
            "proxy_mode": d.get("proxy_mode", "system"),
            "custom_proxy": d.get("custom_proxy", ""),
            "fragments_concurrency": d.get("fragments_concurrency", 8),
            "aria2c_enabled": (d.get("aria2c") or {}).get("enabled", False),
            "cookies": snapshot.get("cookies", ""),
            "cookies_from_browser": snapshot.get("cookies_from_browser", "")},
            "max_concurrent": snapshot.get("pipeline", {}).get("max_concurrent", 2),
            "model": tc.get("model", "small"),
            "transcriber_provider": tc.get("provider", "faster-whisper"),
            "asr_vendor": cloud.get("vendor", ""),
            "asr_base_url": cloud.get("base_url", ""),
            "asr_api_key_masked": _mask_key(cloud.get("api_key", "")),
            "asr_model": cloud.get("model", ""),
            "asr_hotwords": tc.get("hotwords", "") or "",
            "translator_provider": t.get("provider", "null"),
            "llm_api_key_masked": _mask_key((t.get("llm") or {}).get("api_key", "")),
            "llm_model": (t.get("llm") or {}).get("model", ""),
            "llm_vendor": (t.get("llm") or {}).get("vendor", ""),
            "llm_base_url": (t.get("llm") or {}).get("base_url", ""),
            "llm_domain_context": (t.get("llm") or {}).get("domain_context", "") or "",
            "llm_terminology_notes": (t.get("llm") or {}).get("terminology_notes", "") or ""}

    @router.post("/api/v1/config")
    def set_download_config(body: DownloadConfigIn):
        """完整保存后更新运行设置；并发保存按请求顺序串行完成。"""
        with runtime.config_lock:
            candidate = copy.deepcopy(runtime.config.raw)
            d = candidate.setdefault("download", {})
            if body.proxy_mode in ("system", "direct", "custom"):
                d["proxy_mode"] = body.proxy_mode
            if body.custom_proxy is not None:
                d["custom_proxy"] = body.custom_proxy.strip()
            if body.fragments_concurrency:
                d["fragments_concurrency"] = max(1, min(32, body.fragments_concurrency))
            if body.aria2c_enabled is not None:
                d.setdefault("aria2c", {})["enabled"] = body.aria2c_enabled
            # 登录态（cookies）：变更即失效 probe 缓存，确保重新拿到最新清晰度列表
            cookie_changed = False
            if body.cookies is not None and body.cookies.strip() != candidate.get("cookies", ""):
                candidate["cookies"] = body.cookies.strip()
                cookie_changed = True
            if (body.cookies_from_browser is not None
                    and body.cookies_from_browser.strip() != candidate.get("cookies_from_browser", "")):
                candidate["cookies_from_browser"] = body.cookies_from_browser.strip()
                cookie_changed = True
            if body.max_concurrent is not None:
                candidate.setdefault("pipeline", {})["max_concurrent"] = max(1, min(4, body.max_concurrent))
            tc = candidate.setdefault("transcriber", {})
            if body.model in ("tiny", "base", "small", "medium", "large-v3"):
                tc["model"] = body.model
            if body.transcriber_provider in ("faster-whisper", "cloud"):
                tc["provider"] = body.transcriber_provider
            cloud = tc.setdefault("cloud", {})
            if body.asr_vendor is not None:
                cloud["vendor"] = body.asr_vendor.strip()
            if body.asr_base_url is not None:
                cloud["base_url"] = body.asr_base_url.strip()
            # 留空 = 保持已保存的 Key 不变（前端不再回显明文）
            if body.asr_api_key and body.asr_api_key.strip():
                cloud["api_key"] = body.asr_api_key.strip()
            if body.asr_model is not None:
                cloud["model"] = body.asr_model.strip()
            if body.asr_hotwords is not None:
                tc["hotwords"] = body.asr_hotwords.strip()
            t = candidate.setdefault("translator", {})
            if body.translator_provider in ("null", "llm"):
                t["provider"] = body.translator_provider
            if body.llm_api_key and body.llm_api_key.strip():
                t.setdefault("llm", {})["api_key"] = body.llm_api_key.strip()
            # 留空 = 保持已保存的 Key 不变（前端不再回显明文）
            if body.llm_model is not None:
                t.setdefault("llm", {})["model"] = body.llm_model.strip()
            if body.llm_vendor is not None:
                t.setdefault("llm", {})["vendor"] = body.llm_vendor.strip()
            if body.llm_base_url is not None:
                t.setdefault("llm", {})["base_url"] = body.llm_base_url.strip()
            if body.llm_domain_context is not None:
                t.setdefault("llm", {})["domain_context"] = body.llm_domain_context.strip()
            if body.llm_terminology_notes is not None:
                t.setdefault("llm", {})["terminology_notes"] = body.llm_terminology_notes.strip()
            # 构建识别后端可能失败，必须在落盘前完成；这里不会下载模型。
            prepared = None
            try:
                if tc.get("provider", "faster-whisper") != runtime.config.get("transcriber.provider", "faster-whisper"):
                    from ..services.transcriber import build_transcriber
                    prepared = build_transcriber(Config(candidate))
                _save_config(candidate)
            except Exception:
                if prepared is not None:
                    close = getattr(prepared, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            log.warning("保存失败后的识别实例清理失败")
                # 底层异常可能包含密钥或损坏的私有配置，不能原样回显。
                raise HTTPException(503, "配置保存失败，运行设置保持不变，请检查文件权限或配置格式后重试") from None
            if prepared is not None:
                prepared._cfg = runtime.config
            if body.transcriber_provider:
                runtime.runner.reload_transcriber(prepared=prepared)
            if body.max_concurrent is not None:
                from app.pipeline import runner as _runner_mod
                _runner_mod.set_max_concurrent(runtime.config.get("pipeline.max_concurrent", 2))
            if cookie_changed:
                _clear_probe_cache()
        if body.aria2c_enabled:
            runtime.refresh_aria2c()
        return {"ok": True}

    @router.get("/api/v1/engine/ytdlp")
    def ytdlp_status():
        """解析引擎版本状态：当前生效 / vendor 就绪 / 已知最新。"""
        from ..services import ytdlp_updater
        return ytdlp_updater.status()

    @router.post("/api/v1/engine/ytdlp/check")
    def ytdlp_check():
        """手动触发一次 yt-dlp 更新检查（绕过 12h 节流，同步等待结果）。"""
        from ..services import ytdlp_updater
        return ytdlp_updater.check_now()

    @router.get("/api/v1/fs/drives")
    def fs_drives():
        root = _browse_root()
        return {"drives": [str(root)], "home": str(root)}

    def _browse_root() -> Path:
        data = runtime.data_root.resolve()
        root = (data / "library").resolve()
        if root != data / "library":
            raise HTTPException(403, "媒体库目录不可浏览")
        return root

    @router.get("/api/v1/fs/list")
    def fs_list(path: str = Query("", max_length=2048)):
        """历史客户端只能浏览用户的即影媒体库，桌面用原生目录选择器。"""
        root = _browse_root()
        candidate = Path(path) if path else root
        if not candidate.is_absolute() or not candidate.is_relative_to(root) or ".." in candidate.parts:
            raise HTTPException(403, "仅允许浏览即影媒体库")
        try:
            target = candidate.resolve()
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(404, "目录不可用") from None
        if not target.is_relative_to(root):
            raise HTTPException(403, "仅允许浏览即影媒体库")
        if not target.is_dir():
            raise HTTPException(404, "目录不存在")

        def children():
            for entry in target.iterdir():
                try:
                    resolved = entry.resolve()
                    if resolved.is_relative_to(root) and resolved.is_dir():
                        yield {"name": entry.name, "path": str(resolved)}
                except (OSError, ValueError, RuntimeError):
                    continue

        limit = 500
        try:
            dirs = heapq.nsmallest(limit + 1, children(), key=lambda entry: entry["name"].casefold())
        except OSError:
            raise HTTPException(422, "无法读取目录") from None
        parent = str(target.parent) if target != root else ""
        return {"current": str(target), "parent": parent,
                "home": str(root), "dirs": dirs[:limit], "truncated": len(dirs) > limit}

    return router
