"""收尾异常不能吞掉终态，恢复来源不能覆盖无关文件或移走共享原片。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import cancel
from app.core.config import Config
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.pipeline import runner as pipeline
from app.services.subtitler import save_srt
from app.services.transcriber import Segment


@pytest.fixture
def ready(tmp_path):
    store = Store(tmp_path / 'tasks.db')
    runner = pipeline.PipelineRunner(Config({}), store)
    runner._data = tmp_path / 'data'
    task = runner.create_task('https://example.test/source', 'best', [], 'none')
    tid = task['id']
    work = runner._data / 'tmp' / tid
    work.mkdir(parents=True)
    video = work / 'source.mp4'
    video.write_bytes(b'complete downloaded source')
    store.add_artifact(tid, 'video', str(video))
    subtitle = save_srt([Segment(0, 1, '编辑后的字幕')], work / 'zh.srt')
    store.add_artifact(tid, 'subtitle', str(subtitle), lang='zh')
    store.upsert_stage(tid, 'download', 'done')
    store.update_task(tid, status='completed')
    yield SimpleNamespace(store=store, runner=runner, task=task, tid=tid, work=work, video=video)
    cancel.clear(tid)


@pytest.mark.parametrize('operation', ['pipeline', 'rerender'])
@pytest.mark.parametrize('failed', [False, True])
def test_cleanup_error_does_not_replace_task_result(ready, monkeypatch, operation, failed):
    # 只在收尾查询失败；终态提交依然可用，不能让任务永久停在运行中。
    def cleanup_query_failure(tid, stage):
        raise RuntimeError('模拟收尾查询暂不可用')

    def stage(*args):
        if failed:
            raise AppError(Codes.RENDER_FAILED, '原始处理失败')
        return ready.video if operation == 'pipeline' else [ready.video]

    monkeypatch.setattr(ready.store, 'stage_status', cleanup_query_failure)
    monkeypatch.setattr(ready.runner, '_stage_download' if operation == 'pipeline' else '_stage_render', stage)
    if operation == 'pipeline':
        monkeypatch.setattr(ready.runner, '_finish_download_with_warning', lambda *a: None)
    call = (lambda: ready.runner.run(ready.task)) if operation == 'pipeline' else (
        lambda: ready.runner.rerender_subtitles(ready.tid, 'zh'))
    if failed:
        with pytest.raises(AppError, match='原始处理失败'):
            call()
    else:
        call()
    task = ready.store.get_task(ready.tid)
    assert task['status'] == ('failed' if failed else 'completed')
    assert task['finished_at']
    assert ready.video.read_bytes() == b'complete downloaded source'


def test_cached_source_restore_does_not_overwrite_unregistered_work_file(ready, tmp_path):
    archived = tmp_path / 'saved' / 'source.mp4'
    archived.parent.mkdir()
    ready.video.replace(archived)
    ready.store.move_artifact(ready.tid, str(ready.video), str(archived))
    ready.video.write_bytes(b'unrelated preexisting work file')
    task = {**ready.task, 'subtitle_mode': 'burn'}
    recovered = ready.runner._stage_download(task)
    assert ready.video.read_bytes() == b'unrelated preexisting work file'
    assert recovered != ready.video
    assert recovered.read_bytes() == b'complete downloaded source'
    assert ready.store.artifact_exists(ready.tid, str(recovered))


def test_publishing_shared_source_keeps_other_tasks_registered_input(ready, tmp_path):
    other = ready.runner.create_task('https://example.test/other', 'best', [], 'none')
    ready.store.add_artifact(other['id'], 'video', str(ready.video))
    target = tmp_path / 'output'
    target.mkdir()
    published = ready.runner._publish_file(ready.tid, ready.video, target, 'video', registered=True)
    assert ready.video.read_bytes() == b'complete downloaded source'
    assert published.read_bytes() == b'complete downloaded source'
    assert ready.store.artifact_exists(other['id'], str(ready.video))
    assert ready.store.artifact_exists(ready.tid, str(published))


def test_shared_source_restore_cancellation_keeps_input_and_old_destination(ready, tmp_path, monkeypatch):
    archived = tmp_path / 'saved' / 'source.mp4'
    archived.parent.mkdir()
    ready.video.replace(archived)
    ready.store.move_artifact(ready.tid, str(ready.video), str(archived))
    other = ready.runner.create_task('https://example.test/other', 'best', [], 'none')
    ready.store.add_artifact(other['id'], 'video', str(archived))
    original_open = Path.open
    reads = []

    class CancelOnRead:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            reads.append(size)
            block = self.stream.read(size)
            cancel.cancel(ready.tid)
            return block

    def open_source(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return CancelOnRead(stream) if path == archived and mode == 'rb' else stream

    monkeypatch.setattr(Path, 'open', open_source)
    with pytest.raises(AppError) as error:
        ready.runner._stage_download({**ready.task, 'subtitle_mode': 'burn'})
    assert error.value.code == Codes.TASK_CANCELLED
    assert reads and all(0 < count <= 1024 * 1024 for count in reads)
    assert archived.read_bytes() == b'complete downloaded source'
    assert not ready.video.exists()
    assert ready.store.artifact_exists(ready.tid, str(archived))
    assert not list(ready.work.glob('.publish-*.tmp'))


@pytest.mark.parametrize('operation', ['restore', 'publish'])
def test_waiting_for_another_video_publication_can_be_cancelled(ready, tmp_path, operation):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    archived = tmp_path / 'saved' / 'source.mp4'
    archived.parent.mkdir()
    ready.video.replace(archived)
    ready.store.move_artifact(ready.tid, str(ready.video), str(archived))
    started = threading.Event()

    def work():
        started.set()
        if operation == 'restore':
            return ready.runner._stage_download({**ready.task, 'subtitle_mode': 'burn'})
        return ready.runner._publish_file(ready.tid, archived, ready.work, 'video', registered=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pipeline._PUBLISH_LOCK.acquire()
        try:
            future = pool.submit(work)
            assert started.wait(1)
            cancel.cancel(ready.tid)
            with pytest.raises(AppError) as error:
                future.result(timeout=.8)
            assert error.value.code == Codes.TASK_CANCELLED
        finally:
            pipeline._PUBLISH_LOCK.release()
    assert archived.read_bytes() == b'complete downloaded source'
    assert not ready.video.exists()
    assert ready.store.artifact_exists(ready.tid, str(archived))
