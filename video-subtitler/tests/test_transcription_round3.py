"""第三轮识别复查：空响应、损坏词时间、参数快照与分片语种绑定。"""
import json
import struct
import wave
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.long_asr import transcribe_long_audio
from app.services.transcriber import CloudAsrTranscriber, FasterWhisperTranscriber, Segment, Transcript


def wav_file(path, seconds=3):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        for second in range(seconds):
            output.writeframesraw(struct.pack('<h', second + 1) * 16000)
    return path


def response(payload):
    return SimpleNamespace(status_code=200, json=lambda: payload)


@pytest.mark.parametrize('payload', [{}, {'error': {'message': '模拟服务故障'}},
                                    {'words': None}, {'words': '损坏词表'}])
def test_missing_or_error_cloud_response_is_not_cached_as_no_speech(tmp_path, monkeypatch, payload):
    audio = wav_file(tmp_path / 'audio.wav')
    monkeypatch.setattr('requests.post', lambda *a, **kw: response(payload))
    worker = CloudAsrTranscriber(Config({'transcriber': {'cloud': {'vendor': 'openai'}}}))
    with pytest.raises(AppError) as error:
        worker.transcribe(audio)
    assert error.value.code == Codes.ASR_FAILED and error.value.retryable


@pytest.mark.parametrize('bad_end', ['broken', 0, float('nan'), float('inf')])
def test_words_only_text_survives_bad_word_timestamps(tmp_path, bad_end):
    audio = wav_file(tmp_path / 'audio.wav')
    payload = {'words': [{'word': 'Hello', 'start': 0, 'end': .5},
                         {'word': 'world.', 'start': .5, 'end': bad_end}]}
    words = []
    result, _ = CloudAsrTranscriber(Config({}))._parse(payload, audio, words)
    assert result == [Segment(0, 3, 'Hello world.')]
    assert words == []


