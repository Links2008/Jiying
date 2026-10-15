"""长识别的有界读取、检查点恢复、语种稳定和边界拼接；不调用真实模型。"""
import json
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.long_asr import _merge_overlap, transcribe_long_audio
from app.services.transcriber import FasterWhisperTranscriber, Segment, Transcript, _timed_word_segments


def audio_file(path, seconds=23, silent=False):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        for second in range(seconds):
            output.writeframesraw(struct.pack('<h', 0 if silent else second + 1) * 16000)
    return path


class Recognizer:
    name = 'faster-whisper'
    supports_word_timestamps = True

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def transcribe(self, path, source_lang=None, on_progress=None):
        with wave.open(str(path), 'rb') as source:
            length = source.getnframes() / source.getframerate()
            offset = struct.unpack('<h', source.readframes(1))[0] - 1
        self.calls.append((offset, length, source_lang, Path(path).stat().st_size))
        if self.fail_at == offset:
            raise AppError(Codes.ASR_FAILED, '模拟识别服务失败', retryable=True)
        events = [Segment(1, 1.3, ' Hello'), Segment(9.8, 10.2, ' boundary'),
                  Segment(10.5, 11, ' words.'), Segment(20.3, 20.8, ' Goodbye.')]
        words = [Segment(s.start - offset, s.end - offset, s.text) for s in events
                 if offset <= s.start and s.end <= offset + length]
        raw = [{'word': s.text, 'start': s.start, 'end': s.end} for s in words]
        cues = _timed_word_segments(''.join(s.text for s in words), raw) or []
        if on_progress:
            on_progress(1, '模拟解码完成')
        return Transcript('en', cues, words)


def run(worker, audio, tmp_path, config=None, **kwargs):
    return transcribe_long_audio(worker, audio, config or Config({'transcriber': {
        'chunk_seconds': 10, 'lock_detected_language': True}}),
                                 tmp_path / 'parts', **kwargs)


def test_chunk_bounds_global_times_and_language_lock(tmp_path):
    worker = Recognizer()
    audio = audio_file(tmp_path / 'input.wav')
    progress = []
    result = run(worker, audio, tmp_path, on_progress=lambda fraction, note: progress.append(fraction))
    assert [(offset, length, lang) for offset, length, lang, _ in worker.calls] == [
        (0, 12, None), (8, 14, 'en'), (18, 5, 'en')]
    assert max(size for *_, size in worker.calls) < 450000
    assert [(s.start, s.end) for s in result.words] == [(1, 1.3), (9.8, 10.2), (10.5, 11), (20.3, 20.8)]
    assert ''.join(s.text for s in result.words).strip() == 'Hello boundary words. Goodbye.'
    assert progress == sorted(progress) and progress[-1] == 1
    assert len(list((tmp_path / 'parts').glob('*.json'))) == 3
    assert not list((tmp_path / 'parts').glob('asr-*'))


def test_retry_only_decodes_unfinished_parts(tmp_path):
    audio = audio_file(tmp_path / 'input.wav')
    broken = Recognizer(fail_at=8)
    with pytest.raises(AppError, match='模拟识别服务失败'):
        run(broken, audio, tmp_path)
    assert len(list((tmp_path / 'parts').glob('*.json'))) == 1
    healthy = Recognizer()
    result = run(healthy, audio, tmp_path)
    assert [call[0] for call in healthy.calls] == [8, 18]
    assert healthy.calls[0][2] == 'en'
    assert result.words[-1].start == 20.3
    cached = Recognizer()
    assert run(cached, audio, tmp_path) == result
    assert cached.calls == []


def test_recreated_identical_audio_keeps_checkpoints_but_content_change_invalidates(tmp_path):
    audio = audio_file(tmp_path / 'input.wav')
    run(Recognizer(), audio, tmp_path)
    audio_file(audio)
    restored = Recognizer()
    run(restored, audio, tmp_path)
    assert restored.calls == []
    with audio.open('r+b') as stream:
        stream.seek(-2, 2)
        stream.write(struct.pack('<h', 99))
    changed = Recognizer()
    run(changed, audio, tmp_path)
    assert len(changed.calls) == 3


def test_quality_change_invalidates_results_and_secret_is_not_saved(tmp_path):
    audio = audio_file(tmp_path / 'input.wav')
    config = Config({'transcriber': {'chunk_seconds': 10, 'model': 'small',
                                     'cloud': {'api_key': '模拟秘密不得落盘'}}})
    run(Recognizer(), audio, tmp_path, config)
    assert all('模拟秘密不得落盘' not in p.read_text(encoding='utf-8') for p in (tmp_path / 'parts').glob('*.json'))
    config.set('transcriber.model', 'medium')
    changed = Recognizer()
    run(changed, audio, tmp_path, config)
    assert len(changed.calls) == 3


def test_corrupt_part_retries_only_that_part(tmp_path):
    audio = audio_file(tmp_path / 'input.wav')
    run(Recognizer(), audio, tmp_path)
    (tmp_path / 'parts' / '000001.json').write_text('{坏掉的 JSON', encoding='utf-8')
    worker = Recognizer()
    run(worker, audio, tmp_path)
    assert [call[0] for call in worker.calls] == [8]


