"""字幕合流、烧录与编辑后的重生成。"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from ..core import cancel as task_cancel
from ..core.errors import AppError, Codes
from ..core.workers import WorkerPool
from ..services.ass_composer import compose_person_notes, refresh_edited_cards
from ..services.ass_document import parse_ass
from ..services.person_registry import PersonRegistry, localized_title
from ..services.renderer import _video_traits, burn, softmux
from ..services.subtitler import read_srt, save_srt_bilingual, validate_srt
from .resources import resource_slot, safe_name

log = logging.getLogger(__name__)


class RenderStage:
    def submit_render(self, worker):
        with self._render_lock:
            if self._render_closed:
                raise RuntimeError("重生成工作队列已关闭")
            if self._standalone_renders is None:
                self._standalone_renders = WorkerPool("jiying-render", 2, 8)
            return self._standalone_renders.submit(worker)

    def close_render_pool(self) -> None:
        with self._render_lock:
            self._render_closed = True
            pool, self._standalone_renders = self._standalone_renders, None
        if pool is not None:
            pool.close()

    def _stage_render(self, task: dict, video: Path, subs: dict[str, Path],
                      bilingual_subtitle: tuple[str, Path] | None = None,
                      edited_ass: tuple[str, Path] | None = None) -> list[Path]:
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
        main_lang = next((l for l in targets if l in subs), None) or next(iter(subs), targets[0] if targets else "en")
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
        bilingual = not edited_ass and (bool(task.get("bilingual") and subs) or bilingual_subtitle is not None)
        if bilingual and len(subs) < 2 and bilingual_subtitle is None:
            raise AppError(Codes.CFG_INVALID, "双语字幕缺少第二种语言，请检查翻译设置和目标语言")
        merged: Path | None = None
        def _burn_progress(lang: str):
            label = f"硬字幕烧录（{lang}）"
            mode, fraction = "", 0.0
            last_sent = 0.0

            def _emit() -> None:
                note = f"{label} · {mode}" if mode else label
                self._set_live(tid, stage="render", fraction=fraction, note=note)
                self._emit(type="progress", task_id=tid, stage="render",
                           fraction=fraction, note=note)

            def progress(value: float) -> None:
                nonlocal fraction, last_sent
                fraction = value
                if value >= 1 or time.monotonic() - last_sent >= .25:
                    last_sent = time.monotonic()
                    _emit()

            def encoding(detail: str) -> None:
                nonlocal mode
                mode = detail
                _emit()

            return {"on_progress": progress, "on_encoding": encoding}

        if bilingual:
            # 双语上下堆叠：用户要求「中文在上、英文在下」——上层优先取
            # 中文语言轨（zh / zh-CN / zh-Hans …），否则退回首选译文；下层
            # 为另一种语言。合并成单条 SRT 一次烧录（libass 的 \N 换行）。
            if bilingual_subtitle is not None:
                languages, merged = bilingual_subtitle
                top_lang, bot_lang = languages.split("+", 1)
            else:
                # 先确定所选译文与原文，再在这两种语言中安排中文在上。
                # 多个目标语言同时存在时，不能让默认中文挤掉刚编辑的译文。
                source_lang = next(iter(subs))
                other_lang = source_lang if source_lang != main_lang else next(
                    language for language in subs if language != main_lang)
                pair = [main_lang, other_lang]
                top_lang = next((language for language in pair
                                 if language.lower().startswith("zh")), main_lang)
                bot_lang = next(language for language in pair if language != top_lang)
                merged = work / f"{title}.{top_lang}+{bot_lang}.srt"
                save_srt_bilingual(subs[top_lang], subs[bot_lang], merged)

        dialogue = merged if merged is not None else subs.get(main_lang)
        dialogue_lang = f"{top_lang}+{bot_lang}" if merged is not None else main_lang
        ass = video_traits = None
        if edited_ass:
            dialogue_lang, ass = edited_ass
            ass = refresh_edited_cards(ass, work / "edited-person-cards.ass")
        elif task.get("person_notes"):
            ass_font = font_name or str(self._cfg.get("render.font_name", "") or "Noto Sans CJK SC")
            ass_color = color if mode == "burn" or re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "#FFFFFF"
            notes = [note for lang in dict.fromkeys((main_lang, *dialogue_lang.split("+")))
                     for note in self._person_notes(tid, lang)]
            visual = self._read_screen_notes(tid)
            if task.get("translate", 1) and main_lang in targets:
                registry = PersonRegistry(self._person_notes(tid, main_lang))
                visual = self._resolved_screen_notes(tid, main_lang, visual)
                merged_visual = []
                for note in visual:
                    name, role = registry.resolve(note["source"], note.get("start"))
                    raw_name, title = note["target"], note.get("sense", "")
                    merged_visual.append({**note,
                        "target": name if name and raw_name == note["source"] else raw_name,
                        "sense": localized_title(title, main_lang) or role})
                visual = merged_visual
            notes += visual
            if notes:
                video_traits = _video_traits(video, self._cfg, tid)
                ass = compose_person_notes(
                    read_srt(dialogue) if dialogue is not None else [], notes, work / f"{title}.{dialogue_lang}.人名注释.ass",
                    font_name=ass_font,
                    font_size=font_size or int(self._cfg.get("render.font_size", 18)),
                    position=position, outline=outline, color=ass_color,
                    vertical_offset=vertical_offset, horizontal_offset=horizontal_offset,
                    aspect=video_traits.aspect, duration=video_traits.duration)

        if ass is None and dialogue is None:
            raise AppError(Codes.ASR_NO_SPEECH, "没有检测到可输出的对白或画面人名")
        if mode == "burn":
            out = work / f"{title}.{dialogue_lang}.burned.mp4"
            tag = f"{top_lang}上/{bot_lang}下" if merged is not None else main_lang
            log.info("硬字幕烧录：%s（字体=%s 字号=%s）",
                     tag, font_name or "默认", font_size or "默认")
            burn(self._cfg, video, ass or dialogue, out, font_name=font_name,
                 font_size=font_size, position=position, outline=outline,
                 color=color, vertical_offset=vertical_offset,
                 horizontal_offset=horizontal_offset,
                 task_id=tid, **_burn_progress(tag),
                 **({"video_traits": video_traits} if video_traits is not None else {}))
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
            tracks = ([(subs[main_lang], main_lang)] if main_lang in subs else []) + [
                (path, lang) for lang, path in subs.items() if lang != main_lang]
            if merged is not None and all(path != merged for path, _ in tracks):
                tracks.insert(0, (merged, f"{top_lang}+{bot_lang}"))
            if ass is not None:
                tracks.insert(0, (ass, dialogue_lang))
            with resource_slot('io', tid):
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
        if ass is not None and edited_ass is None:
            # 重渲染可以产生新的字幕，保留使用者已在外部修改过的 ASS。
            content = ass.read_bytes()
            existing = next((Path(a["path"]) for a in reversed(self._store.list_artifacts(tid))
                             if a["kind"] == "person_notes" and a["lang"] == dialogue_lang), None)
            def matches(path: Path) -> bool:
                try:
                    return path.stat().st_size == len(content) and path.read_bytes() == content
                except OSError:
                    return False
            if existing is None or not matches(existing):
                self._publish_file(tid, ass, lib, "person_notes", lang=dialogue_lang, replace_owned=False)
        self._set_stage(tid, "render", "done")
        return published

    def _person_notes(self, tid: str, lang: str) -> list:
        try:
            metadata = json.loads(self._subtitle_source_path(tid, lang).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(metadata, dict) and metadata.get("version") == 1 and metadata.get("complete") is True:
            notes = metadata.get("person_mentions")
            if isinstance(notes, list):
                return notes
        return []

    def rerender_subtitles(self, task_id: str, lang: str = "", artifact_id: int | None = None,
                           *, background: bool = False) -> list[Path]:
        task = self._store.get_task(task_id)
        if not task:
            raise AppError(Codes.CFG_INVALID, f"任务不存在：{task_id}")
        if task["status"] in ("running", "pending", "created"):
            raise AppError(Codes.CFG_INVALID, "任务正在运行，不能同时重渲染")
        previous = self._store.claim_terminal_task_snapshot(task_id)
        if previous is None:
            raise AppError(Codes.CFG_INVALID, "任务正在运行，不能同时重渲染")
        task = previous

        def restore() -> None:
            self._store.finish_task(task_id, status=previous["status"], error=previous["error"],
                                    finished_at=previous["finished_at"])

        # 必须先占用任务再检查输入；之前的文件和任务快照可能已被另一请求更新。
        try:
            artifacts = self._store.list_artifacts(task_id)
            selected = next((a for a in artifacts if a["id"] == artifact_id), None) if artifact_id is not None \
                else next((a for a in artifacts if a["kind"] in ("subtitle", "bilingual") and a["lang"] == lang), None)
            if not selected or selected["kind"] not in ("subtitle", "bilingual", "person_notes"):
                raise AppError(Codes.CFG_INVALID, "所选字幕不存在或不属于此任务")
            subtitle = Path(selected["path"])
            styled = selected["kind"] == "person_notes"
            if subtitle.suffix.casefold() != (".ass" if styled else ".srt") or not subtitle.is_file():
                raise AppError(Codes.CFG_INVALID, "所选字幕文件不存在或格式不匹配")
            try:
                (parse_ass if styled else validate_srt)(subtitle.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise AppError(Codes.CFG_INVALID, f"字幕无法用于重新生成：{exc}") from exc
            lang = selected["lang"]
            bilingual_subtitle = None
            edited_ass = (lang, subtitle) if styled else None
            if selected["kind"] == "bilingual":
                if lang.count("+") != 1 or not all(lang.split("+")):
                    raise AppError(Codes.CFG_INVALID, "双语字幕的语言信息不完整")
                bilingual_subtitle = (lang, subtitle)
            elif not styled and (artifact_id is not None or task.get("bilingual")):
                task = {**task, "target_langs": lang}
            videos = [Path(a["path"]) for a in artifacts
                      if a["kind"] == "video" and not a["resolution"].startswith("anime-x")
                      and Path(a["path"]).exists()]
            if not videos:
                raise AppError(Codes.CFG_INVALID, "任务缺少视频产物，无法重渲染")
            subs = {a["lang"]: Path(a["path"]) for a in artifacts
                    if a["kind"] == "subtitle" and Path(a["path"]).exists()}
            if not styled and (bilingual_subtitle is None or not subs):
                subs[lang] = subtitle
        except Exception:
            restore()
            raise

        def render() -> list[Path]:
            status, error = "failed", ""
            try:
                if task.get("enhance"):
                    self._store.upsert_stage(task_id, "enhance", "done")
                if edited_ass is not None:
                    outputs = self._stage_render(task, videos[0], subs, edited_ass=edited_ass)
                elif bilingual_subtitle is not None:
                    outputs = self._stage_render(task, videos[0], subs, bilingual_subtitle)
                else:
                    outputs = self._stage_render(task, videos[0], subs)
                task_cancel.raise_if_cancelled(task_id)
                status = "completed"
                return outputs
            except Exception as exc:
                err = exc if isinstance(exc, AppError) else AppError(
                    Codes.RENDER_FAILED, "字幕重生成失败", detail=str(exc), retryable=True)
                cancelled = err.code == Codes.TASK_CANCELLED
                status = "cancelled" if cancelled else "failed"
                self._fail_stage(task_id, err, cancelled=cancelled)
                error = json.dumps(err.to_dict(), ensure_ascii=False)
                raise err from (None if err is exc else exc)
            finally:
                terminal_published = False
                try:
                    with self._live_lock:
                        self._live.pop(task_id, None)
                    self._cleanup_after_task(task_id)
                    # 清理期间仍允许取消，全部收尾完成后再发布终态。
                    if task_cancel.is_cancelled(task_id):
                        status = "cancelled"
                        error = json.dumps(AppError(Codes.TASK_CANCELLED, "任务已取消").to_dict(),
                                           ensure_ascii=False)
                    self._store.finish_task(task_id, status=status, error=error)
                    terminal_published = True
                    self._emit(type="task", task_id=task_id, status=status, message=error)
                finally:
                    if not terminal_published:
                        task_cancel.clear(task_id)

        if not background:
            return render()

        def worker() -> None:
            try:
                render()
            except Exception:
                log.exception("字幕重生成失败：%s", task_id)

        try:
            self.submit_render(worker)
        except Exception as exc:
            restore()
            raise AppError(Codes.ENG_FAILED, "后台渲染队列暂不可用，请重试", retryable=True) from exc
        return []
