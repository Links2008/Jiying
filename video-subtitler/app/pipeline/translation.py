"""翻译阶段及译文缓存。"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

from ..core import cancel as task_cancel
from ..core.errors import AppError, Codes
from ..services.subtitler import read_srt, save_srt, segments_cover
from ..services.transcriber import Segment, Transcript, write_json_atomic
from ..services.translator import LLMTranslator, NullTranslator, build_translator

log = logging.getLogger(__name__)


class TranslationStage:
    def _stage_translate(self, task: dict, transcript: Transcript,
                         langs: list[str], video: Path | None = None) -> dict[str, Path]:
        tid, title = task["id"], task.get("title") or "video"
        task_cancel.raise_if_cancelled(tid)
        self._set_stage(tid, "translate", "running")
        result: dict[str, Path] = {}
        targets = [lang for lang in langs if lang != transcript.language]
        source_key = self._subtitle_source_key(transcript)
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
            provenance = self._subtitle_source_path(tid, lang)
            language_key = hashlib.sha256(lang.encode("utf-8")).hexdigest()[:16]
            checkpoints = self._data / "cache" / "translations" / tid / language_key
            try:
                saved_source = json.loads(provenance.read_text(encoding="utf-8"))
                source_matches = (isinstance(saved_source, dict) and saved_source.get("version") == 1
                                  and saved_source.get("complete") is True
                                  and saved_source.get("source") == source_key)
            except FileNotFoundError:
                # 尚未重新识别的历史译稿保留兼容；首次复用时补齐来源指纹。
                source_matches = True
                saved_source = None
            except (OSError, ValueError, TypeError):
                source_matches = False
            cached = srt.exists()
            if cached:
                try:
                    cues = read_srt(srt)
                    complete = source_matches and segments_cover(cues, transcript.segments)
                except (OSError, UnicodeError, AppError):
                    complete = False
                if complete:
                    if task.get("person_notes") and (saved_source is None or "person_mentions" not in saved_source):
                        provider = build_translator(self._cfg, transcript.language, lang)
                        extract = getattr(provider, "backfill_person_mentions", None)
                        if callable(extract):
                            originals = [cue.text for cue in transcript.segments]
                            aligned = []
                            cursor = 0
                            for segment in transcript.segments:
                                while cursor < len(cues) and cues[cursor].end <= segment.start:
                                    cursor += 1
                                overlaps, index = [], cursor
                                while index < len(cues) and cues[index].start < segment.end:
                                    overlaps.append(cues[index].text)
                                    index += 1
                                aligned.append(" ".join(overlaps))
                            try:
                                mentions = extract(originals, aligned, checkpoints,
                                                   lambda: task_cancel.raise_if_cancelled(tid))
                                saved_source = {"version": 1, "source": source_key, "complete": True,
                                                "person_mentions": self._timed_mentions(mentions, transcript.segments)}
                                write_json_atomic(provenance, saved_source)
                            except AppError:
                                raise
                            except Exception as error:
                                log.warning("已保存译稿的人名暂未补齐：%s", type(error).__name__)
                    if saved_source is None:
                        write_json_atomic(provenance, {"version": 1, "source": source_key, "complete": True})
                    if not self._store.artifact_exists(tid, str(srt)):
                        self._store.add_artifact(tid, "subtitle", str(srt), lang=lang)
                    result[lang] = srt
                    report(1.0)
                    continue
                log.warning("旧字幕 %s 不完整或原文已变化，保留备份后重新翻译", lang)
            translator = build_translator(self._cfg, transcript.language, lang)
            if isinstance(translator, NullTranslator):
                log.warning("未配置翻译 Provider（translator.provider: null），"
                            "跳过 %s 语字幕", lang)
                continue
            if callable(getattr(translator, "set_context", None)):
                translator.set_context(title)
            if task.get("person_notes") and callable(getattr(translator, "set_visual_people", None)):
                translator.set_visual_people(self._read_screen_notes(tid))
            if video is not None and callable(getattr(translator, "set_video", None)):
                translator.set_video(video, tid)
            if callable(getattr(translator, "set_checkpoint_dir", None)):
                translator.set_checkpoint_dir(checkpoints)
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
            write_json_atomic(provenance, {"version": 1, "source": source_key, "complete": False})
            save_srt(segs, srt)
            metadata = {"version": 1, "source": source_key, "complete": True}
            person_mentions = getattr(translator, "person_mentions", None)
            if task.get("person_notes") and callable(person_mentions):
                metadata["person_mentions"] = self._timed_mentions(person_mentions(translated, lang), segs)
            write_json_atomic(provenance, metadata)
            if not self._store.artifact_exists(tid, str(srt)):
                self._store.add_artifact(tid, "subtitle", str(srt), lang=lang)
            result[lang] = srt
        self._set_stage(tid, "translate", "done")
        return result

    def _stage_translate_visual(self, task: dict, languages: list[str]) -> None:
        tid = task["id"]
        notes = self._read_screen_notes(tid)
        if not notes:
            return

        def check_cancel():
            task_cancel.raise_if_cancelled(tid)

        provider = LLMTranslator(self._cfg)
        source = self._screen_fingerprint(notes)
        self._set_stage(tid, "translate", "running")
        for lang in dict.fromkeys(languages):
            check_cancel()
            path = self._screen_translation_path(tid, lang)
            resolved = provider.translate_person_notes(
                notes, lang, self._person_notes(tid, lang),
                self._data / "cache" / "translations" / tid / "screen", check_cancel)
            check_cancel()
            write_json_atomic(path, {"version": 1, "source": source, "notes": resolved})
        self._set_stage(tid, "translate", "done")

    def _screen_translation_path(self, tid: str, lang: str) -> Path:
        return self._screen_notes_path(tid).with_name(f"screen-names.{lang}.json")

    @staticmethod
    def _screen_fingerprint(notes) -> str:
        return hashlib.sha256(json.dumps(notes, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _resolved_screen_notes(self, tid: str, lang: str, notes: list[dict]) -> list[dict]:
        try:
            result = json.loads(self._screen_translation_path(tid, lang).read_text(encoding="utf-8"))
            if (result.get("version") == 1 and result.get("source") == self._screen_fingerprint(notes)
                    and isinstance(result.get("notes"), list)):
                return result["notes"]
        except (OSError, ValueError, AttributeError):
            pass
        return notes

    @staticmethod
    def _timed_mentions(mentions, segments):
        return [{**item, "start": segments[item["id"] - 1].start, "end": segments[item["id"] - 1].end}
                for item in mentions if 1 <= item["id"] <= len(segments)]
