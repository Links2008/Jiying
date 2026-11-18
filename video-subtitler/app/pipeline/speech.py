"""提取音频、语音识别、原文字幕及其来源指纹。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path

from ..core import cancel as task_cancel
from ..core.errors import AppError, Codes
from ..services.long_asr import transcribe_long_audio
from ..services.renderer import extract_audio
from ..services.subtitler import layout_segments, read_srt, save_srt, segments_cover
from ..services.transcriber import Transcript, normalize_asr_language, valid_language_code, write_json_atomic
from .resources import resource_slot, safe_name

log = logging.getLogger(__name__)


class SpeechStage:
    def _stage_visual_names(self, task: dict, video: Path) -> None:
        from ..services import visual_names

        tid = task["id"]
        path = self._screen_notes_path(tid)
        media = video.stat()
        fingerprint = [media.st_size, media.st_mtime_ns]
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("media") == fingerprint and isinstance(cached.get("notes"), list):
                return
        except (OSError, ValueError, AttributeError):
            pass
        self._set_stage(tid, "visual_names", "running")

        def report(fraction):
            note = f"识别画面英文人名 · {fraction:.0%}"
            self._set_live(tid, stage="visual_names", fraction=fraction, note=note)
            self._emit(type="progress", task_id=tid, stage="visual_names", fraction=fraction, note=note)

        report(0.)
        try:
            notes = visual_names.detect(self._cfg, video, tid, self._data / "tmp" / tid, report)
        except AppError as error:
            if error.code == Codes.TASK_CANCELLED:
                raise
            self._set_stage(tid, "visual_names", "failed", error.message)
            return
        write_json_atomic(path, {"media": fingerprint, "notes": notes})
        self._set_stage(tid, "visual_names", "done")

    def _screen_notes_path(self, tid: str) -> Path:
        return self._data / "cache" / "subtitles" / tid / "screen-names.json"

    def _read_screen_notes(self, tid: str) -> list[dict]:
        try:
            notes = json.loads(self._screen_notes_path(tid).read_text(encoding="utf-8")).get("notes")
        except (OSError, ValueError, AttributeError):
            return []
        if not isinstance(notes, list):
            return []
        return [note for note in notes if isinstance(note, dict) and isinstance(note.get("source"), str)
                and note["source"].strip() and isinstance(note.get("target"), str)
                and isinstance(note.get("sense", ""), str)]

    def _stage_extract(self, tid: str, video: Path) -> Path:
        task_cancel.raise_if_cancelled(tid)
        if self._store.stage_status(tid, "extract") == "done":
            cached = [Path(a["path"]) for a in self._store.list_artifacts(tid)
                      if a["kind"] == "audio" and Path(a["path"]).exists()]
            if cached:
                return cached[0]
        self._set_stage(tid, "extract", "running")
        with resource_slot('cpu', tid):
            audio = extract_audio(self._cfg, video,
                                  self._data / "tmp" / tid / "audio.wav", task_id=tid)
        if not self._store.artifact_exists(tid, str(audio)):
            self._store.add_artifact(tid, "audio", str(audio))
        self._set_stage(tid, "extract", "done")
        return audio

    def _stage_transcribe(self, task: dict, video: Path, audio: Path
                          ) -> tuple[Transcript, Path]:
        tid, title = task["id"], task.get("title") or "video"
        task_cancel.raise_if_cancelled(tid)
        cache_file = self._data / "cache" / "transcripts" / f"{tid}.json"
        transcript = None
        if self._store.stage_status(tid, "transcribe") == "done" and cache_file.exists():
            try:
                transcript = Transcript.load_json(cache_file)
                if not transcript.segments:
                    raise ValueError('已完成的识别检查点缺少字幕')
            except (OSError, ValueError, TypeError):
                transcript = None
                if cache_file.is_file():
                    # 隔离损坏的最终稿，已有分段检查点仍可复用；任务删除会一并回收。
                    parts = cache_file.with_suffix('.parts')
                    parts.mkdir(parents=True, exist_ok=True)
                    cache_file.replace(parts / f'invalid-transcript-{time.time_ns()}.json')
                log.warning('最终识别检查点损坏，将从有效分段结果恢复')
            else:
                log.info("阶段 transcribe：checkpoint 命中，跳过识别")
        retranscribed = transcript is None
        if retranscribed:
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
            src = normalize_asr_language(task['source_lang']) if task.get('source_lang') else None
            with self._transcriber_session() as transcriber:
                transcript = transcribe_long_audio(
                    transcriber, audio, self._cfg,
                    self._data / "cache" / "transcripts" / f"{tid}.parts",
                    source_lang=src, on_progress=_asr_progress,
                    check_cancel=lambda: task_cancel.raise_if_cancelled(tid))
            # 旧译稿没有来源指纹时，重新识别后不能只凭时间相同继续复用。
            # 在提交最终识别缓存前标记，故障重试也不能漏过这次失效。
            languages = set((task.get("target_langs") or "").split(","))
            languages.update(a["lang"] for a in self._store.list_artifacts(tid)
                             if a["kind"] == "subtitle")
            for language in languages - {"", transcript.language}:
                provenance = self._subtitle_source_path(tid, language)
                if not provenance.exists():
                    write_json_atomic(provenance, {"version": 1, "complete": False})
            write_json_atomic(self._subtitle_source_path(tid, transcript.language),
                              {"version": 1, "complete": False})
            transcript.save_json(cache_file)
            self._set_stage(tid, "transcribe", "done")
        srt = self._srt_path(task, title, transcript.language)
        source_key = self._subtitle_source_key(transcript)
        provenance = self._subtitle_source_path(tid, transcript.language)
        source_changed = retranscribed
        try:
            recorded = json.loads(provenance.read_text(encoding="utf-8"))
            source_changed = source_changed or not (
                isinstance(recorded, dict) and recorded.get("version") == 1
                and recorded.get("complete") is True and recorded.get("source") == source_key)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            source_changed = True
        complete = False
        if srt.exists():
            try:
                previous = read_srt(srt)
                complete = segments_cover(previous, transcript.segments)
                if complete and source_changed:
                    content = lambda items: re.sub(r"\s+", "", "".join(s.text for s in items))
                    complete = content(previous) == content(layout_segments(transcript.segments))
            except (OSError, UnicodeError, AppError):
                pass
            if not complete:
                backup = self._output_dir(task) / f"{srt.stem}.incomplete-{time.time_ns()}.srt"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(srt, backup)
                self._store.add_artifact(tid, "subtitle_backup", str(backup), lang=transcript.language)
                log.warning("原文字幕不完整，保留备份后从识别结果重新生成")
        if not complete:
            save_srt(transcript.segments, srt)
        write_json_atomic(provenance, {"version": 1, "source": source_key, "complete": True})
        if not self._store.artifact_exists(tid, str(srt)):
            self._store.add_artifact(tid, "subtitle", str(srt), lang=transcript.language)
        return transcript, srt

    def _srt_path(self, task: dict, title: str, lang: str) -> Path:
        if not valid_language_code(lang, allow_unknown=True):
            raise AppError(Codes.CFG_INVALID, '字幕语言代码无效，无法生成文件名')
        return self._data / "tmp" / task["id"] / f"{safe_name(title)}.{lang}.srt"

    def _subtitle_source_path(self, tid: str, lang: str) -> Path:
        """仅记录原文指纹与落盘状态；保留到任务删除，以便跨进程验证译稿来源。"""
        key = hashlib.sha256(lang.encode("utf-8")).hexdigest()[:16]
        return self._data / "cache" / "subtitles" / tid / f"{key}.json"

    @staticmethod
    def _subtitle_source_key(transcript) -> str:
        return hashlib.sha256(json.dumps({
            "language": transcript.language,
            "segments": [(s.start, s.end, s.text) for s in transcript.segments],
        }, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
