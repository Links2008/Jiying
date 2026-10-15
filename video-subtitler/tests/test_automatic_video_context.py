"""自动语境：全片依据、画面能力边界、断点恢复与真实 FFmpeg 采样。"""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.translator import LLMTranslator
from app.services.video_context import sample_frames, supports_images


def translator(**settings):
    return LLMTranslator(Config({'translator': {'llm': {'batch_size': 1, **settings}}}))


def reply(number, context=None):
    return json.dumps({'translations': [{'id': number, 'text': f'译文{number}'}], 'context': context})


def number(prompt):
    return int(prompt.split('待翻译台词：\n', 1)[1].split('.', 1)[0])


BACKGROUND = {'summary': '讨论大语言模型训练的访谈，领会意图是调侃。',
              'evidence_ids': [1], 'frame_ids': [], 'confidence': 'high'}


def test_automatic_background_uses_whole_recording_and_is_reused_without_extra_call(monkeypatch):
    worker = translator()
    captured = []
    def request(prompt):
        captured.append(prompt)
        if number(prompt) > 1:
            assert BACKGROUND['summary'] in prompt
            assert '"全片字幕采样（只参考）": []' in prompt
        return reply(number(prompt), BACKGROUND)
    monkeypatch.setattr(worker, '_call_api', request)
    assert worker.translate(['An AI interview.', 'He understood the assignment.', 'RLHF matters.'], 'en', 'zh') == [
        '译文1', '译文2', '译文3']
    assert len(captured) == 3
    assert 'RLHF matters.' in captured[0]
    assert '禁止要求用户填写背景' in captured[0]


def test_hour_long_overview_is_bounded_and_includes_end_and_introduction():
    texts = [f'{i} ' + '长视频字幕' * 100 for i in range(5000)]
    overview = translator()._sample_overview(texts)
    assert len(overview) <= 28
    assert {1, 2, 3, 4, 5000}.issubset({row['id'] for row in overview})
    assert all(len(row['text']) <= 240 for row in overview)


@pytest.mark.parametrize('context', [
    {**BACKGROUND, 'evidence_ids': [999]}, {**BACKGROUND, 'evidence_ids': [True]},
    {**BACKGROUND, 'evidence_ids': [], 'frame_ids': [1]},
    {**BACKGROUND, 'confidence': 'low'}, {**BACKGROUND, 'summary': '甲' * 1001},
    {'untrusted': float('nan')},
])
def test_unsupported_optional_background_is_not_used_or_persisted(monkeypatch, tmp_path, context):
    worker = translator()
    worker.set_checkpoint_dir(tmp_path)
    monkeypatch.setattr(worker, '_call_api', lambda _: reply(1, context))
    assert worker.translate(['Actual source'], 'en', 'zh') == ['译文1']
    assert worker._background == {}
    assert json.loads((tmp_path / '000000.json').read_text())['response']['context'] == {}


def test_retry_restores_background_and_does_not_repeat_completed_analysis(monkeypatch, tmp_path):
    first = translator()
    first.set_checkpoint_dir(tmp_path)
    def request(prompt):
        if number(prompt) == 2:
            raise AppError(Codes.TRANS_FAILED, '模拟中断')
        return reply(1, BACKGROUND)
    monkeypatch.setattr(first, '_call_api', request)
    with pytest.raises(AppError):
        first.translate(['AI interview', 'second'], 'en', 'zh')
    resumed = translator()
    resumed.set_checkpoint_dir(tmp_path)
    requests = []
    def remaining(prompt):
        assert BACKGROUND['summary'] in prompt
        requests.append(number(prompt))
        return reply(number(prompt))
    monkeypatch.setattr(resumed, '_call_api', remaining)
    assert resumed.translate(['AI interview', 'second'], 'en', 'zh') == ['译文1', '译文2']
    assert requests == [2]


def test_background_does_not_leak_between_recordings_or_cancelled_generations(monkeypatch):
    worker = translator()
    monkeypatch.setattr(worker, '_call_api', lambda _: reply(1, BACKGROUND))
    worker.translate(['AI interview'], 'en', 'zh')
    captured = []
    monkeypatch.setattr(worker, '_call_api', lambda p: captured.append(p) or reply(1))
    worker.translate(['Different show'], 'en', 'zh')
    assert worker._background == {} and BACKGROUND['summary'] not in captured[0]
    worker._remember_background(BACKGROUND, worker._generation - 1)
    assert worker._background == {}


@pytest.mark.parametrize('model,expected', [
    ('deepseek-chat', False), ('demo', False), ('gpt-4o-mini', True),
    ('gpt-4o-mini-transcribe', False), ('Qwen/Qwen2.5-VL-72B-Instruct', True),
    ('glm-4v-plus', True),
])
def test_images_only_for_known_visual_model_names(model, expected):
    assert supports_images(model) is expected


