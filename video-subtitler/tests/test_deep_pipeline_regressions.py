"""深度回归：完整内容、异常响应、缓存恢复与资源等待取消；不连接真实服务。"""
import json
import math
import struct
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import cancel
from app.core.config import Config
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.pipeline import runner as pipeline
from app.services.long_asr import transcribe_long_audio
from app.services.subtitler import read_srt
from app.services.transcriber import CloudAsrTranscriber, Segment, Transcript, _timed_word_segments
from app.services.translator import LLMTranslator


def wav_file(path, seconds=3):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        for second in range(seconds):
            stream.writeframesraw(struct.pack('<h', second + 1) * 16000)
    return path


@pytest.mark.parametrize('bad_end', ['broken', 0, float('nan'), float('inf')])
def test_cloud_invalid_segment_cannot_drop_the_tail_of_the_transcript(tmp_path, bad_end):
    audio = wav_file(tmp_path / 'audio.wav')
    payload = {'text': 'First sentence. The tail must remain.', 'duration': 3,
               'segments': [{'start': 0, 'end': 1, 'text': 'First sentence.'},
                            {'start': 1, 'end': bad_end, 'text': 'The tail must remain.'}]}
    words = []
    cues, _ = CloudAsrTranscriber(Config({}))._parse(payload, audio, words)
    assert 'The tail must remain.' in ' '.join(cue.text for cue in cues)
    assert all(math.isfinite(c.start) and math.isfinite(c.end) and c.end > c.start for c in cues)
    assert words == []


def test_cloud_complete_text_takes_priority_over_a_partial_segment_list(tmp_path):
    audio = wav_file(tmp_path / 'audio.wav')
    payload = {'text': 'First sentence. The tail must remain.',
               'segments': [{'start': 0, 'end': 1, 'text': 'First sentence.'}]}
    cues, _ = CloudAsrTranscriber(Config({}))._parse(payload, audio)
    assert ' '.join(c.text for c in cues) == payload['text']
    assert cues[0].start == 0 and cues[-1].end == 3


@pytest.mark.parametrize('reported_duration', [None, -1, float('nan'), float('inf')])
def test_cloud_without_timestamps_uses_actual_wav_duration(tmp_path, reported_duration):
    audio = wav_file(tmp_path / 'audio.wav')
    cues, _ = CloudAsrTranscriber(Config({}))._parse(
        {'text': 'Complete speech.', 'duration': reported_duration}, audio)
    assert cues == [Segment(0, 3, 'Complete speech.')]


def test_cloud_cannot_invent_one_second_when_audio_duration_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr('app.services.renderer.probe_duration', lambda *_: 0)
    with pytest.raises(AppError) as error:
        CloudAsrTranscriber(Config({}))._parse({'text': 'Full speech.'}, tmp_path / 'missing.wav')
    assert error.value.code == Codes.ASR_FAILED


@pytest.mark.parametrize('payload', [[], None, 'invalid'])
def test_cloud_malformed_payload_has_a_clear_asr_error(tmp_path, payload):
    with pytest.raises(AppError) as error:
        CloudAsrTranscriber(Config({}))._parse(payload, tmp_path / 'unused.wav')
    assert error.value.code == Codes.ASR_FAILED


@pytest.mark.parametrize('language,source,expected', [
    ('english', None, 'en'), ('Chinese', None, 'zh'), ('en-US', None, 'en'),
    ('zh-Hant', None, 'zh'), (None, 'ja', 'ja'), ('../../escape', None, 'unknown'),
])
def test_cloud_language_names_and_missing_language_do_not_create_wrong_source_tracks(tmp_path, monkeypatch,
                                                                                       language, source, expected):
    audio = wav_file(tmp_path / 'audio.wav')
    payload = {'text': 'hello', 'duration': 3, 'language': language}
    monkeypatch.setattr('requests.post', lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: payload))
    worker = CloudAsrTranscriber(Config({'transcriber': {'cloud': {'vendor': 'openai'}}}))
    assert worker.transcribe(audio, source_lang=source).language == expected


@pytest.mark.parametrize('response', [None, [], {}, {'choices': []}, {'choices': [None]},
                                     {'choices': [{'message': {'content': None}}]}])
def test_translation_malformed_service_response_is_retried_before_failing(monkeypatch, response):
    calls = []
    def request(*a, **kw):
        calls.append(kw['json'])
        payload = response if len(calls) == 1 else {
            'choices': [{'message': {'content': '1. 完整译文。'}, 'finish_reason': 'stop'}]}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr('requests.post', request)
    monkeypatch.setattr('app.services.translator.time.sleep', lambda _: None)
    worker = LLMTranslator(Config({'translator': {'llm': {
        'base_url': 'https://example.test/v1', 'model': 'test'}}}))
    assert worker.translate(['Complete speech.'], 'en', 'zh') == ['完整译文。']
    assert len(calls) == 2


