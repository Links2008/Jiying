"""语音模型与 GPU 组件的状态和后台下载入口。"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException

from ..core.config import Config
from ..core.errors import AppError
from ..core.workers import WorkerPool

if TYPE_CHECKING:
    from ..pipeline.runner import PipelineRunner
    from .runtime import EngineRuntime

log = logging.getLogger(__name__)
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")


def hf_progress_tqdm(callback):
    from ..services.whisper_models import whisper_progress_tqdm
    return whisper_progress_tqdm(callback)


class Components:
    def __init__(self, config: Config, runner: PipelineRunner | None, pool: WorkerPool | None = None):
        self.config = config
        self.runner = runner
        self.whisper_jobs: dict[str, dict] = {}
        self.whisper_probe: dict[str, tuple[float, bool]] = {}
        self.whisper_lock = threading.Lock()
        self.gpu_job: dict = {}
        self.gpu_lock = threading.Lock()
        self._pool = pool or WorkerPool("jiying-components", 2, 8)
        self.submit_work = self._pool.submit

    def busy(self) -> bool:
        with self.whisper_lock, self.gpu_lock:
            return (any(job.get("status") in {"downloading", "loading"} for job in self.whisper_jobs.values())
                    or self.gpu_job.get("status") == "downloading")

    def get_whisper_job(self, model: str) -> dict:
        with self.whisper_lock:
            return dict(self.whisper_jobs.get(model) or {})

    def set_whisper_job(self, model: str, **kw) -> None:
        with self.whisper_lock:
            self.whisper_jobs.setdefault(model, {}).update(kw)

    def whisper_is_downloaded(self, model: str, ttl: float = 10.0) -> bool:
        """本地 HF 缓存里是否已有该模型（local_files_only，纯本地无网络）。"""
        if model not in WHISPER_MODELS:
            return False
        now = time.monotonic()
        hit = self.whisper_probe.get(model)
        if hit and now - hit[0] < ttl:
            return hit[1]
        try:
            from ..services.whisper_models import local_whisper_model
            ok = local_whisper_model(model) is not None
        except Exception:
            ok = False
        self.whisper_probe[model] = (now, ok)
        return ok

    def download_whisper(self, model: str) -> None:
        """后台线程：下载模型（上报进度）→ 立即载入所需内存。"""
        downloaded = False
        try:
            from ..services.whisper_models import download_whisper_model as fetch_model
            self.set_whisper_job(model, status="downloading", fraction=0.0, error="",
                             downloaded=False, message="正在连接模型仓库…")
            fetch_model(self.config, model, tqdm_class=hf_progress_tqdm(
                lambda f: self.set_whisper_job(model, fraction=round(f, 4))),
                on_status=lambda note: self.set_whisper_job(model, message=note))
            downloaded = True
            self.whisper_probe[model] = (time.monotonic(), True)
            self.set_whisper_job(model, status="loading", fraction=1.0,
                              downloaded=True, message="下载完成，正在载入…")
            can_preload = callable(getattr(getattr(self.runner, "_transcriber", None), "preload", None))
            if can_preload:
                self.runner.preload_whisper(model)
            self.set_whisper_job(model, status="ready" if can_preload else "downloaded",
                              downloaded=True, message="模型已就绪" if can_preload else "模型已下载")
            log.info("Whisper 模型 %s 下载完成（已载入=%s）", model, can_preload)
        except Exception as e:
            detail = str(e) if not isinstance(e, AppError) else e.detail or e.message
            message = "模型已下载，但载入失败" if downloaded else "下载失败"
            self.set_whisper_job(model, status="load_failed" if downloaded else "failed",
                              downloaded=downloaded, error=detail, message=f"{message}：{detail}")
            log.error("Whisper 模型 %s %s: %s", model, message, detail)


def component_routes(runtime: EngineRuntime) -> APIRouter:
    router = APIRouter()
    components = runtime.components

    @router.post("/api/v1/whisper/download")
    def download_whisper_model(body: dict):
        """软件内下载 faster-whisper 模型：后台执行（带进度），完成后立即载入。"""
        model = str((body or {}).get("model") or "small").strip()
        if model not in WHISPER_MODELS:
            raise HTTPException(400, f"不支持的模型: {model}")
        with components.whisper_lock:
            if runtime.exiting:
                raise HTTPException(503, "本地引擎正在退出")
            job = components.whisper_jobs.get(model) or {}
            if job.get("status") in ("downloading", "loading"):
                return {"ok": True, "model": model, "status": job["status"],
                        "started": False, "message": job.get("message", "")}
            components.whisper_probe.pop(model, None)
            components.whisper_jobs[model] = dict(status="downloading", fraction=0.0, error="",
                                       downloaded=False, message="正在连接模型仓库…")
        try:
            components.submit_work(components.download_whisper, model)
        except Exception as e:
            components.set_whisper_job(model, status="failed", error=str(e),
                             message=f"无法启动下载：{e}")
            raise HTTPException(503, "无法启动模型下载，请重试") from e
        return {"ok": True, "model": model, "status": "downloading",
                "started": True, "message": "正在连接模型仓库…"}

    @router.get("/api/v1/whisper/status")
    def whisper_status(model: str = "small"):
        """模型状态：是否已下载 + 当前下载/载入进度（设置页据此显示）。"""
        job = components.get_whisper_job(model) if model in WHISPER_MODELS else {}
        downloaded = components.whisper_is_downloaded(model)
        return {"model": model, "downloaded": downloaded,
                "status": job.get("status", "idle"),
                "fraction": job.get("fraction", 0.0),
                "message": job.get("message", ""),
                "error": job.get("error", "")}

    @router.get("/api/v1/gpu/status")
    def gpu_status():
        from ..core.paths import bin_dir
        from ..services.gpu_component import runtime_download_size, runtime_present
        from ..services.speech_cuda import _has_cuda
        bundle, cache = bin_dir() / 'cuda', runtime.data_root / 'cache' / 'cuda'
        downloaded = runtime_present(bundle, cache)
        with components.gpu_lock:
            job = dict(components.gpu_job)
        if job.get('status') == 'ready' and not downloaded:
            job = {'status': 'idle', 'message': 'GPU 组件需要重新下载；CPU 仍可使用'}
        return {'supported': _has_cuda(), 'downloaded': downloaded,
                'status': job.get('status', 'ready' if downloaded else 'idle'),
                'fraction': job.get('fraction', 0.0), 'message': job.get('message', ''),
                'error': job.get('error', ''), 'size_bytes': runtime_download_size(bundle)}

    @router.post("/api/v1/gpu/download")
    def download_gpu():
        from ..core.paths import bin_dir
        from ..services.gpu_component import download_gpu_runtime
        from ..services.speech_cuda import _has_cuda
        if not _has_cuda():
            raise HTTPException(400, '未检测到可用 NVIDIA 显卡，当前使用 CPU 识别')
        with components.gpu_lock:
            if runtime.exiting:
                raise HTTPException(503, "本地引擎正在退出")
            if components.gpu_job.get('status') == 'downloading':
                return {'ok': True, 'started': False}
            components.gpu_job.update(status='downloading', fraction=0.0, error='', message='正在连接 NVIDIA 组件仓库…')
        def update(fraction, message):
            with components.gpu_lock:
                components.gpu_job.update(fraction=fraction, message=message)
        def worker():
            try:
                download_gpu_runtime(bin_dir() / 'cuda', runtime.data_root / 'cache' / 'cuda', update)
                with components.gpu_lock:
                    components.gpu_job.update(status='ready', fraction=1.0, error='')
            except Exception as exc:
                with components.gpu_lock:
                    components.gpu_job.update(status='failed', error=str(exc), message='GPU 组件下载失败，CPU 仍可使用')
        try:
            components.submit_work(worker)
        except Exception as exc:
            with components.gpu_lock:
                components.gpu_job.update(status='failed', error=str(exc))
            raise HTTPException(503, '无法启动 GPU 组件下载，请重试') from exc
        return {'ok': True, 'started': True}

    return router
