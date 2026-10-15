"""媒体边界复查：接缝漏词、输入变化、取消与可选画面采样。"""
import struct
import tracemalloc
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import cancel
from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.long_asr import _merge_overlap, transcribe_long_audio
from app.services.transcriber import Segment, Transcript, _timed_word_segments
from app.services.video_context import sample_frames


def audio_file(path: Path, seconds=23):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        for second in range(seconds):
            output.writeframesraw(struct.pack('<h', second + 1) * 16000)
    return path


@pytest.mark.parametrize('shared', [False, True])
def test_overlap_keeps_a_word_only_the_previous_chunk_recognized(shared):
    old = [Segment(8, 8.3, 'Opening'), Segment(10.2, 10.5, ' rareterm')]
    new = [Segment(11, 11.5, ' next.')]
    if shared:
        old.insert(1, Segment(9, 9.3, ' anchor'))
        new.insert(0, Segment(9.02, 9.32, ' anchor'))
    result = _merge_overlap(old, new, seam=10, overlap=2)
    assert [s.text.strip() for s in result] == (
        ['Opening', 'anchor', 'rareterm', 'next.'] if shared else ['Opening', 'rareterm', 'next.'])


def test_actual_wav_chunks_preserve_unique_boundary_word_and_cached_resume(tmp_path):
    audio = audio_file(tmp_path / 'audio.wav')
    calls = []
    events = [Segment(1, 1.3, 'Opening'), Segment(9, 9.3, ' anchor'),
              Segment(10.2, 10.5, ' rareterm'), Segment(11, 11.3, ' next.'),
              Segment(21, 21.5, ' Finale.')]

    def recognize(path, **kwargs):
        with wave.open(str(path), 'rb') as source:
            duration = source.getnframes() / 16000
            offset = struct.unpack('<h', source.readframes(1))[0] - 1
        calls.append(offset)
        raw = [{'word': s.text, 'start': s.start - offset, 'end': s.end - offset}
               for s in events if offset <= s.start and s.end <= offset + duration
               and not (offset == 8 and s.text.strip() == 'rareterm')]
        words = []
        cues = _timed_word_segments(''.join(w['word'] for w in raw), raw, words) or []
        return Transcript('en', cues, words)

    worker = SimpleNamespace(name='faster-whisper', supports_word_timestamps=True, transcribe=recognize)
    settings = Config({'transcriber': {'chunk_seconds': 10}})
    result = transcribe_long_audio(worker, audio, settings, tmp_path / 'parts')
    assert calls == [0, 8, 18]
    assert 'rareterm' in ' '.join(cue.text for cue in result.segments)
    assert ' '.join(w.text.strip() for w in result.words) == 'Opening anchor rareterm next. Finale.'
    calls.clear()
    assert transcribe_long_audio(worker, audio, settings, tmp_path / 'parts') == result
    assert calls == []


def test_matching_cannot_remove_a_same_word_outside_the_seam_neighborhood():
    old = [Segment(1, 1.3, 'again'), Segment(40, 40.3, 'again')]
    new = [Segment(1.01, 1.31, 'again'), Segment(40.01, 40.31, 'again')]
    assert len(_merge_overlap(old, new, seam=10, overlap=2)) == 4


def test_dense_bad_timestamps_do_not_allocate_a_quadratic_matching_matrix():
    old = [Segment(9, 9.5, f'旧词{i}') for i in range(1000)]
    new = [Segment(9.1, 9.6, f'新词{i}') for i in range(1000)]
    tracemalloc.start()
    try:
        result = _merge_overlap(old, new, seam=10, overlap=2)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(result) == 2000
    assert peak < 2 * 1024 * 1024


def test_inconsistent_overlap_word_timing_keeps_all_sentence_evidence(tmp_path):
    audio = audio_file(tmp_path / 'audio.wav')
    calls = []

    def recognize(path, **kwargs):
        with wave.open(str(path), 'rb') as source:
            offset = struct.unpack('<h', source.readframes(1))[0] - 1
        calls.append(offset)
        if offset == 0:
            words = [Segment(10.1, 10.9, 'Old evidence.')]
        elif offset == 8:
            words = [Segment(2.3, 2.4, 'New evidence.')]
        else:
            words = [Segment(3, 3.5, 'Final evidence.')]
        return Transcript('en', list(words), words)

    worker = SimpleNamespace(name='faster-whisper', supports_word_timestamps=True, transcribe=recognize)
    result = transcribe_long_audio(worker, audio, Config({'transcriber': {'chunk_seconds': 10}}),
                                  tmp_path / 'parts')
    assert calls == [0, 8, 18]
    assert [cue.text for cue in result.segments] == ['Old evidence.', 'New evidence.', 'Final evidence.']
    assert result.words == []


