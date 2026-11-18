"""任务提交、列表、重试、删除与系统目录入口。"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response

from ..core.errors import AppError
from ..core.events import AppEvent
from .runtime import EngineRuntime
from .schemas import ClearDoneIn, TaskIn

log = logging.getLogger(__name__)


def task_routes(runtime: EngineRuntime) -> APIRouter:
    router = APIRouter()
    # 后台保存、删除和重试沿用同一份任务锁。

    @router.post("/api/v1/tasks")
    def submit(body: TaskIn):
        """提交任务：后台线程执行流水线（MVP 单进程线程池；量大换 Celery/arq）。"""
        from ..services.transcriber import normalize_asr_language, valid_language_code
        if (any(not valid_language_code(lang) for lang in body.langs)
                or body.source_lang and normalize_asr_language(body.source_lang) == 'unknown'):
            raise HTTPException(422, '源语言或目标语言代码无效')
        if body.enhance:
            raise HTTPException(422, "Anime 超分已移除，请关闭超分后重新提交任务")
        if body.burn and (not re.fullmatch(r"#[0-9a-fA-F]{6}", body.color)
                          or not -120 <= body.vertical_offset <= 120
                          or not -270 <= body.horizontal_offset <= 270):
            raise HTTPException(422, "字幕颜色或位置偏移无效")
        if body.translate and not body.langs:
            raise HTTPException(422, "翻译字幕需要选择目标语言")
        try:
            engine, result = runtime.router.probe(body.url)
        except AppError as e:
            raise HTTPException(422, e.to_dict())
        recognize = body.recognize or body.translate or body.person_notes
        audio_subtitles = body.recognize or body.translate or not body.person_notes
        if body.person_notes and body.audio_subtitles is not None:
            audio_subtitles = body.audio_subtitles
        langs = body.langs if body.translate else []
        mode = ("burn" if body.burn else "softmux") if recognize else "none"
        options = dict(output_dir=body.output_dir, font_name=body.font_name,
                       font_size=body.font_size, position=body.position,
                       source_lang=body.source_lang, outline=body.outline,
                       bilingual=body.bilingual if recognize and audio_subtitles else False,
                       person_notes=body.person_notes,
                       audio_subtitles=audio_subtitles,
                       translate=body.translate, color=body.color,
                       vertical_offset=body.vertical_offset,
                       horizontal_offset=body.horizontal_offset)
        with runtime.submit_lock:
            if runtime.exiting:
                raise HTTPException(503, "本地引擎正在退出")
            existing = runtime.store.find_active_equivalent(dict(
                url=body.url, resolution=body.resolution, target_langs=",".join(langs),
                subtitle_mode=mode, **options))
            if existing:
                return existing
            task = runtime.runner.create_task(body.url, body.resolution, langs, mode,
                                       title=result.title, **options)
            runtime.start_task(task["id"])
        return task

    def artifact_size(path: str) -> int:
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0

    @router.get("/api/v1/tasks")
    def list_tasks(response: Response, include_artifacts: bool = False,
                   ids: str = Query("", max_length=4096)):
        task_ids = tuple(dict.fromkeys(ids.split(","))) if ids else ()
        if len(task_ids) > 100 or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tid) for tid in task_ids):
            raise HTTPException(422, "任务编号列表无效")
        # 在读取状态前取水位。客户端继续消费之后的事件，覆盖读库期间发生的变更。
        response.headers["X-Jiying-Event-Sequence"] = str(runtime.events.sequence)
        cards = runtime.store.task_snapshot(task_ids, include_artifacts=include_artifacts)
        sizes: dict[str, int] = {}
        for task in cards:
            task["progress"] = (runtime.runner.live_progress(task["id"])
                                if task["status"] in ("pending", "running") else {})
            if include_artifacts:
                for artifact in task["artifacts"]:
                    path = artifact["path"]
                    if path not in sizes:
                        sizes[path] = artifact_size(path)
                    artifact["size"] = sizes[path]
        return cards

    @router.post("/api/v1/tasks/{tid}/cancel")
    def cancel_task(tid: str):
        """取消任务：立即终止已登记的子进程，流水线在最近安全点退出。"""
        from ..core import cancel as task_cancel
        with runtime.submit_lock:
            if tid in runtime.terminal_operations:
                raise HTTPException(409, "任务正在保存或删除文件，请稍后再操作")
            task = runtime.store.request_cancel_if_active(tid, task_cancel.cancel)
            if not task:
                raise HTTPException(404, "任务不存在")
            if task["status"] not in ("running", "pending"):
                raise HTTPException(409, f"任务已是 {task['status']} 状态，无需取消")
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
        candidates += [runtime.data_root / "library", runtime.data_root / "tmp"]
        roots = []
        for r in candidates:
            try:
                roots.append(r.resolve())
            except OSError:
                pass
        deleted: list[str] = []
        skipped: list[str] = []
        removable: list[int] = []
        for a in runtime.store.list_artifacts(task["id"]):
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
            if runtime.store.artifact_owned_by_other(task["id"], raw):
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

    @router.delete("/api/v1/tasks/{tid}")
    def delete_task(tid: str, with_files: bool = False):
        """删除任务记录并回收工作文件（tmp 与转写缓存）。

        with_files=true 时连带删除 artifact 登记的产物文件（安全边界见
        _safe_delete_files）；默认 false，行为与旧版完全兼容。
        """
        task = runtime.store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        # 判断共享所有者、处理文件和移除登记必须串行，最后一个所有者才会回收文件。
        with runtime.deletion_lock, runtime.terminal_operation(task, "任务进行中，先取消再删除") as task:
            deleted, skipped, _ = _safe_delete_files(task) if with_files else ([], [], [])
            runtime.runner.cleanup_task_files(tid)
            runtime.store.delete_task(tid)
        runtime.events.publish(AppEvent(type="task", task_id=tid, status="deleted"))
        return {"ok": True, "task_id": tid,
                "deleted_files": deleted, "skipped": skipped}

    @router.delete("/api/v1/tasks/{tid}/artifacts")
    def delete_task_artifacts(tid: str, kind: str):
        if kind not in ("subtitle", "video"):
            raise HTTPException(400, "只能选择字幕或视频")
        task = runtime.store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        with runtime.deletion_lock, runtime.terminal_operation(task, "任务进行中，先取消再删除") as task:
            kinds = {"subtitle", "bilingual", "person_notes"} if kind == "subtitle" else {"video", "output"}
            deleted, skipped, removable = _safe_delete_files(task, kinds)
            runtime.store.delete_artifacts(tid, removable)
            if removable:
                runtime.events.publish(AppEvent(type="task", task_id=tid, status="updated"))
            return {"ok": True, "task_id": tid,
                    "deleted_files": deleted, "skipped": skipped}

    @router.post("/api/v1/tasks/clear_done")
    def clear_done(body: ClearDoneIn | None = None):
        """批量清除终态任务（completed/failed/cancelled/interrupted）。

        with_files=true 时对每个任务走同一套连带删除安全边界。
        """
        with_files = bool(body and body.with_files)
        kind = body.kind if body else ""
        if kind not in ("", "subtitle", "video"):
            raise HTTPException(400, "只能选择字幕或视频")
        task_ids = set(body.task_ids) if body and body.task_ids is not None else None
        cleared, deleted, skipped = 0, [], []
        for t in runtime.store.list_tasks():
            if task_ids is not None and t["id"] not in task_ids:
                continue
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

    @router.post("/api/v1/tasks/{tid}/retry")
    def retry_task(tid: str):
        """重试失败/已取消的任务：checkpoint 机制自动从断点继续。"""
        task = runtime.store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        with runtime.submit_lock:
            if runtime.exiting:
                raise HTTPException(503, "本地引擎正在退出")
            if runtime.store.find_active_equivalent(task):
                raise HTTPException(409, "相同设置的下载任务正在运行，请查看现有任务")
            current = runtime.store.claim_retry_snapshot(tid)
            if current is None:
                raise HTTPException(409, "任务正在运行或已成功完成，无需重试")
            runtime.start_task(tid, previous=current)
        return {"ok": True, "task_id": tid}

    @router.get("/api/v1/tasks/{tid}")
    def task_detail(tid: str):
        task = runtime.store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        arts = [dict(a, size=artifact_size(a["path"])) for a in runtime.store.list_artifacts(tid)]
        return {**task, "stages": runtime.store.list_stages(tid),
                "progress": runtime.runner.live_progress(tid), "artifacts": arts}

    @router.post("/api/v1/tasks/{tid}/reveal")
    def reveal_task_folder(tid: str):
        """在系统文件管理器中打开该任务产物所在文件夹（并尽量选中该文件）。

        路径只从数据库取，绝不接受客户端传入路径，杜绝任意路径打开。
        """
        import subprocess
        task = runtime.store.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        arts = runtime.store.list_artifacts(tid)
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
            args = ["/select,", str(select)] if select is not None else [str(folder)]
            subprocess.Popen([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe"), *args])
        else:
            import sys as _sys
            opener = "open" if _sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(folder)])
        return {"ok": True, "folder": str(folder), "path": str(target)}

    return router