def test_cloud_error_in_the_middle_of_long_audio_retries_only_unfinished_parts(tmp_path, monkeypatch):
    audio = wav_file(tmp_path / 'audio.wav', 23)
    calls = []
    def post(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            return response({'error': {'message': '模拟短暂过载'}})
        return response({'text': 'Complete speech.', 'language': 'en',
                         'segments': [{'start': 0, 'end': 1, 'text': 'Complete speech.'}]})
    monkeypatch.setattr('requests.post', post)
    settings = Config({'transcriber': {'chunk_seconds': 10, 'cloud': {'vendor': 'siliconflow'}}})
    worker = CloudAsrTranscriber(settings)
    with pytest.raises(AppError) as error:
        transcribe_long_audio(worker, audio, settings, tmp_path / 'parts')
    assert error.value.code == Codes.ASR_FAILED
    assert len(list((tmp_path / 'parts').glob('*.json'))) == 1
    result = transcribe_long_audio(worker, audio, settings, tmp_path / 'parts')
    assert len(calls) == 4
    assert [(s.start, s.end) for s in result.segments] == [(0, 1), (10, 11), (20, 21)]


def test_local_model_loading_and_decode_use_the_same_settings_snapshot(tmp_path, monkeypatch):
    import faster_whisper
    from app.services import transcriber as tc
    settings = Config({'transcriber': {'model': 'small', 'device': 'cpu', 'compute_type': 'int8',
        'beam_size': 7, 'condition_on_previous_text': True, 'initial_prompt': '原提示',
        'hotwords': '原术语', 'idle_unload_minutes': 0}})
    loaded, decoded, changed = [], [], []

    def create_model(path, device, compute_type):
        loaded.append((str(path), device, compute_type))
        def decode(*args, **kwargs):
            decoded.append(kwargs)
            return iter([SimpleNamespace(start=0, end=1, text='Speech.', words=None)]), SimpleNamespace(
                language='en', duration=1)
        return SimpleNamespace(transcribe=decode)

    def progress(fraction, note):
        if not changed and '正在检查 Whisper' in note:
            changed.append(True)
            settings.replace_raw({'transcriber': {'model': 'medium', 'device': 'cuda', 'compute_type': 'float16',
                'beam_size': 1, 'condition_on_previous_text': False, 'initial_prompt': '新提示',
                'hotwords': '新术语', 'idle_unload_minutes': 0}})

    monkeypatch.setattr(faster_whisper, 'WhisperModel', create_model)
    monkeypatch.setattr(tc, '_add_nvidia_dll_dirs', lambda *a: None)
    monkeypatch.setattr(tc, 'download_whisper_model', lambda config, size, **kw: tmp_path / size)
    worker = FasterWhisperTranscriber(settings)
    assert worker.transcribe(tmp_path / 'unused.wav', on_progress=progress).segments[0].text == 'Speech.'
    assert loaded == [(str(tmp_path / 'small'), 'cpu', 'int8')]
    assert (decoded[0]['beam_size'], decoded[0]['condition_on_previous_text'],
            decoded[0]['initial_prompt'], decoded[0]['hotwords']) == (7, True, '原提示', '原术语')
    worker.transcribe(tmp_path / 'unused.wav')
    assert loaded[-1] == (str(tmp_path / 'medium'), 'cuda', 'float16')
    assert decoded[-1]['beam_size'] == 1 and decoded[-1]['initial_prompt'] == '新提示'


def test_local_vad_retry_cannot_switch_decoding_parameters_mid_request(tmp_path, monkeypatch):
    settings = Config({'transcriber': {'beam_size': 7, 'hotwords': '原术语', 'idle_unload_minutes': 0}})
    calls = []
    def decode(*args, **kwargs):
        calls.append((kwargs['vad_filter'], kwargs['beam_size'], kwargs['hotwords']))
        if len(calls) == 1:
            settings.set('transcriber.beam_size', 1)
            settings.set('transcriber.hotwords', '新术语')
            segments = []
        else:
            segments = [SimpleNamespace(start=0, end=1, text='Recovered speech.', words=None)]
        return iter(segments), SimpleNamespace(language='en', duration=1)
    worker = FasterWhisperTranscriber(settings)
    monkeypatch.setattr(worker, '_load_model', lambda *a, **kw: SimpleNamespace(transcribe=decode))
    assert worker.transcribe(tmp_path / 'unused.wav').segments[0].text == 'Recovered speech.'
    assert calls == [(True, 7, '原术语'), (False, 7, '原术语')]


def test_redecoded_first_part_rebinds_remaining_checkpoints_to_actual_locked_language(tmp_path):
    audio = wav_file(tmp_path / 'audio.wav', 23)
    detected, calls = ['en'], []
    def decode(path, source_lang=None, **kwargs):
        with wave.open(str(path), 'rb') as stream:
            offset = struct.unpack('<h', stream.readframes(1))[0] - 1
        calls.append((offset, source_lang))
        language = source_lang or detected[0]
        return Transcript(language, [Segment(0, 1, f'Complete {language} speech.')])
    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=decode)
    settings = Config({'transcriber': {'chunk_seconds': 10, 'lock_detected_language': True}})
    transcribe_long_audio(worker, audio, settings, tmp_path / 'parts')
    assert calls == [(0, None), (10, 'en'), (20, 'en')]
    (tmp_path / 'parts/000000.json').write_text('{damaged', encoding='utf-8')
    detected[0] = 'zh'
    calls.clear()
    result = transcribe_long_audio(worker, audio, settings, tmp_path / 'parts')
    assert calls == [(0, None), (10, 'zh'), (20, 'zh')]
    assert result.language == 'zh'
    assert all('zh' in segment.text for segment in result.segments)


def test_a_checkpoint_copied_to_another_part_cannot_replace_that_parts_speech(tmp_path):
    audio = wav_file(tmp_path / 'audio.wav', 23)
    calls = []
    def decode(path, **kwargs):
        with wave.open(str(path), 'rb') as stream:
            offset = struct.unpack('<h', stream.readframes(1))[0] - 1
        calls.append(offset)
        return Transcript('en', [Segment(0, 1, f'Part {offset} speech.')])
    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=decode)
    settings = Config({'transcriber': {'chunk_seconds': 10}})
    transcribe_long_audio(worker, audio, settings, tmp_path / 'parts')
    source = tmp_path / 'parts/000000.json'
    (tmp_path / 'parts/000001.json').write_bytes(source.read_bytes())
    calls.clear()
    result = transcribe_long_audio(worker, audio, settings, tmp_path / 'parts')
    assert calls == [10]
    assert [s.text for s in result.segments] == ['Part 0 speech.', 'Part 10 speech.', 'Part 20 speech.']


@pytest.mark.parametrize('end', [0, .0001])
def test_unrenderable_provider_timing_never_creates_a_part_checkpoint(tmp_path, end):
    audio = wav_file(tmp_path / 'audio.wav', 23)
    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False,
        transcribe=lambda *a, **kw: Transcript('en', [Segment(0, end, 'Invalid timing.')]))
    with pytest.raises(AppError) as error:
        transcribe_long_audio(worker, audio, Config({'transcriber': {'chunk_seconds': 10}}), tmp_path / 'parts')
    assert error.value.code == Codes.ASR_FAILED
    assert not list((tmp_path / 'parts').glob('*.json'))


