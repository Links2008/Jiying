"""长音频识别：有界 PCM 读取、重叠上下文、逐段检查点和全局时间轴。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import tempfile
import wave
from dataclasses import asdict
from pathlib import Path

from ..core.errors import AppError, Codes
from .transcriber import (Segment, Transcript, _join_speech_tokens, _timed_word_segments,
                          normalize_asr_language, write_json_atomic)

log = logging.getLogger(__name__)


def _key(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum())


def _merge_overlap(old: list[Segment], new: list[Segment], seam: float, overlap: float) -> list[Segment]:
    """只合并接缝附近的共同识别，保留任一侧单独识别出的词或句子。"""
    if not old:
        return new
    if not new:
        return old
    window_start, window_end = seam - overlap - 1, seam + overlap + 1
    near_seam = lambda item: item.end >= window_start and item.start <= window_end
    left = [i for i, s in enumerate(old) if near_seam(s)]
    right = [i for i, s in enumerate(new) if near_seam(s)]
    if len(left) * len(right) > 100_000:
        # 异常词表可能把大量词挤在几秒内，不能让匹配矩阵耗尽长任务内存。
        log.warning('接缝词时间过于密集，保留两边识别内容供复核')
        return sorted(old + new, key=lambda item: (item.start, item.end))
    # 一对一、保持词序的匹配优先共同词数量，其次优先时间更接近的组合。
    # 不能从单个锚点整段裁剪：下一段可能漏掉锚点后的词，上一段却识别到了。
    scores = [[(0, 0.0) for _ in range(len(right) + 1)] for _ in range(len(left) + 1)]
    directions = {}
    for a in range(len(left) - 1, -1, -1):
        for b in range(len(right) - 1, -1, -1):
            best, direction = scores[a + 1][b], 'old'
            if scores[a][b + 1] > best:
                best, direction = scores[a][b + 1], 'new'
            first, second = old[left[a]], new[right[b]]
            key = _key(first.text)
            distance = abs((first.start + first.end - second.start - second.end) / 2)
            if key and key == _key(second.text) and distance <= .5:
                following = scores[a + 1][b + 1]
                match = (following[0] + 1, following[1] - distance)
                if match >= best:
                    best, direction = match, 'match'
            scores[a][b], directions[a, b] = best, direction
    duplicate_old = set()
    a = b = 0
    while a < len(left) and b < len(right):
        direction = directions[a, b]
        if direction == 'match':
            duplicate_old.add(left[a])
            a += 1
            b += 1
        elif direction == 'old':
            a += 1
        else:
            b += 1
    # 无可靠匹配时保留两边的证据，宁可让后续复核看到不同措辞，也不能静默漏词。
    return sorted([s for i, s in enumerate(old) if i not in duplicate_old] + new,
                  key=lambda item: (item.start, item.end))


def _options(provider, config, source_lang, chunk_seconds, overlap) -> dict:
    fields = ('provider', 'model', 'device', 'compute_type', 'beam_size', 'condition_on_previous_text',
              'initial_prompt', 'hotwords', 'lock_detected_language', 'chunk_seconds', 'chunk_overlap_seconds')
    cloud = config.get('transcriber.cloud', {}) or {}
    return {'version': 1, 'provider': provider.name, 'source_lang': source_lang,
            'chunk_seconds': chunk_seconds, 'overlap': overlap,
            'options': {key: config.get('transcriber.' + key) for key in fields},
            'cloud': {key: cloud.get(key) for key in ('vendor', 'model', 'base_url', 'language')}}


def _shift(items: list[Segment], offset: float, length: float) -> list[Segment]:
    out = []
    for item in items:
        if (not math.isfinite(item.start) or not math.isfinite(item.end)
                or item.start < 0 or item.start > length or item.end < item.start
                or item.end > length + .1 or not isinstance(item.text, str) or not item.text.strip()):
            raise AppError(Codes.ASR_FAILED, '分段识别返回了无效时间轴', retryable=True)
        out.append(Segment(offset + item.start, offset + min(item.end, length), item.text))
    return out


def _checked_part(transcript: Transcript, length: float) -> Transcript:
    """写入前校验字幕可显示、词表结构与实际音频边界，空的无语音结果仍可保存。"""
    try:
        if not isinstance(transcript, Transcript):
            raise ValueError('识别结果类型无效')
        transcript.validate()
        if transcript.words and not transcript.segments:
            raise ValueError('词表存在但缺少对应字幕句')
        result = Transcript(normalize_asr_language(transcript.language),
                            _shift(transcript.segments, 0, length),
                            _shift(transcript.words, 0, length))
        result.validate()
        return result
    except (TypeError, ValueError) as exc:
        raise AppError(Codes.ASR_FAILED, '分段识别返回了无效文本或时间轴', retryable=True) from exc


def transcribe_long_audio(provider, audio_path: Path, config, checkpoint_dir: Path,
                          source_lang: str | None = None, on_progress=None, check_cancel=None) -> Transcript:
    """短音频沿用原 Provider；长音频默认 5 分钟一段，失败重试仅识别未完成段。

    输入为流水线提取的 16 kHz 单声道 PCM WAV。数字静音不提交到识别服务。
    云端无词时间的接口使用非重叠音频，避免整段文本在边界重复。检查点只存
    内容指纹和识别结果，不保存 API Key 或其他凭据。
    """
    def cancelled():
        if check_cancel:
            check_cancel()

    def direct_transcribe():
        def direct_progress(fraction, note):
            cancelled()
            if on_progress:
                on_progress(fraction, note)
            cancelled()
        result = provider.transcribe(audio_path, source_lang=source_lang,
                                     on_progress=direct_progress if on_progress or check_cancel else None)
        cancelled()
        return result

    cancelled()
    try:
        audio_stat = audio_path.stat()
        source = wave.open(str(audio_path), 'rb')
    except (OSError, wave.Error):
        return direct_transcribe()

    def unchanged_audio():
        try:
            current = audio_path.stat()
        except OSError as exc:
            raise AppError(Codes.ASR_FAILED, '音频文件在识别期间发生变化，请重试', retryable=True) from exc
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity(current) != identity(audio_stat):
            raise AppError(Codes.ASR_FAILED, '音频文件在识别期间发生变化，请重试', retryable=True)

    with source:
        rate, frames = source.getframerate(), source.getnframes()
        seconds = float(config.get('transcriber.chunk_seconds', 300))
        if not math.isfinite(seconds) or not 10 <= seconds <= 600:
            raise AppError(Codes.CFG_INVALID, '识别分段长度必须在 10 到 600 秒之间')
        if frames / rate <= seconds:
            result = direct_transcribe()
            unchanged_audio()
            return result
        if rate != 16000 or source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise AppError(Codes.ASR_FAILED, '长音频需先提取为 16 kHz 单声道 PCM WAV')
        overlap = float(config.get('transcriber.chunk_overlap_seconds', 2))
        if not math.isfinite(overlap) or not 0 <= overlap <= min(5, seconds / 4):
            raise AppError(Codes.CFG_INVALID, '识别重叠上下文必须在 0 到 5 秒之间且不超过段长四分之一')
        if not getattr(provider, 'supports_word_timestamps', False):
            overlap = 0
        options = _options(provider, config, source_lang, seconds, overlap)
        digest = hashlib.sha256()
        with audio_path.open('rb') as audio:
            while block := audio.read(1024 * 1024):
                cancelled()
                digest.update(block)
        digest.update(json.dumps(options, sort_keys=True, ensure_ascii=False).encode('utf-8'))
        fingerprint = digest.hexdigest()
        unchanged_audio()
        chunk_frames, pad = round(seconds * rate), round(overlap * rate)
        count, duration = math.ceil(frames / chunk_frames), frames / rate
        words, cues = [], []
        all_words = True
        language = source_lang or 'unknown'
        language_votes: dict[str, float] = {}
        locked_language = source_lang
        last_progress = 0.0
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

        def progress(fraction, note):
            nonlocal last_progress
            cancelled()
            last_progress = max(last_progress, min(1.0, max(0.0, fraction)))
            if on_progress:
                on_progress(last_progress, note)
            cancelled()

        with tempfile.TemporaryDirectory(prefix='asr-', dir=checkpoint_dir) as temporary:
            for index in range(count):
                cancelled()
                unchanged_audio()
                if _options(provider, config, source_lang, seconds, overlap) != options:
                    raise AppError(Codes.CFG_INVALID, '识别设置已改变，请重试以使用一致的模型和参数')
                first, last = index * chunk_frames, min((index + 1) * chunk_frames, frames)
                read_first, read_last = max(0, first - pad), min(frames, last + pad)
                offset, length = read_first / rate, (read_last - read_first) / rate
                chunk_info = {'index': index, 'first_frame': read_first, 'last_frame': read_last,
                              'source_lang': locked_language}
                checkpoint = checkpoint_dir / f'{index:06d}.json'
                transcript = None
                try:
                    saved = json.loads(checkpoint.read_text(encoding='utf-8'))
                    # 旧版没有分段元数据，仅在调用语种仍由全局指纹确定时可安全复用。
                    legacy_safe = ('chunk' not in saved
                                   and (not locked_language or locked_language == source_lang))
                    if (saved['fingerprint'] == fingerprint
                            and (saved.get('chunk') == chunk_info or legacy_safe)):
                        transcript = _checked_part(Transcript.from_dict(saved['transcript']), length)
                except (OSError, ValueError, KeyError, TypeError, AppError):
                    transcript = None
                prefix = f'识别第 {index + 1}/{count} 段'
                if transcript is None:
                    progress(first / frames, prefix)
                    source.setpos(read_first)
                    pcm = source.readframes(read_last - read_first)
                    if len(pcm) != (read_last - read_first) * 2:
                        raise AppError(Codes.ASR_FAILED, '音频数据不完整，无法识别全部内容')
                    chunk = Path(temporary) / 'chunk.wav'
                    if not any(pcm):
                        transcript = Transcript(language=language)
                    else:
                        with wave.open(str(chunk), 'wb') as output:
                            output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
                            output.writeframes(pcm)

                        def part_progress(frac, note):
                            cancelled()
                            done = min(last, read_first + max(0.0, min(1.0, frac)) * (read_last - read_first))
                            progress(done / frames, f'{prefix} · {note}')

                        try:
                            transcript = provider.transcribe(chunk, source_lang=locked_language,
                                                             on_progress=part_progress)
                        except AppError as error:
                            if error.code != Codes.ASR_NO_SPEECH:
                                raise
                            transcript = Transcript(language=language)
                    cancelled()
                    unchanged_audio()
                    if _options(provider, config, source_lang, seconds, overlap) != options:
                        raise AppError(Codes.CFG_INVALID, '识别设置已改变，请重试以使用一致的模型和参数')
                    transcript = _checked_part(transcript, length)
                    write_json_atomic(checkpoint, {'fingerprint': fingerprint, 'chunk': chunk_info,
                                                   'transcript': asdict(transcript)})
                if transcript.segments and language == 'unknown':
                    language = transcript.language
                if transcript.segments and transcript.language != 'unknown':
                    language_votes[transcript.language] = language_votes.get(transcript.language, 0) + sum(
                        s.end - s.start for s in transcript.segments)
                if (not locked_language and transcript.segments
                        and config.get('transcriber.lock_detected_language', False)
                        and re.fullmatch('[a-z]{2,3}', transcript.language)):
                    locked_language = transcript.language
                shifted = _shift(transcript.segments, offset, length)
                if overlap:
                    cues = _merge_overlap(cues, shifted, first / rate, overlap)
                else:
                    cues.extend(shifted)
                if transcript.segments:
                    all_words = all_words and bool(transcript.words)
                shifted_words = _shift(transcript.words, offset, length)
                if shifted_words:
                    words = _merge_overlap(words, shifted_words, first / rate, overlap) if overlap else words + shifted_words
                progress(last / frames, f'{prefix} · 已保存检查点')
        unchanged_audio()
        if not cues:
            raise AppError(Codes.ASR_NO_SPEECH, '未在音频中检测到语音内容')
        if all_words and words:
            # 标点与空格均已对齐到词，重新按全局真实时间分段。
            raw = [{'word': s.text, 'start': s.start, 'end': s.end} for s in words]
            aligned = []
            # 每段的第一个词经原文对齐后不带前置空格，直接拼接会把英文词黏在一起。
            timed = _timed_word_segments(_join_speech_tokens(s.text for s in words), raw, aligned)
            if timed:
                cues = timed
                words = aligned
            else:
                log.warning('重叠词时间无法完整对齐，保留分段字幕时间轴')
                words = []
        else:
            words = []
        if not source_lang and language_votes:
            language = max(language_votes, key=language_votes.get)
        progress(1.0, f'转写完成：{duration / 60:.1f} 分钟，{count} 段')
        return Transcript(language=language, segments=cues, words=words)
