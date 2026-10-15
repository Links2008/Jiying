"""多语重生成、占用时序和工作目录回收必须保留实际选择与共享文件。"""
from types import SimpleNamespace

import pytest

from app.core import cancel
from app.core.config import Config
from app.core.errors import AppError
from app.models.db import Store
from app.pipeline import runner as pipeline
from app.services.subtitler import read_srt, save_srt
from app.services.transcriber import Segment


@pytest.fixture
def ready(tmp_path):
    store = Store(tmp_path / 'tasks.db')
    runner = pipeline.PipelineRunner(Config({}), store)
    runner._data = tmp_path / 'data'
    task = runner.create_task('https://example.test/video', 'best', ['zh', 'ja'], 'burn', bilingual=True)
    tid = task['id']
    work = runner._data / 'tmp' / tid
    work.mkdir(parents=True)
    video = work / 'source.mp4'
    video.write_bytes(b'isolated source')
    store.add_artifact(tid, 'video', str(video))
    for lang, text in [('en', 'Original speech'), ('zh', '中文译文'), ('ja', '修正した日本語')]:
        path = save_srt([Segment(0, 1, text)], work / f'{lang}.srt')
        store.add_artifact(tid, 'subtitle', str(path), lang=lang)
    store.upsert_stage(tid, 'download', 'done')
    store.update_task(tid, status='completed', error='', finished_at='first completion')
    yield SimpleNamespace(store=store, runner=runner, tid=tid, work=work)
    cancel.clear(tid)


@pytest.mark.parametrize('by_id', [False, True])
def test_selected_translation_is_in_bilingual_rerender(ready, monkeypatch, by_id):
    chosen = next(a for a in ready.store.list_artifacts(ready.tid) if a['lang'] == 'ja')
    rendered = []

    def burn(config, video, subtitle, output, **kwargs):
        rendered.extend(s.text for s in read_srt(subtitle))
        output.write_bytes(b'new video')

    monkeypatch.setattr(pipeline, 'burn', burn)
    ready.runner.rerender_subtitles(ready.tid, 'ja', artifact_id=chosen['id'] if by_id else None)
    assert rendered == ['修正した日本語\nOriginal speech']
    bilingual = next(a for a in ready.store.list_artifacts(ready.tid) if a['kind'] == 'bilingual')
    assert bilingual['lang'] == 'ja+en'


def test_selected_source_keeps_chinese_above_it(ready, monkeypatch):
    used = []

    def burn(config, video, subtitle, output, **kwargs):
        used.extend(s.text for s in read_srt(subtitle))
        output.write_bytes(b'new video')

    monkeypatch.setattr(pipeline, 'burn', burn)
    ready.runner.rerender_subtitles(ready.tid, 'en')
    assert used == ['中文译文\nOriginal speech']


def test_rerender_rechecks_artifacts_after_claim(ready, monkeypatch):
    chosen = next(a for a in ready.store.list_artifacts(ready.tid) if a['lang'] == 'ja')
    claim = ready.store.claim_terminal_task_snapshot

    def remove_before_claim(tid):
        ready.store.delete_artifacts(tid, [chosen['id']])
        return claim(tid)

    monkeypatch.setattr(ready.store, 'claim_terminal_task_snapshot', remove_before_claim)
    used = []
    monkeypatch.setattr(ready.runner, '_stage_render', lambda *a: used.append(a) or [])
    with pytest.raises(AppError):
        ready.runner.rerender_subtitles(ready.tid, artifact_id=chosen['id'])
    assert used == []
    assert ready.store.get_task(ready.tid)['status'] == 'completed'
    assert ready.store.get_task(ready.tid)['finished_at'] == 'first completion'


def test_rerender_start_failure_restores_current_snapshot_and_clears_cancel(ready, monkeypatch):
    claim = ready.store.claim_terminal_task_snapshot

    def latest_before_claim(tid):
        ready.store.update_task(tid, status='failed', error='latest failure', finished_at='latest completion')
        return claim(tid)

    def fail_start(thread):
        cancel.cancel(ready.tid)
        raise RuntimeError('模拟线程资源不足')

    monkeypatch.setattr(ready.store, 'claim_terminal_task_snapshot', latest_before_claim)
    monkeypatch.setattr(pipeline.threading.Thread, 'start', fail_start)
    with pytest.raises(RuntimeError):
        ready.runner.rerender_subtitles(ready.tid, 'ja', background=True)
    task = ready.store.get_task(ready.tid)
    assert (task['status'], task['error'], task['finished_at']) == (
        'failed', 'latest failure', 'latest completion')
    assert not cancel.is_cancelled(ready.tid)


@pytest.mark.parametrize('cleanup', ['terminal', 'delete'])
def test_cleanup_preserves_nested_registered_and_shared_files(ready, cleanup):
    nested = ready.work / 'nested'
    nested.mkdir()
    shared, owned, junk = [nested / name for name in ('shared.srt', 'owned.srt', 'audio.wav')]
    for path in (shared, owned, junk):
        path.write_bytes(b'keep shared subtitles')
    other = ready.runner.create_task('https://example.test/other', 'best', [], 'none')
    ready.store.add_artifact(other['id'], 'subtitle', str(shared), lang='en')
    ready.store.add_artifact(ready.tid, 'subtitle', str(owned), lang='zh')
    if cleanup == 'terminal':
        ready.runner._cleanup_tmp(ready.tid)
        assert owned.exists()
    else:
        ready.runner.cleanup_task_files(ready.tid)
        assert not owned.exists()
    assert shared.read_bytes() == b'keep shared subtitles'
    assert not junk.exists()


def test_cleanup_keeps_files_if_shared_registration_cannot_be_read(ready, monkeypatch):
    def unavailable(*args):
        raise RuntimeError('模拟数据库暂不可读')

    monkeypatch.setattr(ready.store, 'artifact_paths_owned_by_other', unavailable)
    ready.runner.cleanup_task_files(ready.tid)
    assert (ready.work / 'source.mp4').exists()


@pytest.mark.parametrize('cleanup', ['terminal', 'delete'])
@pytest.mark.parametrize('link_kind', ['file', 'directory'])
def test_cleanup_preserves_registered_nested_symlink_path(ready, tmp_path, cleanup, link_kind):
    nested = ready.work / 'nested'
    nested.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'shared.srt'
    target.write_bytes(b'shared subtitle')
    link = nested / ('linked.srt' if link_kind == 'file' else 'linked-directory')
    link.symlink_to(target if link_kind == 'file' else outside, target_is_directory=link_kind == 'directory')
    registered = link if link_kind == 'file' else link / 'shared.srt'
    junk = nested / 'unused.wav'
    junk.write_bytes(b'can remove')
    other = ready.runner.create_task('https://example.test/shared', 'best', [], 'none')
    ready.store.add_artifact(other['id'], 'subtitle', str(registered), lang='en')
    operation = ready.runner._cleanup_tmp if cleanup == 'terminal' else ready.runner.cleanup_task_files
    operation(ready.tid)
    assert registered.read_bytes() == b'shared subtitle'
    assert target.read_bytes() == b'shared subtitle'
    assert not junk.exists()
