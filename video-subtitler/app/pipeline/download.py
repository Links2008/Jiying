"""下载与断点续传阶段。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ..core import cancel as task_cancel
from ..core.errors import AppError, Codes
from ..services.downloader.kernel import JobRecorder, SpeedMonitor
from ..services.engines.base import DownloadProgress
from .resources import resource_slot

log = logging.getLogger(__name__)


class DownloadStage:
    def _stage_download(self, task: dict) -> Path:
        tid = task["id"]
        task_cancel.raise_if_cancelled(tid)
        if self._store.stage_status(tid, "download") == "done":
            cached = [Path(a["path"]) for a in self._store.list_artifacts(tid)
                      if a["kind"] == "video" and not a["resolution"].startswith("anime-x")
                      and Path(a["path"]).exists()]
            if cached:
                log.info("阶段 download：checkpoint 命中，跳过下载")
                video = cached[0]
                if task.get("subtitle_mode") != "none" and not task.get("enhance") and video.parent != self._data / "tmp" / tid:
                    work = self._data / "tmp" / tid
                    work.mkdir(parents=True, exist_ok=True)
                    # 恢复来源与发布成品使用同样的冲突检查和可取消原子复制。
                    video = self._publish_file(tid, video, work, "video", registered=True)
                return video
        dest = self._data / "tmp" / tid
        for stage in self._store.list_stages(tid):
            if stage["stage"] == "download" and stage["status"] == "failed":
                # claim_retry 已清 task.error；在覆盖阶段错误前隔离旧版确认损坏的来源。
                from ..services.engines.ytdlp_engine import _quarantine_failed_split
                _quarantine_failed_split(dest, task.get("resolution") or "best",
                                         stage.get("error") or "")
        self._set_stage(tid, "download", "running")
        with resource_slot('io', tid):
            task_cancel.raise_if_cancelled(tid)
            return self._download_inner(task, tid, dest)

    def _download_inner(self, task: dict, tid: str, dest: Path) -> Path:
        monitor = SpeedMonitor(float(self._cfg.get("download.speed_floor_kbps", 500)))
        recorder = JobRecorder(self._store, tid)
        last_emit = [0.0]  # 进度节流：yt-dlp 钩子每秒触发上百次，全量推送会淹没前端

        # 解析链接、建立连接期间尚无字节可报：先推一条 0% 事件，避免前端进度
        # 长时间停在空白/0% 让人误以为卡死（首次使用的观感问题）
        self._set_live(tid, stage="download", fraction=0.0, speed_bps=0.0,
                       eta=0.0, note="正在解析链接并建立连接…")
        self._emit(type="progress", task_id=tid, stage="download", fraction=0.0,
                   speed_bps=0.0, eta=0.0, backend=None,
                   note="正在解析链接并建立连接…")

        def on_progress(p: DownloadProgress):
            task_cancel.raise_if_cancelled(tid)
            now = time.monotonic()
            if p.fraction < 1.0 and now - last_emit[0] < 0.25:
                return
            last_emit[0] = now
            monitor.sample(p.speed_bps)
            recorder.update(self._store, fraction=p.fraction, speed_bps=p.speed_bps,
                            backend=p.backend or None)
            self._set_live(tid, stage="download", fraction=p.fraction,
                           speed_bps=p.speed_bps, eta=float(p.eta or 0), note=p.note or "")
            self._emit(type="progress", task_id=tid, stage="download",
                       fraction=p.fraction, speed_bps=p.speed_bps,
                       eta=float(p.eta or 0), backend=p.backend, note=p.note)
            if monitor.is_stalled():
                log.warning("下载速度持续低于 %.0f KB/s —— 建议开启 download.aria2c.enabled",
                            float(self._cfg.get("download.speed_floor_kbps", 500)))

        try:
            engine_name, files = self._router.download(
                task["url"], task.get("resolution") or "best", dest, on_progress,
                task_id=tid)
            task_cancel.raise_if_cancelled(tid)
        except AppError as e:
            recorder.update(self._store, status="cancelled" if e.code == Codes.TASK_CANCELLED else "failed")
            raise
        recorder.update(self._store, status="completed", fraction=1.0)

        # 合并产物通常单文件；备用引擎可能返回分离流，取最大者当视频
        video = max(files, key=lambda p: p.stat().st_size)
        if task.get("subtitle_mode") == "none":
            output_dir = self._output_dir(task)
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                video = self._publish_file(tid, video, output_dir, "video",
                                           resolution=task.get("resolution") or "")
            except OSError as e:
                raise AppError(Codes.CFG_INVALID, f"输出目录不可用：{output_dir}",
                               detail=str(e),
                               suggestion="检查路径是否存在、是否有写入权限（含盘符拼写）") from e
        else:
            self._store.add_artifact(tid, "video", str(video), task.get("resolution") or "")
        self._set_stage(tid, "download", "done")
        log.info("下载完成（引擎 %s）：%s", engine_name, video.name)
        return video
