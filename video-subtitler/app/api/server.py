"""HTTP 控制面（可选依赖）：pip install fastapi uvicorn 后运行 `py -m app.api.server`。

提供任务提交/查询/产物列表 + SSE 实时事件流（/api/v1/events）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..core.config import load_config
from ..core.errors import AppError
from ..core.events import bus
from ..core.logging import setup_logging
from ..core.paths import app_root, data_dir
from ..models.db import Store
from ..pipeline.runner import PipelineRunner
from ..services.engines.router import EngineRouter
from ..services.subtitler import validate_srt

_store: Store | None = None
_runner: PipelineRunner | None = None
_router: EngineRouter | None = None
_config = None
_submit_lock = threading.Lock()
_gpu_job: dict = {}
_gpu_job_lock = threading.Lock()
log = logging.getLogger(__name__)


def _aria2c_alive(config) -> bool:
    """探测 aria2c RPC 是否已可用。"""
    from app.services.downloader.aria2c import Aria2cRpcClient
    from app.core.errors import AppError
    ac = config.get("download.aria2c", {})
    try:
        Aria2cRpcClient(str(ac.get("rpc_url", "")),
                        str(ac.get("secret") or "")).get_version()
        return True
    except AppError:
        return False


def _ensure_aria2c(config) -> None:
    """aria2c 未运行时拉起 bin/ 内置进程（随本服务退出而停止）。"""
    import os
    import subprocess
    from urllib.parse import urlparse
    from app.core.paths import bin_dir
    from app.services.downloader.aria2c import Aria2cRpcClient
    if not config.get("download.aria2c.enabled", False):
        return
    rpc_url = str(config.get("download.aria2c.rpc_url", "http://127.0.0.1:6800/jsonrpc"))
    try:
        Aria2cRpcClient(rpc_url, str(config.get("download.aria2c.secret") or "")).get_version()
        log.info("aria2c RPC 已在运行：%s", rpc_url)
        return
    except Exception:
        pass
    exe = bin_dir() / ("aria2c.exe" if os.name == "nt" else "aria2c")
    if not exe.exists():
        log.info("未找到内置 aria2c，直链将使用单线程下载")
        return
    port = urlparse(rpc_url).port or 6800
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        [str(exe), "--enable-rpc", f"--rpc-listen-port={port}",
         "--rpc-listen-all=false", "--quiet=true",
         f"--stop-with-process={os.getpid()}"],
        creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("已自动拉起内置 aria2c（端口 %d）", port)


class TaskIn(BaseModel):
    url: str
    resolution: str = "best"
    langs: list[str] = []
    recognize: bool = False
    translate: bool = False
    burn: bool = False
    output_dir: str = ""      # 自定义输出目录（空 = 默认数据目录）
    font_name: str = ""       # 硬字幕字体（空 = 配置默认）
    font_size: int = 0        # 硬字幕字号（0 = 配置默认）
    position: str = "bottom"  # 字幕位置 bottom/middle/top
    source_lang: str = ""     # 源语言（空 = 自动检测）
    outline: int = 1          # 硬字幕黑色描边粗细（0-8）
    bilingual: bool = False   # 双语上下堆叠烧录（中文在上、英文在下）
    color: str = "#FFFFFF"
    vertical_offset: int = 0
    horizontal_offset: int = 0
    enhance: bool = False     # 仅用于拒绝旧客户端的超分请求


class ProbeIn(BaseModel):
    url: str
    force_refresh: bool = False


class BilibiliLoginPollIn(BaseModel):
    session_id: str


class ClearDoneIn(BaseModel):
    with_files: bool = False   # true = 连带删除登记产物（同一套安全边界）
    kind: str = ""              # subtitle/video = 只删除该类文件，保留记录


class SubtitleIn(BaseModel):
    text: str


class DownloadConfigIn(BaseModel):
    proxy_mode: str | None = None        # system | direct | custom
    custom_proxy: str | None = None
    fragments_concurrency: int | None = None
    aria2c_enabled: bool | None = None
    cookies: str | None = None            # Netscape cookies.txt 路径（会员/4K 用）
    cookies_from_browser: str | None = None  # 直读浏览器登录态：chrome/edge/firefox
    max_concurrent: int | None = None    # 同时下载数（任务并发上限）
    model: str | None = None             # whisper: tiny/base/small/medium/large-v3
    transcriber_provider: str | None = None  # faster-whisper | cloud
    asr_vendor: str | None = None        # openai/siliconflow/zhipu/custom
    asr_base_url: str | None = None      # OpenAI 兼容接口根地址
    asr_api_key: str | None = None
    asr_model: str | None = None
    translator_provider: str | None = None  # null | llm
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_vendor: str | None = None        # deepseek/openai/siliconflow/zhipu/custom
    llm_base_url: str | None = None


def _mask_key(key: str) -> str:
    """API Key 掩码：保留前 3 位供辨认，其余隐藏。"""
    return (key[:3] + "***") if key else ""


def _artifact_or_404(aid: int) -> dict:
    a = _store.get_artifact(aid)
    if not a:
        raise HTTPException(404, "产物不存在")
    return a


# ---- Whisper 本地模型：软件内下载（带进度）+ 下载完立即载入 ----
_WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")
_whisper_jobs: dict[str, dict] = {}          # model -> {status, fraction, ...}
_whisper_jobs_lock = threading.Lock()
_whisper_probe: dict[str, tuple[float, bool]] = {}  # 已下载探测缓存（带 TTL）


def _whisper_job_get(model: str) -> dict:
    with _whisper_jobs_lock:
        return dict(_whisper_jobs.get(model) or {})


def _whisper_job_set(model: str, **kw) -> None:
    with _whisper_jobs_lock:
        _whisper_jobs.setdefault(model, {}).update(kw)


def _whisper_is_downloaded(model: str, ttl: float = 10.0) -> bool:
    """本地 HF 缓存里是否已有该模型（local_files_only，纯本地无网络）。"""
    if model not in _WHISPER_MODELS:
        return False
    now = time.monotonic()
    hit = _whisper_probe.get(model)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        from ..services.transcriber import local_whisper_model
        ok = local_whisper_model(model) is not None
    except Exception:
        ok = False
    _whisper_probe[model] = (now, ok)
    return ok


def _hf_progress_tqdm(callback):
    from ..services.transcriber import whisper_progress_tqdm
    return whisper_progress_tqdm(callback)


def _whisper_download_worker(model: str) -> None:
    """后台线程：下载模型（上报进度）→ 立即载入所需内存。"""
    downloaded = False
    try:
        from ..services.transcriber import download_whisper_model as fetch_model
        _whisper_job_set(model, status="downloading", fraction=0.0, error="",
                         downloaded=False, message="正在连接模型仓库…")
        fetch_model(_config, model, tqdm_class=_hf_progress_tqdm(
            lambda f: _whisper_job_set(model, fraction=round(f, 4))),
            on_status=lambda note: _whisper_job_set(model, message=note))
        downloaded = True
        _whisper_probe[model] = (time.monotonic(), True)
        _whisper_job_set(model, status="loading", fraction=1.0,
                          downloaded=True, message="下载完成，正在载入…")
        can_preload = callable(getattr(getattr(_runner, "_transcriber", None), "preload", None))
        if can_preload:
            _runner.preload_whisper(model)
        _whisper_job_set(model, status="ready" if can_preload else "downloaded",
                          downloaded=True, message="模型已就绪" if can_preload else "模型已下载")
        log.info("Whisper 模型 %s 下载完成（已载入=%s）", model, can_preload)
    except Exception as e:
        detail = str(e) if not isinstance(e, AppError) else e.detail or e.message
        message = "模型已下载，但载入失败" if downloaded else "下载失败"
        _whisper_job_set(model, status="load_failed" if downloaded else "failed",
                          downloaded=downloaded, error=detail, message=f"{message}：{detail}")
        log.error("Whisper 模型 %s %s: %s", model, message, detail)


def create_app() -> FastAPI:
    """构建 FastAPI 应用（供 uvicorn 或 pywebview 壳复用）。"""
    global _store, _runner, _router, _config
    setup_logging()
    _config = load_config()
    # HF 镜像端点（国内网络加速/解锁 Whisper 模型下载，如 https://hf-mirror.com）：
    # huggingface_hub 在首次导入时读取该环境变量，须赶在模型加载之前设置
    hf_ep = str(_config.get("transcriber.hf_endpoint", "") or "").strip()
    if hf_ep:
        os.environ["HF_ENDPOINT"] = hf_ep
        if "huggingface.co" not in hf_ep:
            # 镜像站不支持 Xet 传输协议（hf_xet 已随包分发），禁用后走普通 HTTP
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        log.info("HF 模型仓库端点: %s", hf_ep)
    _store = Store(data_dir() / "tasks.db")
    _router = EngineRouter(_config, _store)
    _runner = PipelineRunner(_config, _store)
    from app.pipeline import runner as _runner_mod
    _runner_mod.set_max_concurrent(_config.raw.get("pipeline", {}).get("max_concurrent", 2))

    # 启动恢复：上次进程退出时遗留的 running/pending 是僵尸任务
    try:
        _recovered = _store.recover_stale_running()
        if _recovered:
            print(f"[startup] 已将 {_recovered} 个僵尸任务标记为 interrupted（可在已完成列表重试）")
    except Exception as e:
        print(f"[startup] 僵尸任务恢复失败: {e}")

    app = FastAPI(title="VideoSubtitler", version=__version__)

    @app.get("/api/v1/instance")
    def instance():
        return {"engine_root": str(app_root())}

    @app.post("/api/v1/tasks")
    def submit(body: TaskIn):
        """提交任务：后台线程执行流水线（MVP 单进程线程池；量大换 Celery/arq）。"""
        if body.enhance:
            raise HTTPException(422, "Anime 超分已移除，请关闭超分后重新提交任务")
        if body.burn and (not re.fullmatch(r"#[0-9a-fA-F]{6}", body.color)
                          or not -120 <= body.vertical_offset <= 120
                          or not -270 <= body.horizontal_offset <= 270):
            raise HTTPException(422, "字幕颜色或位置偏移无效")
        if body.translate and not body.langs:
            raise HTTPException(422, "翻译字幕需要选择目标语言")
        try:
            engine, result = _router.probe(body.url)
        except AppError as e:
            raise HTTPException(422, e.to_dict())
        recognize = body.recognize or body.translate
        langs = body.langs if body.translate else []
        mode = ("burn" if body.burn else "softmux") if recognize else "none"
        options = dict(output_dir=body.output_dir, font_name=body.font_name,
                       font_size=body.font_size, position=body.position,
                       source_lang=body.source_lang, outline=body.outline,
                       bilingual=body.bilingual if recognize else False,
                       translate=body.translate, color=body.color,
                       vertical_offset=body.vertical_offset,
                       horizontal_offset=body.horizontal_offset)
        with _submit_lock:
            existing = _store.find_active_equivalent(dict(
                url=body.url, resolution=body.resolution, target_langs=",".join(langs),
                subtitle_mode=mode, **options))
            if existing:
                return existing
            task = _runner.create_task(body.url, body.resolution, langs, mode,
                                       title=result.title, **options)
            threading.Thread(target=_run, args=(task["id"],), daemon=True).start()
        return task

    @app.get("/api/v1/tasks")
    def list_tasks():
        # 附带阶段与实时进度快照：页面刷新/断线后靠轮询即可完整恢复卡片状态，
        # SSE 只做加速，不再是唯一状态来源
        return [{**t, "stages": _store.list_stages(t["id"]),
                 "progress": _runner.live_progress(t["id"])}
                for t in _store.list_tasks()]

    @app.post("/api/v1/tasks/{tid}/cancel")
    def cancel_task(tid: str):
        """取消任务：立即终止已登记的子进程，流水线在最近安全点退出。"""
        task = _store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] not in ("running", "pending"):
            raise HTTPException(409, f"任务已是 {task['status']} 状态，无需取消")
        from ..core import cancel as task_cancel
        task_cancel.cancel(tid)
        return {"ok": True, "task_id": tid}

    def _safe_delete_files(task: dict, kinds: set[str] | None = None
                           ) -> tuple[list[str], list[str], list[int]]:
        """连带删除产物文件 —— 防误删四层安全边界：
        1) 只删 artifact 表登记过的文件，逐个删，绝不递归扫目录；
        2) 每个路径 resolve() 后必须位于允许根内（任务 output_dir / 默认
           library / 数据 tmp），越界一律跳过并记日志；
        3) 文件不存在或删除失败只跳过，绝不阻断记录删除；
        4) 结果（deleted/skipped）如实返回，UI 可见可核对。
        """
        candidates: list[Path] = []
        custom = (task.get("output_dir") or "").strip()
        if custom:
            candidates.append(Path(custom))
        candidates += [data_dir() / "library", data_dir() / "tmp"]
        roots = []
        for r in candidates:
            try:
                roots.append(r.resolve())
            except OSError:
                pass
        deleted: list[str] = []
        skipped: list[str] = []
        removable: list[int] = []
        for a in _store.list_artifacts(task["id"]):
            if kinds is not None and a["kind"] not in kinds:
                continue
            raw = (a.get("path") or "").strip()
            if not raw:
                continue
            try:
                p = Path(raw).resolve()
            except OSError:
                skipped.append(f"{raw}（路径无法解析）")
                continue
            if not any(p == r or r in p.parents for r in roots):
                skipped.append(f"{p}（越界路径，已跳过）")
                log.warning("连带删除拦截越界路径：%s", p)
                continue
            if _store.artifact_owned_by_other(task["id"], raw):
                skipped.append(f"{p}（其他任务仍在使用）")
                continue
            try:
                if p.is_file():
                    p.unlink()
                    deleted.append(str(p))
                    removable.append(a["id"])
                elif not p.exists():
                    removable.append(a["id"])
                else:
                    skipped.append(f"{p}（不是文件）")
            except OSError as e:
                skipped.append(f"{p}（删除失败：{e}）")
        return deleted, skipped, removable

    @app.delete("/api/v1/tasks/{tid}")
    def delete_task(tid: str, with_files: bool = False):
        """删除任务记录并回收工作文件（tmp 与转写缓存）。

        with_files=true 时连带删除 artifact 登记的产物文件（安全边界见
        _safe_delete_files）；默认 false，行为与旧版完全兼容。
        """
        task = _store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        if not _store.claim_terminal_task(tid):
            raise HTTPException(409, "任务进行中，先取消再删除")
        try:
            deleted, skipped, _ = _safe_delete_files(task) if with_files else ([], [], [])
            _runner.cleanup_task_files(tid)
            _store.delete_task(tid)
        except Exception:
            _store.update_task(tid, status=task["status"], finished_at=task.get("finished_at", ""))
            raise
        return {"ok": True, "task_id": tid,
                "deleted_files": deleted, "skipped": skipped}

    @app.delete("/api/v1/tasks/{tid}/artifacts")
    def delete_task_artifacts(tid: str, kind: str):
        if kind not in ("subtitle", "video"):
            raise HTTPException(400, "只能选择字幕或视频")
        task = _store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        if not _store.claim_terminal_task(tid):
            raise HTTPException(409, "任务进行中，先取消再删除")
        try:
            kinds = {"subtitle", "bilingual"} if kind == "subtitle" else {"video", "output"}
            deleted, skipped, removable = _safe_delete_files(task, kinds)
            _store.delete_artifacts(tid, removable)
            return {"ok": True, "task_id": tid,
                    "deleted_files": deleted, "skipped": skipped}
        finally:
            _store.update_task(tid, status=task["status"],
                               finished_at=task.get("finished_at", ""))

    @app.post("/api/v1/tasks/clear_done")
    def clear_done(body: ClearDoneIn | None = None):
        """批量清除终态任务（completed/failed/cancelled/interrupted）。

        with_files=true 时对每个任务走同一套连带删除安全边界。
        """
        with_files = bool(body and body.with_files)
        kind = body.kind if body else ""
        if kind not in ("", "subtitle", "video"):
            raise HTTPException(400, "只能选择字幕或视频")
        cleared, deleted, skipped = 0, [], []
        for t in _store.list_tasks():
            if t["status"] not in ("completed", "failed", "cancelled", "interrupted"):
                continue
            try:
                result = (delete_task_artifacts(t["id"], kind)
                          if kind else delete_task(t["id"], with_files))
            except HTTPException as exc:
                if exc.status_code in (404, 409):
                    continue
                raise
            deleted += result["deleted_files"]
            skipped += result["skipped"]
            cleared += 1
        return {"ok": True, "cleared": cleared,
                "deleted_files": deleted, "skipped": skipped}

    @app.get("/api/v1/engine/ytdlp")
    def ytdlp_status():
        """解析引擎版本状态：当前生效 / vendor 就绪 / 已知最新。"""
        from ..services import ytdlp_updater
        return ytdlp_updater.status()

    @app.post("/api/v1/engine/ytdlp/check")
    def ytdlp_check():
        """手动触发一次 yt-dlp 更新检查（绕过 12h 节流，同步等待结果）。"""
        from ..services import ytdlp_updater
        return ytdlp_updater.check_now()

    @app.post("/api/v1/tasks/{tid}/retry")
    def retry_task(tid: str):
        """重试失败/已取消的任务：checkpoint 机制自动从断点继续。"""
        task = _store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        with _submit_lock:
            if _store.find_active_equivalent(task):
                raise HTTPException(409, "相同设置的下载任务正在运行，请查看现有任务")
            if not _store.claim_retry(tid):
                raise HTTPException(409, "任务正在运行或已成功完成，无需重试")
            threading.Thread(target=_run, args=(tid,), daemon=True).start()
        return {"ok": True, "task_id": tid}

    @app.get("/api/v1/preview/subtitle")
    def subtitle_preview(text: str = "字幕预览 Subtitle 123",
                          font_name: str = "", font_size: int = 24,
                          position: str = "bottom", outline: int = 1,
                          color: str = "#FFFFFF", vertical_offset: int = 0,
                          horizontal_offset: int = 0):
        """字幕样式预览帧：按所选字体/字号/位置渲染样例文本（PNG）。

        结果按参数哈希缓存，同参数秒回。
        """
        import hashlib
        from app.services.renderer import render_preview_png
        pos = position if position in ("bottom", "middle", "top") else "bottom"
        if (not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
                or not -120 <= vertical_offset <= 120
                or not -270 <= horizontal_offset <= 270):
            raise HTTPException(422, "字幕颜色或位置偏移无效")
        try:
            width = max(0, min(8, int(outline)))
        except (TypeError, ValueError):
            width = 1
        key = hashlib.sha1(
            f"{text}|{font_name}|{font_size}|{pos}|{width}|{color}|{vertical_offset}|{horizontal_offset}".encode(),
            usedforsecurity=False).hexdigest()
        cache_dir = data_dir() / "cache" / "preview"
        png = cache_dir / f"{key}.png"
        if not png.exists():
            try:
                render_preview_png(_config, text, font_name, font_size, pos, png,
                                   outline=width, color=color,
                                   vertical_offset=vertical_offset,
                                   horizontal_offset=horizontal_offset)
            except AppError as e:
                raise HTTPException(500, e.to_dict())
        return FileResponse(png, media_type="image/png")

    @app.get("/api/v1/tasks/{tid}")
    def task_detail(tid: str):
        task = _store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        arts = []
        for a in _store.list_artifacts(tid):
            try:
                size = Path(a["path"]).stat().st_size
            except OSError:
                size = 0  # exists/stat 之间存在竞态，缺文件按 0 处理
            arts.append({**a, "size": size})
        return {**task, "stages": _store.list_stages(tid),
                "progress": _runner.live_progress(tid), "artifacts": arts}

    @app.post("/api/v1/probe")
    def probe(body: ProbeIn):
        """两阶段交互第一步：探测可用分辨率（缓存 24h）。"""
        try:
            engine, result = _router.probe(body.url, use_cache=not body.force_refresh)
        except AppError as e:
            raise HTTPException(422, e.to_dict())
        return {"engine": engine.name, "title": result.title,
                "duration": result.duration,
                "thumbnail": getattr(result, "thumbnail", ""),
                "quality_notice": result.quality_notice,
                "auth_status": result.auth_status,
                "formats": [asdict(f) for f in result.formats]}

    @app.get("/api/v1/artifacts/{aid}/content")
    def artifact_content(aid: int):
        """读取字幕文本（编辑器用）。"""
        a = _artifact_or_404(aid)
        if a["kind"] not in ("subtitle", "bilingual"):
            raise HTTPException(400, "仅字幕产物支持文本读写")
        p = Path(a["path"])
        if not p.exists():
            raise HTTPException(404, "字幕文件不存在")
        return {"text": p.read_text(encoding="utf-8-sig", errors="replace")}

    @app.put("/api/v1/artifacts/{aid}/content")
    def save_artifact_content(aid: int, body: SubtitleIn):
        """保存编辑后的字幕（仅 SRT；文件路径取自数据库，杜绝路径穿越）。"""
        a = _artifact_or_404(aid)
        if a["kind"] not in ("subtitle", "bilingual") or not str(a["path"]).lower().endswith(".srt"):
            raise HTTPException(400, "仅支持编辑 SRT 字幕文件")
        path = Path(a["path"])
        if not path.is_file():
            raise HTTPException(404, "字幕文件不存在，请重新生成字幕")
        if _store.artifact_owned_by_other(a["task_id"], str(path)):
            raise HTTPException(409, "此字幕被多个历史任务共用，无法直接覆盖；请先保留独立副本")
        try:
            validate_srt(body.text)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        task = _store.get_task(a["task_id"])
        if not task or not _store.claim_terminal_task(a["task_id"]):
            raise HTTPException(409, "任务正在处理，请完成后再修改字幕")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="\n",
                                             dir=path.parent, prefix=".subtitle-", suffix=".tmp",
                                             delete=False) as f:
                temporary = Path(f.name)
                f.write(body.text)
            temporary.replace(path)
        except OSError as e:
            raise HTTPException(500, "保存字幕失败，请检查文件权限或占用情况") from e
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.warning("字幕临时文件清理失败：%s", temporary)
            _store.update_task(task["id"], status=task["status"], finished_at=task["finished_at"])
        return {"ok": True, "path": a["path"]}

    @app.get("/api/v1/artifacts/{aid}/file")
    def artifact_file(aid: int):
        """产物文件下载（按数据库 id 映射路径，不接受客户端路径）。"""
        a = _artifact_or_404(aid)
        p = Path(a["path"])
        if not p.exists():
            raise HTTPException(404, "文件不存在")
        return FileResponse(p, filename=p.name)

    @app.post("/api/v1/tasks/{tid}/rerender")
    def rerender(tid: str, body: dict | None = None):
        """字幕编辑后重渲染：复用视频与 SRT，仅重跑 render 阶段。"""
        body = body or {}
        lang = body.get("lang") or ""
        artifact_id = body.get("artifact_id")
        background = body.get("background", False)
        if not isinstance(lang, str) or (artifact_id is None and not lang):
            raise HTTPException(400, "请选择要用于重新生成视频的字幕")
        if artifact_id is not None and (type(artifact_id) is not int or artifact_id <= 0):
            raise HTTPException(400, "字幕产物编号无效")
        if type(background) is not bool:
            raise HTTPException(400, "background 参数必须为布尔值")
        try:
            outputs = _runner.rerender_subtitles(tid, lang, artifact_id=artifact_id, background=background)
        except AppError as e:
            raise HTTPException(422, e.to_dict())
        return {"ok": True, "status": "running" if background else "completed",
                "outputs": [str(o) for o in outputs]}

    @app.get("/api/v1/fs/drives")
    def fs_drives():
        """列出本机所有磁盘盘符（目录选择器用）。Windows 枚举逻辑盘符。"""
        import os
        import string
        drives: list[str] = []
        if os.name == "nt":
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i, letter in enumerate(string.ascii_uppercase):
                if bitmask >> i & 1:
                    drives.append(f"{letter}:\\")
        else:
            drives = ["/"]
        return {"drives": drives, "home": str(Path.home())}

    @app.get("/api/v1/fs/list")
    def fs_list(path: str = ""):
        """列出某目录下的子文件夹（目录选择器逐级导航）。只读目录名，不碰文件内容。"""
        target = Path(path) if path else Path.home()
        if not target.exists() or not target.is_dir():
            raise HTTPException(404, "目录不存在")
        dirs = []
        try:
            for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if entry.is_dir():
                        dirs.append({"name": entry.name, "path": str(entry)})
                except OSError:
                    continue  # 个别系统目录无权限，跳过即可
        except OSError as e:
            raise HTTPException(422, f"无法读取目录：{e}")
        parent = str(target.parent) if target.parent != target else ""
        return {"current": str(target), "parent": parent,
                "home": str(Path.home()), "dirs": dirs}

    @app.post("/api/v1/tasks/{tid}/reveal")
    def reveal_task_folder(tid: str):
        """在系统文件管理器中打开该任务产物所在文件夹（并尽量选中该文件）。

        路径只从数据库取，绝不接受客户端传入路径，杜绝任意路径打开。
        """
        import subprocess
        task = _store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        arts = _store.list_artifacts(tid)
        pick = next((a for a in arts if a["kind"] == "output"), None) \
            or next((a for a in arts if a["kind"] == "video"), None) \
            or (arts[0] if arts else None)
        target: Path | None = None
        if pick:
            target = Path(pick["path"])
        elif (task.get("output_dir") or "").strip():
            target = Path(task["output_dir"])
        if target is None:
            raise HTTPException(422, "该任务暂无可打开的目录")
        if target.is_file():
            folder, select = target.parent, target
        elif target.is_dir():
            folder, select = target, None
        else:
            folder, select = target.parent, None
        if not folder.is_dir():
            raise HTTPException(422, f"目录不存在：{folder}")
        if os.name == "nt":
            if select is not None:
                subprocess.Popen(f'explorer /select,"{select}"')
            else:
                subprocess.Popen(f'explorer "{folder}"')
        else:
            import sys as _sys
            opener = "open" if _sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(folder)])
        return {"ok": True, "folder": str(folder), "path": str(target)}

    def _clear_probe_cache():
        """登录态变更后清空探测缓存：否则旧的「只有 1080p」列表会继续命中
        24h 缓存，用户补了 cookies 也看不到 4K。"""
        try:
            import shutil
            p = data_dir() / "cache" / "probe"
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
        try:
            if _store:
                _store.cache_purge_kind("probe")
        except Exception:
            pass

    def _save_config():
        """保守合并写回，保留用户已有配置键。"""
        import yaml
        from app.core.config import default_config_path, deep_merge
        path = default_config_path()
        existing = {}
        if path.exists():
            try:
                existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                existing = {}
        deep_merge(existing, _config.raw)
        path.write_text(yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")

    def _connect_bilibili(cookie_path: Path):
        previous = (_config.raw.get("cookies", ""), _config.raw.get("cookies_from_browser", ""))
        _config.raw.update(cookies=str(cookie_path), cookies_from_browser="")
        try:
            _save_config()
        except Exception:
            _config.raw.update(cookies=previous[0], cookies_from_browser=previous[1])
            raise
        _clear_probe_cache()

    from ..services.bilibili_login import BilibiliLogin
    bilibili_login = BilibiliLogin(_config, data_dir() / "bilibili-cookies.txt", _connect_bilibili)

    @app.post("/api/v1/bilibili/login/start")
    def start_bilibili_login():
        try:
            return bilibili_login.start()
        except AppError as e:
            raise HTTPException(503, e.to_dict()) from None

    @app.post("/api/v1/bilibili/login/poll")
    def poll_bilibili_login(body: BilibiliLoginPollIn):
        try:
            return bilibili_login.poll(body.session_id)
        except AppError as e:
            raise HTTPException(503, e.to_dict()) from None

    @app.post("/api/v1/platform/session")
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

    @app.get("/api/v1/config")
    def get_download_config():
        """返回前端可调的下载设置。"""
        d = _config.raw.get("download", {})
        t = _config.raw.get("translator", {})
        tc = _config.raw.get("transcriber", {})
        cloud = tc.get("cloud") or {}
        return {"download": {
            "proxy_mode": d.get("proxy_mode", "system"),
            "custom_proxy": d.get("custom_proxy", ""),
            "fragments_concurrency": d.get("fragments_concurrency", 8),
            "aria2c_enabled": (d.get("aria2c") or {}).get("enabled", False),
            "cookies": _config.raw.get("cookies", ""),
            "cookies_from_browser": _config.raw.get("cookies_from_browser", "")},
            "max_concurrent": _config.raw.get("pipeline", {}).get("max_concurrent", 2),
            "model": tc.get("model", "small"),
            "transcriber_provider": tc.get("provider", "faster-whisper"),
            "asr_vendor": cloud.get("vendor", ""),
            "asr_base_url": cloud.get("base_url", ""),
            "asr_api_key_masked": _mask_key(cloud.get("api_key", "")),
            "asr_model": cloud.get("model", ""),
            "translator_provider": t.get("provider", "null"),
            "llm_api_key_masked": _mask_key((t.get("llm") or {}).get("api_key", "")),
            "llm_model": (t.get("llm") or {}).get("model", ""),
            "llm_vendor": (t.get("llm") or {}).get("vendor", ""),
            "llm_base_url": (t.get("llm") or {}).get("base_url", "")}

    @app.post("/api/v1/config")
    def set_download_config(body: DownloadConfigIn):
        """更新下载设置：运行时立即生效，并持久化到 config.yaml。"""
        d = _config.raw.setdefault("download", {})
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
        if body.cookies is not None and body.cookies.strip() != _config.raw.get("cookies", ""):
            _config.raw["cookies"] = body.cookies.strip()
            cookie_changed = True
        if (body.cookies_from_browser is not None
                and body.cookies_from_browser.strip() != _config.raw.get("cookies_from_browser", "")):
            _config.raw["cookies_from_browser"] = body.cookies_from_browser.strip()
            cookie_changed = True
        if cookie_changed:
            _clear_probe_cache()
        if body.max_concurrent is not None:
            _config.raw.setdefault("pipeline", {})["max_concurrent"] = max(1, min(4, body.max_concurrent))
            from app.pipeline import runner as _runner_mod
            _runner_mod.set_max_concurrent(body.max_concurrent)
        tc = _config.raw.setdefault("transcriber", {})
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
        if body.asr_api_key:
            cloud["api_key"] = body.asr_api_key.strip()
        if body.asr_model:
            cloud["model"] = body.asr_model.strip()
        if body.transcriber_provider:  # Provider 变更 → 重建识别后端
            _runner.reload_transcriber()
        t = _config.raw.setdefault("translator", {})
        if body.translator_provider in ("null", "llm"):
            t["provider"] = body.translator_provider
        if body.llm_api_key:
            t.setdefault("llm", {})["api_key"] = body.llm_api_key.strip()
        # 留空 = 保持已保存的 Key 不变（前端不再回显明文）
        if body.llm_model:
            t.setdefault("llm", {})["model"] = body.llm_model.strip()
        if body.llm_vendor is not None:
            t.setdefault("llm", {})["vendor"] = body.llm_vendor.strip()
        if body.llm_base_url:
            t.setdefault("llm", {})["base_url"] = body.llm_base_url.strip()
        _save_config()
        if body.aria2c_enabled:
            threading.Thread(target=_ensure_aria2c, args=(_config,), daemon=True).start()
        return {"ok": True}

    @app.get("/api/v1/events")
    async def events():
        """SSE 事件流：进度/阶段/任务事件（前端 EventSource 直连，15s 心跳）。"""
        q = bus.subscribe()

        async def gen():
            loop = asyncio.get_running_loop()
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        ev = await loop.run_in_executor(None, q.get, True, 15)
                        yield ev.to_sse()
                    except queue.Empty:
                        yield ": keep-alive\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.post("/api/v1/whisper/download")
    def download_whisper_model(body: dict):
        """软件内下载 faster-whisper 模型：后台执行（带进度），完成后立即载入。"""
        model = str((body or {}).get("model") or "small").strip()
        if model not in _WHISPER_MODELS:
            raise HTTPException(400, f"不支持的模型: {model}")
        with _whisper_jobs_lock:
            job = _whisper_jobs.get(model) or {}
            if job.get("status") in ("downloading", "loading"):
                return {"ok": True, "model": model, "status": job["status"],
                        "started": False, "message": job.get("message", "")}
            _whisper_probe.pop(model, None)
            _whisper_jobs[model] = dict(status="downloading", fraction=0.0, error="",
                                       downloaded=False, message="正在连接模型仓库…")
        try:
            threading.Thread(target=_whisper_download_worker, args=(model,),
                             daemon=True).start()
        except Exception as e:
            _whisper_job_set(model, status="failed", error=str(e),
                             message=f"无法启动下载：{e}")
            raise HTTPException(503, "无法启动模型下载，请重试") from e
        return {"ok": True, "model": model, "status": "downloading",
                "started": True, "message": "正在连接模型仓库…"}

    @app.get("/api/v1/whisper/status")
    def whisper_status(model: str = "small"):
        """模型状态：是否已下载 + 当前下载/载入进度（设置页据此显示）。"""
        job = _whisper_job_get(model) if model in _WHISPER_MODELS else {}
        downloaded = _whisper_is_downloaded(model)
        return {"model": model, "downloaded": downloaded,
                "status": job.get("status", "idle"),
                "fraction": job.get("fraction", 0.0),
                "message": job.get("message", ""),
                "error": job.get("error", "")}

    @app.get("/api/v1/gpu/status")
    def gpu_status():
        from ..core.paths import bin_dir
        from ..services.gpu_component import runtime_present
        from ..services.transcriber import _has_cuda
        bundle, cache = bin_dir() / 'cuda', data_dir() / 'cache' / 'cuda'
        downloaded = runtime_present(bundle, cache)
        with _gpu_job_lock:
            job = dict(_gpu_job)
        if job.get('status') == 'ready' and not downloaded:
            job = {'status': 'idle', 'message': 'GPU 组件需要重新下载；CPU 仍可使用'}
        return {'supported': _has_cuda(), 'downloaded': downloaded,
                'status': job.get('status', 'ready' if downloaded else 'idle'),
                'fraction': job.get('fraction', 0.0), 'message': job.get('message', ''),
                'error': job.get('error', ''), 'size_bytes': 553162896}

    @app.post("/api/v1/gpu/download")
    def download_gpu():
        from ..core.paths import bin_dir
        from ..services.gpu_component import download_gpu_runtime
        from ..services.transcriber import _has_cuda
        if not _has_cuda():
            raise HTTPException(400, '未检测到可用 NVIDIA 显卡，当前使用 CPU 识别')
        with _gpu_job_lock:
            if _gpu_job.get('status') == 'downloading':
                return {'ok': True, 'started': False}
            _gpu_job.update(status='downloading', fraction=0.0, error='', message='正在连接 NVIDIA 组件仓库…')
        def update(fraction, message):
            with _gpu_job_lock:
                _gpu_job.update(fraction=fraction, message=message)
        def worker():
            try:
                download_gpu_runtime(bin_dir() / 'cuda', data_dir() / 'cache' / 'cuda', update)
                with _gpu_job_lock:
                    _gpu_job.update(status='ready', fraction=1.0, error='')
            except Exception as exc:
                with _gpu_job_lock:
                    _gpu_job.update(status='failed', error=str(exc), message='GPU 组件下载失败，CPU 仍可使用')
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            with _gpu_job_lock:
                _gpu_job.update(status='failed', error=str(exc))
            raise HTTPException(503, '无法启动 GPU 组件下载，请重试') from exc
        return {'ok': True, 'started': True}

    # 内嵌 Web UI（路由注册之后挂载，静态站不抢占 API）
    web_dir = Path(__file__).resolve().parents[1] / "web"
    if (web_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    @app.middleware("http")
    async def local_guard(request, call_next):
        """本机服务防护（无 token 的轻量方案）：

        1) Host 必须是本机回环 —— 防 DNS rebinding（恶意域名解析到 127.0.0.1）；
        2) 写请求（POST/PUT/DELETE）若带 Origin 则必须与 Host 同源 —— 防恶意
           网页发起 CSRF（下载任意 URL 并写入任意 output_dir / 枚举文件夹 /
           读取 API Key）。curl/CLI 等非浏览器客户端不带 Origin，不受影响。
        """
        from fastapi.responses import JSONResponse
        from urllib.parse import urlparse
        host = request.headers.get("host", "")
        hostname = (urlparse(f"//{host}").hostname or "").lower() if host else ""
        if hostname and hostname not in ("127.0.0.1", "localhost", "::1"):
            return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and request.method in ("POST", "PUT", "DELETE"):
            if urlparse(origin).netloc != host:
                return JSONResponse({"detail": "跨站请求已拒绝"}, status_code=403)
        return await call_next(request)

    # 直链多线程加速：随服务自动拉起内置 aria2c（不可达时下载自动降级原生）
    threading.Thread(target=_ensure_aria2c, args=(_config,), daemon=True).start()

    # yt-dlp 热更新：同步挂载已下载的新版（纯文件操作、无网络），
    # 后台线程跟随上游 PyPI 版本，站点解析规则过期自动换新，全程不阻塞启动
    from ..services import ytdlp_updater
    try:
        ytdlp_updater.activate_vendored()
    except Exception as e:
        log.warning("挂载热更新 yt-dlp 失败（使用内置版本）：%s", e)
    threading.Thread(target=ytdlp_updater.auto_update, args=(_config,), daemon=True).start()

    return app


def _run(task_id: str) -> None:
    task = _store.get_task(task_id)
    if task:
        try:
            _runner.run(task)
        except Exception:  # 失败状态已由 runner 写库，此处仅防线程崩
            pass


def _port_free(port: int) -> bool:
    """端口空闲判定：Windows 上绑定试探不可靠（SO_REUSEADDR 允许双绑），
    唯一可靠的探测是真实发起一次 TCP 连接 —— 能连上 = 被占用。"""
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _pick_port(preferred: int = 8765) -> int:
    """从首选端口起顺延找真正无冲突的端口。"""
    for port in range(preferred, preferred + 20):
        if _port_free(port):
            return port
    raise RuntimeError("8765~8784 均被占用，未找到可用端口")


def main() -> None:
    import uvicorn  # 可选依赖，延迟导入
    port = _pick_port()
    print(f"VideoSubtitler 服务已启动：http://127.0.0.1:{port}")  # run 阻塞，须先打印
    # access_log 关闭：前端每 2.5s 轮询一次任务列表，访问日志纯属噪音与磁盘负担
    uvicorn.run(create_app(), host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