@pytest.mark.parametrize('change_at', [1, 3])
def test_audio_changed_during_decode_does_not_save_the_changed_part(tmp_path, change_at):
    audio = audio_file(tmp_path / 'audio.wav')
    calls = []

    def recognize(path, **kwargs):
        calls.append(True)
        if len(calls) == change_at:
            with audio.open('r+b') as stream:
                stream.seek(-2, 2)
                stream.write(struct.pack('<h', 999))
        return Transcript('en', [Segment(0, 1, 'Complete speech.')])

    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=recognize)
    with pytest.raises(AppError) as error:
        transcribe_long_audio(worker, audio, Config({'transcriber': {'chunk_seconds': 10}}), tmp_path / 'parts')
    assert error.value.code == Codes.ASR_FAILED
    assert len(list((tmp_path / 'parts').glob('*.json'))) == change_at - 1


def test_audio_changed_during_fingerprint_never_decodes_under_a_mixed_digest(tmp_path, monkeypatch):
    from app.services import long_asr
    audio = audio_file(tmp_path / 'audio.wav')
    original_hash = long_asr.hashlib.sha256
    changed, calls = [], []

    class ChangingHash:
        def __init__(self):
            self.inner = original_hash()
        def update(self, data):
            self.inner.update(data)
            if not changed:
                changed.append(True)
                original_stat = audio.stat()
                with audio.open('r+b') as stream:
                    stream.seek(-2, 2)
                    stream.write(struct.pack('<h', 888))
                # 快速写入可能落在同一系统时间刻度，明确模拟可观察的文件修改。
                import os
                os.utime(audio, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000))
        def hexdigest(self):
            return self.inner.hexdigest()

    monkeypatch.setattr(long_asr.hashlib, 'sha256', ChangingHash)
    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False,
                             transcribe=lambda *a, **kw: calls.append(True) or Transcript('en'))
    with pytest.raises(AppError) as error:
        transcribe_long_audio(worker, audio, Config({'transcriber': {'chunk_seconds': 10}}), tmp_path / 'parts')
    assert error.value.code == Codes.ASR_FAILED and calls == []
    assert not list(tmp_path.rglob('*.json'))


@pytest.mark.parametrize('seconds', [3, 23])
def test_cancel_during_last_result_callback_never_reports_success(tmp_path, seconds):
    audio = audio_file(tmp_path / 'audio.wav', seconds)
    stopped = []

    def recognize(path, on_progress=None, **kwargs):
        if seconds == 3:
            stopped.append(True)
        return Transcript('en', [Segment(0, 1, 'Complete speech.')])

    def check_cancel():
        if stopped:
            raise AppError(Codes.TASK_CANCELLED, '模拟用户取消')

    def progress(fraction, note):
        if '转写完成' in note:
            stopped.append(True)

    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=recognize)
    with pytest.raises(AppError) as error:
        transcribe_long_audio(worker, audio, Config({'transcriber': {'chunk_seconds': 10}}), tmp_path / 'parts',
                              on_progress=progress, check_cancel=check_cancel)
    assert error.value.code == Codes.TASK_CANCELLED


def test_cancelled_frame_sampling_does_not_start_a_blocking_probe(tmp_path, monkeypatch):
    task_id = 'isolated-cancelled-frame-probe'
    calls = []
    monkeypatch.setattr('app.services.renderer.probe_duration', lambda *_: calls.append(True) or 3)
    cancel.cancel(task_id)
    try:
        with pytest.raises(AppError) as error:
            sample_frames(tmp_path / 'unused.mp4', Config({}), task_id)
        assert error.value.code == Codes.TASK_CANCELLED
        assert calls == []
    finally:
        cancel.clear(task_id)


def test_missing_optional_frame_dependency_keeps_text_translation_available(tmp_path, monkeypatch):
    def missing(*args):
        raise AppError(Codes.DEP_MISSING, '隔离测试缺少可选 FFmpeg')
    monkeypatch.setattr('app.services.renderer.probe_duration', missing)
    assert sample_frames(tmp_path / 'unused.mp4', Config({})) == []


def test_frame_probe_receives_task_id_and_preserves_cancel_error(tmp_path, monkeypatch):
    task_id = 'isolated-frame-probe-task'
    calls = []
    def probe(config, path, *, task_id=None):
        calls.append(task_id)
        raise AppError(Codes.TASK_CANCELLED, '模拟探测期间取消')
    monkeypatch.setattr('app.services.renderer.probe_duration', probe)
    with pytest.raises(AppError) as error:
        sample_frames(tmp_path / 'unused.mp4', Config({}), task_id)
    assert error.value.code == Codes.TASK_CANCELLED and calls == [task_id]