def test_cancel_keeps_completed_part_for_resume(tmp_path):
    audio = audio_file(tmp_path / 'input.wav')
    stopped = [False]
    def progress(fraction, note):
        if '已保存检查点' in note:
            stopped[0] = True
    def check_cancel():
        if stopped[0]:
            raise AppError(Codes.TASK_CANCELLED, '模拟用户取消')
    with pytest.raises(AppError) as error:
        run(Recognizer(), audio, tmp_path, on_progress=progress, check_cancel=check_cancel)
    assert error.value.code == Codes.TASK_CANCELLED
    assert len(list((tmp_path / 'parts').glob('*.json'))) == 1
    worker = Recognizer()
    run(worker, audio, tmp_path)
    assert [call[0] for call in worker.calls] == [8, 18]


def test_digital_silence_does_not_invoke_recognizer(tmp_path):
    worker = Recognizer()
    with pytest.raises(AppError) as error:
        run(worker, audio_file(tmp_path / 'silent.wav', silent=True), tmp_path)
    assert error.value.code == Codes.ASR_NO_SPEECH
    assert worker.calls == []


def test_overlap_keeps_real_repeated_words_and_drops_duplicate_context():
    old = [Segment(8, 8.3, ' yes'), Segment(9, 9.3, ' yes'), Segment(10, 10.3, ' now')]
    new = [Segment(9.02, 9.32, ' yes'), Segment(10.02, 10.32, ' now'), Segment(11, 11.3, ' go')]
    result = _merge_overlap(old, new, 10, 2)
    assert [s.text.strip() for s in result] == ['yes', 'yes', 'now', 'go']


def test_plain_text_cloud_chunks_are_not_overlapped(tmp_path):
    calls = []
    def transcribe(path, source_lang=None, on_progress=None):
        with wave.open(str(path), 'rb') as source:
            length = source.getnframes() / 16000
        calls.append(length)
        return Transcript('en', [Segment(0, length, '段落 ' + str(len(calls)))])
    cloud = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=transcribe)
    result = run(cloud, audio_file(tmp_path / 'input.wav'), tmp_path)
    assert calls == [10, 10, 3]
    assert [(s.start, s.end) for s in result.segments] == [(0, 10), (10, 20), (20, 23)]


def test_local_decode_receives_quality_options_and_word_cache(tmp_path):
    received = []
    def transcribe(*args, **kwargs):
        received.append(kwargs)
        words = [SimpleNamespace(word='你好。', start=.1, end=.6)]
        return iter([SimpleNamespace(text='你好。', words=words, start=0, end=1)]), SimpleNamespace(
            duration=1, language='zh')
    worker = FasterWhisperTranscriber(Config({'transcriber': {'idle_unload_minutes': 0,
        'hotwords': '专有名词', 'initial_prompt': '技术访谈'}}))
    sink = []
    worker._run_model(SimpleNamespace(transcribe=transcribe), tmp_path / 'unused.wav', 'zh', None, True, sink)
    assert received[0]['condition_on_previous_text'] is False
    assert received[0]['beam_size'] == 5
    assert received[0]['hotwords'] == '专有名词'
    assert received[0]['initial_prompt'] == '技术访谈'
    assert sink == [Segment(.1, .6, '你好。')]


def test_old_transcript_cache_remains_compatible(tmp_path):
    path = tmp_path / 'old.json'
    path.write_text(json.dumps({'language': 'en', 'segments': [{'start': 0, 'end': 1, 'text': 'hello'}]}))
    assert Transcript.load_json(path) == Transcript('en', [Segment(0, 1, 'hello')])


def test_auto_language_does_not_lock_short_opening_and_majority_sets_output_language(tmp_path):
    languages = []
    def transcribe(path, source_lang=None, on_progress=None):
        with wave.open(str(path), 'rb') as source:
            length = source.getnframes() / 16000
        languages.append(source_lang)
        return Transcript('en' if len(languages) == 1 else 'zh', [Segment(0, length, '真实语句占位')])
    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=transcribe)
    result = run(worker, audio_file(tmp_path / 'input.wav'), tmp_path,
                 Config({'transcriber': {'chunk_seconds': 10}}))
    assert languages == [None, None, None]
    assert result.language == 'zh'


def test_mid_transcription_quality_change_does_not_checkpoint_wrong_parameters(tmp_path):
    config = Config({'transcriber': {'chunk_seconds': 10, 'model': 'small'}})
    def transcribe(path, **kwargs):
        config.set('transcriber.model', 'medium')
        return Transcript('en', [Segment(0, 1, 'hello')])
    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=transcribe)
    with pytest.raises(AppError) as error:
        run(worker, audio_file(tmp_path / 'input.wav'), tmp_path, config)
    assert error.value.code == Codes.CFG_INVALID
    assert not list((tmp_path / 'parts').glob('*.json'))


def test_task_deletion_cleans_part_checkpoints(tmp_path):
    from app.models.db import Store
    from app.pipeline.runner import PipelineRunner
    runner = PipelineRunner(Config({}), Store(tmp_path / 'tasks.db'))
    runner._data = tmp_path
    task = runner.create_task('https://example.test/long', 'best', [], 'softmux')
    parts = tmp_path / 'cache' / 'transcripts' / (task['id'] + '.parts')
    parts.mkdir(parents=True)
    (parts / '000000.json').write_text('{}')
    runner.cleanup_task_files(task['id'])
    assert not parts.exists()