@pytest.mark.parametrize('cache', [
    '{truncated',
    json.dumps({'language': False, 'segments': [{'start': 0, 'end': 1, 'text': 'old'}]}),
    json.dumps({'language': 'en', 'segments': None}),
    json.dumps({'language': 'en', 'segments': []}),
    json.dumps({'language': 'en', 'segments': [{'start': 0, 'end': 1, 'text': None}]}),
    json.dumps({'language': 'en', 'segments': [{'start': 0, 'end': 0, 'text': 'old'}]}),
    json.dumps({'language': 'en', 'segments': [{'start': 0, 'end': .0001, 'text': 'old'}]}),
    json.dumps({'language': 'en', 'segments': [{'start': 0, 'end': float('nan'), 'text': 'old'}]}),
])
def test_invalid_final_transcript_checkpoint_is_recovered_instead_of_failing_every_retry(tmp_path, cache):
    runner = pipeline.PipelineRunner(Config({}), Store(tmp_path / 'tasks.db'))
    runner._data = tmp_path / 'data'
    task = runner.create_task('https://example.test/clip', 'best', [], 'burn')
    final = runner._data / 'cache/transcripts' / f"{task['id']}.json"
    final.parent.mkdir(parents=True)
    final.write_text(cache, encoding='utf-8')
    runner._store.upsert_stage(task['id'], 'transcribe', 'done')
    calls = []
    restored = Transcript('en', [Segment(0, 1, 'Recovered complete speech.')])
    runner._transcriber = SimpleNamespace(transcribe=lambda *a, **kw: calls.append(True) or restored)
    transcript, captions = runner._stage_transcribe(task, tmp_path / 'video.mp4', tmp_path / 'unused.wav')
    assert transcript == restored and calls == [True]
    assert read_srt(captions) == restored.segments
    assert Transcript.load_json(final) == restored
    assert runner._store.stage_status(task['id'], 'transcribe') == 'done'


@pytest.mark.parametrize('stage', ['download', 'extract', 'render'])
def test_resource_waiting_task_can_cancel_before_other_jobs_release_the_pool(tmp_path, monkeypatch, stage):
    runner = pipeline.PipelineRunner(Config({}), Store(tmp_path / 'tasks.db'))
    runner._data = tmp_path / 'data'
    task = runner.create_task('https://example.test/clip', 'best', [], 'burn')
    entered, finished = threading.Event(), threading.Event()
    semaphore = threading.Semaphore(0)
    releases, errors = [], []
    class WaitingPool:
        def acquire(self, *a, **kw):
            entered.set()
            return semaphore.acquire(*a, **kw)
        def release(self):
            releases.append(True)
            semaphore.release()
        def __enter__(self):
            self.acquire()
        def __exit__(self, *a):
            self.release()
    monkeypatch.setitem(pipeline._POOLS, 'io' if stage == 'download' else 'cpu', WaitingPool())
    if stage in ('extract', 'render'):
        runner._stage_download = lambda *_: tmp_path / 'video.mp4'
        monkeypatch.setattr(pipeline, 'extract_audio', lambda *a, **kw: tmp_path / 'audio.wav')
    if stage == 'render':
        from app.services.subtitler import save_srt
        runner._stage_extract = lambda *_: tmp_path / 'audio.wav'
        transcript = Transcript('en', [Segment(0, 1, 'Isolated speech.')])
        captions = save_srt(transcript.segments, tmp_path / 'captions.srt')
        runner._stage_transcribe = lambda *_: (transcript, captions)
    def work():
        try:
            runner.run(task)
        except AppError as error:
            errors.append(error.code)
        finally:
            finished.set()
    thread = threading.Thread(target=work)
    try:
        thread.start()
        assert entered.wait(2)
        cancel.cancel(task['id'])
        assert finished.wait(1), '资源未释放前，等待任务也必须及时取消'
        assert errors == [Codes.TASK_CANCELLED]
        assert runner._store.get_task(task['id'])['status'] == 'cancelled'
        assert releases == [], '未取得资源时不能多释放一个名额'
    finally:
        semaphore.release()
        thread.join(3)
        cancel.clear(task['id'])


