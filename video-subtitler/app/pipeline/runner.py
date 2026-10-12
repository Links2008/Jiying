"""流水线编排：download → extract → transcribe → translate → render。

每阶段产物落盘并在 task_stage 登记 checkpoint：
- 重跑时已完成阶段直接跳过（失败恢复的基石）；
- 转写结果缓存于 cache/transcripts/<task>.json，事后补翻译语言零重复开销。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path

from ..core import cancel as task_cancel
from ..core.config import Config
from ..core.errors import AppError, Codes
from ..core.events import AppEvent, bus as _default_bus
from ..core.paths import data_dir
from ..models.db import Store
from ..services.downloader.kernel import JobRecorder, SpeedMonitor
from ..services.engines.base import DownloadProgress
from ..services.engines.router import EngineRouter
from ..services.renderer import burn, extract_audio, softmux
from ..services.subtitler import parse_srt, save_srt, save_srt_bilingual, segments_cover, validate_srt
from ..services.transcriber import Segment, Transcript, build_transcriber
from ..services.translator import NullTranslator, build_translator

log = logging.getLogger(__name__)

# 全局资源池（架构 v3 的承诺）：跨任务共享，防止多任务同时提交时
# 互相争抢 —— Whisper 串行（显存/内存友好），ffmpeg 双并发，下载三并发
# 调整上限时保留已占用的名额和等待者，避免释放到另一把信号量。
_TASK_SLOTS = threading.Condition()
_TASK_LIMIT = 2
_ACTIVE_TASKS = 0
# ponytail: one process-wide publication lock; use per-directory locks if file moves become a bottleneck.
_PUBLISH_LOCK = threading.Lock()


def set_max_concurrent(n: int) -> None:
    global _TASK_LIMIT
    with _TASK_SLOTS:
        _TASK_LIMIT = max(1, int(n))
        _TASK_SLOTS.notify_all()


_POOLS = {
    "io": threading.Semaphore(3),
    "gpu": threading.Semaphore(1),
    "cpu": threading.Semaphore(2),
}


def safe_name(title: str, fallback: str = "video") -> str:
    """标题 → 文件名安全片段。"""
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (title or "").strip())
    return (name or fallback)[:80]


class PipelineRunner:
    def __init__(self, config: Config, store: Store, bus=None):
        self._cfg = config
        self._store = store
        self._router = EngineRouter(config, store)
        self._transcriber = build_transcriber(config)  # 模型懒加载，构造很轻
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

    def reload_transcriber(self) -> None:
        """Provider 切换后重建识别后端（本地模型实例也随之更换）。

        识别后端在 __init__ 里只构建一次，运行期改配置只改了 _cfg，已建实例
        不会自动跟随。同名时不重建，避免丢弃已加载的本地 Whisper 模型。
        """
        provider = self._cfg.get("transcriber.provider", "faster-whisper")
        if getattr(self._transcriber, "name", None) == provider:
            return
        self._transcriber = build_transcriber(self._cfg)
        log.info("识别后端已切换为 %s", provider)

    def preload_whisper(self, model: str) -> None:
        """软件内下载完成后立即载入该模型，让下一次识别零加载等待。

        reload_transcriber() 在 provider 未变时直接 return，表达不了「载入」语义，
        因此直达识别后端的 preload；云端识别等无本地模型的后端静默跳过。
        """
        fn = getattr(self._transcriber, "preload", None)
        if callable(fn):
            fn(model)
        else:
            log.info("当前识别后端无本地模型预热能力，跳过载入：%s", model)

    def _emit(self, **kw) -> None:
        """事件总线广播（测试/无订阅者场景零成本）。"""
        try:
            self._bus.publish(AppEvent(**kw))
        except Exception:  # 事件从不影响主流程
            pass

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
                     horizontal_offset: int = 0) -> dict:
        task = self._store.create_task(url, resolution, langs, subtitle_mode, title,
                                       output_dir=output_dir, font_name=font_name,
                                       font_size=font_size, position=position,
                                       source_lang=source_lang, outline=outline,
                                        bilingual=bilingual, translate=translate,
                                        color=color, vertical_offset=vertical_offset,
                                        horizontal_offset=horizontal_offset)
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
                audio = self._stage_extract(tid, video)
                transcript, src_srt = self._stage_transcribe(task, video, audio)
                subs: dict[str, Path] = {transcript.language: src_srt}
                if bool(task.get("translate", 1)) and langs:
                    subs.update(self._stage_translate(task, transcript, langs))
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
            try:
                with self._live_lock:
                    self._live.pop(tid, None)
                self._cleanup_tmp(tid)
                # 收尾完成后再发布终态，避免重试或删除与旧工作线程重叠。
                if task_cancel.is_cancelled(tid):
                    final_status = "cancelled"
                    final_error = json.dumps(AppError(Codes.TASK_CANCELLED, "任务已取消").to_dict(),
                                             ensure_ascii=False)
                self._store.update_task(tid, status=final_status, error=final_error)
                self._emit(type="task", task_id=tid, status=final_status, message=final_error)
            finally:
                task_cancel.clear(tid)
                if acquired:
                    with _TASK_SLOTS:
                        _ACTIVE_TASKS -= 1
                        _TASK_SLOTS.notify_all()
        return self._store.get_task(tid) or task

    # ---- 阶段 1：下载 ----
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
                    source = self._data / "tmp" / tid / video.name
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if self._store.artifact_owned_by_other(tid, str(video)):
                        shutil.copy2(str(video), str(source))
                    else:
                        shutil.move(str(video), str(source))
                    self._store.move_artifact(tid, str(video), str(source))
                    video = source
                return video
        dest = self._data / "tmp" / tid
        for stage in self._store.list_stages(tid):
            if stage["stage"] == "download" and stage["status"] == "failed":
                # claim_retry 已清 task.error；在覆盖阶段错误前隔离旧版确认损坏的来源。
                from ..services.engines.ytdlp_engine import _quarantine_failed_split
                _quarantine_failed_split(dest, task.get("resolution") or "best",
                                         stage.get("error") or "")
        self._set_stage(tid, "download", "running")
        with _POOLS["io"]:
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

    # ---- 阶段 2：提取音频 ----
    def _stage_extract(self, tid: str, video: Path) -> Path:
        task_cancel.raise_if_cancelled(tid)
        if self._store.stage_status(tid, "extract") == "done":
            cached = [Path(a["path"]) for a in self._store.list_artifacts(tid)
                      if a["kind"] == "audio" and Path(a["path"]).exists()]
            if cached:
                return cached[0]
        self._set_stage(tid, "extract", "running")
        with _POOLS["cpu"]:
            audio = extract_audio(self._cfg, video,
                                  self._data / "tmp" / tid / "audio.wav", task_id=tid)
        if not self._store.artifact_exists(tid, str(audio)):
            self._store.add_artifact(tid, "audio", str(audio))
        self._set_stage(tid, "extract", "done")
        return audio

    # ---- 阶段 3：语音识别 ----
    def _stage_transcribe(self, task: dict, video: Path, audio: Path
                          ) -> tuple[Transcript, Path]:
        tid, title = task["id"], task.get("title") or "video"
        task_cancel.raise_if_cancelled(tid)
        cache_file = self._data / "cache" / "transcripts" / f"{tid}.json"
        if self._store.stage_status(tid, "transcribe") == "done" and cache_file.exists():
            transcript = Transcript.load_json(cache_file)
            log.info("阶段 transcribe：checkpoint 命中，跳过识别")
        else:
            self._set_stage(tid, "transcribe", "running")
            self._set_live(tid, stage="transcribe", fraction=0.0,
                           note="正在加载语音识别模型…")
            last_progress = [None]
            def _asr_progress(frac: float, note: str) -> None:
                task_cancel.raise_if_cancelled(tid)
                progress = (frac, note)
                if progress == last_progress[0]:
                    return
                last_progress[0] = progress
                log.info("转写 %.0f%% %s", frac * 100, note)
                self._set_live(tid, stage="transcribe", fraction=frac, note=note)
                self._emit(type="progress", task_id=tid, stage="transcribe", fraction=frac,
                           note=note)

            # 源语言留空 → 自动检测；指定后 Whisper 跳过语种判别，结果更稳定
            src = (task.get("source_lang") or "").strip() or None
            transcript = self._transcriber.transcribe(
                audio, source_lang=src, on_progress=_asr_progress)
            transcript.save_json(cache_file)
            self._set_stage(tid, "transcribe", "done")
        srt = self._srt_path(task, title, transcript.language)
        complete = False
        if srt.exists():
            try:
                complete = segments_cover(parse_srt(srt.read_text(encoding="utf-8-sig")), transcript.segments)
            except (OSError, UnicodeError):
                pass
            if not complete:
                backup = self._output_dir(task) / f"{srt.stem}.incomplete-{time.time_ns()}.srt"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(srt, backup)
                self._store.add_artifact(tid, "subtitle_backup", str(backup), lang=transcript.language)
                log.warning("原文字幕不完整，保留备份后从识别结果重新生成")
        if not complete:
            save_srt(transcript.segments, srt)
        if not self._store.artifact_exists(tid, str(srt)):
            self._store.add_artifact(tid, "subtitle", str(srt), lang=transcript.language)
        return transcript, srt

    # ---- 阶段 4：多语言翻译 ----
    def _stage_translate(self, task: dict, transcript: Transcript,
                         langs: list[str]) -> dict[str, Path]:
        tid, title = task["id"], task.get("title") or "video"
        task_cancel.raise_if_cancelled(tid)
        self._set_stage(tid, "translate", "running")
        result: dict[str, Path] = {}
        targets = [lang for lang in langs if lang != transcript.language]
        for index, lang in enumerate(targets):
            task_cancel.raise_if_cancelled(tid)  # LLM 翻译耗时长，逐语言检查取消
            def report(fraction: float) -> None:
                task_cancel.raise_if_cancelled(tid)
                overall = (index + fraction) / len(targets)
                note = f"翻译字幕 {lang} · {overall:.0%}"
                self._set_live(tid, stage="translate", fraction=overall, note=note)
                self._emit(type="progress", task_id=tid, stage="translate",
                           fraction=overall, note=note)

            srt = self._srt_path(task, title, lang)
            cached = srt.exists()
            if cached:
                try:
                    complete = segments_cover(parse_srt(srt.read_text(encoding="utf-8-sig")), transcript.segments)
                except (OSError, UnicodeError):
                    complete = False
                if complete:
                    if not self._store.artifact_exists(tid, str(srt)):
                        self._store.add_artifact(tid, "subtitle", str(srt), lang=lang)
                    result[lang] = srt
                    report(1.0)
                    continue
                log.warning("旧字幕 %s 不完整，保留备份后重新翻译", lang)
            translator = build_translator(self._cfg, transcript.language, lang)
            if isinstance(translator, NullTranslator):
                log.warning("未配置翻译 Provider（translator.provider: null），"
                            "跳过 %s 语字幕", lang)
                continue
            log.info("翻译字幕 → %s（%d 句）", lang, len(transcript.segments))
            translated = translator.translate(
                [s.text for s in transcript.segments], transcript.language, lang,
                on_progress=report)
            if len(translated) != len(transcript.segments) or any(
                    not isinstance(text, str) or not text.strip() for text in translated):
                raise AppError(Codes.TRANS_FAILED, "翻译返回的句数或内容不完整",
                               suggestion="重试字幕处理；若仍失败，请在设置中更换翻译模型")
            segs = [Segment(s.start, s.end, t)
                    for s, t in zip(transcript.segments, translated)]
            if cached:
                backup = self._output_dir(task) / f"{srt.stem}.incomplete-{time.time_ns()}.srt"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(srt, backup)
                self._store.add_artifact(tid, "subtitle_backup", str(backup), lang=lang)
            save_srt(segs, srt)
            if not self._store.artifact_exists(tid, str(srt)):
                self._store.add_artifact(tid, "subtitle", str(srt), lang=lang)
            result[lang] = srt
        self._set_stage(tid, "translate", "done")
        return result

    # ---- 阶段 5：渲染 ----
    def _stage_render(self, task: dict, video: Path, subs: dict[str, Path],
                      bilingual_subtitle: tuple[str, Path] | None = None) -> list[Path]:
        tid = task["id"]
        title = safe_name(task.get("title") or "video")
        task_cancel.raise_if_cancelled(tid)
        self._set_stage(tid, "render", "running")
        # 输出目录：任务自定义路径优先，默认数据目录/library/<任务>
        lib = self._output_dir(task)
        work = self._data / "tmp" / tid
        try:
            lib.mkdir(parents=True, exist_ok=True)
            work.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise AppError(Codes.CFG_INVALID, f"输出目录不可用：{lib}",
                           detail=str(e),
                           suggestion="检查路径是否存在、是否有写入权限（含盘符拼写）") from e
        targets = [l for l in (task.get("target_langs") or "").split(",") if l]
        main_lang = next((l for l in targets if l in subs), None) or next(iter(subs))
        outputs: list[Path] = []
        font_name = (task.get("font_name") or "").strip()
        font_size = int(task.get("font_size") or 0)
        position = (task.get("position") or "bottom").strip()
        color = task.get("color") or "#FFFFFF"
        vertical_offset = int(task.get("vertical_offset") or 0)
        horizontal_offset = int(task.get("horizontal_offset") or 0)
        try:
            outline = int(task.get("outline")) if task.get("outline") is not None else 1
        except (TypeError, ValueError):
            outline = 1

        mode = task.get("subtitle_mode") or "softmux"
        bilingual = bool(task.get("bilingual")) or bilingual_subtitle is not None
        if bilingual and len(subs) < 2 and bilingual_subtitle is None:
            raise AppError(Codes.CFG_INVALID, "双语字幕缺少第二种语言，请检查翻译设置和目标语言")
        merged: Path | None = None
        with _POOLS["cpu"]:
            def _burn_progress(lang: str, h: int | None):
                # 烧录重编码是全流程最慢的一步，按 ffmpeg 已编码时长回报百分比
                label = f"硬字幕烧录（{lang}{'，' + str(h) + 'p' if h else ''}）"

                def _cb(frac: float) -> None:
                    self._set_live(tid, stage="render", fraction=frac, note=label)
                    self._emit(type="progress", task_id=tid, stage="render",
                               fraction=frac, note=label)
                return _cb

            if bilingual:
                # 双语上下堆叠：用户要求「中文在上、英文在下」——上层优先取
                # 中文语言轨（zh / zh-CN / zh-Hans …），否则退回首选译文；下层
                # 为另一种语言。合并成单条 SRT 一次烧录（libass 的 \N 换行）。
                if bilingual_subtitle is not None:
                    languages, merged = bilingual_subtitle
                    top_lang, bot_lang = languages.split("+", 1)
                else:
                    order = list(subs)
                    top_lang = next((l for l in order if l.lower().startswith("zh")), None) \
                        or (main_lang if main_lang != order[0] else order[1])
                    bot_lang = next((l for l in order if l != top_lang), order[0])
                    merged = work / f"{title}.{top_lang}+{bot_lang}.srt"
                    save_srt_bilingual(subs[top_lang], subs[bot_lang], merged)

            if mode == "burn" and merged is not None:
                out = work / f"{title}.{top_lang}+{bot_lang}.burned.mp4"
                tag = f"{top_lang}上/{bot_lang}下"
                log.info("双语硬字幕烧录：%s（字体=%s 字号=%s）",
                         tag, font_name or "默认", font_size or "默认")
                burn(self._cfg, video, merged, out, font_name=font_name,
                      font_size=font_size, position=position, outline=outline,
                      color=color, vertical_offset=vertical_offset,
                      horizontal_offset=horizontal_offset,
                     task_id=tid, on_progress=_burn_progress(tag, None))
                outputs.append(out)
            elif mode == "burn":
                out = work / f"{title}.{main_lang}.burned.mp4"
                log.info("硬字幕烧录：%s（字体=%s 字号=%s）",
                         main_lang, font_name or "默认", font_size or "默认")
                burn(self._cfg, video, subs[main_lang], out, font_name=font_name,
                     font_size=font_size, position=position, outline=outline,
                     color=color, vertical_offset=vertical_offset,
                     horizontal_offset=horizontal_offset,
                     task_id=tid, on_progress=_burn_progress(main_lang, None))
                outputs.append(out)
            else:
                # 软封装嵌入全部语言轨（每条轨带语言元数据，播放器可切换）
                out = work / f"{title}.{main_lang}.subbed.mp4"
                log.info("软字幕封装：%d 条语言轨（%s）",
                         len(subs), "/".join(subs))
                # subs 是 dict；softmux 期望 [(字幕路径, 语言代码)]，
                # 注意 items() 的顺序是 (语言, 路径)，必须翻转
                # softmux 在 MP4 装不下时会回退 .mkv，必须记录它实际
                # 返回的路径，而不是我们请求的 .mp4 路径
                tracks = [(subs[main_lang], main_lang)] + [
                    (path, lang) for lang, path in subs.items() if lang != main_lang]
                if merged is not None and all(path != merged for path, _ in tracks):
                    tracks.insert(0, (merged, f"{top_lang}+{bot_lang}"))
                subbed = softmux(self._cfg, video, tracks, out, task_id=tid)
                outputs.append(subbed)

        published: list[Path] = []
        for out in outputs:
            final = self._publish_file(tid, out, lib, "output",
                                       resolution=task.get("resolution") or "",
                                       lang=main_lang if mode != "burn" else "")
            published.append(final)
        if merged is not None and bilingual_subtitle is None:
            self._publish_file(tid, merged, lib, "bilingual",
                               lang=f"{top_lang}+{bot_lang}")
        self._set_stage(tid, "render", "done")
        return published

    # ---- 字幕编辑后的重渲染（增量路径：复用视频与 SRT，仅重跑 render）----
    def rerender_subtitles(self, task_id: str, lang: str = "", artifact_id: int | None = None,
                           *, background: bool = False) -> list[Path]:
        task = self._store.get_task(task_id)
        if not task:
            raise AppError(Codes.CFG_INVALID, f"任务不存在：{task_id}")
        if task["status"] in ("running", "pending", "created"):
            raise AppError(Codes.CFG_INVALID, "任务正在运行，不能同时重渲染")
        artifacts = self._store.list_artifacts(task_id)
        selected = next((a for a in artifacts if a["id"] == artifact_id), None) if artifact_id is not None \
            else next((a for a in artifacts if a["kind"] in ("subtitle", "bilingual") and a["lang"] == lang), None)
        if not selected or selected["kind"] not in ("subtitle", "bilingual"):
            raise AppError(Codes.CFG_INVALID, "所选字幕不存在或不属于此任务")
        subtitle = Path(selected["path"])
        if subtitle.suffix.lower() != ".srt" or not subtitle.is_file():
            raise AppError(Codes.CFG_INVALID, "所选 SRT 字幕文件不存在")
        try:
            validate_srt(subtitle.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise AppError(Codes.CFG_INVALID, f"字幕无法用于重新生成：{exc}") from exc
        lang = selected["lang"]
        bilingual_subtitle = None
        if selected["kind"] == "bilingual":
            if lang.count("+") != 1 or not all(lang.split("+")):
                raise AppError(Codes.CFG_INVALID, "双语字幕的语言信息不完整")
            bilingual_subtitle = (lang, subtitle)
        elif artifact_id is not None and not task.get("bilingual"):
            task = {**task, "target_langs": lang}
        videos = [Path(a["path"]) for a in artifacts
                  if a["kind"] == "video" and not a["resolution"].startswith("anime-x")
                  and Path(a["path"]).exists()]
        if not videos:
            raise AppError(Codes.CFG_INVALID, "任务缺少视频产物，无法重渲染")
        subs = {a["lang"]: Path(a["path"]) for a in artifacts
                if a["kind"] == "subtitle" and Path(a["path"]).exists()}
        if bilingual_subtitle is None or not subs:
            subs[lang] = subtitle
        if not self._store.claim_terminal_task(task_id):
            raise AppError(Codes.CFG_INVALID, "任务正在运行，不能同时重渲染")
        def render() -> list[Path]:
            status, error = task["status"], task.get("error", "")
            try:
                if task.get("enhance"):
                    self._store.upsert_stage(task_id, "enhance", "done")
                outputs = self._stage_render(task, videos[0], subs, bilingual_subtitle) \
                    if bilingual_subtitle is not None else self._stage_render(task, videos[0], subs)
                status, error = "completed", ""
                return outputs
            except Exception as exc:
                err = exc if isinstance(exc, AppError) else AppError(Codes.NET_FAILED, str(exc))
                self._fail_stage(task_id, err)
                error = json.dumps(err.to_dict(), ensure_ascii=False)
                raise
            finally:
                with self._live_lock:
                    self._live.pop(task_id, None)
                task_cancel.clear(task_id)
                self._cleanup_tmp(task_id)
                self._store.update_task(task_id, status=status, error=error)
                self._emit(type="task", task_id=task_id, status=status, message=error)

        if not background:
            return render()

        def worker() -> None:
            try:
                render()
            except Exception:
                log.exception("字幕重生成失败：%s", task_id)

        try:
            threading.Thread(target=worker, name=f"subtitle-{task_id}", daemon=True).start()
        except Exception:
            self._store.update_task(task_id, status=task["status"], finished_at=task["finished_at"])
            raise
        return []

    # ---- 临时目录回收 ----
    def _cleanup_tmp(self, tid: str, keep_video: bool | None = None) -> None:
        """任务到达终态后回收 data/tmp/<tid>。

        保留源片与字幕供编辑/重渲染；清掉 audio.wav、合并稿和下载残片。
        paths.keep_source_video=false 时只保留字幕。
        清理失败只记日志，绝不影响任务结论。
        """
        root = self._data / "tmp" / tid
        if not root.exists():
            return
        if self._store.stage_status(tid, "download") != "done":
            return  # 未下载完成时保留媒体残片与 .aria2/.ytdl，重试才能续传。
        if keep_video is None:
            keep_video = bool(self._cfg.get("paths.keep_source_video", True))
        if (self._store.get_task(tid) or {}).get("enhance"):
            keep_video = True
        try:
            keep = {Path(a["path"]) for a in self._store.list_artifacts(tid)
                    if a["kind"] in ("subtitle", "bilingual") or (keep_video and a["kind"] == "video")}
        except Exception:  # 库不可用也不该阻断清理
            keep = set()
        try:
            for child in root.iterdir():
                if child in keep:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            try:
                root.rmdir()  # 仅当已空（保留源片时目录非空，自然跳过）
            except OSError:
                pass
        except OSError as e:
            log.warning("临时目录清理失败（可忽略）：%s", e)

    def cleanup_task_files(self, tid: str) -> None:
        """任务记录删除后回收其工作文件（tmp 与转写缓存；library 产物保留）。

        delete_task 只删库表行，源片与 audio.wav 会永久滞留；任务行没了就
        再也无法重渲染，源片已无价值，连同转写缓存一起回收。
        """
        shutil.rmtree(self._data / "tmp" / tid, ignore_errors=True)
        try:
            (self._data / "cache" / "transcripts" / f"{tid}.json").unlink(
                missing_ok=True)
        except OSError as e:
            log.warning("转写缓存清理失败（可忽略）：%s", e)

    # ---- helpers ----
    def _output_dir(self, task: dict) -> Path:
        custom = (task.get("output_dir") or "").strip()
        return Path(custom) if custom else self._data / "library" / task["id"]

    def _publish_file(self, tid: str, source: Path, directory: Path, kind: str,
                      resolution: str = "", lang: str = "", registered: bool = False) -> Path:
        # Choose, move, and register together so concurrent same-title tasks cannot share a path.
        with _PUBLISH_LOCK:
            def available(path: Path) -> bool:
                return (not self._store.artifact_owned_by_other(tid, str(path))
                        and (not path.exists() or self._store.artifact_exists(tid, str(path))))

            final = directory / source.name
            if not available(final):
                final = directory / f"{source.stem}-{tid}{source.suffix}"
                n = 2
                while not available(final):
                    final = directory / f"{source.stem}-{tid}-{n}{source.suffix}"
                    n += 1
            if source.resolve() != final.resolve():
                shutil.move(str(source), str(final))
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

    def _srt_path(self, task: dict, title: str, lang: str) -> Path:
        return self._data / "tmp" / task["id"] / f"{safe_name(title)}.{lang}.srt"

    def _fail_stage(self, tid: str, err: AppError, cancelled: bool = False) -> None:
        """把失败登记到当前 running 阶段（便于定位断点）。"""
        for row in self._store.list_stages(tid):
            if row["status"] == "running":
                stage_status = "cancelled" if cancelled else "failed"
                self._set_stage(tid, row["stage"], stage_status, error=str(err))
                return
