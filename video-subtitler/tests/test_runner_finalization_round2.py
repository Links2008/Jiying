"""收到完成通知后启动的新尝试，不能被上一轮的取消清理覆盖。"""
from types import SimpleNamespace

import pytest

from app.core import cancel
from app.core.config import Config
from app.core.events import EventBus
from app.models.db import Store
from app.pipeline.runner import PipelineRunner
from app.services.subtitler import save_srt
from app.services.transcriber import Segment


@pytest.mark.parametrize('operation', ['pipeline', 'rerender'])
def test_finished_worker_cannot_clear_new_attempt_cancellation(tmp_path, monkeypatch, operation):
    store = Store(tmp_path / 'tasks.db')
    runner = PipelineRunner(Config({}), store)
    runner._data = tmp_path / 'data'
    task = runner.create_task('https://example.test/video', 'best', [], 'none')
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'isolated source')
    store.add_artifact(task['id'], 'video', str(video))
    subtitle = save_srt([Segment(0, 1, 'isolated speech')], tmp_path / 'source.en.srt')
    store.add_artifact(task['id'], 'subtitle', str(subtitle), lang='en')
    monkeypatch.setattr(runner, '_stage_download', lambda *_: video)
    monkeypatch.setattr(runner, '_stage_render', lambda *_: [video])
    monkeypatch.setattr(runner, '_cleanup_tmp', lambda *_: None)
    new_attempts = []

    def completed(event):
        if event.type == 'task' and event.status == 'completed':
            # 完成通知是外部允许重生成的时刻，在此立即领取并取消下一次操作。
            new_attempts.append(store.claim_terminal_task(task['id']))
            cancel.cancel(task['id'])

    runner._bus = SimpleNamespace(publish=completed)
    try:
        if operation == 'pipeline':
            runner.run(task)
        else:
            store.update_task(task['id'], status='completed')
            runner.rerender_subtitles(task['id'], 'en')
        assert new_attempts == [True]
        assert store.get_task(task['id'])['status'] == 'running'
        assert cancel.is_cancelled(task['id']), '旧工作线程清掉了下一次操作的取消请求'
    finally:
        cancel.clear(task['id'])


def test_task_and_stage_notifications_are_delivered_with_their_messages(tmp_path):
    bus = EventBus()
    messages = bus.subscribe()
    store = Store(tmp_path / 'tasks.db')
    runner = PipelineRunner(Config({}), store, bus=bus)
    task = runner.create_task('https://example.test/video', 'best', [], 'none')
    runner._set_stage(task['id'], 'render', 'failed', '模拟字幕处理错误')
    runner._emit(type='task', task_id=task['id'], status='failed', message='模拟任务失败')
    delivered = []
    while not messages.empty():
        delivered.append(messages.get_nowait())
    assert any(event.type == 'stage' and event.status == 'failed'
               and event.message == '模拟字幕处理错误' for event in delivered)
    assert any(event.type == 'task' and event.status == 'failed'
               and event.message == '模拟任务失败' for event in delivered)