def test_text_model_does_not_extract_or_send_images(monkeypatch, tmp_path):
    worker = translator(model='deepseek-chat', base_url='https://example.test')
    worker.set_video(tmp_path / 'source.mp4')
    monkeypatch.setattr('app.services.translator.sample_frames', lambda *_: pytest.fail('文本模型不得抽取画面'))
    captured = []
    monkeypatch.setattr(worker, '_call_api', lambda p: captured.append(p) or reply(1))
    worker.translate(['source'], 'en', 'zh')
    assert worker._frames == []
    assert '"本批实际附带的采样画面": []' in captured[0]


def test_visual_model_receives_actual_image_payload_once_and_frame_evidence(monkeypatch, tmp_path):
    video = tmp_path / 'source.mp4'
    video.write_bytes('模拟视频'.encode())
    worker = translator(model='gpt-4o-mini', base_url='https://example.test')
    worker.set_video(video)
    frame = {'id': 1, 'seconds': 2.0, 'url': 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8sample').decode()}
    monkeypatch.setattr('app.services.translator.sample_frames', lambda *_: [frame])
    payloads = []
    def post(*_, **kwargs):
        payload = kwargs['json']
        payloads.append(payload)
        content = payload['messages'][-1]['content']
        prompt = content[0]['text'] if isinstance(content, list) else content
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'choices': [
            {'finish_reason': 'stop', 'message': {'content': reply(number(prompt), {
                **BACKGROUND, 'evidence_ids': [], 'frame_ids': [1]})}}]})
    monkeypatch.setattr('requests.post', post)
    assert worker.translate(['first', 'second'], 'en', 'zh') == ['译文1', '译文2']
    image = payloads[0]['messages'][-1]['content'][1]
    assert image['type'] == 'image_url' and image['image_url']['url'] == frame['url']
    assert isinstance(payloads[1]['messages'][-1]['content'], str)
    assert worker._background['frame_ids'] == [1]


def test_pipeline_provides_source_video_without_manual_background(monkeypatch, tmp_path):
    from app.models.db import Store
    from app.pipeline.runner import PipelineRunner
    from app.services.transcriber import Segment, Transcript
    runner = PipelineRunner(Config({}), Store(tmp_path / 'tasks.db'))
    runner._data = tmp_path
    task = runner.create_task('https://example.test/video', 'best', ['zh'], 'softmux')
    sources = []
    monkeypatch.setattr('app.pipeline.runner.build_translator', lambda *_: SimpleNamespace(
        set_video=lambda path, tid: sources.append((path, tid)), translate=lambda *a, **kw: ['译文']))
    video = tmp_path / 'source.mp4'
    runner._stage_translate(task, Transcript('en', [Segment(0, 1, 'hello')]), ['zh'], video)
    assert sources == [(video, task['id'])]


def test_cancelled_frame_worker_cannot_replace_next_recording_images(monkeypatch, tmp_path):
    worker = translator(model='gpt-4o-mini', base_url='https://example.test')
    first, second = tmp_path / 'first.mp4', tmp_path / 'second.mp4'
    first.write_bytes(b'first'); second.write_bytes(b'second')
    worker.set_video(first)
    gate, started, old_done = threading.Event(), threading.Event(), threading.Event()
    def frames(path, *_):
        if path == first:
            started.set()
            assert gate.wait(3)
        return [{'id': 1, 'seconds': 1, 'url': str(path)}]
    monkeypatch.setattr('app.services.translator.sample_frames', frames)
    original = worker._translate_chunk
    def chunk(texts, *args, **kwargs):
        try:
            return original(texts, *args, **kwargs)
        finally:
            if texts == ['old']:
                old_done.set()
    monkeypatch.setattr(worker, '_translate_chunk', chunk)
    def cancel(_):
        assert started.is_set()
        raise AppError(Codes.TASK_CANCELLED, '模拟取消')
    with pytest.raises(AppError):
        worker.translate(['old'], 'en', 'zh', on_progress=cancel)
    worker.set_video(second)
    def request(_):
        gate.set()
        assert old_done.wait(2)
        assert worker._frames[0]['url'] == str(second)
        assert worker._worker_context.frames[0]['url'] == str(second)
        return reply(1)
    monkeypatch.setattr(worker, '_call_api', request)
    assert worker.translate(['new'], 'en', 'zh') == ['译文1']


def test_real_ffmpeg_frame_sampling_is_bounded_and_decodable(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    assert ffmpeg, '此环境验证要求真实 FFmpeg'
    video = tmp_path / 'short.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=1:duration=6',
                    '-c:v', 'libx264', '-y', str(video)], check=True)
    frames = sample_frames(video, Config({}))
    assert len(frames) == 4
    for frame in frames:
        raw = base64.b64decode(frame['url'].split(',', 1)[1])
        assert raw.startswith(b'\xff\xd8') and len(raw) <= 160_000
        path = tmp_path / f"{frame['id']}.jpg"
        path.write_bytes(raw)
        data = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path)]))
        assert data['streams'][0]['width'] <= 640 and data['streams'][0]['height'] <= 360
