"""流水线编排：处理任务调度、阶段顺序、检查点和最终状态。"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

from ..core import cancel as task_cancel
from ..core.config import Config
from ..core.errors import AppError, Codes
from ..core.events import AppEvent, bus as _default_bus
from ..core.paths import data_dir
from ..models.db import Store
from ..services.engines.router import EngineRouter
from ..services.transcriber import build_transcriber
from .artifacts import TaskArtifacts
from .download import DownloadStage
from .rendering import RenderStage
from .speech import SpeechStage
from .translation import TranslationStage

log = logging.getLogger(__name__)

# 调整上限时保留已占用的名额和等待者。
_TASK_SLOTS = threading.Condition()
_TASK_LIMIT = 2
_ACTIVE_TASKS = 0


def set_max_concurrent(n: int) -> None:
    global _TASK_LIMIT
    with _TASK_SLOTS:
        _TASK_LIMIT = max(1, int(n))
        _TASK_SLOTS.notify_all()


class PipelineRunner(DownloadStage, SpeechStage, TranslationStage, RenderStage, TaskArtifacts):
    def __init__(self, config: Config, store: Store, bus=None):
        self._cfg = config
        self._store = store
        self._router = EngineRouter(config, store)
        self._transcriber = build_transcriber(config)  # 模型懒加载，构造很轻
        self._transcriber_lock = threading.Lock()
        self._render_lock = threading.Lock()
        self._standalone_renders = None
        self._render_closed = False
        self._data = data_dir()
        self._bus = bus or _default_bus
        # 各任务最新进度（内存快照，供任务查询接口携带，页面刷新/断线也能恢复）
        self._live: dict[str, dict] = {}
        self._live_lock = threading.Lock()

    def _set_live(self, tid: str, **kw) -> None:
        with self._live_lock:
            self._live.setdefault(tid, {}).update(kw)

    def live_progress(self, tid: str) -> dict:
        with self._live_lock:
            return dict(self._live.get(tid) or {})

    def reload_transcriber(self, prepared=None) -> None:
        """Provider 切换后重建识别后端（本地模型实例也随之更换）。

        识别后端在 __init__ 里只构建一次，运行期改配置只改了 _cfg，已建实例
        不会自动跟随。同名时不重建，避免丢弃已加载的本地 Whisper 模型。
        """
        with self._transcriber_lock:
            provider = self._cfg.get("transcriber.provider", "faster-whisper")
            changed = getattr(self._transcriber, "name", None) != provider
            if changed:
                # 配置接口可在保存前预构建；构建失败时仍保留旧实例。
                replacement = prepared if prepared is not None else build_transcriber(self._cfg)
                previous, self._transcriber = self._transcriber, replacement
            else:
                previous = prepared if prepared is not self._transcriber else None
        if previous is not None:
            close = getattr(previous, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.exception("旧识别后端关闭失败；新配置仍然有效")
        if changed:
            log.info("识别后端已切换为 %s", provider)

    @contextmanager
    def _transcriber_session(self):
        """整段长音频共用租约，模型不能在任务仍使用时释放。

        识别参数变化仍由长音频的一致性检查决定是否要求重试。
        """
        with ExitStack() as stack:
            with self._transcriber_lock:
                transcriber = self._transcriber
                lease = getattr(transcriber, "lease", None)
                stack.enter_context(lease() if callable(lease) else nullcontext())
            yield transcriber

    def preload_whisper(self, model: str) -> None:
        """软件内下载完成后立即载入该模型，让下一次识别零加载等待。

        reload_transcriber() 在 provider 未变时直接 return，表达不了「载入」语义，
        因此直达识别后端的 preload；云端识别等无本地模型的后端静默跳过。
        """
        with self._transcriber_session() as transcriber:
            fn = getattr(transcriber, "preload", None)
            if callable(fn):
                fn(model)
            else:
                log.info("当前识别后端无本地模型预热能力，跳过载入：%s", model)

    def close_transcriber(self) -> None:
        with self._transcriber_lock:
            close = getattr(self._transcriber, "close", None)
            if callable(close):
                close()

    def close(self) -> None:
        from ..services.model_maintenance import wait_if_idle

        self.close_render_pool()
        self.close_transcriber()
        wait_if_idle()

    def _emit(self, **kw) -> None:
        """广播任务事件；系统完成通知不依赖桌面界面订阅。"""
        try:
            self._bus.publish(AppEvent(**kw))
        except Exception:  # 事件从不影响主流程
            pass
        if kw.get('type') == 'task' and kw.get('status') == 'completed':
            try:
                from ..services.completion_notifications import notify_completed
                task = self._store.get_task(kw['task_id']) or {}
                notify_completed(kw['task_id'], task.get('title') or '视频下载')
            except Exception:
                log.exception('完成通知失败；任务结果已保存')

    def _set_stage(self, tid: str, stage: str, status: str, error: str = "") -> None:
        """checkpoint 写库 + 阶段事件广播。"""
        self._store.upsert_stage(tid, stage, status, error)
        if status == "running":  # 进入新阶段：进度基线归零
            self._set_live(tid, stage=stage, fraction=0.0, speed_bps=0.0, eta=0.0, note="")
        self._emit(type="stage", task_id=tid, stage=stage, status=status, message=error)

    # ---- 任务生命周期 ----
    def create_task(self, url: str, resolution: str, langs: list[str],
                    subtitle_mode: str, title: str = "", output_dir: str = "",
                    font_name: str = "", font_size: int = 0,
                    position: str = "bottom", source_lang: str = "",
                     outline: int = 1, bilingual: bool = False,
                     translate: bool = True, color: str = "#FFFFFF",
                     vertical_offset: int = 0,
                     horizontal_offset: int = 0, person_notes: bool = False, audio_subtitles: bool = True) -> dict:
        task = self._store.create_task(url, resolution, langs, subtitle_mode, title,
                                       output_dir=output_dir, font_name=font_name,
                                       font_size=font_size, position=position,
                                       source_lang=source_lang, outline=outline,
                                        bilingual=bilingual, translate=translate,
                                        color=color, vertical_offset=vertical_offset,
                                       horizontal_offset=horizontal_offset, person_notes=person_notes,
                                       audio_subtitles=audio_subtitles)
        self._emit(type="task", task_id=task["id"], status="created", message=url)
        return task

    def run(self, task: dict) -> dict:
        """执行全流水线；下载落盘后，后处理失败保留视频并给出可重试警告。"""
        global _ACTIVE_TASKS
        tid = task["id"]
        acquired = False
        final_status, final_error = "failed", ""
        langs = [l for l in (task.get("target_langs") or "").split(",") if l]
        try:
            with _TASK_SLOTS:
                while _ACTIVE_TASKS >= _TASK_LIMIT:
                    task_cancel.raise_if_cancelled(tid)
                    _TASK_SLOTS.wait(.2)
                task_cancel.raise_if_cancelled(tid)
                _ACTIVE_TASKS += 1
                acquired = True
            self._store.update_task(tid, status="running", finished_at="")
            self._emit(type="task", task_id=tid, status="running", message=task.get("url", ""))
            video = self._stage_download(task)
            # Historical Anime stages are ignored on retry; original files stay protected.
            if task.get("enhance"):
                self._store.upsert_stage(tid, "enhance", "done")
            if task.get("subtitle_mode") != "none":
                subs: dict[str, Path] = {}
                if task.get("person_notes"):
                    self._stage_visual_names(task, video)
                if task.get("audio_subtitles", 1):
                    try:
                        if task.get("person_notes"):
                            from ..services.renderer import _video_traits
                            if _video_traits(video, self._cfg, tid).audio_codecs == ():
                                self._set_stage(tid, "extract", "done")
                                raise AppError(Codes.ASR_NO_SPEECH, "视频没有音轨")
                        audio = self._stage_extract(tid, video)
                        transcript, src_srt = self._stage_transcribe(task, video, audio)
                        subs[transcript.language] = src_srt
                    except AppError as error:
                        if error.code != Codes.ASR_NO_SPEECH or not task.get("person_notes"):
                            raise
                        self._set_stage(tid, "transcribe", "done")
                if subs and bool(task.get("translate", 1)) and langs:
                    subs.update(self._stage_translate(task, transcript, langs, video))
                if task.get("person_notes") and task.get("translate", 1) and langs:
                    self._stage_translate_visual(task, langs)
                self._stage_render(task, video, subs)
            task_cancel.raise_if_cancelled(tid)
            final_status = "completed"
            log.info("任务 %s 完成 ✓", tid)
        except AppError as e:
            cancelled = e.code == Codes.TASK_CANCELLED
            warning = None if cancelled else self._finish_download_with_warning(tid, e)
            if warning:
                final_status, final_error = "completed", warning
            else:
                final_status = "cancelled" if cancelled else "failed"
                final_error = json.dumps(e.to_dict(), ensure_ascii=False)
                self._fail_stage(tid, e, cancelled=cancelled)
                raise
        except Exception as e:  # 未预期异常也收敛到统一错误体系
            err = AppError(Codes.NET_FAILED, f"未预期的错误：{e}", detail=str(e))
            warning = self._finish_download_with_warning(tid, err)
            if warning:
                final_status, final_error = "completed", warning
            else:
                final_error = json.dumps(err.to_dict(), ensure_ascii=False)
                self._fail_stage(tid, err)
                raise err from e
        finally:
            terminal_published = False
            try:
                with self._live_lock:
                    self._live.pop(tid, None)
                self._cleanup_after_task(tid)
                # 收尾完成后再发布终态，避免重试或删除与旧工作线程重叠。
                if task_cancel.is_cancelled(tid):
                    final_status = "cancelled"
                    final_error = json.dumps(AppError(Codes.TASK_CANCELLED, "任务已取消").to_dict(),
                                             ensure_ascii=False)
                self._store.finish_task(tid, status=final_status, error=final_error)
                terminal_published = True
                self._emit(type="task", task_id=tid, status=final_status, message=final_error)
            finally:
                if not terminal_published:
                    task_cancel.clear(tid)
                if acquired:
                    with _TASK_SLOTS:
                        _ACTIVE_TASKS -= 1
                        _TASK_SLOTS.notify_all()
        return self._store.get_task(tid) or task

    # ---- 阶段 1：下载 ----

    # ---- 阶段 2：提取音频 ----

    # ---- 阶段 3：语音识别 ----

    # ---- 阶段 4：多语言翻译 ----

    # ---- 阶段 5：渲染 ----

    # ---- 字幕编辑后的重渲染（增量路径：复用视频与 SRT，仅重跑 render）----

    def _fail_stage(self, tid: str, err: AppError, cancelled: bool = False) -> None:
        """把失败登记到当前 running 阶段（便于定位断点）。"""
        for row in self._store.list_stages(tid):
            if row["status"] == "running":
                stage_status = "cancelled" if cancelled else "failed"
                self._set_stage(tid, row["stage"], stage_status, error=str(err))
                return