@pytest.mark.parametrize('tokens,expected', [
    (['Hello', ' boundary', ' words.', ' Goodbye.'], 'Hello boundary words. Goodbye.'),
    (['你好', '边界', '台词。', '再见。'], '你好边界台词。再见。'),
])
def test_long_asr_keeps_word_spacing_at_real_parser_chunk_boundaries(tmp_path, tokens, expected):
    audio = wav_file(tmp_path / 'audio.wav', seconds=23)
    events = [Segment(start, end, token) for (start, end), token in zip(
        [(1, 1.3), (9.8, 10.2), (10.5, 11), (20.3, 20.8)], tokens)]
    def recognize(path, **kw):
        with wave.open(str(path), 'rb') as source:
            length = source.getnframes() / 16000
            offset = struct.unpack('<h', source.readframes(1))[0] - 1
        words = [{'word': s.text, 'start': s.start - offset, 'end': s.end - offset}
                 for s in events if offset <= s.start and s.end <= offset + length]
        aligned = []
        cues = _timed_word_segments(''.join(w['word'] for w in words), words, aligned)
        return Transcript('en', cues or [], aligned)
    worker = SimpleNamespace(name='faster-whisper', supports_word_timestamps=True, transcribe=recognize)
    result = transcribe_long_audio(worker, audio, Config({'transcriber': {'chunk_seconds': 10}}),
                                   tmp_path / 'parts')
    text = ' '.join(c.text for c in result.segments) if expected.isascii() else ''.join(c.text for c in result.segments)
    assert text == expected
    assert ''.join(w.text for w in result.words) == expected


@pytest.mark.parametrize('language', ['../../escape', 'zh/en', 'zh\\en'])
def test_language_field_cannot_escape_the_task_subtitle_directory(tmp_path, language):
    runner = pipeline.PipelineRunner(Config({}), Store(tmp_path / 'tasks.db'))
    task = runner.create_task('https://example.test/clip', 'best', [], 'burn')
    with pytest.raises(AppError) as error:
        runner._srt_path(task, 'clip', language)
    assert error.value.code == Codes.CFG_INVALID


@pytest.mark.parametrize('options', [{'langs': ['../../escape'], 'translate': True},
                                   {'source_lang': 'zh/../../escape'}])
def test_invalid_language_is_rejected_before_network_probe_or_task_creation(monkeypatch, options):
    from fastapi import HTTPException
    from app.api import server
    app = server.create_app()
    def forbidden_probe(*a, **kw):
        raise AssertionError('无效语言参数不能触发探测请求')
    monkeypatch.setattr(server._router, 'probe', forbidden_probe)
    submit = next(r.endpoint for r in app.routes if r.path == '/api/v1/tasks' and 'POST' in r.methods)
    with pytest.raises(HTTPException) as error:
        submit(server.TaskIn(url='https://example.test/video', **options))
    assert error.value.status_code == 422
    assert server._store.list_tasks() == []


def test_recovering_invalid_final_cache_reuses_all_valid_parts_and_preserves_diagnostic_copy(tmp_path):
    config = Config({'transcriber': {'chunk_seconds': 10}})
    runner = pipeline.PipelineRunner(config, Store(tmp_path / 'tasks.db'))
    runner._data = tmp_path / 'data'
    task = runner.create_task('https://example.test/long', 'best', [], 'burn')
    audio = wav_file(tmp_path / 'audio.wav', 23)
    calls = []
    def recognize(path, **kw):
        with wave.open(str(path), 'rb') as stream:
            duration = stream.getnframes() / 16000
            offset = struct.unpack('<h', stream.readframes(1))[0] - 1
        calls.append(offset)
        return Transcript('en', [Segment(0, duration, f'Complete part {offset}.')])
    runner._transcriber = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=recognize)
    original, _ = runner._stage_transcribe(task, tmp_path / 'video.mp4', audio)
    assert calls == [0, 10, 20]
    final = runner._data / 'cache/transcripts' / f"{task['id']}.json"
    final.write_bytes(b'{truncated-final-checkpoint')
    calls.clear()
    restored, _ = runner._stage_transcribe(task, tmp_path / 'video.mp4', audio)
    assert restored == original and calls == []
    parts = final.with_suffix('.parts')
    copies = list(parts.glob('invalid-transcript-*.json'))
    assert len(copies) == 1 and copies[0].read_bytes() == b'{truncated-final-checkpoint'
    runner.cleanup_task_files(task['id'])
    assert not parts.exists() and not final.exists()


def test_translation_repeated_malformed_responses_fail_cleanly_without_creating_a_checkpoint(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('requests.post', lambda *a, **kw:
                        calls.append(kw['json']) or SimpleNamespace(raise_for_status=lambda: None,
                                                                  json=lambda: {'choices': []}))
    monkeypatch.setattr('app.services.translator.time.sleep', lambda _: None)
    worker = LLMTranslator(Config({'translator': {'llm': {
        'base_url': 'https://example.test/v1', 'model': 'test'}}}))
    worker.set_checkpoint_dir(tmp_path / 'translations')
    with pytest.raises(AppError) as error:
        worker.translate(['Complete speech.'], 'en', 'zh')
    assert error.value.code == Codes.TRANS_FAILED and len(calls) == 3
    assert not list(tmp_path.rglob('*.json'))
