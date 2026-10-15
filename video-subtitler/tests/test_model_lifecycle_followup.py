"""识别后端切换应释放旧模型，并让已开始的任务安全完成；全程不加载真实模型。"""
import gc
import threading
import time
import weakref
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.pipeline.runner import PipelineRunner
from app.services.transcriber import CloudAsrTranscriber, FasterWhisperTranscriber, Segment


class ModelToken:
    pass


def eventually_gone(reference, timeout=2):
    deadline = time.monotonic() + timeout
    while reference() is not None and time.monotonic() < deadline:
        gc.collect()
        time.sleep(.01)
    return reference() is None


def test_switching_provider_releases_the_idle_model_and_maintenance_thread(tmp_path):
    config = Config({'transcriber': {'provider': 'faster-whisper', 'idle_unload_minutes': 10}})
    runner = PipelineRunner(config, Store(tmp_path / 'tasks.db'))
    previous = runner._transcriber
    previous._model = ModelToken()
    model, provider = weakref.ref(previous._model), weakref.ref(previous)
    config.set('transcriber.provider', 'cloud')
    runner.reload_transcriber()
    del previous
    assert eventually_gone(model), '服务切换后不能让闲置线程继续持有旧模型'
    assert eventually_gone(provider), '已退出的后端不能被永久维护线程保留'


@pytest.mark.parametrize('idle_minutes', [0, 10])
def test_switching_provider_does_not_block_or_unload_active_inference(tmp_path, monkeypatch, idle_minutes):
    config = Config({'transcriber': {'provider': 'faster-whisper', 'idle_unload_minutes': idle_minutes}})
    runner = PipelineRunner(config, Store(tmp_path / 'tasks.db'))
    previous = runner._transcriber
    previous._model = ModelToken()
    model = weakref.ref(previous._model)
    entered, release = threading.Event(), threading.Event()
    outcomes, errors = [], []
    monkeypatch.setattr(previous, '_load_model', lambda *a, **kw: previous._model)
    def infer(*a, **kw):
        entered.set()
        assert release.wait(3)
        return [Segment(0, 1, 'Complete speech.')], SimpleNamespace(duration=1, language='en')
    monkeypatch.setattr(previous, '_run_model', infer)
    def work():
        try:
            outcomes.append(previous.transcribe(tmp_path / 'unused.wav'))
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=work)
    try:
        thread.start()
        assert entered.wait(2)
        config.set('transcriber.provider', 'cloud')
        started = time.monotonic()
        runner.reload_transcriber()
        assert time.monotonic() - started < .5
        assert model() is not None, '正在推理的模型不能被提前释放'
    finally:
        release.set()
        thread.join(3)
    assert not errors and outcomes[0].segments == [Segment(0, 1, 'Complete speech.')]
    assert eventually_gone(model), '推理完成后必须释放旧模型'


def test_retired_provider_allows_existing_task_nested_calls_but_rejects_new_tasks(tmp_path, monkeypatch):
    worker = FasterWhisperTranscriber(Config({'transcriber': {'idle_unload_minutes': 0}}))
    worker._model = ModelToken()
    model = weakref.ref(worker._model)
    monkeypatch.setattr(worker, '_load_model', lambda *a, **kw: worker._model)
    monkeypatch.setattr(worker, '_run_model', lambda *a, **kw: (
        [Segment(0, 1, 'Existing task.')], SimpleNamespace(duration=1, language='en')))
    errors = []
    with worker.lease():
        worker.close()
        assert worker.transcribe(tmp_path / 'unused.wav').segments == [Segment(0, 1, 'Existing task.')]
        def new_task():
            try:
                worker.transcribe(tmp_path / 'unused.wav')
            except AppError as error:
                errors.append(error.code)
        thread = threading.Thread(target=new_task)
        thread.start()
        thread.join(2)
        assert errors == [Codes.CFG_INVALID]
        assert model() is not None
    assert eventually_gone(model)


def test_cloud_provider_reads_new_settings_after_config_snapshot_replacement(tmp_path, monkeypatch):
    config = Config({'transcriber': {'cloud': {'vendor': 'openai', 'model': 'whisper-1',
                                              'base_url': 'https://old.example.test/v1', 'api_key': 'mock-old'}}})
    worker = CloudAsrTranscriber(config)
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'isolated-test-audio')
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs['data']['model'], kwargs['headers']['Authorization']))
        return SimpleNamespace(status_code=200, json=lambda: {
            'language': 'en', 'text': 'Hello.', 'segments': [{'start': 0, 'end': 1, 'text': 'Hello.'}]})
    monkeypatch.setattr('requests.post', request)
    worker.transcribe(audio)
    config.replace_raw({'transcriber': {'cloud': {'vendor': 'siliconflow', 'model': 'new-model',
                                                'base_url': 'https://new.example.test/v1', 'api_key': 'mock-new'}}})
    worker.transcribe(audio)
    assert calls == [('https://old.example.test/v1/audio/transcriptions', 'whisper-1', 'Bearer mock-old'),
                     ('https://new.example.test/v1/audio/transcriptions', 'new-model', 'Bearer mock-new')]
    assert not worker.supports_word_timestamps


def test_cloud_request_uses_one_consistent_settings_snapshot(tmp_path, monkeypatch):
    original = {'transcriber': {'cloud': {'vendor': 'openai', 'model': 'whisper-1',
                                        'base_url': 'https://old.example.test/v1', 'api_key': 'mock-old'}}}
    config = Config(original)
    worker = CloudAsrTranscriber(config)
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'isolated-test-audio')
    base_url = worker._base_url
    def switch_after_url(*args):
        url = base_url(*args)
        config.replace_raw({'transcriber': {'cloud': {'vendor': 'siliconflow', 'model': 'new-model',
                                                    'base_url': 'https://new.example.test/v1', 'api_key': 'mock-new'}}})
        return url
    monkeypatch.setattr(worker, '_base_url', switch_after_url)
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs['data']['model'], kwargs['headers']['Authorization']))
        return SimpleNamespace(status_code=200, json=lambda: {
            'language': 'en', 'text': 'Hello.', 'segments': [{'start': 0, 'end': 1, 'text': 'Hello.'}]})
    monkeypatch.setattr('requests.post', request)
    worker.transcribe(audio)
    assert calls == [('https://old.example.test/v1/audio/transcriptions', 'whisper-1', 'Bearer mock-old')]
