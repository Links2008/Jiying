"""产物发布、共享所有权及工作文件回收。"""
from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from ..core import cancel as task_cancel
from ..core.paths import replace_with_retry
from ..core.errors import AppError
from .resources import publication_slot

log = logging.getLogger(__name__)


class TaskArtifacts:
    def _cleanup_after_task(self, tid: str) -> None:
        """临时清理属于尽力操作，失败不能吞掉处理结果或跳过终态提交。"""
        try:
            self._cleanup_tmp(tid)
        except Exception:
            log.warning("工作目录清理暂未完成，保留文件供后续处理：%s", tid, exc_info=True)

    @staticmethod
    def _prune_worktree(root: Path, protected: set[Path]) -> None:
        """只回收不再使用的工作文件；保留嵌套登记文件且不沿符号链接删除。"""
        if not root.exists() or root.is_symlink():
            return
        for child in root.iterdir():
            resolved = child.resolve()
            lexical = Path(os.path.abspath(child))
            if resolved in protected or lexical in protected:
                continue
            contains_kept = any(path.is_relative_to(resolved) or path.is_relative_to(lexical)
                                for path in protected)
            if child.is_symlink():
                if not contains_kept:
                    child.unlink(missing_ok=True)
            elif child.is_dir():
                if contains_kept:
                    TaskArtifacts._prune_worktree(child, protected)
                else:
                    shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        try:
            root.rmdir()
        except OSError:
            pass  # 仍保留登记文件的目录不会为空。

    def _cleanup_tmp(self, tid: str, keep_video: bool | None = None) -> None:
        """终态保留可编辑字幕与需要的源片，同时保护其他任务仍登记的文件。"""
        root = self._data / "tmp" / tid
        if not root.exists():
            return
        if self._store.stage_status(tid, "download") != "done":
            return  # 未下载完成时保留续传残片。
        if keep_video is None:
            keep_video = bool(self._cfg.get("paths.keep_source_video", True))
        if (self._store.get_task(tid) or {}).get("enhance"):
            keep_video = True
        try:
            paths = self._store.artifact_paths_owned_by_other(tid)
            paths.extend(a["path"] for a in self._store.list_artifacts(tid)
                         if a["kind"] in ("subtitle", "bilingual")
                         or (keep_video and a["kind"] == "video"))
            # 链接的登记路径和实际目标都须保留，否则目标尚在但其他任务已无法读取。
            keep = {location for path in paths
                    for location in (Path(os.path.abspath(path)), Path(path).resolve())}
        except Exception:
            log.warning("产物登记暂不可读取，跳过工作文件清理：%s", tid)
            return
        try:
            self._prune_worktree(root, keep)
        except (OSError, RuntimeError) as exc:
            log.warning("临时目录清理失败（可忽略）：%s", exc)

    def cleanup_task_files(self, tid: str) -> None:
        """删除任务时回收工作文件；历史共享文件保留给仍登记它们的任务。"""
        try:
            keep = {location for path in self._store.artifact_paths_owned_by_other(tid)
                    for location in (Path(os.path.abspath(path)), Path(path).resolve())}
        except Exception:
            log.warning("共享产物登记暂不可读取，跳过工作文件清理：%s", tid)
            return
        for root in (self._data / "tmp" / tid,
                     self._data / "cache" / "transcripts" / f"{tid}.parts",
                     self._data / "cache" / "translations" / tid,
                     self._data / "cache" / "subtitles" / tid):
            try:
                self._prune_worktree(root, keep)
            except (OSError, RuntimeError) as exc:
                log.warning("工作文件清理失败（可忽略）：%s", exc)
        try:
            transcript = self._data / "cache" / "transcripts" / f"{tid}.json"
            if transcript.resolve() not in keep:
                transcript.unlink(missing_ok=True)
        except (OSError, RuntimeError) as exc:
            log.warning("转写缓存清理失败（可忽略）：%s", exc)

    def _output_dir(self, task: dict) -> Path:
        custom = (task.get("output_dir") or "").strip()
        return Path(custom) if custom else self._data / "library" / task["id"]

    def _publish_file(self, tid: str, source: Path, directory: Path, kind: str,
                      resolution: str = "", lang: str = "", registered: bool = False,
                      replace_owned: bool = True) -> Path:
        # Choose, move, and register together so concurrent same-title tasks cannot share a path.
        with publication_slot(tid):
            def available(path: Path) -> bool:
                return (not self._store.artifact_owned_by_other(tid, str(path))
                        and (not path.exists() or (replace_owned and self._store.artifact_exists(tid, str(path)))))

            final = directory / source.name
            if not available(final):
                final = directory / f"{source.stem}-{tid}{source.suffix}"
                n = 2
                while not available(final):
                    final = directory / f"{source.stem}-{tid}-{n}{source.suffix}"
                    n += 1
            if source.resolve() != final.resolve():
                task_cancel.raise_if_cancelled(tid)
                shared_source = self._store.artifact_owned_by_other(tid, str(source))
                def check_cancelled():
                    task_cancel.raise_if_cancelled(tid)

                copy_required = shared_source
                if not shared_source:
                    try:
                        replace_with_retry(source, final, before_retry=check_cancelled)
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise
                        copy_required = True
                if copy_required:
                    # 跨盘或共享来源均先写临时副本；完成前保留原文件和上一版成品。
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=directory, prefix=f".publish-{tid}-",
                                                         suffix=".tmp", delete=False) as staging:
                            temporary = Path(staging.name)
                            with source.open("rb") as encoded:
                                while True:
                                    task_cancel.raise_if_cancelled(tid)
                                    block = encoded.read(1024 * 1024)
                                    if not block:
                                        break
                                    staging.write(block)
                            staging.flush()
                            os.fsync(staging.fileno())
                        shutil.copystat(source, temporary)
                        task_cancel.raise_if_cancelled(tid)
                        replace_with_retry(temporary, final, before_retry=check_cancelled)
                        if not shared_source:
                            try:
                                source.unlink()
                            except OSError:
                                log.warning("产物已保存，工作副本待清理：%s", source)
                    finally:
                        if temporary is not None:
                            try:
                                temporary.unlink(missing_ok=True)
                            except OSError:
                                log.warning("产物临时副本待清理：%s", temporary)
            if registered:
                self._store.move_artifact(tid, str(source), str(final))
            elif not self._store.artifact_exists(tid, str(final)):
                self._store.add_artifact(tid, kind, str(final), resolution, lang)
            return final

    def _finish_download_with_warning(self, tid: str, err: AppError) -> str | None:
        """后处理失败不抹掉已经落盘的视频；保留错误并允许前端重试字幕。"""
        if self._store.stage_status(tid, "download") != "done":
            return None
        videos = [a for a in self._store.list_artifacts(tid)
                  if a["kind"] == "video" and Path(a["path"]).exists()]
        if not videos:
            return None
        task = self._store.get_task(tid)
        video = Path(videos[0]["path"])
        if task and video.parent != self._output_dir(task):
            try:
                output = self._output_dir(task)
                output.mkdir(parents=True, exist_ok=True)
                self._publish_file(tid, video, output, "video", registered=True)
            except OSError as move_error:
                log.error("字幕处理失败后无法保存原片：%s", move_error)
                return None
        self._fail_stage(tid, err)
        payload = err.to_dict()
        payload["message"] = f"视频已下载；字幕处理未完成：{err.message}"
        log.warning("任务 %s 视频已保存，后处理失败：%s", tid, err)
        return json.dumps(payload, ensure_ascii=False)
