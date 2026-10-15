"""字幕缓存不能将相同时间窗误认为相同原文，并须保留人工改稿。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.models.db import Store
from app.pipeline import runner as pipeline
from app.services.subtitler import read_srt, save_srt
from app.services.transcriber import Segment, Transcript


@pytest.fixture
def work(tmp_path, monkeypatch):
    runner = pipeline.PipelineRunner(Config({}), Store(tmp_path / 'tasks.db'))
    runner._data = tmp_path / 'data'
    task = runner.create_task('https://example.test/video', 'best', ['zh'], 'burn', title='clip')
    calls = []

    def translate(texts, source, target, **kwargs):
        calls.append((list(texts), source, target))
        return ['译文：' + text for text in texts]

    monkeypatch.setattr(pipeline, 'build_translator', lambda *_: SimpleNamespace(translate=translate))
    return SimpleNamespace(runner=runner, task=task, calls=calls, root=tmp_path)


@pytest.mark.parametrize('changed', ['text', 'language', 'segmentation'])
def test_same_time_coverage_does_not_reuse_translation_from_another_source(work, changed):
    original = Transcript('en', [Segment(0, 2, 'old source')])
    path = work.runner._stage_translate(work.task, original, ['zh'])['zh']
    old = path.read_bytes()
    updated = {
        'text': Transcript('en', [Segment(0, 2, 'new source')]),
        'language': Transcript('fr', [Segment(0, 2, 'old source')]),
        'segmentation': Transcript('en', [Segment(0, 1, 'old'), Segment(1, 2, ' source')]),
    }[changed]
    # 模拟应用重启：不能只靠内存中的上次原文判断。
    resumed = pipeline.PipelineRunner(Config({}), work.runner._store)
    resumed._data = work.runner._data
    result = resumed._stage_translate(work.task, updated, ['zh'])['zh']
    assert len(work.calls) == 2
    assert [s.text for s in read_srt(result)] == ['译文：' + s.text for s in updated.segments]
    backups = list(resumed._output_dir(work.task).glob('*.incomplete-*.srt'))
    assert len(backups) == 1 and backups[0].read_bytes() == old


def test_unchanged_source_preserves_manual_reflow_and_translation_edits(work):
    transcript = Transcript('en', [Segment(0, 2, 'original')])
    path = work.runner._stage_translate(work.task, transcript, ['zh'])['zh']
    edited = [Segment(0, 1, '人工修正的前半句'), Segment(1, 2, '人工修正的后半句')]
    save_srt(edited, path)
    work.runner._stage_translate(work.task, transcript, ['zh'])
    assert len(work.calls) == 1
    assert read_srt(path) == edited


def test_recovered_transcript_replaces_stale_source_and_legacy_translation(work):
    original = Transcript('en', [Segment(0, 2, 'old source')])
    source = work.runner._srt_path(work.task, 'clip', 'en')
    translated = work.runner._srt_path(work.task, 'clip', 'zh')
    save_srt(original.segments, source)
    save_srt([Segment(0, 2, '旧版译文')], translated)
    for language, path in [('en', source), ('zh', translated)]:
        work.runner._store.add_artifact(work.task['id'], 'subtitle', str(path), lang=language)
    cache = work.runner._data / 'cache/transcripts' / (work.task['id'] + '.json')
    cache.parent.mkdir(parents=True)
    cache.write_text('{broken', encoding='utf-8')
    work.runner._store.upsert_stage(work.task['id'], 'transcribe', 'done')
    restored = Transcript('en', [Segment(0, 2, 'new source')])
    work.runner._transcriber = SimpleNamespace(transcribe=lambda *a, **k: restored)
    transcript, result = work.runner._stage_transcribe(work.task, Path('unused-video'), Path('unused-audio'))
    assert read_srt(result) == restored.segments
    result = work.runner._stage_translate(work.task, transcript, ['zh'])['zh']
    assert read_srt(result) == [Segment(0, 2, '译文：new source')]
    assert len(list(work.runner._output_dir(work.task).glob('*.incomplete-*.srt'))) == 2


def test_failed_subtitle_write_does_not_certify_old_text_for_new_source(work, monkeypatch):
    original = Transcript('en', [Segment(0, 2, 'old source')])
    updated = Transcript('en', [Segment(0, 2, 'new source')])
    path = work.runner._stage_translate(work.task, original, ['zh'])['zh']
    before = path.read_bytes()
    save = pipeline.save_srt

    def fail(*args):
        raise OSError('模拟新字幕写入失败')

    monkeypatch.setattr(pipeline, 'save_srt', fail)
    with pytest.raises(OSError):
        work.runner._stage_translate(work.task, updated, ['zh'])
    assert path.read_bytes() == before
    monkeypatch.setattr(pipeline, 'save_srt', save)
    work.runner._stage_translate(work.task, updated, ['zh'])
    assert read_srt(path) == [Segment(0, 2, '译文：new source')]


def test_valid_transcript_checkpoint_keeps_user_edited_source(work):
    original = Transcript('en', [Segment(0, 2, 'old source')])
    cache = work.runner._data / 'cache/transcripts' / (work.task['id'] + '.json')
    original.save_json(cache)
    work.runner._store.upsert_stage(work.task['id'], 'transcribe', 'done')
    source = work.runner._srt_path(work.task, 'clip', 'en')
    edited = [Segment(0, 2, 'human correction')]
    save_srt(edited, source)
    work.runner._stage_transcribe(work.task, Path('unused-video'), Path('unused-audio'))
    assert read_srt(source) == edited


def test_source_write_retry_cannot_restore_stale_srt_after_new_checkpoint(work, monkeypatch):
    source = work.runner._srt_path(work.task, 'clip', 'en')
    save_srt([Segment(0, 2, 'stale text')], source)
    restored = Transcript('en', [Segment(0, 2, 'recovered text')])
    work.runner._transcriber = SimpleNamespace(transcribe=lambda *a, **k: restored)
    save = pipeline.save_srt
    monkeypatch.setattr(pipeline, 'save_srt', lambda *a: (_ for _ in ()).throw(OSError('模拟写入失败')))
    with pytest.raises(OSError):
        work.runner._stage_transcribe(work.task, Path('unused-video'), Path('unused-audio'))
    monkeypatch.setattr(pipeline, 'save_srt', save)
    work.runner._stage_transcribe(work.task, Path('unused-video'), Path('unused-audio'))
    assert read_srt(source) == restored.segments


def test_task_cleanup_keeps_provenance_until_explicit_task_deletion(work):
    transcript = Transcript('en', [Segment(0, 2, 'original')])
    work.runner._stage_translate(work.task, transcript, ['zh'])
    source_records = work.runner._data / 'cache/subtitles' / work.task['id']
    assert source_records.exists()
    work.runner._cleanup_tmp(work.task['id'])
    assert source_records.exists()
    work.runner.cleanup_task_files(work.task['id'])
    assert not source_records.exists()