def test_real_no_speech_parts_still_resume_without_repeating_provider_calls(tmp_path):
    audio = wav_file(tmp_path / 'audio.wav', 23)
    calls = []
    def decode(*args, **kwargs):
        calls.append(True)
        raise AppError(Codes.ASR_NO_SPEECH, '明确无语音')
    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=decode)
    settings = Config({'transcriber': {'chunk_seconds': 10}})
    for _ in range(2):
        with pytest.raises(AppError) as error:
            transcribe_long_audio(worker, audio, settings, tmp_path / 'parts')
        assert error.value.code == Codes.ASR_NO_SPEECH
    assert len(calls) == 3 and len(list((tmp_path / 'parts').glob('*.json'))) == 3


@pytest.mark.parametrize('mode', ['words', 'nested_words', 'segments'])
def test_cloud_timestamps_outside_actual_short_audio_keep_text_in_the_real_range(tmp_path, mode):
    audio = wav_file(tmp_path / 'audio.wav')
    payload = {'text': 'Complete speech.'}
    words = [{'word': 'Complete', 'start': 20, 'end': 21},
             {'word': 'speech.', 'start': 21, 'end': 22}]
    if mode == 'words':
        payload['words'] = words
    elif mode == 'nested_words':
        payload['segments'] = [{'text': payload['text'], 'start': 20, 'end': 22, 'words': words}]
    else:
        payload['segments'] = [{'text': payload['text'], 'start': 20, 'end': 22}]
    sink = []
    result, _ = CloudAsrTranscriber(Config({}))._parse(payload, audio, sink)
    assert result == [Segment(0, 3, 'Complete speech.')]
    assert sink == []


def test_local_bad_word_duration_uses_valid_segment_timing(tmp_path, monkeypatch):
    audio = wav_file(tmp_path / 'audio.wav')
    worker = FasterWhisperTranscriber(Config({'transcriber': {'idle_unload_minutes': 0}}))
    segment = SimpleNamespace(text='Speech.', start=0, end=3,
                              words=[SimpleNamespace(word='Speech.', start=20, end=22)])
    monkeypatch.setattr(worker, '_load_model', lambda *a, **kw: SimpleNamespace(
        transcribe=lambda *a, **kw: (iter([segment]), SimpleNamespace(duration=3, language='en'))))
    result = worker.transcribe(audio)
    assert result.segments == [Segment(0, 3, 'Speech.')] and result.words == []


def test_local_invalid_segment_timing_fails_before_producing_a_transcript(tmp_path, monkeypatch):
    audio = wav_file(tmp_path / 'audio.wav')
    worker = FasterWhisperTranscriber(Config({'transcriber': {'idle_unload_minutes': 0}}))
    segment = SimpleNamespace(text='Speech.', start=20, end=22, words=None)
    monkeypatch.setattr(worker, '_load_model', lambda *a, **kw: SimpleNamespace(
        transcribe=lambda *a, **kw: (iter([segment]), SimpleNamespace(duration=3, language='en'))))
    with pytest.raises(AppError) as error:
        worker.transcribe(audio)
    assert error.value.code == Codes.ASR_FAILED


@pytest.mark.parametrize('source_lang,lock,expected_offsets', [(None, False, []), ('en', True, []),
                                                           (None, True, [10, 20])])
def test_legacy_part_metadata_is_reused_only_with_a_known_call_language(tmp_path, source_lang, lock,
                                                                    expected_offsets):
    audio = wav_file(tmp_path / 'audio.wav', 23)
    calls = []
    def decode(path, source_lang=None, **kwargs):
        with wave.open(str(path), 'rb') as stream:
            offset = struct.unpack('<h', stream.readframes(1))[0] - 1
        calls.append(offset)
        return Transcript(source_lang or 'en', [Segment(0, 1, f'Part {offset} speech.')])
    worker = SimpleNamespace(name='cloud', supports_word_timestamps=False, transcribe=decode)
    settings = Config({'transcriber': {'chunk_seconds': 10, 'lock_detected_language': lock}})
    original = transcribe_long_audio(worker, audio, settings, tmp_path / 'parts', source_lang=source_lang)
    for part in (tmp_path / 'parts').glob('*.json'):
        payload = json.loads(part.read_text())
        payload.pop('chunk')
        part.write_text(json.dumps(payload), encoding='utf-8')
    calls.clear()
    assert transcribe_long_audio(worker, audio, settings, tmp_path / 'parts', source_lang=source_lang) == original
    assert calls == expected_offsets
